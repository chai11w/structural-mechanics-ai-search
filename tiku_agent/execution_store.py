"""Phase 5 authority: epochs, CAS snapshots and durable parent/child identities.

Opt-in only. Legacy session databases are never opened or dual-written here.
All authoritative mutations use one SQLite transaction domain.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time
import threading
from typing import Any, Callable
from uuid import uuid4


class ExecutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionPolicy:
    session_ttl: int = 7200
    epoch_max_age: int = 30 * 86400
    history_ttl: int = 30 * 86400
    lease_seconds: int = 300
    max_execution_seconds: int = 1800
    max_sessions: int = 10000
    max_tasks: int = 100000
    max_operations: int = 100000
    max_state_bytes: int = 1024 * 1024
    max_result_bytes: int = 256 * 1024
    max_total_result_bytes: int = 32 * 1024 * 1024
    max_database_bytes: int = 256 * 1024 * 1024
    min_free_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError("execution limits must be positive integers")


@dataclass(frozen=True)
class StateVersion:
    authority: str
    session: str
    epoch: str
    kind: str
    version: int
    present: bool


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512:
        raise ExecutionError("EXECUTION_IDENTITY_INVALID")
    return hashlib.sha256(session_id.strip().encode()).hexdigest()


_READS: ContextVar[dict[tuple[str, str, str], StateVersion]] = ContextVar("execution_state_reads", default={})
# Installed by the execution coordinator in 5.3. Kept out of persisted JSON.
_WRITER: ContextVar[Any] = ContextVar("execution_writer", default=None)
_TRANSACTION: ContextVar[Any] = ContextVar("execution_transaction", default=None)


def inherit_state_version(source, target):
    version = getattr(source, "_execution_version", None)
    if version is not None:
        target._execution_version = version
    return target


class ExecutionStore:
    def __init__(self, path: str | Path, *, policy: ExecutionPolicy | None = None,
                 now: Callable[[], float] = time.time):
        self.path = Path(path).resolve()
        self.policy = policy or ExecutionPolicy()
        self.now = now
        self.require_writer = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            schema = """
                CREATE TABLE IF NOT EXISTS execution_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_sessions (
                    session TEXT PRIMARY KEY, epoch TEXT NOT NULL UNIQUE,
                    created REAL NOT NULL, expires REAL NOT NULL, version INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS execution_states (
                    session TEXT NOT NULL REFERENCES execution_sessions(session),
                    epoch TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('workflow','child')),
                    version INTEGER NOT NULL, payload TEXT,
                    PRIMARY KEY(session,kind));
                CREATE TABLE IF NOT EXISTS execution_tasks (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, epoch TEXT NOT NULL,
                    kind TEXT NOT NULL, task_id TEXT NOT NULL, task_revision INTEGER NOT NULL,
                    parent_id TEXT REFERENCES execution_tasks(id), unit_id TEXT NOT NULL,
                    input_version TEXT NOT NULL, origin TEXT NOT NULL, phase TEXT NOT NULL,
                    state_version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(session,epoch,kind,task_id,task_revision));
                CREATE INDEX IF NOT EXISTS execution_tasks_parent ON execution_tasks(parent_id,unit_id);
                CREATE INDEX IF NOT EXISTS execution_tasks_updated ON execution_tasks(updated);
                CREATE TABLE IF NOT EXISTS execution_page_errors (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, search_id TEXT NOT NULL,
                    task_kind TEXT NOT NULL, phase TEXT NOT NULL, error_type TEXT NOT NULL,
                    error_code TEXT NOT NULL, error_message TEXT NOT NULL, created_at REAL NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            row = conn.execute("SELECT value FROM execution_meta WHERE key='schema'").fetchone()
            if row is not None and row[0] != "1":
                raise ExecutionError("EXECUTION_SCHEMA_UNSUPPORTED")
            conn.execute("INSERT OR IGNORE INTO execution_meta VALUES ('schema','1')")
            conn.execute("INSERT OR IGNORE INTO execution_meta VALUES ('authority',?)", (uuid4().hex,))
            self.authority = conn.execute("SELECT value FROM execution_meta WHERE key='authority'").fetchone()[0]
            migration = conn.execute("SELECT value FROM execution_meta WHERE key='migration'").fetchone()
            if migration is not None and migration[0] != "complete":
                raise ExecutionError("EXECUTION_MIGRATION_INCOMPLETE")
            enabled = conn.execute("SELECT value FROM execution_meta WHERE key='execution_enabled'").fetchone()
            self.require_writer = bool(enabled and enabled[0] == "1")

    @contextmanager
    def transaction(self):
        current = _TRANSACTION.get()
        if current is not None and current[:2] == (str(self.path), threading.get_ident()):
            yield current[2]
            return
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        token = None
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            token = _TRANSACTION.set((str(self.path), threading.get_ident(), conn))
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if token is not None:
                _TRANSACTION.reset(token)
            conn.close()

    def clock(self, conn) -> float:
        now = float(self.now())
        if not math.isfinite(now) or now <= 0:
            raise ExecutionError("EXECUTION_CLOCK_INVALID")
        row = conn.execute("SELECT value FROM execution_meta WHERE key='clock'").fetchone()
        previous = float(row[0]) if row else now
        if now < previous - 300:
            raise ExecutionError("EXECUTION_CLOCK_ROLLBACK")
        now = max(previous, now)
        conn.execute("INSERT OR REPLACE INTO execution_meta VALUES ('clock',?)", (str(now),))
        return now

    def capacity(self, conn, table: str, limit: int):
        if table not in {"execution_sessions", "execution_tasks", "execution_operations", "execution_effects"}:
            raise ValueError("unknown capacity table")
        if conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= limit:
            raise ExecutionError("EXECUTION_CAPACITY")
        self.storage_capacity()

    def storage_capacity(self, extra=0):
        size = sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists())
        if size + extra >= self.policy.max_database_bytes or shutil.disk_usage(self.path.parent).free < self.policy.min_free_bytes + extra:
            raise ExecutionError("EXECUTION_CAPACITY")

    def _session(self, conn, key: str, now: float, *, create=False):
        row = conn.execute("SELECT * FROM execution_sessions WHERE session=?", (key,)).fetchone()
        if row is None and create:
            self.capacity(conn, "execution_sessions", self.policy.max_sessions)
            conn.execute("INSERT INTO execution_sessions VALUES (?,?,?,?,0)",
                         (key, uuid4().hex, now, now + self.policy.session_ttl))
            row = conn.execute("SELECT * FROM execution_sessions WHERE session=?", (key,)).fetchone()
        return row

    def _valid_session(self, row, now):
        return row is not None and row["expires"] > now and row["created"] + self.policy.epoch_max_age > now

    def context(self, session_id: str) -> dict[str, Any]:
        key = session_key(session_id)
        with self.transaction() as conn:
            now = self.clock(conn)
            row = self._session(conn, key, now, create=True)
            if not self._valid_session(row, now):
                if self.writer_required(conn) and conn.execute("SELECT 1 FROM execution_operations WHERE session=? AND epoch=? AND status IN ('RUNNING','UNKNOWN') LIMIT 1",(key,row["epoch"])).fetchone():
                    return {"schema":1,"epoch":row["epoch"],"state_version":row["version"],"expires_at":row["expires"]}
                self._rotate(conn, key, now)
                row = self._session(conn, key, now)
            return {"schema": 1, "epoch": row["epoch"], "state_version": row["version"],
                    "expires_at": row["expires"]}

    def _rotate(self, conn, key, now):
        epoch = uuid4().hex
        conn.execute("UPDATE execution_sessions SET epoch=?,created=?,expires=?,version=version+1 WHERE session=?",
                     (epoch, now, now + self.policy.session_ttl, key))
        conn.execute("UPDATE execution_states SET epoch=?,version=version+1,payload=NULL WHERE session=?", (epoch, key))
        return epoch

    def rotate(self, session_id: str, *, expected_epoch: str) -> str:
        key = session_key(session_id)
        with self.transaction() as conn:
            now = self.clock(conn)
            row = self._session(conn, key, now)
            if row is None or row["epoch"] != expected_epoch:
                raise ExecutionError("EXECUTION_STALE")
            self.check_writer(conn, key, row["epoch"], now)
            epoch = self._rotate(conn, key, now)
        return epoch

    def writer_required(self, conn):
        if not self.require_writer:
            enabled = conn.execute("SELECT value FROM execution_meta WHERE key='execution_enabled'").fetchone()
            self.require_writer = bool(enabled and enabled[0] == "1")
        return self.require_writer

    def check_writer(self, conn, key, epoch, now):
        writer = _WRITER.get()
        if writer is None:
            if self.writer_required(conn):
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
            return
        writer.validate(conn, self, key, epoch, now)

    def read(self, session_id: str, kind: str):
        key = session_key(session_id)
        with self.transaction() as conn:
            now = self.clock(conn)
            session = self._session(conn, key, now)
            row = conn.execute("SELECT * FROM execution_states WHERE session=? AND kind=?", (key, kind)).fetchone()
            valid = self._valid_session(session, now)
            present = bool(valid and row and row["epoch"] == session["epoch"] and row["payload"] is not None)
            version = StateVersion(self.authority, key, session["epoch"] if session else "",
                                   kind, row["version"] if row else 0, present)
            payload = json.loads(row["payload"]) if present else None
        _READS.set({**_READS.get(), (self.authority, key, kind): version})
        return payload, version

    def save(self, session_id: str, kind: str, payload: dict, expected: StateVersion | None, *, origin="live"):
        if kind not in {"workflow", "child"}:
            raise ValueError("invalid state kind")
        key = session_key(session_id)
        encoded = canonical(payload)
        if len(encoded.encode()) > self.policy.max_state_bytes:
            raise ExecutionError("EXECUTION_CAPACITY")
        self.storage_capacity(extra=2 * len(encoded.encode()))
        with self.transaction() as conn:
            now = self.clock(conn)
            session = self._session(conn, key, now, create=True)
            if not self._valid_session(session, now):
                raise ExecutionError("EXECUTION_STALE")
            self.check_writer(conn, key, session["epoch"], now)
            row = conn.execute("SELECT * FROM execution_states WHERE session=? AND kind=?", (key, kind)).fetchone()
            version = row["version"] if row else 0
            if expected is None:
                expected = _READS.get().get((self.authority, key, kind))
                if expected is not None and expected.present:
                    raise ExecutionError("EXECUTION_VERSION_REQUIRED")
            if expected is None:
                if row is not None:
                    raise ExecutionError("EXECUTION_VERSION_REQUIRED")
            elif (expected.authority != self.authority or expected.session != key or expected.kind != kind
                  or expected.version != version or (expected.epoch and expected.epoch != session["epoch"])):
                raise ExecutionError("EXECUTION_STALE")
            version += 1
            self._record_task(conn, key, session["epoch"], kind, payload, version, now, origin)
            conn.execute("INSERT INTO execution_states VALUES (?,?,?,?,?) ON CONFLICT(session,kind) DO UPDATE SET "
                         "epoch=excluded.epoch,version=excluded.version,payload=excluded.payload",
                         (key, session["epoch"], kind, version, encoded))
            conn.execute("UPDATE execution_sessions SET version=version+1,expires=? WHERE session=?",
                         (min(now + self.policy.session_ttl, session["created"] + self.policy.epoch_max_age), key))
            result = StateVersion(self.authority, key, session["epoch"], kind, version, True)
        _READS.set({**_READS.get(), (self.authority, key, kind): result})
        return result

    def _record_task(self, conn, key, epoch, kind, payload, version, now, origin):
        task_id = str(payload.get("workflow_search_id") or payload.get("current_search_id") or "") if kind == "workflow" else str(payload.get("current_search_id") or "")
        revision = int(payload.get("task_revision") or 0)
        if not task_id or revision <= 0:
            return
        parent_id = None
        unit_id = ""
        input_version = digest({"image": payload.get("current_image_path", payload.get("source_page_path", ""))})
        if kind == "child":
            parent = conn.execute("SELECT payload FROM execution_states WHERE session=? AND kind='workflow' AND epoch=?", (key, epoch)).fetchone()
            if parent and parent[0]:
                data = json.loads(parent[0])
                if data.get("entry_route") in {"A2", "A3"}:
                    unit_id = str(data.get("selected_unit_id") or "")
                    if data.get("entry_route") == "A3" and unit_id not in {str(u.get("unit_id")) for u in data.get("units", [])}:
                        raise ExecutionError("EXECUTION_PARENT_INVALID")
                    parent_task_id = data.get("workflow_search_id") or data.get("current_search_id")
                    parent_row = conn.execute("SELECT id FROM execution_tasks WHERE session=? AND epoch=? AND kind='workflow' AND task_id=? AND task_revision=?", (key, epoch, parent_task_id, data["task_revision"])).fetchone()
                    if parent_row is None or parent_task_id == task_id:
                        raise ExecutionError("EXECUTION_PARENT_INVALID")
                    parent_id = parent_row[0]
                    crop = data.get("crop_drafts", {}).get(unit_id, {})
                    input_version = digest({"crop": crop, "image": payload.get("current_image_path", "")})
                    source = Path(str(crop.get("path") or data.get("source_page_path") or ""))
                    child_image = Path(str(payload.get("current_image_path") or ""))
                    if not source.is_file() or not child_image.is_file():
                        raise ExecutionError("EXECUTION_PARENT_INPUT_UNAVAILABLE")
                    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                    if hashlib.sha256(child_image.read_bytes()).hexdigest() != source_hash:
                        raise ExecutionError("EXECUTION_PARENT_INPUT_MISMATCH")
                    input_version = digest({"content": source_hash, "bounds": crop.get("bounds", {})})
        old = conn.execute("SELECT * FROM execution_tasks WHERE session=? AND epoch=? AND kind=? AND task_id=? AND task_revision=?", (key, epoch, kind, task_id, revision)).fetchone()
        if old:
            record_id = old["id"]
            if kind == "child" and (old["parent_id"], old["unit_id"], old["input_version"]) != (parent_id, unit_id, input_version):
                raise ExecutionError("EXECUTION_PARENT_CHANGED")
            conn.execute("UPDATE execution_tasks SET phase=?,state_version=?,updated=? WHERE id=?", (payload.get("phase", ""), version, now, old["id"]))
        else:
            self.capacity(conn, "execution_tasks", self.policy.max_tasks)
            record_id = uuid4().hex
            conn.execute("INSERT INTO execution_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (record_id, key, epoch, kind, task_id, revision, parent_id, unit_id, input_version, origin, payload.get("phase", ""), version, now, now))
        writer = _WRITER.get()
        if self.require_writer and writer is not None:
            conn.execute("INSERT INTO execution_task_attempts VALUES (?,?,?,?) "
                         "ON CONFLICT(task_record_id,attempt_id) DO UPDATE SET last_state_version=excluded.last_state_version",
                         (record_id, writer.attempt_id, version, version))

    def clear(self, session_id: str, kind: str):
        key = session_key(session_id)
        with self.transaction() as conn:
            now = self.clock(conn)
            session = self._session(conn, key, now, create=True)
            self.check_writer(conn, key, session["epoch"], now)
            conn.execute("INSERT INTO execution_states VALUES (?,?,?,1,NULL) ON CONFLICT(session,kind) "
                         "DO UPDATE SET version=version+1,payload=NULL,epoch=excluded.epoch", (key, session["epoch"], kind))
            conn.execute("UPDATE execution_sessions SET version=version+1 WHERE session=?", (key,))
        self.read(session_id, kind)

    def tasks(self, session_id: str):
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM execution_tasks WHERE session=? ORDER BY created,id", (session_key(session_id),))]


class ExecutionSessionStore:
    """Existing runtime interface backed solely by the unified authority."""
    def __init__(self, authority: ExecutionStore, kind="child"):
        if kind not in {"workflow", "child"}:
            raise ValueError("invalid kind")
        self.authority = authority
        self.kind = kind
        self.database_path = authority.path

    def load(self, session_id):
        payload, version = self.authority.read(session_id, self.kind)
        if payload is None:
            return None
        if self.kind == "workflow":
            from tiku_agent.a3_runtime import A3SessionState
            state = A3SessionState.from_dict(payload)
        else:
            from tiku_agent.state import AgentState
            state = AgentState.from_dict(payload)
        state._execution_version = version
        return state

    def save(self, state):
        state.validate()
        version = self.authority.save(state.session_id, self.kind, state.to_dict(), getattr(state, "_execution_version", None))
        state._execution_version = version

    def clear(self, session_id):
        self.authority.clear(session_id, self.kind)

    def purge_expired(self):
        # Expired snapshots read as absent. Epoch rotation invalidates both
        # slots atomically; artifacts/active operation retention is separate.
        return []

    def record_page_error(self, state, *, task_kind, diagnostic):
        with self.authority.transaction() as conn:
            now = self.authority.clock(conn)
            conn.execute("DELETE FROM execution_page_errors WHERE created_at<?", (now - 7 * 86400,))
            conn.execute("DELETE FROM execution_page_errors WHERE id IN (SELECT id FROM execution_page_errors ORDER BY id DESC LIMIT -1 OFFSET 999)")
            conn.execute("INSERT INTO execution_page_errors VALUES (NULL,?,?,?,?,?,?,?,?)",
                         (session_key(state.session_id), state.current_search_id, str(task_kind)[:80],
                          state.phase, str(diagnostic.get("error_type", ""))[:80],
                          str(diagnostic.get("error_code", ""))[:80],
                          str(diagnostic.get("error_message", ""))[:480], now))

    def recent_page_errors(self, *, limit=20):
        with self.authority.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT search_id,task_kind,phase,error_type,error_code,error_message,created_at FROM execution_page_errors ORDER BY id DESC LIMIT ?", (max(1, min(100, int(limit))),))]

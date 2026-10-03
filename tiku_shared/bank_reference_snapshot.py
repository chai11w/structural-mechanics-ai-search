"""Read-only inventory of durable search references for bank retention.

No deletion, publication, schema migration, audit insertion or model endpoint.
The maintenance caller must supply every deployed source and hold its own writer
and reader-maintenance locks when checking freshness before a retention commit.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, UTC
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from tiku_agent.checkpoint_bank_reference import BankReferenceV1, CheckpointBankCatalog
from tiku_shared.bank_readers import reader_gate


KINDS = {"agent", "a3", "execution", "checkpoints"}
EXECUTION_TABLES = {
    "execution_meta", "execution_sessions", "execution_states", "execution_tasks", "execution_page_errors",
    "execution_operations", "execution_attempts", "execution_owners", "execution_cost_runs", "execution_task_attempts",
    "execution_effects", "execution_cost_outbox", "execution_collectors", "execution_files", "execution_handoffs",
    "execution_unit_batches", "execution_unit_checks", "execution_model_recoveries",
}


class ReferenceSnapshotError(ValueError):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _timestamp(value):
    if not isinstance(value, str):
        raise ReferenceSnapshotError("invalid reference expiry")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ReferenceSnapshotError("reference expiry requires timezone")
    return parsed.astimezone(UTC).timestamp()


def _identity(path):
    from tiku_agent.checkpoint_store import _reject_linked_path
    _reject_linked_path(path)
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ReferenceSnapshotError("invalid reference database file")
    return (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)


def _sidecars(path):
    from tiku_agent.checkpoint_store import _reject_linked_path
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        _reject_linked_path(sidecar)
        if sidecar.exists():
            _identity(sidecar)


@dataclass(frozen=True)
class ReferenceSource:
    name: str
    kind: str
    path: Path

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name):
            raise ReferenceSnapshotError("invalid reference source name")
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ReferenceSnapshotError("unsupported reference source kind")
        path = Path(self.path)
        if not path.is_absolute() or ".." in path.parts:
            raise ReferenceSnapshotError("reference database path must be absolute")
        object.__setattr__(self, "path", path)


class SearchReferenceSnapshot:
    """A bounded scan plus live, same-connection SQLite change detectors.

    Connections stay open until context exit, but read transactions end before
    the result is returned. Never use a serialized report as a freshness check.
    """

    def __init__(self, sources, *, catalog: CheckpointBankCatalog, now=None,
                 max_rows=100_000, max_value_bytes=2 * 1024 * 1024,
                 max_total_bytes=128 * 1024 * 1024, max_scan_seconds=10.0):
        self.sources = tuple(sources)
        if (not 1 <= len(self.sources) <= 16 or any(type(item) is not ReferenceSource for item in self.sources)
                or len({item.name for item in self.sources}) != len(self.sources)
                or len({os.path.normcase(str(item.path)) for item in self.sources}) != len(self.sources)):
            raise ReferenceSnapshotError("reference sources must be explicit and unique")
        if type(catalog) is not CheckpointBankCatalog or "published" not in catalog.roots:
            raise ReferenceSnapshotError("published bank mapping is required")
        for value, ceiling in ((max_rows, 100_000), (max_value_bytes, 2 * 1024 * 1024),
                               (max_total_bytes, 128 * 1024 * 1024)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise ReferenceSnapshotError("invalid reference scan bound")
        if not math.isfinite(max_scan_seconds) or not 0 < max_scan_seconds <= 30:
            raise ReferenceSnapshotError("invalid reference scan duration")
        self.catalog = catalog
        moment = now if now is not None else datetime.now(UTC)
        if not isinstance(moment, datetime) or moment.tzinfo is None:
            raise ReferenceSnapshotError("reference snapshot requires a trusted aware time")
        self.now = moment.astimezone(UTC)
        self.max_rows, self.max_value_bytes = max_rows, max_value_bytes
        self.max_total_bytes, self.max_scan_seconds = max_total_bytes, max_scan_seconds
        self._rows = self._bytes = 0
        self._versions, self._guards = set(), []
        self._stack = None
        self._report = None
        self._started = False

    def __enter__(self):
        if self._started:
            raise ReferenceSnapshotError("reference snapshot cannot be reused")
        self._started = True
        self._stack = ExitStack()
        self._deadline = time.monotonic() + self.max_scan_seconds
        try:
            with reader_gate(self.catalog.roots["published"]):
                sources = []
                for source in sorted(self.sources, key=lambda item: item.name):
                    try:
                        sources.append(self._scan(source))
                    except Exception as error:
                        reason = str(error) if isinstance(error, ReferenceSnapshotError) else "reference scan unavailable"
                        raise ReferenceSnapshotError(source.name + ": " + reason) from error
                self.assert_unchanged()
                self._check_time()
            configuration = {"sources": [(item.name, item.kind, str(item.path)) for item in self.sources],
                             "banks": {key: str(path) for key, path in self.catalog.roots.items()}}
            result = {"schema": 1, "scope": "configured-search-sources", "as_of": self.now.isoformat(),
                      "configuration_sha256": hashlib.sha256(_json_bytes(configuration)).hexdigest(),
                      "sources": sources, "protected_versions": sorted(self._versions)}
            self._report = {**result, "snapshot_sha256": hashlib.sha256(_json_bytes(result)).hexdigest()}
            return self
        except Exception as error:
            self.close()
            if isinstance(error, ReferenceSnapshotError):
                raise
            raise ReferenceSnapshotError("reference scan unavailable") from error
        except BaseException:
            self.close()
            raise

    def close(self):
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()

    def __exit__(self, *args):
        self.close()

    def report(self):
        if self._report is None:
            raise ReferenceSnapshotError("reference snapshot was not captured")
        return deepcopy(self._report)

    def assert_unchanged(self):
        if self._stack is None:
            raise ReferenceSnapshotError("reference snapshot is closed")
        for source, connection, identity, version in self._guards:
            try:
                _sidecars(source.path)
                if (_identity(source.path) != identity or connection.in_transaction
                        or connection.execute("PRAGMA data_version").fetchone()[0] != version):
                    raise ReferenceSnapshotError("reference source changed")
            except Exception as error:
                raise ReferenceSnapshotError("reference source changed: " + source.name) from error

    def _check_time(self):
        if time.monotonic() >= self._deadline:
            raise ReferenceSnapshotError("reference scan duration exceeded")

    def _parse(self, value):
        if not isinstance(value, str) or len(value.encode("utf-8")) > self.max_value_bytes:
            raise ReferenceSnapshotError("reference payload exceeds bound")
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ReferenceSnapshotError("duplicate reference payload key")
                result[key] = item
            return result
        def invalid(value):
            raise ReferenceSnapshotError("nonfinite reference payload")
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)

    def _row(self, row, hasher):
        self._check_time()
        self._rows += 1
        if self._rows > self.max_rows:
            raise ReferenceSnapshotError("reference row bound exceeded")
        values = dict(row)
        if any(isinstance(value, str) and len(value.encode("utf-8")) > self.max_value_bytes for value in values.values()):
            raise ReferenceSnapshotError("reference payload exceeds bound")
        encoded = _json_bytes(values)
        self._bytes += len(encoded)
        if self._bytes > self.max_total_bytes:
            raise ReferenceSnapshotError("reference byte bound exceeded")
        hasher.update(len(encoded).to_bytes(8, "big")); hasher.update(encoded)
        return values

    def _path_version(self, value):
        path = Path(value)
        root = self.catalog.roots["published"]
        if path.is_absolute() and path.is_relative_to(root):
            parts = path.relative_to(root).parts
            if (".." in path.parts or len(parts) < 4 or parts[0] != "versions"
                    or not re.fullmatch(r"[a-f0-9]{64}", parts[1]) or parts[2] not in {"main", "symbolic"}):
                raise ReferenceSnapshotError("unresolved published bank path")
            self._versions.add(parts[1])

    def _walk(self, value, *, depth=0):
        self._check_time()
        if depth > 32:
            raise ReferenceSnapshotError("reference payload nesting exceeded")
        if isinstance(value, dict):
            if {"bank_id", "relative_key", "lookup_mode"} <= set(value):
                reference = BankReferenceV1.from_dict(value)
                root = self.catalog.roots.get(reference.bank_id)
                if root is None:
                    raise ReferenceSnapshotError("unconfigured bank reference")
                self._path_version(str(root.joinpath(*reference.relative_key.split("/"))))
                return
            for item in value.values():
                self._walk(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value:
                self._walk(item, depth=depth + 1)
        elif isinstance(value, str):
            self._path_version(value)

    @staticmethod
    def _tables(connection):
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    @staticmethod
    def _columns(connection, table, required, *, exact=False):
        row = connection.execute("SELECT type FROM sqlite_schema WHERE name=?", (table,)).fetchone()
        if row is None or row[0] != "table":
            raise ReferenceSnapshotError("reference table is missing")
        actual = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
        if not required <= actual or exact and required != actual:
            raise ReferenceSnapshotError("unsupported reference table schema")

    def _scan(self, source):
        self._check_time()
        identity = _identity(source.path)
        _sidecars(source.path)
        connection = sqlite3.connect(source.path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        self._stack.callback(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max(65536, self.max_value_bytes * 4))
        connection.set_progress_handler(lambda: int(time.monotonic() >= self._deadline), 1000)
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        before = self._rows
        hasher = hashlib.sha256()
        connection.execute("BEGIN")
        try:
            if source.kind in {"agent", "a3"}:
                live = self._sessions(connection, source.kind, hasher)
            elif source.kind == "checkpoints":
                live = self._checkpoints(connection, hasher)
            else:
                live = self._execution(connection, hasher)
        finally:
            connection.rollback()
            connection.set_progress_handler(None, 0)
        self._guards.append((source, connection, identity, version))
        return {"name": source.name, "kind": source.kind, "rows": self._rows - before,
                "live_rows": live, "content_sha256": hasher.hexdigest()}

    def _state(self, payload, kind):
        if not isinstance(payload, dict):
            raise ReferenceSnapshotError("reference state must be an object")
        if kind == "agent":
            from tiku_agent.state import AgentState
            return AgentState.from_dict(deepcopy(payload))
        from tiku_agent.a3_runtime import A3SessionState
        return A3SessionState.from_dict(deepcopy(payload))

    def _sessions(self, connection, kind, hasher):
        table = "agent_sessions" if kind == "agent" else "a3_sessions"
        allowed = {table} if kind == "agent" else {table, "a3_page_errors"}
        if self._tables(connection) - allowed:
            raise ReferenceSnapshotError("unsupported session reference schema")
        columns = {"session_id", "state_json", "updated_at", "expires_at"}
        if kind == "agent":
            columns |= {"schema_version", "created_at"}
        self._columns(connection, table, columns, exact=True)
        live = 0
        for row in connection.execute("SELECT * FROM " + table + " ORDER BY session_id"):
            value = self._row(row, hasher)
            if kind == "agent" and (type(value["schema_version"]) is not int or value["schema_version"] != 1):
                raise ReferenceSnapshotError("unsupported reference state version")
            payload = self._parse(value["state_json"])
            state = self._state(payload, kind)
            if state.session_id != value["session_id"]:
                raise ReferenceSnapshotError("reference state identity mismatch")
            if _timestamp(value["expires_at"]) > self.now.timestamp():
                self._walk(payload); live += 1
        return live

    def _checkpoints(self, connection, hasher):
        from tiku_agent.checkpoint_store import _verify_read_schema, SQLiteCheckpointStore
        if self._tables(connection) - {"checkpoint_store_meta", "checkpoints", "artifact_blobs", "artifacts",
                                       "checkpoint_artifacts", "evidence_audit"}:
            raise ReferenceSnapshotError("unsupported checkpoint reference schema")
        if not _verify_read_schema(connection):
            raise ReferenceSnapshotError("checkpoint source is uninitialized")
        live = 0
        for row in connection.execute("SELECT * FROM checkpoints ORDER BY checkpoint_id"):
            value = self._row(row, hasher)
            self._parse(value["payload_json"])
            checkpoint = SQLiteCheckpointStore._checkpoint_row(connection, row)
            if _timestamp(checkpoint.expires_at) > self.now.timestamp():
                self._walk(checkpoint.to_dict()["result"]); live += 1
        return live

    def _execution(self, connection, hasher):
        tables = self._tables(connection)
        if not {"execution_meta", "execution_sessions", "execution_states"} <= tables or tables - EXECUTION_TABLES:
            raise ReferenceSnapshotError("unsupported execution reference schema")
        self._columns(connection, "execution_meta", {"key", "value"}, exact=True)
        self._columns(connection, "execution_sessions", {"session", "epoch", "created", "expires", "version"}, exact=True)
        self._columns(connection, "execution_states", {"session", "epoch", "kind", "version", "payload"}, exact=True)
        metadata = {}
        for row in connection.execute("SELECT key,value FROM execution_meta ORDER BY key"):
            value = self._row(row, hasher)
            metadata[value["key"]] = value["value"]
        if (metadata.get("schema") != "1" or metadata.get("migration", "complete") != "complete"
                or not re.fullmatch(r"[a-f0-9]{32}", metadata.get("authority", ""))):
            raise ReferenceSnapshotError("unsupported execution reference version")
        sessions = {}
        for row in connection.execute("SELECT * FROM execution_sessions ORDER BY session"):
            value = self._row(row, hasher)
            if any(type(value[key]) not in (float, int) or not math.isfinite(value[key]) for key in ("created", "expires")):
                raise ReferenceSnapshotError("invalid execution reference expiry")
            sessions[value["session"]] = value
        live = 0
        for row in connection.execute("SELECT * FROM execution_states ORDER BY session,kind"):
            value = self._row(row, hasher)
            session = sessions.get(value["session"])
            if session is None or value["kind"] not in {"workflow", "child"}:
                raise ReferenceSnapshotError("execution reference owner is missing")
            if value["payload"] is not None:
                payload = self._parse(value["payload"])
                state = self._state(payload, "a3" if value["kind"] == "workflow" else "agent")
                from tiku_agent.execution_store import session_key
                if session_key(state.session_id) != value["session"]:
                    raise ReferenceSnapshotError("execution reference identity mismatch")
                if session["epoch"] == value["epoch"] and session["expires"] > self.now.timestamp():
                    self._walk(payload); live += 1
        # Operation receipts and replay state have their own lifecycle. Keep
        # their references until that owner removes them, including unfinished
        # handoffs and results from a prior epoch. Bank GC never expires them.
        for table in sorted(tables - {"execution_meta", "execution_sessions", "execution_states"}):
            for row in connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid'):
                value = self._row(row, hasher)
                for item in value.values():
                    if isinstance(item, str):
                        if item.lstrip().startswith(("{", "[")):
                            self._walk(self._parse(item))
                        else:
                            self._path_version(item)
                live += 1
        return live

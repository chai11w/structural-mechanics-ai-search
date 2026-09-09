"""Durable operation admission and fenced attempts; no automatic effect replay."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import hashlib
from pathlib import Path
from uuid import uuid4

from tiku_agent.execution_store import ExecutionError, ExecutionStore, canonical, digest, session_key


@dataclass(frozen=True)
class OperationRequest:
    key: str
    epoch: str
    state_version: int

    @classmethod
    def parse(cls, value):
        if isinstance(value, cls):
            value = {"key": value.key, "epoch": value.epoch, "state_version": value.state_version}
        if (type(value) is not dict or set(value) != {"key", "epoch", "state_version"}
                or not isinstance(value.get("key"), str)
                or not re.fullmatch(r"[A-Za-z0-9:_.-]{8,128}", value["key"])
                or not isinstance(value.get("epoch"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["epoch"])
                or type(value.get("state_version")) is not int or value["state_version"] < 0):
            raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
        return cls(**value)


@dataclass(frozen=True)
class ExecutionWriter:
    authority: str
    session: str
    epoch: str
    operation_id: str
    attempt_id: str
    token: str

    def validate(self, conn, store, key, epoch, now):
        if (self.authority, self.session, self.epoch) != (store.authority, key, epoch):
            raise ExecutionError("EXECUTION_STALE")
        row = conn.execute("SELECT status,token,lease_until FROM execution_operations WHERE id=?", (self.operation_id,)).fetchone()
        if row is None or row["status"] != "RUNNING" or row["token"] != self.token or row["lease_until"] <= now:
            raise ExecutionError("EXECUTION_LEASE_LOST")
        attempt = conn.execute("SELECT created FROM execution_attempts WHERE id=? AND operation_id=? AND token=?",
                               (self.attempt_id, self.operation_id, self.token)).fetchone()
        if attempt is None or attempt[0] + store.policy.max_execution_seconds <= now:
            raise ExecutionError("EXECUTION_LEASE_LOST")


class OperationStore:
    def __init__(self, authority: ExecutionStore):
        self.authority = authority
        # Fixed source identity of the loaded release. Stored results do not
        # cross a changed implementation or prompt set silently.
        root = Path(__file__).resolve().parents[1]
        hasher = hashlib.sha256()
        files = sorted([*root.glob("*.py"), *root.joinpath("tiku_agent").rglob("*.py"), *root.joinpath("tiku_agent/prompts").glob("*.txt"), *root.joinpath("tiku_shared").glob("*.py")])
        for file in files:
            hasher.update(file.relative_to(root).as_posix().encode())
            hasher.update(file.read_bytes())
        self.producer = hasher.hexdigest()
        with authority.transaction() as conn:
            schema = """
                CREATE TABLE IF NOT EXISTS execution_operations (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, epoch TEXT NOT NULL,
                    identity TEXT NOT NULL, op_key TEXT NOT NULL, kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, expected_version INTEGER NOT NULL, producer TEXT NOT NULL,
                    target TEXT NOT NULL, previous_operation_id TEXT,
                    status TEXT NOT NULL, token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    result TEXT, result_bytes INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(session,epoch,identity,op_key));
                CREATE INDEX IF NOT EXISTS execution_operations_session ON execution_operations(session,epoch,status);
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES execution_operations(id),
                    token TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_owners (session TEXT PRIMARY KEY,epoch TEXT NOT NULL,identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_cost_runs (run_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL REFERENCES execution_operations(id),
                    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id));
                CREATE TABLE IF NOT EXISTS execution_task_attempts (
                    task_record_id TEXT NOT NULL REFERENCES execution_tasks(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id) ON DELETE CASCADE,
                    first_state_version INTEGER NOT NULL, last_state_version INTEGER NOT NULL,
                    PRIMARY KEY(task_record_id,attempt_id));
            """
            for statement in schema.split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns={row[1] for row in conn.execute("PRAGMA table_info(execution_operations)")}
            if not {"producer", "target", "previous_operation_id"}.issubset(columns):
                raise ExecutionError("EXECUTION_SCHEMA_UNSUPPORTED")
            from tiku_agent.execution_effects import create_effect_schema
            create_effect_schema(conn)

    def lookup(self, sid, identity, request):
        req = OperationRequest.parse(request)
        with self.authority.transaction() as conn:
            row = conn.execute("SELECT * FROM execution_operations WHERE session=? AND epoch=? AND identity=? AND op_key=?",
                               (session_key(sid), req.epoch, digest(identity), req.key)).fetchone()
            return dict(row) if row else None

    def verify_owner(self, sid, identity, *, claim=False):
        store = self.authority
        key = session_key(sid)
        with store.transaction() as conn:
            now = store.clock(conn)
            session = store._session(conn,key,now)
            if session is None:
                return
            owner = conn.execute("SELECT * FROM execution_owners WHERE session=?",(key,)).fetchone()
            if owner and owner["epoch"]==session["epoch"] and owner["identity"]!=digest(identity):
                raise ExecutionError("EXECUTION_STALE")
            if claim:
                conn.execute("INSERT OR REPLACE INTO execution_owners VALUES (?,?,?)",(key,session["epoch"],digest(identity)))

    def observe(self, sid, identity, request):
        with self.authority.transaction() as conn:
            self.verify_owner(sid,identity)
            row=self.lookup(sid,identity,request)
            if row is None:
                return None
            status=row["status"]
            now=self.authority.clock(conn)
            # Observation classifies an expired lease without taking execution
            # ownership or replaying anything. Maintenance persists UNKNOWN.
            if status=="RUNNING" and row["lease_until"]<=now:
                status="UNKNOWN"
            attempts=[{"attempt_id":item["id"],"status":item["status"]} for item in conn.execute("SELECT id,status FROM execution_attempts WHERE operation_id=? ORDER BY created DESC LIMIT 20",(row["id"],))]
            effect_counts = {item["status"]: item["count"] for item in conn.execute(
                "SELECT status,count(*) AS count FROM execution_effects WHERE operation_id=? GROUP BY status", (row["id"],))}
            pending = conn.execute("SELECT count(*) FROM execution_cost_runs r LEFT JOIN execution_cost_outbox c ON c.run_id=r.run_id "
                                   "WHERE r.operation_id=? AND (c.status<>'CONFIRMED' OR (c.run_id IS NULL AND EXISTS "
                                   "(SELECT 1 FROM execution_effects e WHERE e.run_id=r.run_id)))", (row["id"],)).fetchone()[0]
        return {"operation_id":row["id"],"kind":row["kind"],"status":status,"attempts":attempts,
                "effects":effect_counts,"accounting":{"pending_runs":pending}}

    def register(self, sid, identity, request, kind, inputs):
        req = OperationRequest.parse(request)
        key = session_key(sid)
        fingerprint = digest({"kind": kind, "input": inputs, "state_version": req.state_version})
        store = self.authority
        with store.transaction() as conn:
            now = store.clock(conn)
            self.verify_owner(sid,identity,claim=True)
            row = conn.execute("SELECT * FROM execution_operations WHERE session=? AND epoch=? AND identity=? AND op_key=?",
                               (key, req.epoch, digest(identity), req.key)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint or row["kind"] != kind:
                    raise ExecutionError("EXECUTION_INPUT_CONFLICT")
                if row["producer"] != self.producer:
                    raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
                current = store._session(conn,key,now)
                if row["kind"] != "clear" and (not store._valid_session(current,now) or current["epoch"]!=req.epoch):
                    raise ExecutionError("EXECUTION_STALE")
                return dict(row)
            current = store._session(conn, key, now)
            if not store._valid_session(current, now) or current["epoch"] != req.epoch or current["version"] != req.state_version:
                raise ExecutionError("EXECUTION_STALE")
            if conn.execute("SELECT 1 FROM execution_cost_outbox WHERE status<>'CONFIRMED' LIMIT 1").fetchone():
                raise ExecutionError("EXECUTION_COST_PENDING")
            if conn.execute("SELECT 1 FROM execution_effects e JOIN execution_operations o ON o.id=e.operation_id "
                            "LEFT JOIN execution_cost_outbox c ON c.run_id=e.run_id "
                            "WHERE o.status IN ('SUCCEEDED','UNKNOWN','CANCELLED','FAILED') "
                            "AND (e.status IN ('SENT','UNKNOWN') OR (e.status='CONFIRMED' AND (c.run_id IS NULL OR e.usage_known=0))) LIMIT 1").fetchone():
                raise ExecutionError("EXECUTION_COST_PENDING")
            store.capacity(conn,"execution_operations",store.policy.max_operations)
            used = conn.execute("SELECT coalesce(sum(result_bytes),0) FROM execution_operations").fetchone()[0]
            reserved = conn.execute("SELECT count(*) FROM execution_operations WHERE status IN ('REGISTERED','RUNNING','UNKNOWN')").fetchone()[0] * store.policy.max_result_bytes
            if used + reserved + store.policy.max_result_bytes > store.policy.max_total_result_bytes:
                raise ExecutionError("EXECUTION_CAPACITY")
            operation_id = uuid4().hex
            store.storage_capacity(extra=2*store.policy.max_state_bytes+store.policy.max_result_bytes)
            target = {}
            for state in conn.execute("SELECT kind,payload FROM execution_states WHERE session=? AND epoch=? AND payload IS NOT NULL", (key, req.epoch)):
                data = json.loads(state["payload"])
                target[state["kind"]] = {name: data[name] for name in ("workflow_search_id", "current_search_id", "task_revision", "candidate_generation", "selected_unit_id") if name in data}
            target["action"] = {name: inputs[name] for name in ("unit_id", "unit_ids", "task_revision", "workflow_search_id", "bounds") if name in inputs}
            action = inputs.get("action_context")
            if isinstance(action, dict):
                target["action_context"] = {name: action[name] for name in ("type", "task_id", "task_revision", "candidate_generation", "rank") if name in action}
            previous = conn.execute("SELECT id FROM execution_operations WHERE session=? AND epoch=? ORDER BY rowid DESC LIMIT 1", (key, req.epoch)).fetchone()
            conn.execute("INSERT INTO execution_operations (id,session,epoch,identity,op_key,kind,fingerprint,expected_version,producer,target,previous_operation_id,status,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (operation_id,key,req.epoch,digest(identity),req.key,kind,fingerprint,req.state_version,self.producer,canonical(target),previous[0] if previous else None,"REGISTERED",now,now))
            return dict(conn.execute("SELECT * FROM execution_operations WHERE id=?",(operation_id,)).fetchone())

    def claim(self, operation_id):
        store = self.authority
        failure = None
        writer = None
        with store.transaction() as conn:
            now = store.clock(conn)
            row = conn.execute("SELECT * FROM execution_operations WHERE id=?",(operation_id,)).fetchone()
            if row is None:
                raise ExecutionError("EXECUTION_STALE")
            if row["status"] != "REGISTERED":
                if row["status"] == "RUNNING" and row["lease_until"] <= now:
                    self._unknown(conn,row["id"],now)
                    failure = "EXECUTION_UNKNOWN"
                else:
                    failure = "EXECUTION_BUSY" if row["status"] == "RUNNING" else "EXECUTION_UNKNOWN"
            else:
                session = store._session(conn,row["session"],now)
                if not store._valid_session(session,now) or session["epoch"] != row["epoch"] or session["version"] != row["expected_version"]:
                    failure = "EXECUTION_STALE"
                    conn.execute("UPDATE execution_operations SET status='FAILED',updated=? WHERE id=?",(now,operation_id))
                else:
                    competing = conn.execute("SELECT * FROM execution_operations WHERE session=? AND epoch=? AND status IN ('RUNNING','UNKNOWN') AND id<>?",(row["session"],row["epoch"],row["id"])).fetchall()
                    for other in competing:
                        if other["status"] == "RUNNING" and other["lease_until"] <= now:
                            self._unknown(conn,other["id"],now)
                    if competing:
                        failure = "EXECUTION_BUSY" if all(o["status"] == "RUNNING" and o["lease_until"]>now for o in competing) else "EXECUTION_UNKNOWN"
                    else:
                        token, attempt = uuid4().hex, uuid4().hex
                        conn.execute("UPDATE execution_operations SET status='RUNNING',token=?,lease_until=?,updated=? WHERE id=? AND status='REGISTERED'",(token,now+min(store.policy.lease_seconds,store.policy.max_execution_seconds),now,operation_id))
                        conn.execute("INSERT INTO execution_attempts VALUES (?,?,?,'RUNNING',?,?)",(attempt,operation_id,token,now,now))
                        writer = ExecutionWriter(store.authority,row["session"],row["epoch"],operation_id,attempt,token)
        if failure:
            raise ExecutionError(failure)
        return writer

    def _unknown(self, conn, operation_id, now):
        conn.execute("UPDATE execution_effects SET status='UNKNOWN',updated=? WHERE operation_id=? AND status='SENT'", (now, operation_id))
        conn.execute("UPDATE execution_operations SET status='UNKNOWN',token='',updated=? WHERE id=?",(now,operation_id))
        conn.execute("UPDATE execution_attempts SET status='UNKNOWN',updated=? WHERE operation_id=? AND status='RUNNING'",(now,operation_id))

    def renew(self, writer):
        with self.authority.transaction() as conn:
            now = self.authority.clock(conn)
            writer.validate(conn,self.authority,writer.session,writer.epoch,now)
            attempt = conn.execute("SELECT created FROM execution_attempts WHERE id=?",(writer.attempt_id,)).fetchone()
            if attempt is None or attempt[0]+self.authority.policy.max_execution_seconds <= now:
                raise ExecutionError("EXECUTION_LEASE_LOST")
            session = self.authority._session(conn,writer.session,now)
            if session["epoch"] != writer.epoch:
                raise ExecutionError("EXECUTION_STALE")
            deadline = min(now+self.authority.policy.lease_seconds, attempt[0]+self.authority.policy.max_execution_seconds)
            conn.execute("UPDATE execution_operations SET lease_until=? WHERE id=?",(deadline,writer.operation_id))

    def finish(self, writer, result, *, reset=False):
        with self.authority.transaction() as conn:
            now = self.authority.clock(conn)
            writer.validate(conn,self.authority,writer.session,writer.epoch,now)
            if conn.execute("SELECT 1 FROM execution_effects WHERE operation_id=? AND status<>'CONFIRMED' LIMIT 1",
                            (writer.operation_id,)).fetchone():
                raise ExecutionError("EXECUTION_UNKNOWN")
            session = self.authority._session(conn,writer.session,now)
            if session["epoch"] != writer.epoch:
                raise ExecutionError("EXECUTION_STALE")
            if reset:
                self.authority._rotate(conn,writer.session,now)
            session = self.authority._session(conn,writer.session,now)
            context = {"schema":1,"epoch":session["epoch"],"state_version":session["version"],"expires_at":session["expires"]}
            result = {**result,"context":context}
            encoded = canonical(result)
            size = len(encoded.encode())
            if size > self.authority.policy.max_result_bytes:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            conn.execute("UPDATE execution_operations SET status='SUCCEEDED',result=?,result_bytes=?,token='',updated=? WHERE id=?",(encoded,size,now,writer.operation_id))
            conn.execute("UPDATE execution_attempts SET status='SUCCEEDED',updated=? WHERE id=?",(now,writer.attempt_id))
        return context

    def fail(self, writer, *, known_not_started=False):
        with self.authority.transaction() as conn:
            now = self.authority.clock(conn)
            row = conn.execute("SELECT token,status FROM execution_operations WHERE id=?",(writer.operation_id,)).fetchone()
            if row and row["token"] == writer.token and row["status"] == "RUNNING":
                self._unknown(conn,writer.operation_id,now)
                if known_not_started:
                    operation = conn.execute("SELECT session,epoch,expected_version FROM execution_operations WHERE id=?",(writer.operation_id,)).fetchone()
                    session = self.authority._session(conn,operation["session"],now)
                    linked = conn.execute("SELECT 1 FROM execution_cost_runs WHERE operation_id=? LIMIT 1",(writer.operation_id,)).fetchone()
                    if (session and session["epoch"]==operation["epoch"] and session["version"]==operation["expected_version"] and linked is None):
                        conn.execute("UPDATE execution_operations SET status='REGISTERED' WHERE id=?",(writer.operation_id,))
                        conn.execute("UPDATE execution_attempts SET status='FAILED' WHERE id=?",(writer.attempt_id,))

    def bind_cost_run(self, writer, run_id):
        with self.authority.transaction() as conn:
            now=self.authority.clock(conn)
            writer.validate(conn,self.authority,writer.session,writer.epoch,now)
            conn.execute("INSERT INTO execution_cost_runs VALUES (?,?,?)",(run_id,writer.operation_id,writer.attempt_id))

    def maintain(self):
        """Bounded cleanup. Never evict active/unknown operations or live-epoch receipts."""
        store = self.authority
        with store.transaction() as conn:
            now = store.clock(conn)
            for row in conn.execute("SELECT id FROM execution_operations WHERE status='RUNNING' AND lease_until<=? LIMIT 100",(now,)).fetchall():
                self._unknown(conn,row[0],now)
            removable = conn.execute("SELECT o.id FROM execution_operations o LEFT JOIN execution_sessions s ON s.session=o.session WHERE o.status IN ('SUCCEEDED','FAILED') AND o.updated<? AND (s.epoch<>o.epoch OR s.expires<=?) "
                                     "AND NOT EXISTS (SELECT 1 FROM execution_effects e LEFT JOIN execution_cost_outbox c ON c.run_id=e.run_id WHERE e.operation_id=o.id AND (e.status<>'CONFIRMED' OR e.usage_known=0 OR c.status IS NULL OR c.status<>'CONFIRMED')) "
                                     "AND NOT EXISTS (SELECT 1 FROM execution_cost_outbox c JOIN execution_cost_runs r ON r.run_id=c.run_id WHERE r.operation_id=o.id AND c.status<>'CONFIRMED') LIMIT 100",(now-store.policy.history_ttl,now)).fetchall()
            for row in removable:
                conn.execute("DELETE FROM execution_effects WHERE operation_id=?",(row[0],))
                conn.execute("DELETE FROM execution_collectors WHERE run_id IN (SELECT run_id FROM execution_cost_runs WHERE operation_id=?)",(row[0],))
                conn.execute("DELETE FROM execution_cost_outbox WHERE run_id IN (SELECT run_id FROM execution_cost_runs WHERE operation_id=?)",(row[0],))
                conn.execute("DELETE FROM execution_cost_runs WHERE operation_id=?",(row[0],))
                conn.execute("DELETE FROM execution_attempts WHERE operation_id=?",(row[0],))
                conn.execute("DELETE FROM execution_operations WHERE id=?",(row[0],))
            # Children before parents; current epoch and unresolved-session history stay.
            conn.execute("DELETE FROM execution_tasks WHERE id IN (SELECT t.id FROM execution_tasks t JOIN execution_sessions s ON s.session=t.session WHERE t.updated<? AND (t.epoch<>s.epoch OR s.expires<=?) AND NOT EXISTS (SELECT 1 FROM execution_operations o WHERE o.session=t.session AND o.status IN ('REGISTERED','RUNNING','UNKNOWN')) AND NOT EXISTS (SELECT 1 FROM execution_tasks c WHERE c.parent_id=t.id) LIMIT 100)",(now-store.policy.history_ttl,now))
            return {"operations_removed":len(removable)}

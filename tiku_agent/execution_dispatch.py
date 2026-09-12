"""Opt-in durable command admission on the existing execution authority.

This is a private Python boundary; the HTTP submission/observation protocol is
introduced separately. A grant is created only by a trusted authenticated entry.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from uuid import uuid4

from tiku_agent.execution_dispatch_input import freeze_input
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_store import ExecutionError, canonical, digest, session_key, _TRANSACTION


@dataclass(frozen=True)
class DispatchPolicy:
    max_concurrent: int = 1
    max_queued: int = 2
    queue_seconds: int = 55
    max_records: int = 1000
    max_grants: int = 1000
    max_json_bytes: int = 32 * 1024
    max_image_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_total_input_bytes: int = 64 * 1024 * 1024
    input_ttl: int = 86400
    grant_max_age: int = 30 * 86400

    def __post_init__(self):
        if any(type(v) is not int or v < (0 if k == "max_queued" else 1) for k, v in vars(self).items()):
            raise ValueError("dispatch limits must be bounded integers")
        if self.max_concurrent + self.max_queued > self.max_records:
            raise ValueError("dispatch capacity exceeds record limit")


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_dispatch_grants (
    id TEXT PRIMARY KEY, session TEXT NOT NULL, identity TEXT NOT NULL,
    auth_version INTEGER NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS execution_dispatch (
    operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id) ON DELETE CASCADE,
    grant_id TEXT NOT NULL REFERENCES execution_dispatch_grants(id),
    status TEXT NOT NULL CHECK(status IN ('WAITING','CLAIMED','SETTLED','REVOKED')),
    accepted REAL NOT NULL, deadline REAL NOT NULL, updated REAL NOT NULL,
    error_code TEXT NOT NULL DEFAULT '', progress_version INTEGER NOT NULL DEFAULT 0,
    progress_stage TEXT NOT NULL DEFAULT 'queued');
CREATE INDEX IF NOT EXISTS execution_dispatch_queue ON execution_dispatch(status,accepted);
CREATE TABLE IF NOT EXISTS execution_dispatch_inputs (
    operation_id TEXT PRIMARY KEY REFERENCES execution_dispatch(operation_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL, identity_key TEXT NOT NULL, payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL, image BLOB, image_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL);
"""


class DispatchStore:
    def __init__(self, runtime, *, authorize, policy=None):
        self.runtime = runtime
        self.operations = runtime.execution_operations
        self.store = self.operations.authority
        self.policy = policy or DispatchPolicy()
        self.accepting = True
        if not callable(authorize):
            raise ValueError("a live identity/version authorizer is required")
        self.authorize = authorize
        # A dedicated runtime has one durable outer queue. Inherited A3 -> A2
        # gates retain their original limits. Legacy business entry is disabled.
        gate = runtime._execution_gate
        if gate.max_concurrent and self.policy.max_concurrent > gate.max_concurrent:
            raise ValueError("dispatch cannot increase runtime concurrency")
        if gate.max_concurrent and (self.policy.max_queued > gate.max_queued or self.policy.queue_seconds > gate.wait_seconds):
            raise ValueError("dispatch cannot increase runtime queue limits")
        with gate._condition:
            if gate._active or gate._waiters:
                raise ValueError("attach background dispatch only to an idle isolated runtime")
        with self.store.transaction() as conn:
            previous = conn.execute("SELECT value FROM execution_meta WHERE key='dispatch_schema'").fetchone()
            if previous and previous[0] != "1":
                raise ExecutionError("EXECUTION_SCHEMA_UNSUPPORTED")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            definition = canonical(vars(self.policy))
            old = conn.execute("SELECT value FROM execution_meta WHERE key='dispatch_policy'").fetchone()
            if old and old[0] != definition:
                raise ExecutionError("EXECUTION_DISPATCH_POLICY_MISMATCH")
            conn.execute("INSERT OR IGNORE INTO execution_meta VALUES ('dispatch_schema','1')")
            conn.execute("INSERT OR IGNORE INTO execution_meta VALUES ('dispatch_policy',?)", (definition,))
        self.operations.background_required = True
        runtime.execution_dispatch = self
        child = getattr(runtime, "a2_runtime", None)
        if child is not None:
            child.execution_dispatch = self

    def _identity(self, identity, version):
        if (type(identity) is not str or not identity.strip() or len(identity) > 128
                or type(version) is not int or version < 1):
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        try:
            allowed = self.authorize(identity, version)
        except Exception:
            raise ExecutionError("EXECUTION_AUTH_UNAVAILABLE") from None
        if allowed is not True:
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")

    def create_grant(self, sid, identity, *, auth_version, expires_at):
        """Trusted login integration only; this reference is never a bearer token."""
        key = session_key(sid)
        self._identity(identity, auth_version)
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            session = self.store._session(conn, key, now)
            if not self.store._valid_session(session, now) or expires_at <= now:
                raise ExecutionError("EXECUTION_STALE")
            self.operations.verify_owner(sid, identity, claim=True)
            if conn.execute("SELECT count(*) FROM execution_dispatch_grants").fetchone()[0] >= self.policy.max_grants:
                raise ExecutionError("EXECUTION_CAPACITY")
            self.store.storage_capacity(extra=4096)
            grant = uuid4().hex
            conn.execute("INSERT INTO execution_dispatch_grants VALUES (?,?,?,?,?,0)",
                         (grant, key, digest(identity), auth_version, min(expires_at, now + self.policy.grant_max_age)))
            return grant

    def revoke_grant(self, sid, identity, grant):
        with self.store.transaction() as conn:
            conn.execute("UPDATE execution_dispatch_grants SET revoked=1 WHERE id=? AND session=? AND identity=?",
                         (grant, session_key(sid), digest(identity)))

    def _grant(self, conn, sid, identity, grant, now):
        row = self._local_grant(conn, sid, identity, grant, now)
        self._identity(identity, row["auth_version"])
        return row

    @staticmethod
    def _local_grant(conn, sid, identity, grant, now):
        row = conn.execute("SELECT * FROM execution_dispatch_grants WHERE id=? AND session=? AND identity=?",
                           (grant, session_key(sid), digest(identity))).fetchone()
        if row is None or row["revoked"] or row["expires"] <= now:
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        return row

    def _admission(self, conn, sid, identity, grant, epoch, now, *, budget=True):
        self._grant(conn, sid, identity, grant, now)
        session = self.store._session(conn, session_key(sid), now)
        if not self.store._valid_session(session, now) or session["epoch"] != epoch:
            raise ExecutionError("EXECUTION_STALE")
        self.operations.verify_owner(sid, identity)
        if budget:
            self.operations.ensure_cost_available()
            getattr(self.runtime, "a2_runtime", self.runtime).ensure_budget_available(identity)
        # Control/ledger reads may have consumed time. Recheck local expiration
        # with a fresh clock immediately before accepting or sending work.
        now = self.store.clock(conn)
        self._local_grant(conn, sid, identity, grant, now)
        session = self.store._session(conn, session_key(sid), now)
        if not self.store._valid_session(session, now) or session["epoch"] != epoch:
            raise ExecutionError("EXECUTION_STALE")

    def accept(self, sid, identity, grant, request, kind, parameters, *, image=None):
        current = _TRANSACTION.get()
        if current is not None and current[0] == str(self.store.path):
            raise ExecutionError("EXECUTION_ACCEPTANCE_NESTED")
        request = OperationRequest.parse(request)
        payload, inputs, image_hash = freeze_input(self.runtime, kind, parameters, image, self.policy)
        size = len(payload.encode()) + len(image or b"") + len(sid.encode()) + len(identity.encode())
        with self.store.transaction() as conn:
            if not self.accepting:
                raise ExecutionError("EXECUTION_SHUTTING_DOWN")
            now = self.store.clock(conn)
            self._admission(conn, sid, identity, grant, request.epoch, now, budget=False)
            # Idempotency is checked before capacity; retransmission never adds a
            # second slot or extends its first acceptance deadline.
            old = self.operations.lookup(sid, identity, request)
            if old is not None:
                old = self.operations.register(sid, identity, request, kind, inputs, dispatch=True)
                job = conn.execute("SELECT * FROM execution_dispatch WHERE operation_id=?", (old["id"],)).fetchone()
                if job is None:
                    raise ExecutionError("EXECUTION_INPUT_CONFLICT")
                self._grant(conn, sid, identity, job["grant_id"], now)
                return self._receipt(old, job, now)
            self._admission(conn, sid, identity, grant, request.epoch, now)
            if conn.execute("SELECT 1 FROM execution_operations WHERE session=? AND epoch=? AND status IN ('REGISTERED','RUNNING','UNKNOWN') LIMIT 1",
                            (session_key(sid), request.epoch)).fetchone():
                raise ExecutionError("EXECUTION_BUSY")
            self._settle(conn, now)
            active = conn.execute("SELECT count(*) FROM execution_dispatch WHERE status IN ('WAITING','CLAIMED')").fetchone()[0]
            if active >= self.policy.max_concurrent + self.policy.max_queued:
                raise ExecutionError("EXECUTION_QUEUE_FULL")
            if conn.execute("SELECT count(*) FROM execution_dispatch").fetchone()[0] >= self.policy.max_records:
                raise ExecutionError("EXECUTION_CAPACITY")
            used = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM execution_dispatch_inputs").fetchone()[0]
            if used + size > self.policy.max_total_input_bytes:
                raise ExecutionError("EXECUTION_CAPACITY")
            # Account for database/WAL and a disposable materialization copy.
            self.store.storage_capacity(extra=3 * size + 8192)
            operation = self.operations.register(sid, identity, request, kind, inputs, dispatch=True)
            conn.execute("INSERT INTO execution_dispatch (operation_id,grant_id,status,accepted,deadline,updated) VALUES (?,?,'WAITING',?,?,?)",
                         (operation["id"], grant, now, now + self.policy.queue_seconds, now))
            conn.execute("INSERT INTO execution_dispatch_inputs VALUES (?,?,?,?,?,?,?,?)",
                         (operation["id"], sid, identity, payload, hashlib.sha256(payload.encode()).hexdigest(), image, image_hash, size))
            job = conn.execute("SELECT * FROM execution_dispatch WHERE operation_id=?", (operation["id"],)).fetchone()
            receipt = self._receipt(operation, job, now)
        return receipt  # only after the outer durable COMMIT

    @staticmethod
    def _receipt(operation, job, now):
        status = operation["status"]
        if status == "RUNNING" and operation["lease_until"] <= now:
            status = "UNKNOWN"
        return {"operation_id": operation["id"], "kind": operation["kind"], "status": status,
                "dispatch_status": job["status"], "accepted_at": job["accepted"], "queue_deadline": job["deadline"],
                "error_code": job["error_code"], "progress_version": job["progress_version"], "progress_stage": job["progress_stage"]}

    def observe(self, sid, identity, grant, request, *, result=False):
        """Authorized read, no claim, session creation/renewal or delivery side effect."""
        request = OperationRequest.parse(request)
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            self._admission(conn, sid, identity, grant, request.epoch, now, budget=False)
            operation = self.operations.lookup(sid, identity, request)
            if operation is None:
                return None
            job = conn.execute("SELECT * FROM execution_dispatch WHERE operation_id=?", (operation["id"],)).fetchone()
            if job is None:
                return None
            self._grant(conn, sid, identity, job["grant_id"], now)
            receipt = self._receipt(operation, job, now)
            if result and operation["status"] == "SUCCEEDED":
                # Private business receipt only; stable public publication is 6.3.
                from tiku_agent.execution_runtime import decode_response
                receipt["result"] = decode_response(json.loads(operation["result"]))
            return receipt

    def _revoke(self, conn, operation_id, now, code):
        conn.execute("UPDATE execution_dispatch SET status='REVOKED',error_code=?,updated=?,progress_version=progress_version+1,progress_stage='failed' WHERE operation_id=? AND status='WAITING'",
                     (code, now, operation_id))
        conn.execute("UPDATE execution_operations SET status='FAILED',updated=? WHERE id=? AND status='REGISTERED'", (now, operation_id))

    def _settle(self, conn, now):
        for row in conn.execute("SELECT d.*,o.status AS op_status,o.lease_until FROM execution_dispatch d JOIN execution_operations o ON o.id=d.operation_id WHERE d.status IN ('WAITING','CLAIMED')").fetchall():
            if row["op_status"] == "RUNNING" and row["lease_until"] <= now:
                self.operations._unknown(conn, row["operation_id"], now)
                conn.execute("UPDATE execution_dispatch SET status='SETTLED',error_code='EXECUTION_UNKNOWN',progress_stage='needs_review',progress_version=progress_version+1,updated=? WHERE operation_id=?", (now, row["operation_id"]))
            elif row["status"] == "CLAIMED" and row["op_status"] == "REGISTERED":
                # fail(known_not_started=True) committed, then the worker died
                # before settling its dispatch. Its proof allows failure, never
                # a second implicit queue entry for the same accepted command.
                conn.execute("UPDATE execution_operations SET status='FAILED',updated=? WHERE id=?", (now, row["operation_id"]))
                conn.execute("UPDATE execution_dispatch SET status='SETTLED',error_code='EXECUTION_WORKER_FAILED',progress_stage='failed',progress_version=progress_version+1,updated=? WHERE operation_id=?", (now, row["operation_id"]))
            elif row["op_status"] not in {"REGISTERED", "RUNNING"}:
                conn.execute("UPDATE execution_dispatch SET status='SETTLED',progress_stage=?,progress_version=progress_version+1,updated=? WHERE operation_id=?", (terminal_stage(row["op_status"]), now, row["operation_id"]))
            elif row["status"] == "WAITING" and row["deadline"] <= now:
                self._revoke(conn, row["operation_id"], now, "EXECUTION_QUEUE_TIMEOUT")

    def _input(self, conn, operation):
        row = conn.execute("SELECT * FROM execution_dispatch_inputs WHERE operation_id=?", (operation["id"],)).fetchone()
        if row is None or hashlib.sha256(row["payload"].encode()).hexdigest() != row["payload_hash"]:
            raise ExecutionError("EXECUTION_INPUT_UNAVAILABLE")
        try:
            parsed = json.loads(row["payload"])
            parameters = dict(parsed["parameters"])
            parameters.pop("image_path", None)
            payload, inputs, image_hash = freeze_input(self.runtime, operation["kind"], parameters, row["image"], self.policy)
            if (payload != row["payload"] or image_hash != row["image_hash"]
                    or digest({"kind": operation["kind"], "input": inputs, "state_version": operation["expected_version"]}) != operation["fingerprint"]
                    or session_key(row["session_id"]) != operation["session"] or digest(row["identity_key"]) != operation["identity"]):
                raise ValueError("private input mismatch")
        except Exception:
            raise ExecutionError("EXECUTION_INPUT_UNAVAILABLE") from None
        return dict(row), parsed

    def claim_next(self):
        with self.store.transaction() as conn:
            if not self.accepting:
                return None
            now = self.store.clock(conn)
            self._settle(conn, now)
            running = conn.execute("SELECT count(*) FROM execution_operations WHERE status='RUNNING' AND kind NOT IN ('clear','control_execution')").fetchone()[0]
            if running >= self.policy.max_concurrent:
                return None
            queued = conn.execute("SELECT o.*,d.grant_id,d.deadline FROM execution_dispatch d JOIN execution_operations o ON o.id=d.operation_id WHERE d.status='WAITING' ORDER BY d.accepted,d.rowid").fetchall()
            occupied = {self.runtime._lock(row[0]) for row in conn.execute(
                "SELECT i.session_id FROM execution_dispatch_inputs i JOIN execution_operations o ON o.id=i.operation_id WHERE o.status='RUNNING'")}
            for operation in queued:
                try:
                    private, parsed = self._input(conn, operation)
                    if self.runtime._lock(private["session_id"]) in occupied:
                        continue  # avoid a second wait inside the striped runtime gate
                    self._admission(conn, private["session_id"], private["identity_key"], operation["grant_id"], operation["epoch"], now)
                    now = self.store.clock(conn)
                    if operation["deadline"] <= now:
                        self._revoke(conn, operation["id"], now, "EXECUTION_QUEUE_TIMEOUT")
                        continue
                    writer = self.operations.claim(operation["id"], dispatch=True)
                except Exception as exc:
                    self._revoke(conn, operation["id"], now, safe_error(exc))
                    continue
                conn.execute("UPDATE execution_dispatch SET status='CLAIMED',updated=?,progress_version=progress_version+1,progress_stage='running' WHERE operation_id=?", (now, operation["id"]))
                return writer, dict(operation), private, parsed
        return None

    def check_running(self, writer, private, grant):
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            writer.validate(conn, self.store, writer.session, writer.epoch, now)
            self._admission(conn, private["session_id"], private["identity_key"], grant, writer.epoch, now)
            writer.validate(conn, self.store, writer.session, writer.epoch, self.store.clock(conn))

    def maintain(self):
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            self._settle(conn, now)
            # UNKNOWN payloads and every live input remain protected. Saturation
            # rejects new work instead of deleting unresolved evidence.
            removed = conn.execute("DELETE FROM execution_dispatch_inputs WHERE operation_id IN (SELECT d.operation_id FROM execution_dispatch d JOIN execution_operations o ON o.id=d.operation_id WHERE d.status IN ('SETTLED','REVOKED') AND o.status IN ('SUCCEEDED','FAILED','CANCELLED') AND d.updated<? LIMIT 100)", (now - self.policy.input_ttl,)).rowcount
            conn.execute("DELETE FROM execution_dispatch_grants WHERE id IN (SELECT id FROM execution_dispatch_grants WHERE (expires<=? OR revoked=1) AND NOT EXISTS (SELECT 1 FROM execution_dispatch d WHERE d.grant_id=execution_dispatch_grants.id) LIMIT 100)", (now,))
            return {"inputs_removed": removed}


def safe_error(exc):
    code = getattr(exc, "code", "")
    if code in {"EXECUTION_STALE", "EXECUTION_BUSY", "EXECUTION_UNKNOWN", "EXECUTION_COST_PENDING",
                "EXECUTION_CAPACITY", "EXECUTION_INPUT_UNAVAILABLE", "EXECUTION_AUTH_REQUIRED",
                "EXECUTION_AUTH_UNAVAILABLE", "EXECUTION_RESULT_UNAVAILABLE", "EXECUTION_LEASE_LOST",
                "GLOBAL_DAILY_QUOTA_EXCEEDED", "INVITE_DAILY_QUOTA_EXCEEDED", "INVITE_IDENTITY_MISSING"}:
        return code
    return "EXECUTION_WORKER_FAILED"


def terminal_stage(status):
    return {"SUCCEEDED": "completed", "UNKNOWN": "needs_review", "CANCELLED": "stopped"}.get(status, "failed")

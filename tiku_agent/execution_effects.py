"""Durable model boundaries and a cost outbox, independent of diagnostics."""
from __future__ import annotations

import json
from pathlib import Path

from tiku_agent.execution_store import ExecutionError, canonical, digest


def create_effect_schema(conn):
    for statement in """
        CREATE TABLE IF NOT EXISTS execution_effects (
            call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES execution_cost_runs(run_id),
            operation_id TEXT NOT NULL REFERENCES execution_operations(id),
            attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
            provider TEXT NOT NULL, model TEXT NOT NULL, call_type TEXT NOT NULL,
            status TEXT NOT NULL, record TEXT, created REAL NOT NULL, updated REAL NOT NULL,
            usage_known INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS execution_effects_operation ON execution_effects(operation_id,status);
        CREATE TABLE IF NOT EXISTS execution_cost_outbox (
            run_id TEXT PRIMARY KEY REFERENCES execution_cost_runs(run_id),
            ledger_key TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_collectors (
            run_id TEXT PRIMARY KEY REFERENCES execution_cost_runs(run_id),
            metadata TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
    """.split(";"):
        if statement.strip():
            conn.execute(statement)


def collector_metadata(collector):
    return {name: getattr(collector, name) for name in
            ("run_id", "session_key", "identity_key", "search_key", "task_kind", "started_at", "trace_id")}


class ExecutionEffects:
    def __init__(self, operations, writer, *, ledger_paths=()):
        self.operations = operations
        self.store = operations.authority
        self.writer = writer
        self.ledger_keys = {digest(str(Path(path).resolve()).casefold()) for path in ledger_paths}

    def _validate(self, conn):
        now = self.store.clock(conn)
        self.writer.validate(conn, self.store, self.writer.session, self.writer.epoch, now)
        return now

    def collector_started(self, collector):
        encoded = canonical({"collector": collector_metadata(collector), "ledger_keys": sorted(self.ledger_keys)})
        with self.store.transaction() as conn:
            self._validate(conn)
            conn.execute("INSERT INTO execution_collectors (run_id,metadata) VALUES (?,?) "
                         "ON CONFLICT(run_id) DO UPDATE SET metadata=excluded.metadata", (collector.run_id, encoded))

    def collector_closed(self, collector):
        # This is evidence only; a late collector cannot authorize a state write.
        with self.store.transaction() as conn:
            conn.execute("UPDATE execution_collectors SET closed=1 WHERE run_id=?", (collector.run_id,))

    def prepare_model(self, *, call_id, run_id, provider, model, call_type):
        with self.store.transaction() as conn:
            now = self._validate(conn)
            if conn.execute("SELECT 1 FROM execution_effects WHERE operation_id=? AND status='UNKNOWN' LIMIT 1",
                            (self.writer.operation_id,)).fetchone():
                raise ExecutionError("EXECUTION_UNKNOWN")
            link = conn.execute("SELECT operation_id,attempt_id FROM execution_cost_runs WHERE run_id=?", (run_id,)).fetchone()
            if link is None or tuple(link) != (self.writer.operation_id, self.writer.attempt_id):
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
            self.store.capacity(conn, "execution_effects", self.store.policy.max_operations * 20)
            self.store.storage_capacity(extra=8192)
            conn.execute("INSERT INTO execution_effects (call_id,run_id,operation_id,attempt_id,provider,model,call_type,status,record,created,updated) VALUES (?,?,?,?,?,?,?,'PREPARED',NULL,?,?)",
                         (call_id, run_id, self.writer.operation_id, self.writer.attempt_id,
                          provider, model, call_type, now, now))

    def model_sent(self, call_id):
        with self.store.transaction() as conn:
            now = self._validate(conn)
            changed = conn.execute("UPDATE execution_effects SET status='SENT',updated=? "
                                   "WHERE call_id=? AND attempt_id=? AND status='PREPARED'",
                                   (now, call_id, self.writer.attempt_id)).rowcount
            if changed != 1:
                raise ExecutionError("EXECUTION_UNKNOWN")

    def model_finished(self, call_id, record, *, confirmed, usage_known=False):
        # A late provider response may add evidence to its original call only.
        # It never changes operation ownership or applies a business result.
        encoded = canonical(record.to_dict()) if record is not None else None
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            row = conn.execute("SELECT * FROM execution_effects WHERE call_id=? AND attempt_id=?",
                               (call_id, self.writer.attempt_id)).fetchone()
            if row is None:
                raise ExecutionError("EXECUTION_UNKNOWN")
            status = "CONFIRMED" if confirmed else "UNKNOWN"
            if row["status"] == "CONFIRMED":
                if row["record"] != encoded:
                    raise ExecutionError("EXECUTION_COST_CONFLICT")
                return
            conn.execute("UPDATE execution_effects SET status=?,record=?,updated=?,usage_known=? WHERE call_id=?",
                         (status, encoded, now, int(usage_known), call_id))

    def prepare_cost(self, ledger_path, collector, *, finished_at, outcome):
        payload = {"collector": collector_metadata(collector),
                   "records": [record.to_dict() for record in collector.records()],
                   "finished_at": finished_at, "outcome": outcome}
        encoded = canonical(payload)
        if len(encoded.encode()) > self.store.policy.max_result_bytes:
            raise ExecutionError("EXECUTION_CAPACITY")
        fingerprint = digest(payload)
        ledger_key = digest(str(Path(ledger_path).resolve()).casefold())
        if ledger_key not in self.ledger_keys:
            raise ExecutionError("EXECUTION_COST_TARGET_INVALID")
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            effects = conn.execute("SELECT call_id,status,record,usage_known FROM execution_effects WHERE run_id=?", (collector.run_id,)).fetchall()
            expected = {record["call_id"]: canonical(record) for record in payload["records"]}
            if (any(effect["status"] != "CONFIRMED" or not effect["usage_known"] for effect in effects)
                    or {effect["call_id"]: effect["record"] for effect in effects} != expected):
                raise ExecutionError("EXECUTION_COST_PENDING")
            row = conn.execute("SELECT fingerprint,ledger_key FROM execution_cost_outbox WHERE run_id=?", (collector.run_id,)).fetchone()
            if row:
                if tuple(row) != (fingerprint, ledger_key):
                    raise ExecutionError("EXECUTION_COST_CONFLICT")
                return
            link = conn.execute("SELECT operation_id,attempt_id FROM execution_cost_runs WHERE run_id=?", (collector.run_id,)).fetchone()
            if link is None or tuple(link) != (self.writer.operation_id, self.writer.attempt_id):
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
            self.store.storage_capacity(extra=2*len(encoded.encode()))
            conn.execute("INSERT INTO execution_cost_outbox VALUES (?,?,?,?, 'PENDING',?)",
                         (collector.run_id, ledger_key, encoded, fingerprint, now))

    def cost_confirmed(self, run_id):
        with self.store.transaction() as conn:
            conn.execute("UPDATE execution_cost_outbox SET status='CONFIRMED',updated=? WHERE run_id=?",
                         (self.store.clock(conn), run_id))


def reconcile_cost(operations, ledger, run_id):
    """Reissue only an exact local ledger write; never invoke a model or tool."""
    from tiku_shared.execution_hooks import execution_effect_scope
    from tiku_shared.model_costs import ModelCallRecord, ModelCostCollector, ModelCostConflict
    store = operations.authority
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM execution_cost_outbox WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["ledger_key"] != digest(str(Path(ledger.path).resolve()).casefold()):
            raise ExecutionError("EXECUTION_COST_TARGET_INVALID")
        payload = json.loads(row["payload"])
        if digest(payload) != row["fingerprint"]:
            raise ExecutionError("EXECUTION_COST_CONFLICT")
    collector = ModelCostCollector(**payload["collector"])
    collector._records = [ModelCallRecord(**record) for record in payload["records"]]
    try:
        with execution_effect_scope(None):
            ledger.write_run(collector, finished_at=payload["finished_at"], outcome=payload["outcome"], idempotent=True)
    except ModelCostConflict:
        with store.transaction() as conn:
            conn.execute("UPDATE execution_cost_outbox SET status='CONFLICT',updated=? WHERE run_id=?", (store.clock(conn), run_id))
        raise ExecutionError("EXECUTION_COST_CONFLICT") from None
    with store.transaction() as conn:
        conn.execute("UPDATE execution_cost_outbox SET status='CONFIRMED',updated=? WHERE run_id=? AND fingerprint=?",
                     (store.clock(conn), run_id, row["fingerprint"]))


def stage_unwritten_cost(operations, ledger, run_id):
    """Recover a stopped collector from confirmed call evidence, never guesses usage.

    Used by explicit reconciliation after a crash before the outbox write. A
    live collector, an unconfirmed call, or missing usage remains unresolved.
    """
    store = operations.authority
    with store.transaction() as conn:
        now = store.clock(conn)
        existing = conn.execute("SELECT fingerprint FROM execution_cost_outbox WHERE run_id=?", (run_id,)).fetchone()
        if existing:
            return existing[0]
        metadata = conn.execute("SELECT metadata FROM execution_collectors WHERE run_id=?", (run_id,)).fetchone()
        operation = conn.execute("SELECT o.* FROM execution_operations o JOIN execution_cost_runs r ON r.operation_id=o.id WHERE r.run_id=?", (run_id,)).fetchone()
        if metadata is None or operation is None:
            raise ExecutionError("EXECUTION_COST_PENDING")
        if operation["status"] == "RUNNING" and operation["lease_until"] > now:
            raise ExecutionError("EXECUTION_BUSY")
        effects = conn.execute("SELECT * FROM execution_effects WHERE run_id=? ORDER BY created,call_id", (run_id,)).fetchall()
        if any(row["status"] != "CONFIRMED" or not row["usage_known"] or not row["record"] for row in effects):
            raise ExecutionError("EXECUTION_COST_PENDING")
        if operation["status"] == "RUNNING":
            operations._unknown(conn, operation["id"], now)
        records = sorted([json.loads(row["record"]) for row in effects], key=lambda record: record["sequence"])
        saved_metadata = json.loads(metadata[0])
        ledger_key = digest(str(Path(ledger.path).resolve()).casefold())
        if ledger_key not in saved_metadata["ledger_keys"]:
            raise ExecutionError("EXECUTION_COST_TARGET_INVALID")
        collector = saved_metadata["collector"]
        payload = {"collector": collector, "records": records,
                   "finished_at": max([collector["started_at"], *[record["finished_at"] for record in records]]),
                   "outcome": "interrupted"}
        encoded = canonical(payload)
        if len(encoded.encode()) > store.policy.max_result_bytes:
            raise ExecutionError("EXECUTION_CAPACITY")
        store.storage_capacity(extra=2*len(encoded.encode()))
        fingerprint = digest(payload)
        conn.execute("INSERT INTO execution_cost_outbox VALUES (?,?,?,?, 'PENDING',?)",
                     (run_id, ledger_key, encoded, fingerprint, now))
        return fingerprint

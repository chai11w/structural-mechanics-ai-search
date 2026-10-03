"""Durable model boundaries and a cost outbox, independent of diagnostics."""
from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path
from uuid import uuid4

from tiku_agent.execution_store import ExecutionError, canonical, digest


MODEL_FALLBACK_CALL_TYPES = {
    "rerank_coarse": {"qwen_shape_rerank", "zhipu_shape_rerank"},
    "length_existing_order": {"qwen_length_tie_break", "zhipu_length_tie_break"},
    "crop_manual": {"glm_a3_page_auto_crop", "qwen_a3_crop_compare", "external_load_screen"},
    "crop_draft": {"qwen_a3_crop_compare", "external_load_screen"},
    "page_retry": {"qwen_a3_page_understanding"},
    "intent_clarify": {"qwen_intent_decision", "qwen_a3_intent_decision"},
    "fixed_reply": {"qwen_safe_answer", "qwen_image_triage_reply"},
    "structure_filter_skip": {"qwen_structure_type"},
    "single_question_route": {"qwen_image_scope"},
}


def valid_model_fallback(effect, attempt_id, kind):
    """An observed model error handled by a defined business fallback."""
    if (effect["attempt_id"] != attempt_id or effect["status"] != "UNKNOWN"
            or effect["call_type"] not in MODEL_FALLBACK_CALL_TYPES.get(kind, ()) or not effect["record"]):
        return False
    record = json.loads(effect["record"])
    return record.get("status") == "error" and record.get("call_id") == effect["call_id"]


def confirmed_model_recoveries(conn, operation_id, attempt_id):
    """Business recovery proof never changes the first provider's UNKNOWN state."""
    result = {}
    for link in conn.execute(
            "SELECT source_call_id,target_call_id FROM execution_model_recoveries "
            "WHERE operation_id=? AND attempt_id=? AND target_call_id IS NOT NULL",
            (operation_id, attempt_id)):
        source = conn.execute("SELECT * FROM execution_effects WHERE call_id=?", (link[0],)).fetchone()
        target = conn.execute("SELECT * FROM execution_effects WHERE call_id=?", (link[1],)).fetchone()
        if source is None or target is None:
            continue
        fields = ("operation_id", "attempt_id", "provider", "model", "call_type")
        if (any(source[name] != target[name] for name in fields)
                or source["attempt_id"] != attempt_id or source["operation_id"] != operation_id
                or source["status"] != "UNKNOWN" or target["status"] != "CONFIRMED"
                or not source["record"] or not target["record"]):
            continue
        original, replacement = json.loads(source["record"]), json.loads(target["record"])
        if (original.get("status") == "error" and original.get("call_id") == source["call_id"]
                and replacement.get("status") == "success" and replacement.get("call_id") == target["call_id"]):
            result[source["call_id"]] = target["call_id"]
    return result


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
        CREATE TABLE IF NOT EXISTS execution_model_recoveries (
            source_call_id TEXT PRIMARY KEY REFERENCES execution_effects(call_id) ON DELETE CASCADE,
            target_call_id TEXT UNIQUE,
            operation_id TEXT NOT NULL REFERENCES execution_operations(id),
            attempt_id TEXT NOT NULL REFERENCES execution_attempts(id));
        CREATE INDEX IF NOT EXISTS execution_model_recoveries_operation
            ON execution_model_recoveries(operation_id,attempt_id);
        CREATE TABLE IF NOT EXISTS execution_cost_outbox (
            run_id TEXT PRIMARY KEY REFERENCES execution_cost_runs(run_id),
            ledger_key TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_collectors (
            run_id TEXT PRIMARY KEY REFERENCES execution_cost_runs(run_id),
            metadata TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS execution_files (
            id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES execution_operations(id),
            attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
            path TEXT NOT NULL UNIQUE, temporary_path TEXT NOT NULL, digest TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, updated REAL NOT NULL);
    """.split(";"):
        if statement.strip():
            conn.execute(statement)

    if 'ledger_key' not in {row[1] for row in conn.execute('PRAGMA table_info(execution_collectors)')}:
        conn.execute("ALTER TABLE execution_collectors ADD COLUMN ledger_key TEXT NOT NULL DEFAULT ''")


def collector_metadata(collector):
    return {name: getattr(collector, name) for name in
            ("run_id", "session_key", "identity_key", "search_key", "task_kind", "started_at", "trace_id")}


class ExecutionEffects:
    def __init__(self, operations, writer, *, ledger_paths=(), artifact_roots=(), admission_check=None):
        self.operations = operations
        self.store = operations.authority
        self.writer = writer
        self.ledger_keys = {digest(str(Path(path).resolve()).casefold()) for path in ledger_paths}
        self.artifact_roots = tuple(Path(path).resolve() for path in artifact_roots)
        self.admission_check = admission_check
        self._model_fallbacks = {}
        self._fallback_lock = threading.Lock()
        self._recovery_sources = set()
        paths = {Path(path).resolve() for path in ledger_paths}
        self._recovery_ledger = next(iter(paths)) if len(paths) == 1 else None

    def model_recovery_enabled(self):
        return self.store.policy.model_transport_recovery and self._recovery_ledger is not None

    def model_recovery_pending(self, call_id):
        with self._fallback_lock:
            return call_id in self._recovery_sources

    def write_recovery_cost(self, collector, *, finished_at, outcome):
        from tiku_shared.model_costs import SQLiteModelCostLedger
        try:
            SQLiteModelCostLedger(self._recovery_ledger).write_run(
                collector, finished_at=finished_at, outcome=outcome)
        except Exception as exc:
            # The independent receipt/outbox retains exact successful usage.
            # Accounting recovery may write fees, never repeat a provider call.
            self.record_failure("", "recovery_cost_ledger", exc)

    def model_fallbacks(self):
        with self._fallback_lock:
            return dict(self._model_fallbacks)

    def accept_model_fallback(self, call_id, kind):
        # The acknowledgment lives only in this writer. A crash does not invent
        # a completed business result; finish persists it together with the reply.
        with self.store.transaction() as conn:
            self._validate(conn)
            call_ids = [call_id]
            link = conn.execute("SELECT source_call_id FROM execution_model_recoveries "
                                "WHERE target_call_id=? AND operation_id=? AND attempt_id=?",
                                (call_id, self.writer.operation_id, self.writer.attempt_id)).fetchone()
            if link is not None:
                call_ids.append(link[0])
            for current in call_ids:
                effect = conn.execute("SELECT * FROM execution_effects WHERE call_id=? AND operation_id=?",
                                      (current, self.writer.operation_id)).fetchone()
                if effect is None or not valid_model_fallback(effect, self.writer.attempt_id, kind):
                    raise ExecutionError("EXECUTION_UNKNOWN")
            with self._fallback_lock:
                self._model_fallbacks.update({current: kind for current in call_ids})

    def prepare_file(self, path, temporary):
        if not any(path.is_relative_to(root) and temporary.is_relative_to(root) for root in self.artifact_roots):
            raise ExecutionError("EXECUTION_ARTIFACT_INVALID")
        with self.store.transaction() as conn:
            now = self._validate(conn)
            self.store.storage_capacity(extra=4096)
            file_id = uuid4().hex
            conn.execute("INSERT INTO execution_files (id,operation_id,attempt_id,path,temporary_path,status,updated) VALUES (?,?,?,?,?,'PREPARED',?)",
                         (file_id, self.writer.operation_id, self.writer.attempt_id, str(path), str(temporary), now))
        return file_id

    def file_ready(self, file_id, temporary):
        hasher = hashlib.sha256()
        with temporary.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024*1024), b""):
                hasher.update(chunk)
        with self.store.transaction() as conn:
            now = self._validate(conn)
            count = conn.execute("UPDATE execution_files SET digest=?,status='READY',updated=? WHERE id=? AND attempt_id=? AND status='PREPARED'",
                                 (hasher.hexdigest(), now, file_id, self.writer.attempt_id)).rowcount
            if count != 1:
                raise ExecutionError("EXECUTION_ARTIFACT_INVALID")

    def file_published(self, file_id):
        with self.store.transaction() as conn:
            count = conn.execute("UPDATE execution_files SET status='PUBLISHED',updated=? WHERE id=? AND attempt_id=? AND status='READY'",
                                 (self.store.clock(conn), file_id, self.writer.attempt_id)).rowcount
            if count != 1:
                raise ExecutionError("EXECUTION_ARTIFACT_INVALID")

    def file_aborted(self, file_id):
        """Record proven local non-publication after temporary-file cleanup."""
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM execution_files WHERE id=? AND attempt_id=?",
                               (file_id, self.writer.attempt_id)).fetchone()
            if row is None or row["status"] not in {"PREPARED", "READY"}:
                return
            if Path(row["path"]).exists() or Path(row["temporary_path"]).exists():
                return  # A possibly published artifact still needs its receipt.
            conn.execute("UPDATE execution_files SET status='ABORTED',updated=? WHERE id=?",
                         (self.store.clock(conn), file_id))

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
            conn.execute("UPDATE execution_collectors SET closed=max(closed,1) WHERE run_id=?", (collector.run_id,))

    def cost_write_finished(self, run_id, ledger_path=None):
        # 0 = collecting, 1 = model scope closed, 2 = ledger attempt ended.
        # Outbox PENDING is normal during the ledger transaction. Only after
        # this marker (or writer loss) does it prove unresolved accounting.
        with self.store.transaction() as conn:
            key = digest(str(Path(ledger_path).resolve()).casefold()) if ledger_path is not None else ''
            if key and key not in self.ledger_keys:
                raise ExecutionError("EXECUTION_COST_TARGET_INVALID")
            conn.execute("UPDATE execution_collectors SET closed=2,ledger_key=? WHERE run_id=?", (key, run_id))

    def prepare_model(self, *, call_id, run_id, provider, model, call_type, recovery_of=None):
        with self.store.transaction() as conn:
            now = self._validate(conn)
            if self.admission_check is not None:
                self.admission_check()
            handled = set(self.model_fallbacks())
            with self._fallback_lock:
                handled.update(self._recovery_sources)
                live_sources = set(self._recovery_sources)
            # A sibling may still be returning its second failure to the crop
            # fallback handler. Do not block independent in-flight work during
            # that interval; finish still requires explicit proof for BOTH calls.
            handled.update(row[1] for row in conn.execute(
                "SELECT source_call_id,target_call_id FROM execution_model_recoveries "
                "WHERE operation_id=? AND attempt_id=? AND target_call_id IS NOT NULL",
                (self.writer.operation_id, self.writer.attempt_id)) if row[0] in live_sources)
            if any(row[0] not in handled for row in conn.execute(
                    "SELECT call_id FROM execution_effects WHERE operation_id=? AND status='UNKNOWN'",
                    (self.writer.operation_id,))):
                raise ExecutionError("EXECUTION_UNKNOWN")
            if recovery_of is not None:
                source = conn.execute("SELECT * FROM execution_effects WHERE call_id=?", (recovery_of,)).fetchone()
                link = conn.execute("SELECT * FROM execution_model_recoveries WHERE source_call_id=?",
                                    (recovery_of,)).fetchone()
                if (not self.model_recovery_enabled() or not self.model_recovery_pending(recovery_of)
                        or source is None or link is None or link["target_call_id"] is not None
                        or source["status"] != "UNKNOWN"
                        or run_id == source["run_id"]
                        or (source["operation_id"], source["attempt_id"], source["provider"], source["model"], source["call_type"])
                        != (self.writer.operation_id, self.writer.attempt_id, provider, model, call_type)):
                    raise ExecutionError("EXECUTION_UNKNOWN")
            self.operations.ensure_cost_available(identity_digest=conn.execute(
                "SELECT identity FROM execution_operations WHERE id=?", (self.writer.operation_id,)).fetchone()[0])
            link = conn.execute("SELECT operation_id,attempt_id FROM execution_cost_runs WHERE run_id=?", (run_id,)).fetchone()
            if link is None or tuple(link) != (self.writer.operation_id, self.writer.attempt_id):
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
            self.store.capacity(conn, "execution_effects", self.store.policy.max_operations * 20)
            self.store.storage_capacity(extra=8192)
            conn.execute("INSERT INTO execution_effects (call_id,run_id,operation_id,attempt_id,provider,model,call_type,status,record,created,updated) VALUES (?,?,?,?,?,?,?,'PREPARED',NULL,?,?)",
                         (call_id, run_id, self.writer.operation_id, self.writer.attempt_id,
                          provider, model, call_type, now, now))
            if recovery_of is not None:
                conn.execute("UPDATE execution_model_recoveries SET target_call_id=? WHERE source_call_id=?",
                             (call_id, recovery_of))

    def model_sent(self, call_id):
        with self.store.transaction() as conn:
            now = self._validate(conn)
            if self.admission_check is not None:
                self.admission_check()
            self.operations.ensure_cost_available(identity_digest=conn.execute(
                "SELECT identity FROM execution_operations WHERE id=?", (self.writer.operation_id,)).fetchone()[0])
            changed = conn.execute("UPDATE execution_effects SET status='SENT',updated=? "
                                   "WHERE call_id=? AND attempt_id=? AND status='PREPARED'",
                                   (now, call_id, self.writer.attempt_id)).rowcount
            if changed != 1:
                raise ExecutionError("EXECUTION_UNKNOWN")

    def record_failure(self, call_id, stage, exc):
        from tiku_agent.execution_receipts import ReceiptJournal
        ReceiptJournal(self.store).failure(self.writer.operation_id, call_id, stage, exc)

    def model_finished(self, call_id, record, *, confirmed, usage_known=False, recovery_requested=False):
        from tiku_agent.execution_receipts import ReceiptJournal, apply_receipt
        journal = ReceiptJournal(self.store)
        try:
            journal.save(call_id, self.writer.attempt_id, record, confirmed, usage_known)
        except Exception as exc:
            self.record_failure(call_id, 'receipt_journal', exc)
            if isinstance(exc, ExecutionError) and exc.code == 'EXECUTION_COST_CONFLICT':
                raise
            # Primary persistence is still worth attempting if the journal disk
            # is unavailable. Never issue another provider call here.
        try:
            def authorize_recovery(conn):
                self._validate(conn)
                if (not self.model_recovery_enabled() or confirmed or record is None or record.status != "error"
                        or conn.execute("SELECT 1 FROM execution_model_recoveries WHERE target_call_id=?",
                                        (call_id,)).fetchone()):
                    raise ExecutionError("EXECUTION_UNKNOWN")
                self.store.storage_capacity(extra=1024)
                conn.execute("INSERT INTO execution_model_recoveries VALUES (?,NULL,?,?)",
                             (call_id, self.writer.operation_id, self.writer.attempt_id))
                # Published while holding the same transaction that publishes
                # UNKNOWN: parallel siblings cannot see an unhandled gap.
                with self._fallback_lock:
                    self._recovery_sources.add(call_id)
            apply_receipt(self.store, call_id, self.writer.attempt_id, {
                'record': record.to_dict() if record is not None else None,
                'confirmed': confirmed, 'usage_known': usage_known},
                after_apply=authorize_recovery if recovery_requested else None)
        except Exception as exc:
            self.record_failure(call_id, 'receipt_persistence', exc)
            raise
        try:
            journal.remove(call_id, self.writer.attempt_id)
        except Exception as exc:
            self.record_failure(call_id, 'receipt_cleanup', exc)

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


def reconcilable_effect(effect):
    """PREPARED is proof of no send only after the owning writer is fenced."""
    if effect["status"] in {"PREPARED", "NOT_SENT"}:
        return effect["record"] is None and not effect["usage_known"]
    return effect["status"] == "CONFIRMED" and effect["usage_known"] and bool(effect["record"])


def stage_unwritten_cost(operations, ledger, run_id):
    """Recover a stopped collector from confirmed call evidence, never guesses usage.

    Used by explicit reconciliation after a crash before the outbox write. A
    live writer, unknown sent call, or missing response usage stays unresolved.
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
        if any(not reconcilable_effect(row) for row in effects):
            raise ExecutionError("EXECUTION_COST_PENDING")
        if operation["status"] == "RUNNING":
            operations._unknown(conn, operation["id"], now)
        # The operation is no longer a valid writer. model_sent requires that
        # writer and a PREPARED row, so these calls can never start afterwards.
        # Keep the original call identity without inventing a zero-usage reply.
        conn.execute("UPDATE execution_effects SET status='NOT_SENT',updated=? WHERE run_id=? AND status='PREPARED'",
                     (now, run_id))
        records = sorted([json.loads(row["record"]) for row in effects if row["status"] == "CONFIRMED"], key=lambda record: record["sequence"])
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

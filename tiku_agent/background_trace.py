"""One background terminal attempt per operation, separate from HTTP traces."""
from tiku_shared.trace_context import TraceContext, trace_context_scope
from tiku_shared.trace_events import trace_event_scope, record_public_terminal


class BackgroundTrace:
    def __init__(self, dispatch, recorder):
        self.dispatch, self.store, self.recorder = dispatch, dispatch.store, recorder
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS execution_background_trace (operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id) ON DELETE CASCADE,status TEXT NOT NULL)")

    def complete(self, operation_id):
        if self.recorder is None:
            return
        with self.store.transaction() as conn:
            row = conn.execute("SELECT o.*,i.identity_key FROM execution_operations o JOIN execution_dispatch d ON d.operation_id=o.id LEFT JOIN execution_dispatch_inputs i ON i.operation_id=o.id WHERE o.id=?", (operation_id,)).fetchone()
            if row is None or row["status"] not in {"SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED"}:
                return
            changed = conn.execute("INSERT OR IGNORE INTO execution_background_trace VALUES (?,?)", (operation_id, row["status"])).rowcount
            if not changed:
                return
            status, sid, identity = row["status"], row["session"], row["identity_key"]
        context = TraceContext("trace_" + operation_id, "req_" + operation_id)
        with trace_context_scope(context), trace_event_scope(self.recorder, trace_id=context.trace_id,
                request_id=context.request_id, session_key=sid, identity_key=identity or ""):
            record_public_terminal(stage="background_execution", outcome="success" if status == "SUCCEEDED" else "error", failed=status != "SUCCEEDED")

    def maintain(self):
        if self.recorder is None:
            return
        with self.store.transaction() as conn:
            ids = [row[0] for row in conn.execute("SELECT o.id FROM execution_operations o JOIN execution_dispatch d ON d.operation_id=o.id LEFT JOIN execution_background_trace t ON t.operation_id=o.id WHERE o.status IN ('SUCCEEDED','FAILED','UNKNOWN','CANCELLED') AND t.operation_id IS NULL LIMIT 100")]
        for operation_id in ids:
            self.complete(operation_id)

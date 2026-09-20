"""Recoverable background terminal delivery; never re-executes business effects."""
from datetime import datetime, UTC
import json

from tiku_shared.evidence_io_budget import evidence_io_budget
from tiku_shared.trace_events import TraceEvent


class BackgroundTrace:
    RETRY_SECONDS = 5
    BATCH_SIZE = 10

    def __init__(self, dispatch, recorder):
        self.dispatch, self.store, self.recorder = dispatch, dispatch.store, recorder
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS execution_background_trace (operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id) ON DELETE CASCADE,status TEXT NOT NULL)")
            # Separate table keeps old two-column marker writers compatible.
            conn.execute("""CREATE TABLE IF NOT EXISTS execution_background_trace_delivery (
                operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id) ON DELETE CASCADE,
                payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN (0,1)),
                legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),
                next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_trace_pending ON execution_background_trace_delivery(delivered,next_attempt)")

    def complete(self, operation_id):
        if self.recorder is None:
            return
        with self.store.transaction() as conn:
            row = conn.execute("SELECT o.*,i.identity_key FROM execution_operations o JOIN execution_dispatch d ON d.operation_id=o.id LEFT JOIN execution_dispatch_inputs i ON i.operation_id=o.id WHERE o.id=?", (operation_id,)).fetchone()
            if row is None or row["status"] not in {"SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED"}:
                return
            inserted = conn.execute("INSERT OR IGNORE INTO execution_background_trace (operation_id,status) VALUES (?,?)", (operation_id, row["status"])).rowcount
            status = conn.execute("SELECT status FROM execution_background_trace WHERE operation_id=?", (operation_id,)).fetchone()[0]
            if conn.execute("SELECT 1 FROM execution_background_trace_delivery WHERE operation_id=?", (operation_id,)).fetchone() is None:
                event = TraceEvent.create(
                    event_id="evt_" + operation_id, trace_id="trace_" + operation_id,
                    request_id="req_" + operation_id, stage="background_execution",
                    event_type="public_response_finalized" if status == "SUCCEEDED" else "request_failed",
                    outcome="success" if status == "SUCCEEDED" else "error",
                    occurred_at=datetime.fromtimestamp(row["updated"], UTC).isoformat(),
                    session_key=row["session"], identity_key=row["identity_key"] or "",
                )
                payload = event.to_dict()
                payload.pop("schema_version")
                conn.execute("INSERT INTO execution_background_trace_delivery (operation_id,payload,legacy) VALUES (?,?,?)",
                             (operation_id, json.dumps(payload, sort_keys=True, separators=(",", ":")), int(not inserted)))
        self._deliver(operation_id)

    def _deliver(self, operation_id):
        # Reserve a bounded retry, releasing the authority lock before Trace I/O.
        # Acceptance into the in-memory queue is never a durable ACK.
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            row = conn.execute("SELECT * FROM execution_background_trace_delivery WHERE operation_id=? AND delivered=0 AND next_attempt<=?", (operation_id, now)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE execution_background_trace_delivery SET next_attempt=?,attempts=MIN(attempts+1,2147483647) WHERE operation_id=?",
                         (now + self.RETRY_SECONDS, operation_id))
        try:
            event = TraceEvent.create(**json.loads(row["payload"]))
            with evidence_io_budget(0.05):
                committed = self.recorder.store.terminal_for_trace(event.trace_id)
            if committed is not None:
                if committed != {key: getattr(event, key) for key in ("event_type", "stage", "outcome")}:
                    return  # Conflicting evidence is not a successful delivery.
                with self.store.transaction() as conn:
                    conn.execute("UPDATE execution_background_trace_delivery SET delivered=1 WHERE operation_id=? AND payload=?",
                                 (operation_id, row["payload"]))
            elif not row["legacy"]:
                # Old markers have no durable original event. Absence may mean
                # intentional retention; never fabricate/revive legacy evidence.
                self.recorder.record(event)
        except Exception:
            # Read errors and lost commit ACKs leave the original event pending.
            # Recovery checks durable evidence first; it never replays business.
            return

    def maintain(self):
        if self.recorder is None:
            return
        with self.store.reading() as conn:
            now = self.store.read_clock(conn)
            ids = [row[0] for row in conn.execute("""
                SELECT o.id FROM execution_operations o JOIN execution_dispatch d ON d.operation_id=o.id
                LEFT JOIN execution_background_trace_delivery t ON t.operation_id=o.id
                WHERE o.status IN ('SUCCEEDED','FAILED','UNKNOWN','CANCELLED')
                  AND (t.operation_id IS NULL OR (t.delivered=0 AND t.legacy=0 AND t.next_attempt<=?))
                ORDER BY COALESCE(t.next_attempt,0),o.updated LIMIT ?
            """, (now, self.BATCH_SIZE))]
        for operation_id in ids:
            self.complete(operation_id)

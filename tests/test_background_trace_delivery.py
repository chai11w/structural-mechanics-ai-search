"""Durable diagnostic delivery never repeats business calls or cost effects."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_execution_dispatch import DispatchFixture
from tiku_agent.background_trace import BackgroundTrace
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent, TraceEventRecorder


class BackgroundTraceDeliveryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        root.mkdir(exist_ok=True)
        self.directory = self.enterContext(tempfile.TemporaryDirectory(dir=root))
        self.f = DispatchFixture(self.directory)
        self.store = SQLiteTraceEventStore(self.f.root / "trace.db")
        self.recorder = TraceEventRecorder(self.store)
        self.addCleanup(self.recorder.close)
        self.tracer = BackgroundTrace(self.f.dispatch, self.recorder)
        ack, _, _ = self.f.accept()
        self.operation = ack["operation_id"]
        self.assertTrue(self.f.worker.run_once())
        self.before = {name: self.f.rows(name) for name in
            ("execution_operations", "execution_effects", "execution_cost_runs")}

    def delivery(self):
        return self.f.rows("execution_background_trace_delivery")[0]

    def settle(self):
        self.f.clock[0] += 10
        self.tracer.maintain()
        self.assertTrue(self.recorder.flush(5))

    def assert_business_unchanged(self):
        self.assertEqual(self.f.calls, ["hello"])
        for name, rows in self.before.items():
            self.assertEqual(self.f.rows(name), rows)

    def assert_delivered_once(self):
        self.assertEqual(self.delivery()["delivered"], 1)
        events = self.store.events_for_trace("trace_" + self.operation)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "evt_" + self.operation)
        self.assert_business_unchanged()

    def test_queue_acceptance_is_not_commit_and_repeat_maintenance_is_idempotent(self):
        self.tracer.complete(self.operation)
        self.assertEqual(self.delivery()["delivered"], 0)
        self.assertTrue(self.recorder.flush(5))
        self.settle()
        self.settle()
        self.assert_delivered_once()

    def test_queue_rejection_and_write_failure_retry_the_same_frozen_event(self):
        with patch.object(self.recorder, "record", return_value=None):
            self.tracer.complete(self.operation)
        payload = self.delivery()["payload"]
        with patch.object(self.store, "write", side_effect=OSError("synthetic failure")):
            self.settle()
        self.assertEqual(self.delivery()["delivered"], 0)
        self.assertEqual(self.delivery()["payload"], payload)
        self.settle()
        self.settle()
        self.assert_delivered_once()

    def test_committed_event_with_lost_ack_is_confirmed_without_reinsert(self):
        write = self.store.write
        def lose_ack(event):
            write(event)
            raise OSError("lost trace commit ack")
        with patch.object(self.store, "write", lose_ack):
            self.tracer.complete(self.operation)
            self.assertTrue(self.recorder.flush(5))
        with patch.object(self.recorder, "record", side_effect=AssertionError("must only reconcile")):
            self.settle()
        self.assert_delivered_once()

    def test_read_failure_is_not_absence_and_does_not_enqueue(self):
        with patch.object(self.store, "terminal_for_trace", side_effect=OSError("read unavailable")), \
                patch.object(self.recorder, "record") as record:
            self.tracer.complete(self.operation)
            record.assert_not_called()
        self.assertEqual(self.delivery()["delivered"], 0)
        self.settle()
        self.settle()
        self.assert_delivered_once()

    def test_legacy_marker_missing_trace_is_not_fabricated_or_revived(self):
        with self.f.authority.transaction() as conn:
            conn.execute("INSERT INTO execution_background_trace VALUES (?, 'SUCCEEDED')", (self.operation,))
        with patch.object(self.recorder, "record") as record:
            self.tracer.maintain()
            self.settle()
            record.assert_not_called()
        self.assertEqual((self.delivery()["legacy"], self.delivery()["delivered"]), (1, 0))
        self.assert_business_unchanged()

    def test_legacy_marker_with_existing_terminal_can_be_verified(self):
        with self.f.authority.transaction() as conn:
            conn.execute("INSERT INTO execution_background_trace VALUES (?, 'SUCCEEDED')", (self.operation,))
        event = TraceEvent.create(trace_id="trace_" + self.operation,
            event_type="public_response_finalized", stage="background_execution", outcome="success")
        self.store.write(event)
        with patch.object(self.recorder, "record") as record:
            self.tracer.maintain()
            record.assert_not_called()
        self.assertEqual(self.delivery()["delivered"], 1)
        self.assertEqual(self.store.events_for_trace(event.trace_id), [event])

    def test_process_crash_before_enqueue_and_after_trace_commit_recovers(self):
        program = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from tiku_agent.execution_store import ExecutionStore
from tiku_agent.background_trace import BackgroundTrace
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder
root=Path(sys.argv[1])
class Store(SQLiteTraceEventStore):
    def write(self, event):
        super().write(event)
        if sys.argv[3] == 'after-commit': os._exit(73)
recorder=TraceEventRecorder(Store(root/'trace.db'))
tracer=BackgroundTrace(SimpleNamespace(store=ExecutionStore(root/'execution.db')),recorder)
if sys.argv[3] == 'before-enqueue':
    tracer._deliver=lambda operation: os._exit(73)
tracer.complete(sys.argv[2])
recorder.flush(5)
'''
        # A crash before enqueue leaves a complete durable intent.
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", program,
            self.directory, self.operation, "before-enqueue"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr)
        original = self.delivery()["payload"]
        # A second process exits after durable Trace commit, before any ACK.
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", program,
            self.directory, self.operation, "after-commit"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.tracer = BackgroundTrace(self.f.dispatch, self.recorder)
        self.settle()
        self.assertEqual(self.delivery()["payload"], original)
        self.assert_delivered_once()

    def test_new_delivery_survives_dispatch_input_expiry(self):
        with patch.object(self.recorder, "record", return_value=None):
            self.tracer.complete(self.operation)
        frozen = json.loads(self.delivery()["payload"])
        with self.f.authority.transaction() as conn:
            conn.execute("DELETE FROM execution_dispatch_inputs WHERE operation_id=?", (self.operation,))
        self.settle()
        self.settle()
        self.assert_delivered_once()
        self.assertEqual(self.store.events_for_trace("trace_" + self.operation)[0].identity_key, frozen["identity_key"])

    def test_repeated_observation_cannot_bypass_retry_interval(self):
        with patch.object(self.recorder, "record", return_value=None) as record:
            self.tracer.complete(self.operation)
            for _ in range(10):
                self.tracer.complete(self.operation)
                self.tracer.maintain()
            self.assertEqual(record.call_count, 1)
            self.assertEqual(self.delivery()["attempts"], 1)
            self.settle()
            self.assertEqual(record.call_count, 2)
        self.assert_business_unchanged()

    def test_conflicting_terminal_is_not_acknowledged_or_overwritten(self):
        self.store.write(TraceEvent.create(trace_id="trace_" + self.operation,
            event_type="request_failed", stage="background_execution", outcome="error"))
        with patch.object(self.recorder, "record") as record:
            self.tracer.complete(self.operation)
            record.assert_not_called()
        self.assertEqual(self.delivery()["delivered"], 0)
        self.assertEqual(self.store.terminal_for_trace("trace_" + self.operation)["outcome"], "error")
        self.assert_business_unchanged()

    def test_terminal_probe_does_not_flush_queue_or_create_missing_store(self):
        with patch.object(self.store, "_flush_pending", side_effect=AssertionError("must not flush")):
            self.assertIsNone(self.store.terminal_for_trace("trace_" + self.operation))
            self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()

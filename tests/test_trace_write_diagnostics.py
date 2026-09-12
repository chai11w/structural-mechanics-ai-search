from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from tiku_shared import trace_events as te
from tiku_shared.evidence_io_budget import EvidenceLockBudget
from tiku_shared.trace_context import TraceContext
from tiku_shared.trace_write_diagnostics import write_stage


class TraceWriteDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        # Use a non-linked project-local directory, matching the store boundary.
        root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        root.mkdir(exist_ok=True)
        self.directory = self.enterContext(TemporaryDirectory(dir=root))
        self.path = Path(self.directory) / "trace.sqlite3"
        self.store = te.SQLiteTraceEventStore(self.path, max_rows=100)
        self.store.ensure_store_identity()
        self.recorder = te.TraceEventRecorder(self.store)
        self.addCleanup(self.recorder.close)
        self.clock = 100.0

    def event(self):
        return te.TraceEvent.create(
            trace_id=TraceContext.create().trace_id, event_type="stage_started",
            stage="private_stage_marker", outcome="started", session_key="private_session_marker",
        )

    def run_event(self, event=None):
        self.recorder.record(event or self.event())
        self.assertTrue(self.recorder.flush(3))
        return self.recorder.health()

    def fake_time(self):
        stack = ExitStack()
        for name in ("tiku_shared.trace_write_diagnostics.perf_counter", "tiku_shared.evidence_io_budget.monotonic"):
            stack.enter_context(patch(name, lambda: self.clock))
        return stack

    def connection_patch(self, *, delay_at=None, fail_at=None, fail_close=False):
        owner = self
        original = sqlite3.connect

        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                if delay_at == "insert" and sql.strip().upper().startswith("INSERT"):
                    owner.clock += .55
                return result

            def commit(self):
                if self.in_transaction:
                    if delay_at == "commit":
                        owner.clock += .55
                    if fail_at == "commit":
                        # A provider may fail after a successful durable commit.
                        super().commit()
                        raise OSError("private exception /private/path credential-marker")
                return super().commit()

            def close(self):
                super().close()
                if fail_close:
                    raise OSError("private close message")

            def rollback(self):
                if fail_at == "rollback":
                    raise OSError("private rollback message")
                return super().rollback()

        return patch.object(te.sqlite3, "connect", lambda *a, **kw: original(*a, factory=Connection, **kw))

    def persisted(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]

    def test_metadata_stall_is_distinct_from_budget_detection_stage(self):
        original = te._trace_reject_linked_path
        delayed = False

        def metadata(*a, **kw):
            nonlocal delayed
            if not delayed:
                delayed = True
                self.clock += .55
            return original(*a, **kw)

        with self.fake_time(), patch.object(te, "_trace_reject_linked_path", metadata):
            health = self.run_event()
        sample = health["write_diagnostics"]["recent_failures"][-1]
        self.assertEqual(sample["slowest_stage"], "path_checks")
        self.assertEqual(sample["transaction_state"], "not_started")
        self.assertEqual(sample["error_kind"], "EvidenceDeadlineExceeded")
        self.assertEqual(sample["stage_ms"]["path_checks"], 550)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(self.persisted(), 0)

    def test_insert_stall_rolls_back_and_is_not_replayed(self):
        with self.fake_time(), self.connection_patch(delay_at="insert"):
            health = self.run_event()
        diagnostic = health["write_diagnostics"]
        sample = diagnostic["recent_failures"][-1]
        self.assertEqual(sample["failure_stage"], "precommit_check")
        self.assertEqual(sample["slowest_stage"], "insert")
        self.assertEqual(sample["transaction_state"], "rolled_back")
        self.assertEqual(diagnostic["attempts"], 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(self.persisted(), 0)

    def test_slow_successful_commit_remains_success(self):
        with self.fake_time(), self.connection_patch(delay_at="commit"):
            health = self.run_event()
        diagnostic = health["write_diagnostics"]
        sample = diagnostic["last_over_budget"]
        self.assertEqual(sample["slowest_stage"], "commit")
        self.assertEqual(sample["transaction_state"], "committed")
        self.assertEqual(sample["outcome"], "success")
        self.assertEqual(diagnostic["over_budget_attempts"], 1)
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["dropped"], 0)
        self.assertEqual(self.persisted(), 1)

    def test_ambiguous_commit_is_not_replayed_or_claimed_rolled_back(self):
        with self.connection_patch(fail_at="commit"):
            health = self.run_event()
        sample = health["write_diagnostics"]["recent_failures"][-1]
        self.assertEqual(sample["transaction_state"], "commit_unknown")
        self.assertEqual(sample["failure_stage"], "commit")
        self.assertEqual(health["write_diagnostics"]["attempts"], 1)
        self.assertEqual(self.persisted(), 1)
        self.assertEqual(health["dropped"], 1)

    def test_close_failure_preserves_committed_state(self):
        with self.connection_patch(fail_close=True):
            health = self.run_event()
        sample = health["write_diagnostics"]["recent_failures"][-1]
        self.assertEqual(sample["failure_stage"], "close")
        self.assertEqual(sample["transaction_state"], "committed")
        self.assertEqual(self.persisted(), 1)

    def test_rollback_failure_keeps_original_detection_stage_and_unknown_state(self):
        with self.fake_time(), self.connection_patch(delay_at="insert", fail_at="rollback"):
            health = self.run_event()
        sample = health["write_diagnostics"]["recent_failures"][-1]
        self.assertEqual(sample["failure_stage"], "precommit_check")
        self.assertEqual(sample["transaction_state"], "rollback_unknown")
        self.assertEqual(sample["error_kind"], "OSError")
        self.assertEqual(health["write_diagnostics"]["attempts"], 1)
        self.assertEqual(self.persisted(), 0)

    def test_lock_retry_is_separate_from_failed_writes(self):
        original = self.store.write
        calls = 0

        def contended(event):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EvidenceLockBudget("private lock error")
            return original(event)

        with patch.object(self.store, "write", contended):
            health = self.run_event()
        diagnostic = health["write_diagnostics"]
        self.assertEqual(diagnostic["attempts"], 2)
        self.assertEqual(diagnostic["retryable_attempts"], 1)
        self.assertEqual(diagnostic["last_retryable"]["error_kind"], "EvidenceLockBudget")
        self.assertEqual(diagnostic["recent_failures"], [])
        self.assertEqual(health["written"], 1)
        self.assertEqual(self.persisted(), 1)

    def test_samples_are_bounded_private_and_health_does_not_write(self):
        events = [self.event() for _ in range(12)]
        with patch.object(self.store, "write", side_effect=OSError("private-exception-marker")):
            for event in events:
                health = self.run_event(event)
        diagnostic = health["write_diagnostics"]
        self.assertEqual(len(diagnostic["recent_failures"]), 8)
        self.assertEqual(diagnostic["attempts"], 12)
        encoded = json.dumps(diagnostic)
        for secret in ("private", str(self.path), events[0].trace_id, events[0].event_id):
            self.assertNotIn(secret, encoded)
        diagnostic["recent_failures"].clear()
        with patch.object(te.sqlite3, "connect", side_effect=AssertionError("health must not use SQLite")):
            again = self.recorder.health()["write_diagnostics"]
        self.assertEqual(len(again["recent_failures"]), 8)
        self.assertEqual(again["attempts"], 12)

    def test_duplicate_terminal_does_not_become_diagnostic_failure(self):
        event = te.TraceEvent.create(
            trace_id=TraceContext.create().trace_id, event_type="public_response_finalized",
            stage="http_response", outcome="success",
        )
        self.run_event(event)
        health = self.run_event(event)
        self.assertEqual(health["duplicate_terminals"], 1)
        self.assertEqual(health["write_diagnostics"]["recent_failures"], [])
        self.assertEqual(self.persisted(), 1)

    def test_concurrent_recorders_do_not_share_diagnostic_context(self):
        other_store = te.SQLiteTraceEventStore(Path(self.directory) / "other.sqlite3")
        other = te.TraceEventRecorder(other_store)
        self.addCleanup(other.close)
        barrier = Barrier(2)
        private_error = type("PrivateCredentialMarker", (Exception,), {})

        def failing(event):
            with write_stage("connect"):
                barrier.wait(2)
                raise private_error("private payload")

        def succeeding(event):
            with write_stage("capacity"):
                barrier.wait(2)

        with patch.object(self.store, "write", failing), patch.object(other_store, "write", succeeding):
            self.recorder.record(self.event())
            other.record(self.event())
            self.assertTrue(self.recorder.flush(3))
            self.assertTrue(other.flush(3))
        first = self.recorder.health()["write_diagnostics"]
        second = other.health()["write_diagnostics"]
        self.assertEqual(first["recent_failures"][0]["failure_stage"], "connect")
        self.assertEqual(first["recent_failures"][0]["error_kind"], "unknown")
        self.assertEqual(first["recent_failures"][0]["transaction_state"], "unknown")
        self.assertEqual(set(second["stage_max_ms"]), {"capacity"})
        self.assertEqual(second["recent_failures"], [])


if __name__ == "__main__":
    unittest.main()

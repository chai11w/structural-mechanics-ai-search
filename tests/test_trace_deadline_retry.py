"""Store-certified retries against real SQLite transactions and fake deadlines."""

from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from tiku_shared import trace_events as te
from tiku_shared.evidence_io_budget import EvidenceDeadlineExceeded, evidence_io_budget
from tiku_shared.trace_context import TraceContext


class TraceDeadlineRetryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        root.mkdir(exist_ok=True)
        self.directory = self.enterContext(TemporaryDirectory(dir=root))
        self.store = te.SQLiteTraceEventStore(Path(self.directory) / "trace.sqlite3", max_rows=1)
        self.store.ensure_store_identity()
        self.recorder = te.TraceEventRecorder(self.store, queue_capacity=2)
        self.addCleanup(self.recorder.close)
        self.clock = 100.0
        self.connect = sqlite3.connect
        self.calls = []

    def event(self, terminal=False):
        return te.TraceEvent.create(
            trace_id=TraceContext.create().trace_id,
            event_type="public_response_finalized" if terminal else "stage_started",
            stage="test", outcome="success" if terminal else "started",
        )

    def rows_and_capacity(self):
        with closing(self.connect(self.store.path)) as connection:
            rows = connection.execute("SELECT event_id FROM trace_events").fetchall()
            count = te.trace_row_count(connection)
        return rows, count

    def controlled(self, *, stall_insert=False, error_at=None, stall_seconds=.55):
        owner = self
        stack = ExitStack()
        for target in ("tiku_shared.evidence_io_budget.monotonic", "tiku_shared.trace_write_diagnostics.perf_counter"):
            stack.enter_context(patch(target, lambda: owner.clock))

        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                result = super().execute(sql, *args, **kwargs)
                if sql == "BEGIN IMMEDIATE" and error_at == "begin":
                    raise EvidenceDeadlineExceeded("private begin result unknown")
                if sql.strip().startswith("INSERT INTO trace_events ("):
                    if stall_insert and len(owner.calls) == 1:
                        owner.clock += stall_seconds
                    if error_at == "insert":
                        raise sqlite3.OperationalError("private SQLite interruption")
                return result

            def commit(self):
                was_active = self.in_transaction
                result = super().commit()
                if was_active and error_at == "commit":
                    raise EvidenceDeadlineExceeded("private commit result unknown")
                return result

            def rollback(self):
                if error_at == "rollback":
                    raise EvidenceDeadlineExceeded("private rollback result unknown")
                return super().rollback()

            def close(self):
                super().close()
                if error_at == "close":
                    raise EvidenceDeadlineExceeded("private close failure")

        stack.enter_context(patch.object(te.sqlite3, "connect", lambda *a, **k: owner.connect(*a, factory=Connection, **k)))
        original = self.store.write

        def write(event):
            owner.calls.append(event)
            if len(owner.calls) > 1:
                # Prior event insert and its capacity trigger must both be gone.
                owner.assertEqual(owner.rows_and_capacity(), ([], 0))
            return original(event)

        stack.enter_context(patch.object(self.store, "write", write))
        return stack

    def submit(self, event=None):
        event = event or self.event()
        self.assertIsNotNone(self.recorder.record(event))
        self.assertTrue(self.recorder.flush(3))
        return event, self.recorder.health()

    def test_insert_rollback_retries_same_terminal_once_without_consuming_extra_capacity(self):
        with self.controlled(stall_insert=True):
            event, health = self.submit(self.event(terminal=True))
        self.assertEqual(self.calls, [event, event])
        self.assertIs(self.calls[0], self.calls[1])
        self.assertEqual(self.rows_and_capacity(), ([(event.event_id,)], 1))
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["dropped"], 0)
        self.assertEqual(health["duplicate_terminals"], 0)
        self.assertEqual(health["write_diagnostics"]["retryable_attempts"], 1)
        # A different event is still refused at capacity; no retry of that error.
        _, full = self.submit()
        self.assertEqual(full["last_failure_kind"], "TraceEventCapacityError")
        self.assertEqual(full["write_diagnostics"]["attempts"], 3)
        self.assertEqual(self.rows_and_capacity()[1], 1)

    def test_direct_store_certifies_rollback_without_diagnostics_and_does_not_retry(self):
        event = self.event()
        with self.controlled(stall_insert=True), patch.object(te, "write_transaction"), evidence_io_budget(.5):
            with self.assertRaises(te.TraceEventRetryableDeadline):
                self.store.write(event)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows_and_capacity(), ([], 0))

    def test_lock_file_preparation_deadline_retries_before_transaction(self):
        original = te._trace_reject_linked_path
        stalled = False

        def metadata(path, **kwargs):
            nonlocal stalled
            if Path(path).name == te.TRACE_MAINTENANCE_LOCK_FILENAME and not stalled:
                stalled = True
                self.clock += .55
            return original(path, **kwargs)

        with self.controlled(), patch.object(te, "_trace_reject_linked_path", metadata):
            event, health = self.submit()
        sample = health["write_diagnostics"]["last_retryable"]
        self.assertEqual(sample["failure_stage"], "maintenance_lock")
        self.assertEqual(sample["transaction_state"], "not_started")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.rows_and_capacity(), ([(event.event_id,)], 1))

    def test_uncertain_begin_commit_rollback_and_failed_close_never_retry(self):
        for error_at, stall_insert, expected_count in (
            ("begin", False, 0), ("commit", False, 1),
            ("rollback", True, 0), ("close", True, 0),
        ):
            with self.subTest(error_at=error_at):
                # Each subcase uses a separate store and recorder.
                store = te.SQLiteTraceEventStore(Path(self.directory) / (error_at + ".sqlite3"))
                store.ensure_store_identity()
                recorder = te.TraceEventRecorder(store)
                self.addCleanup(recorder.close)
                self.store, self.recorder, self.calls = store, recorder, []
                with self.controlled(stall_insert=stall_insert, error_at=error_at), patch.object(te, "write_transaction"):
                    _, health = self.submit()
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(health["write_failures"], 1)
                self.assertEqual(health["write_diagnostics"]["retryable_attempts"], 0)
                self.assertEqual(health["last_failure_kind"], "EvidenceDeadlineExceeded")
                self.assertEqual(self.rows_and_capacity()[1], expected_count)

    def test_operational_error_after_insert_is_not_retried_even_after_rollback(self):
        with self.controlled(error_at="insert"):
            _, health = self.submit()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(health["last_failure_kind"], "OperationalError")
        self.assertEqual(self.rows_and_capacity(), ([], 0))

    def test_recovery_window_expired_during_first_attempt_does_not_retry(self):
        with self.controlled(stall_insert=True, stall_seconds=2.1), patch.object(te, "monotonic", lambda: self.clock):
            _, health = self.submit()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(self.rows_and_capacity(), ([], 0))

    def test_lock_wait_after_rollback_cannot_extend_recovery_window(self):
        original = te._trace_writer_maintenance_lock

        @contextmanager
        def maintenance(path):
            if len(self.calls) == 2:
                self.clock += 1.5
                raise te.TraceEventMaintenanceBusy("synthetic lock contention")
            with original(path):
                yield

        with self.controlled(stall_insert=True), patch.object(te, "monotonic", lambda: self.clock), \
                patch.object(te, "_trace_writer_maintenance_lock", maintenance):
            _, health = self.submit()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(health["last_failure_kind"], "TraceEventMaintenanceBusy")
        self.assertEqual(self.rows_and_capacity(), ([], 0))

    def test_expiry_during_backoff_prevents_another_store_call(self):
        def wait(_timeout):
            self.clock += 2
            return False

        with self.controlled(stall_insert=True), patch.object(te, "monotonic", lambda: self.clock), \
                patch.object(self.recorder._cancel, "wait", wait):
            _, health = self.submit()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(self.rows_and_capacity(), ([], 0))

    def test_cancel_during_retry_backoff_prevents_late_write(self):
        entered = Event()
        original_wait = self.recorder._cancel.wait

        def wait(_timeout):
            entered.set()
            return original_wait(2)

        with self.controlled(stall_insert=True), patch.object(self.recorder._cancel, "wait", wait):
            self.recorder.record(self.event())
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.recorder.close(timeout=0))
            self.assertTrue(self.recorder.close(timeout=2))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows_and_capacity(), ([], 0))
        self.assertEqual(self.recorder.health()["pending"], 0)


if __name__ == "__main__":
    unittest.main()

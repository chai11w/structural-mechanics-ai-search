"""F1/F2: bounded evidence failure, recovery, maintenance and shutdown."""

from dataclasses import replace
from contextlib import closing
from threading import Event
from time import perf_counter
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_checkpoint_async as fixture
from tiku_diagnostics.checkpoint_retention import _execution_lock, CheckpointRetentionError
from tiku_shared.evidence_io_budget import evidence_io_budget, check_evidence_budget, EvidenceDeadlineExceeded


class CaptureResilienceTest(unittest.TestCase):
    setUp = fixture.AsyncA2Test.setUp
    trace = fixture.AsyncA2Test.trace
    records = fixture.AsyncA2Test.records
    search = fixture.AsyncA2Test.search
    submit_route = fixture.AsyncA2Test.submit_route
    assert_drained = fixture.AsyncA2Test.assert_drained

    def assert_accounted(self):
        health = self.recorder.health()
        counts = health["counters"]
        self.assertEqual(counts["queued"], health["pending"] + sum(counts[key] for key in
            ("stored", "failed", "expired", "shutdown_dropped", "circuit_dropped")))

    def test_real_database_lock_has_finite_failure_and_recovers(self):
        self.submit_route()
        self.assert_drained()
        with closing(sqlite3.connect(self.store.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            self.submit_route()
            self.assertTrue(self.recorder.flush(3))
            self.assertEqual(self.recorder.health()["counters"]["failed"], 1)
            blocker.rollback()
        self.submit_route()
        self.assert_drained()
        self.assertEqual(self.recorder.health()["status"], "ok")
        self.assertEqual(self.recorder.health()["counters"]["recoveries"], 1)
        self.assertEqual(len(self.records()), 2)
        self.assert_accounted()

    def test_python_lock_has_finite_failure_without_waiting_for_release(self):
        with self.store._lock:
            self.submit_route()
            self.assertTrue(self.recorder.flush(3))
            self.assertEqual(self.recorder.health()["counters"]["failed"], 1)
        self.assert_accounted()

    def test_circuit_rejects_then_probes_new_work_without_retry(self):
        ticks = [0.0]
        self.recorder.clock = lambda: ticks[0]
        with patch.object(self.store, "put_checkpoint", side_effect=OSError("private")) as writer:
            for _ in range(3):
                self.submit_route()
                self.assert_drained()
            self.assertEqual(writer.call_count, 3)
            self.assertEqual(self.submit_route().reason_code, "CAPTURE_CIRCUIT_OPEN")
        self.assertTrue(self.recorder.health()["circuit_open"])
        ticks[0] = 6.0
        self.submit_route()
        self.assert_drained()
        self.assertEqual(self.recorder.health()["status"], "ok")
        self.assertEqual(len(self.records()), 1)
        self.assert_accounted()

    def test_capture_capacity_rejects_without_inline_purge(self):
        self.store.capacity = replace(self.policy, max_checkpoint_rows=1)
        self.submit_route()
        self.assert_drained()
        with patch.object(self.store, "_purge_expired_locked", side_effect=AssertionError("inline purge")) as purge:
            self.submit_route()
            self.assert_drained()
            purge.assert_not_called()
        self.assertEqual(self.recorder.health()["counters"]["failed"], 1)
        self.assertEqual(len(self.records()), 1)

    def test_maintenance_and_capture_exclude_each_other(self):
        with _execution_lock(self.root):
            self.submit_route()
            self.assert_drained()
        self.assertEqual(self.recorder.health()["last_failure_code"], "capture_maintenance_busy")
        entered, release = Event(), Event()
        execute = self.recorder._execute
        def hold(job):
            entered.set()
            self.assertTrue(release.wait(3))
            return execute(job)
        with patch.object(self.recorder, "_execute", side_effect=hold):
            try:
                self.submit_route()
                self.assertTrue(entered.wait(2))
                with self.assertRaises(CheckpointRetentionError) as raised:
                    with _execution_lock(self.root):
                        self.fail("maintenance entered during capture")
                self.assertEqual(raised.exception.code, "RETENTION_ALREADY_RUNNING")
            finally:
                release.set()
            self.assert_drained()
        self.assertEqual(self.recorder.health()["status"], "ok")

    def test_blocked_file_shutdown_drops_queue_and_cancels_late_commit(self):
        entered, release = Event(), Event()
        read = self.engine.read_image
        def hold(path):
            entered.set()
            self.assertTrue(release.wait(4))
            return read(path)
        with patch.object(self.engine, "read_image", side_effect=hold):
            try:
                response = self.search()
                self.assertEqual(response.media_kind, "candidates")
                self.assertTrue(entered.wait(2))
                with self.recorder._condition:
                    self.recorder._active_started -= 3
                self.assertEqual(self.submit_route().reason_code, "CAPTURE_CONSUMER_STALLED")
                start = perf_counter()
                self.assertFalse(self.recorder.close(0.02))
                self.assertLess(perf_counter() - start, 0.5)
                health = self.recorder.health()
                self.assertEqual((health["pending"], health["running"], health["backlog"]), (1, 1, 0))
                self.assertEqual(health["counters"]["shutdown_dropped"], 4)
                self.assert_accounted()
            finally:
                release.set()
            self.assert_drained()
        self.assertEqual(self.records(), [])
        self.assert_accounted()

    def test_shared_trace_lock_does_not_block_capture_or_health_forever(self):
        self.submit_route()
        self.assert_drained()
        self.assertTrue(self.traces.flush())
        with self.trace_store._path_lock:
            self.submit_route()
            self.assertTrue(self.recorder.flush(3))
            start = perf_counter()
            self.traces.health()
            self.assertLess(perf_counter() - start, 0.1)
        self.assert_accounted()


class EvidenceLifecycleTest(unittest.TestCase):
    def test_nested_budget_preserves_cancellation(self):
        cancel = Event()
        with evidence_io_budget(1, cancel=cancel, capture=True):
            with evidence_io_budget(2) as inner:
                self.assertTrue(inner.capture)
                cancel.set()
                with self.assertRaises(EvidenceDeadlineExceeded):
                    check_evidence_budget()

    def test_trace_close_and_health_remain_finite_when_store_is_stuck(self):
        from tiku_shared.trace_events import TraceEventRecorder
        from tiku_shared.trace_context import TraceContext
        entered, release = Event(), Event()
        class Store:
            def write(self, _event):
                entered.set()
                release.wait(4)
                check_evidence_budget()
            def flush(self):
                pass
            def close(self):
                pass
        recorder = TraceEventRecorder(Store())
        try:
            for _ in range(3):
                recorder.record(trace_id=TraceContext.create().trace_id, event_type="request_received",
                                stage="http_request", outcome="started")
            self.assertTrue(entered.wait(2))
            self.assertFalse(recorder.flush(0.01))
            start = perf_counter()
            self.assertFalse(recorder.close(0.02))
            health = recorder.health()
            self.assertLess(perf_counter() - start, 0.5)
            self.assertEqual((health["pending"], health["dropped"]), (1, 2))
            self.assertFalse(health["accepting"])
        finally:
            release.set()
        self.assertTrue(recorder.close(2))
        self.assertEqual(recorder.health()["pending"], 0)
        self.assertEqual(recorder.health()["written"], 0)

    def test_lifespan_does_not_wait_for_blocked_maintenance_executor(self):
        from fastapi.testclient import TestClient
        from tiku_agent.fastapi_demo import create_app
        import tempfile
        from pathlib import Path
        entered, release = Event(), Event()
        def maintenance():
            entered.set()
            release.wait(4)
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(incoming_dir=Path(directory), cleanup_interval_seconds=0,
                checkpoint_retention_runner=maintenance, checkpoint_retention_interval_seconds=1)
            try:
                with TestClient(app):
                    self.assertTrue(entered.wait(2))
                    start = perf_counter()
                self.assertLess(perf_counter() - start, 1)
            finally:
                release.set()

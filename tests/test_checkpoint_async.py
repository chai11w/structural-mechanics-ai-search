"""Deterministic Q1-Q3 acceptance for the asynchronous capture entry points."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, enumerate as threads
from time import perf_counter
import unittest
from unittest.mock import patch

from tests import test_a2_checkpoint_integration as a2_fixture
from tests import test_a3_checkpoint_integration as a3_fixture
from tests import test_a3_runtime as runtime_fixture
from tests.test_checkpoint_capture import _context
from tiku_agent.checkpoint_async import AsyncCheckpointRecorder
from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1
from tiku_agent.checkpoint_stage_input import freeze_a2_stage_input
from tiku_agent.checkpoint_submission_budget import CheckpointSubmissionBudget
from tiku_agent.a3_checkpoint_context import a3_checkpoint_request_scope
from tiku_agent.checkpoint_submission_budget import current_checkpoint_budget
from tiku_agent.checkpoint_resource_leases import CheckpointResourceLeases
from tiku_agent.tool_result import ToolResult, ToolOutcome


ADMISSION = A2CaptureAdmissionV1("search", True, True, True, True, True)


class AsyncA2Test(unittest.TestCase):
    trace = a2_fixture.A2CheckpointIntegrationTest.trace
    records = a2_fixture.A2CheckpointIntegrationTest.records
    search = a2_fixture.A2CheckpointIntegrationTest.search

    def setUp(self):
        a2_fixture.A2CheckpointIntegrationTest.setUp(self)
        self.engine = self.recorder
        self.recorder = AsyncCheckpointRecorder(self.engine, trace_recorder=self.traces)
        self.runtime.checkpoint_recorder = self.recorder
        self.artifacts.checkpoint_resource_leases = self.recorder.resource_leases
        self.addCleanup(self.recorder.close)

    def assert_drained(self):
        self.assertTrue(self.recorder.flush(8))
        health = self.recorder.health()
        self.assertEqual(health["pending"], 0)
        self.assertEqual(health["pending_bytes"], 0)

    def blocked_search(self, mode):
        entered, release = Event(), Event()
        def wait():
            entered.set()
            if not release.wait(8):
                raise TimeoutError("test did not release capture")
        if mode == "file":
            original = self.engine.read_image
            def operation(*args, **kwargs):
                wait()
                return original(*args, **kwargs)
            blocker = patch.object(self.engine, "read_image", side_effect=operation)
        elif mode == "transaction":
            original = self.store._write_connection
            @contextmanager
            def operation():
                with original() as connection:
                    wait()
                    yield connection
            blocker = patch.object(self.store, "_write_connection", operation)
        elif mode == "store_lock":
            original = self.store.latest_checkpoint
            def operation(*args, **kwargs):
                entered.set()
                return original(*args, **kwargs)
            blocker = patch.object(self.store, "latest_checkpoint", side_effect=operation)
        else:
            original = self.store._trace_rows
            def operation():
                entered.set()
                return original()
            blocker = patch.object(self.store, "_trace_rows", side_effect=operation)
        lock = self.store._lock if mode == "store_lock" else self.trace_store._path_lock if mode == "trace_lock" else None
        with blocker, ThreadPoolExecutor(max_workers=1) as pool:
            if lock is not None:
                lock.acquire()
            try:
                response = pool.submit(self.search).result(timeout=4)
                self.assertEqual(response.media_kind, "candidates")
                self.assertTrue(entered.wait(4))
                self.assertFalse(release.is_set())
                self.assertGreater(self.recorder.health()["pending"], 0)
                self.assertEqual(self.recorder.health()["counters"]["stored"], 0)
            finally:
                release.set()
                if lock is not None:
                    lock.release()
            self.assert_drained()
        rows = self.records()
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["outcome"] for row in rows], ["success"] * 5)
        self.assertEqual([row["predecessor_checkpoint_id"] for row in rows[1:]],
                         [row["checkpoint_id"] for row in rows[:-1]])

    def test_business_returns_while_image_read_is_blocked(self):
        self.blocked_search("file")

    def test_business_returns_while_checkpoint_transaction_is_open(self):
        self.blocked_search("transaction")

    def test_business_returns_while_store_lock_is_held(self):
        self.blocked_search("store_lock")

    def test_business_returns_while_shared_trace_path_lock_is_held(self):
        self.blocked_search("trace_lock")

    def test_quick_answer_and_new_search_preserve_original_trace_and_revision(self):
        entered, release = Event(), Event()
        original = self.engine.read_image
        def read(path):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("test did not release")
            return original(path)
        with patch.object(self.engine, "read_image", side_effect=read):
            try:
                self.search()
                first_trace = self.last_trace.trace_id
                self.assertTrue(entered.wait(4))
                with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
                    response = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
                answer_trace = self.last_trace.trace_id
                self.assertEqual(response.media_kind, "answer")
                self.search()
                next_trace = self.last_trace.trace_id
                self.assertEqual(self.recorder.health()["counters"]["stored"], 0)
                self.traces.flush()
                events = self.trace_store.events_for_trace(answer_trace)
                self.assertFalse(any("checkpoint_id" in event.safe_attributes for event in events))
            finally:
                release.set()
            self.assert_drained()
        rows = self.records()
        self.assertEqual([row["trace_id"] for row in rows], [first_trace] * 5 + [answer_trace] + [next_trace] * 5)
        self.assertNotEqual(rows[0]["owner"]["workflow_search_id"], rows[6]["owner"]["workflow_search_id"])
        self.assertEqual(rows[5]["predecessor_checkpoint_id"], rows[4]["checkpoint_id"])
        self.assertEqual(rows[6]["predecessor_checkpoint_id"], "")
        self.traces.flush()
        events = self.trace_store.events_for_trace(answer_trace)
        links = [event for event in events if "checkpoint_id" in event.safe_attributes]
        self.assertEqual([event.safe_attributes["checkpoint_id"] for event in links], [rows[5]["checkpoint_id"]])
        self.assertEqual(links[0].search_id, rows[5]["owner"]["search_id"])

    def submit_route(self, *, identity="invite_test", budget=None, failed=False):
        context = replace(_context(standalone=True), identity_key=identity)
        result = ToolResult(outcome=ToolOutcome.ERROR, code="ROUTE_FAILED") if failed else ToolResult.success(code="IMAGE_ROUTED")
        frozen = freeze_a2_stage_input(result, {
            "inputs": {}, "route_decision": {"route": "A2", "decision_source": "test", "reason_code": "IMAGE_ROUTED"}})
        return self.recorder.submit_stage(context, frozen, stage="image_routed", admission=ADMISSION,
                                          budget=budget, started=perf_counter())

    def test_count_bytes_and_age_bounds_keep_one_consumer(self):
        before_threads = {thread.ident for thread in threads() if thread.name == "tiku-checkpoint-consumer"}
        self.recorder.max_pending = 2
        entered, release = Event(), Event()
        ticks = [0.0]
        self.recorder.clock = lambda: ticks[0]
        execute = self.recorder._execute
        def hold(job):
            entered.set()
            release.wait(8)
            return execute(job)
        with patch.object(self.recorder, "_execute", side_effect=hold):
            try:
                accepted = self.submit_route()
                self.assertEqual((accepted.stored, accepted.checkpoint_id, accepted.reason_code), (False, "", "CAPTURE_QUEUED"))
                self.assertTrue(entered.wait(4))
                self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUED")
                for _ in range(10):
                    self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUE_FULL")
                self.recorder.max_pending = 3
                self.recorder.max_bytes = self.recorder.health()["pending_bytes"]
                self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUE_BYTES_FULL")
                self.assertEqual(before_threads, {thread.ident for thread in threads() if thread.name == "tiku-checkpoint-consumer"})
                ticks[0] = self.recorder.max_age_seconds + 1
            finally:
                release.set()
            self.assert_drained()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.recorder.health()["counters"]["expired"], 2)
        self.assertEqual(self.recorder.health()["counters"]["rejected"], 11)

    def test_dropped_write_does_not_create_a_predecessor_or_trace_link(self):
        with patch.object(self.store, "put_checkpoint", side_effect=OSError("test")):
            self.submit_route()
            self.assert_drained()
        self.submit_route()
        self.assert_drained()
        self.submit_route(identity="other")
        self.assert_drained()
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["predecessor_checkpoint_id"] for row in rows], ["", ""])

    def test_latest_commit_and_last_success_are_distinct_after_failure(self):
        self.submit_route()
        self.submit_route(failed=True)
        self.submit_route(failed=True)
        self.assert_drained()
        rows = self.records()
        self.assertEqual([row["outcome"] for row in rows], ["success", "failed", "failed"])
        self.assertEqual(rows[2]["predecessor_checkpoint_id"], rows[1]["checkpoint_id"])
        self.assertEqual(rows[2]["failure"]["last_successful_checkpoint_id"], rows[0]["checkpoint_id"])

    def test_start_close_and_app_lifecycle_use_one_consumer(self):
        from fastapi.testclient import TestClient
        from tiku_agent.fastapi_demo import create_app
        self.assertTrue(self.recorder.close())
        self.recorder = AsyncCheckpointRecorder(self.engine, trace_recorder=self.traces, autostart=False)
        self.addCleanup(self.recorder.close)
        self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUE_CLOSED")
        app = create_app(runtime=self.runtime, incoming_dir=self.root / "incoming", cleanup_interval_seconds=0,
            checkpoint_capture_start=self.recorder.start, checkpoint_capture_close=self.recorder.close)
        with TestClient(app):
            self.assertTrue(self.recorder.health()["worker_alive"])
            worker = self.recorder._worker
            self.recorder.start()
            self.assertIs(self.recorder._worker, worker)
            self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUED")
            self.assert_drained()
        self.assertFalse(self.recorder.health()["worker_alive"])
        self.assertEqual(self.submit_route().reason_code, "CAPTURE_QUEUE_CLOSED")

    def test_budget_exhaustion_and_disabled_capture_do_not_queue(self):
        budget = CheckpointSubmissionBudget(seconds=0.0)
        self.assertEqual(self.submit_route(budget=budget).reason_code, "CAPTURE_REQUEST_BUDGET_EXHAUSTED")
        self.recorder.gate.enabled = False
        with patch.object(self.recorder, "_execute", side_effect=AssertionError("disabled")):
            self.submit_route()
            self.assert_drained()
        self.assertEqual(self.recorder.health()["counters"]["queued"], 0)


class AsyncA3Test(unittest.TestCase):
    trace = a3_fixture.A3CheckpointIntegrationTest.trace
    records = a3_fixture.A3CheckpointIntegrationTest.records
    business = a3_fixture.A3CheckpointIntegrationTest.business
    upload = a3_fixture.A3CheckpointIntegrationTest.upload

    def setUp(self):
        a3_fixture.A3CheckpointIntegrationTest.setUp(self)
        self.engine = self.recorder
        self.recorder = AsyncCheckpointRecorder(self.engine, trace_recorder=self.traces)
        for runtime in (self.runtime, self.a2):
            runtime.checkpoint_recorder = self.recorder
            runtime.artifacts.checkpoint_resource_leases = self.recorder.resource_leases
        self.addCleanup(self.recorder.close)

    def test_out_of_order_units_keep_parent_chain_and_trace_after_fast_selection(self):
        self.runtime.page_observer = runtime_fixture.FakeObserver()
        self.runtime.auto_cropper = runtime_fixture.FakeAutoCropper(second_status="auto_ready")
        barrier = Barrier(2)
        second_finished, release, entered = Event(), Event(), Event()
        verify, capture, read = self.verifier.verify, self.runtime._capture_checkpoint, self.engine.read_image
        completed = []
        def compare(page, crop, selected, understanding):
            barrier.wait(5)
            if selected["unit_id"] == "g1-u1":
                self.assertTrue(second_finished.wait(5))
            return verify(page, crop, selected, understanding)
        def capture_completed(state, stage, **kwargs):
            result = capture(state, stage, **kwargs)
            if stage == "crop_validated":
                completed.append(kwargs["unit_id"])
                if kwargs["unit_id"] == "g1-u2":
                    second_finished.set()
            return result
        def blocked(path):
            entered.set()
            self.assertTrue(release.wait(8))
            return read(path)
        with patch.object(self.verifier, "verify", side_effect=compare), patch.object(
            self.runtime, "_capture_checkpoint", side_effect=capture_completed
        ), patch.object(self.engine, "read_image", side_effect=blocked):
            try:
                self.upload()
                self.assertTrue(entered.wait(4))
                self.assertEqual(completed, ["g1-u2", "g1-u1"])
                with self.business():
                    self.assertEqual(self.runtime.select_unit("session-test", "g1-u2", identity_key="invite_test").media_kind, "candidates")
                with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
                    self.assertEqual(self.runtime.handle_text("session-test", "1", identity_key="invite_test").media_kind, "answer")
            finally:
                release.set()
            self.assertTrue(self.recorder.flush(8))
        rows = self.records()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[-1]["owner"]["unit_id"], "g1-u2")
        by_id = {row["checkpoint_id"]: row for row in rows}
        for row in rows:
            if row["stage"] == "crop_validated":
                parent = by_id[row["predecessor_checkpoint_id"]]
                self.assertEqual(parent["owner"]["unit_id"], row["owner"]["unit_id"])
        self.traces.flush()
        links = []
        for trace_id in {row["trace_id"] for row in rows}:
            links += [event for event in self.trace_store.events_for_trace(trace_id) if "checkpoint_id" in event.safe_attributes]
        self.assertEqual(len(links), len(rows))
        for event in links:
            row = by_id[event.safe_attributes["checkpoint_id"]]
            self.assertEqual(event.trace_id, row["trace_id"])
            for name in ("session_key", "identity_key", "workflow_search_id", "search_id", "unit_id"):
                self.assertEqual(getattr(event, name), row["owner"][name])

    def test_parent_and_nested_a2_share_the_request_budget(self):
        self.upload()
        self.assertTrue(self.recorder.flush(8))
        state = self.runtime.store.load("session-test")
        with a3_checkpoint_request_scope():
            budget = current_checkpoint_budget.get()
            with a3_checkpoint_request_scope():
                self.assertIs(current_checkpoint_budget.get(), budget)
            budget.charge(0.101)
            with self.trace():
                result = self.recorder.capture_parent(state, stage="page_understood", identity_key="invite_test")
                self.assertEqual(result.reason_code, "CAPTURE_REQUEST_BUDGET_EXHAUSTED")
                child = self.a2.store.load("session-test")
                agent = self.a2._make_agent(child)
                self.a2._attach_checkpoint_emitter(agent, identity_key="invite_test", request_id="")
                queued = self.recorder.health()["counters"]["queued"]
                agent._emit_question_checkpoint()
                self.assertEqual(self.recorder.health()["counters"]["queued"], queued)
                self.assertEqual(self.recorder.health()["last_failure_code"], "CAPTURE_REQUEST_BUDGET_EXHAUSTED")

    def test_parent_child_queue_and_resource_lease_survive_session_clear(self):
        entered, release = Event(), Event()
        read = self.engine.read_image
        def blocked(path):
            entered.set()
            if not release.wait(8):
                raise TimeoutError("test did not release")
            return read(path)
        with patch.object(self.engine, "read_image", side_effect=blocked):
            try:
                self.assertEqual(self.upload().media_kind, "candidates")
                self.assertTrue(entered.wait(4))
                state = self.runtime.store.load("session-test")
                page = Path(state.source_page_path)
                crop = Path(state.auto_crops[state.selected_unit_id]["path"])
                with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
                    self.assertEqual(self.runtime.handle_text("session-test", "1", identity_key="invite_test").media_kind, "answer")
                self.runtime.artifacts.clear_session("session-test")
                self.a2.artifacts.clear_session("session-test")
                self.assertTrue(page.exists())
                self.assertTrue(crop.exists())
                newer = self.runtime.artifacts.persist_image("session-test", self.source)
            finally:
                release.set()
            self.assertTrue(self.recorder.flush(8))
        rows = self.records()
        self.assertEqual(len(rows), 9)
        self.assertEqual([row["outcome"] for row in rows], ["success"] * 9)
        self.assertEqual(rows[5]["predecessor_checkpoint_id"], rows[4]["checkpoint_id"])
        self.assertEqual(rows[8]["owner"]["unit_id"], state.selected_unit_id)
        self.assertFalse(page.exists())
        self.assertFalse(crop.exists())
        self.assertTrue(newer.exists())


class ResourceLeaseTest(unittest.TestCase):
    def test_cleanup_refuses_new_leases_without_waiting_for_file_io(self):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "session"
            target.mkdir()
            leases = CheckpointResourceLeases()
            entered, release = Event(), Event()
            remove = shutil.rmtree
            def blocked(path, **kwargs):
                entered.set()
                release.wait(5)
                return remove(path, **kwargs)
            with patch("tiku_agent.checkpoint_resource_leases.shutil.rmtree", side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(leases.clear_session, target)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertFalse(leases.acquire("new", [target / "image.png"], perf_counter() + 100))
                finally:
                    release.set()
                self.assertTrue(future.result(timeout=3))
            self.assertTrue(leases.acquire("new", [target / "new.png"], perf_counter() + 100))
            leases.release("new")

    def test_leased_cleanup_never_follows_directory_links(self):
        import os
        import tempfile
        from tiku_agent.session_artifacts import SessionArtifacts
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "bank"
            outside.mkdir()
            answer = outside / "answer.png"
            answer.write_bytes(b"original")
            artifacts = SessionArtifacts(root / "sessions")
            protected = artifacts.persist_image("session", answer)
            link = artifacts.session_dir("session") / "linked"
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                link.symlink_to(outside, target_is_directory=True)
            leases = CheckpointResourceLeases()
            artifacts.checkpoint_resource_leases = leases
            leases.acquire("capture", [protected], leases.clock() + 100)
            artifacts.clear_session("session")
            self.assertEqual(answer.read_bytes(), b"original")
            self.assertTrue(protected.exists())
            leases.release("capture")
            self.assertFalse(protected.exists())

    def test_expired_lease_does_not_hold_session_files_forever(self):
        import tempfile
        from tiku_agent.session_artifacts import SessionArtifacts
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = SessionArtifacts(Path(temporary) / "sessions")
            source = Path(temporary) / "source.png"
            source.write_bytes(b"test")
            target = artifacts.persist_image("session", source)
            ticks = [0.0]
            leases = CheckpointResourceLeases(clock=lambda: ticks[0])
            artifacts.checkpoint_resource_leases = leases
            leases.acquire("capture", [target], 10)
            ticks[0] = 11
            artifacts.clear_session("session")
            self.assertFalse(target.exists())
            leases.release("capture")

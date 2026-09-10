"""Accounting admission must be checked after waiting, not only at registration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from tests import test_execution_effects as fixtures
from tests.test_a3_runtime import FakeObserver, FakeVerifier
from tiku_agent.a3_runtime import A3MvpRuntime
from tiku_agent.execution_effects import ExecutionEffects, reconcile_cost
from tiku_agent.execution_operations import OperationStore
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionError, ExecutionSessionStore, ExecutionStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentProtocolError, _ExecutionGate
from tiku_shared.model_costs import timed_model_call


class ExecutionCostAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionEffectsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @contextmanager
    def ledger_outage(self):
        connect = sqlite3.connect
        def fail(path, *args, **kwargs):
            if Path(path) == self.fixture.ledger.path:
                raise sqlite3.OperationalError("isolated ledger outage")
            return connect(path, *args, **kwargs)
        with patch("tiku_shared.model_costs.sqlite3.connect", side_effect=fail):
            yield

    def assert_pending(self, callback):
        with self.assertRaises((ExecutionError, AgentProtocolError)) as caught:
            callback()
        self.assertEqual(caught.exception.code, "EXECUTION_COST_PENDING")

    def queue_scenario(self, kind):
        f = self.fixture
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def invoke(name):
            def provider():
                calls.append(name)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError("test provider was not released")
                return {"input_tokens": 10, "output_tokens": 3}
            timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                             call_type="queued-admission", usage_getter=lambda value: value)
        class Agent(f.runtime.agent_factory):
            def handle_text(self, text):
                invoke(text)
                return fixtures.AgentResponse(text="receipt", state=self.state.to_dict(), intent="greeting")
        f.runtime.agent_factory = Agent
        runtime = f.runtime
        image = f.root / "page.jpg"
        if kind == "a3":
            class Observer(FakeObserver):
                def observe(self, path):
                    invoke("image")
                    return super().observe(path)
            Image.new("RGB", (1000, 800), "white").save(image)
            runtime = A3MvpRuntime(store=ExecutionSessionStore(f.store, "workflow"),
                artifacts=SessionArtifacts(f.root / "a3"), a2_runtime=f.runtime,
                page_observer=Observer(), crop_verifier=FakeVerifier(), cost_ledger=f.ledger)
            attach_execution(runtime, f.store, configuration_version="queue-cost-fixture-v1")
        runtime._execution_gate = _ExecutionGate(1, 2, 10)
        request1, request2 = f.request("first"), f.request("second")
        def execute(sid, request, progress=None):
            if kind == "a3":
                return runtime.handle_image(sid, image, operation_request=request, progress=progress)
            return runtime.handle_text(sid, sid, operation_request=request, progress=progress)
        def progress(stage, text):
            if stage == "queued":
                queued.set()
        with self.ledger_outage(), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(execute, "first", request1)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(execute, "second", request2, progress)
                self.assertTrue(queued.wait(5))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).execution_receipt["status"], "SUCCEEDED")
            self.assert_pending(lambda: second.result(timeout=10))
        self.assertEqual(len(calls), 1)
        self.assertIsNone(runtime.store.load("second"))
        operation = runtime.execution_operations.lookup("second", "local", request2)
        self.assertEqual(operation["status"], "REGISTERED")
        with f.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT status FROM execution_attempts WHERE operation_id=?",
                                          (operation["id"],)).fetchone()[0], "FAILED")
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_cost_runs WHERE operation_id=?",
                                          (operation["id"],)).fetchone()[0], 0)
        for outbox in f.rows("execution_cost_outbox"):
            reconcile_cost(runtime.execution_operations, f.ledger, outbox["run_id"])
        result = execute("second", request2)
        self.assertEqual(result.execution_receipt["operation_id"], operation["id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row["status"] for row in f.rows("execution_cost_outbox")], ["CONFIRMED"] * 2)

    def test_a2_queue_rechecks_and_keeps_unsent_operation_retryable(self):
        self.queue_scenario("a2")

    def test_a3_queue_rechecks_before_workflow_or_model_starts(self):
        self.queue_scenario("a3")

    def test_registered_operation_cannot_claim_after_other_runtime_leaves_pending_cost(self):
        f = self.fixture
        row = f.ops.register("held", "local", f.request("held"), "handle_text", {"text": "held"})
        independent = OperationStore(ExecutionStore(f.store.path))
        independent.configuration_version = f.ops.configuration_version
        with self.ledger_outage():
            f.runtime.handle_text("first", "success", operation_request=f.request("first"))
        self.assert_pending(lambda: independent.claim(row["id"]))
        self.assertEqual(len(f.rows("execution_attempts")), 1)

    def send_scenario(self, prepared):
        f = self.fixture
        row = f.ops.register("held", "local", f.request("held"), "handle_text", {})
        writer = f.ops.claim(row["id"])
        f.ops.bind_cost_run(writer, "held-run")
        # A different store connection sees accounting from the first runtime.
        independent = OperationStore(ExecutionStore(f.store.path))
        observer = ExecutionEffects(independent, writer)
        def prepare():
            observer.prepare_model(call_id="held-call", run_id="held-run", provider="fake",
                                   model="fake", call_type="test")
        if prepared:
            prepare()
        with self.ledger_outage():
            f.runtime.handle_text("first", "success", operation_request=f.request("first"))
        self.assert_pending(lambda: observer.model_sent("held-call") if prepared else prepare())
        rows = [r for r in f.rows("execution_effects") if r["operation_id"] == row["id"]]
        self.assertEqual([r["status"] for r in rows], ["PREPARED"] if prepared else [])
        self.assertEqual(f.calls, ["success"])

    def test_claimed_operation_rechecks_before_preparing_model(self):
        self.send_scenario(False)

    def test_prepared_call_rechecks_immediately_before_send(self):
        self.send_scenario(True)

    def test_closed_collector_is_pending_before_operation_finishes(self):
        f = self.fixture
        original_finish = f.ops.finish
        checked = []
        def finish(writer, result, **kwargs):
            # The provider replied without usage; collector is closed but the
            # operation has not yet moved from RUNNING to SUCCEEDED.
            self.assert_pending(f.ops.ensure_cost_available)
            checked.append(True)
            return original_finish(writer, result, **kwargs)
        with patch.object(f.ops, "finish", side_effect=finish):
            f.runtime.handle_text("s", "missing_usage", operation_request=f.request())
        self.assertEqual(checked, [True])

    def test_live_ledger_transaction_does_not_block_another_model(self):
        f = self.fixture
        entered, release = threading.Event(), threading.Event()
        original = ExecutionEffects.prepare_cost
        def prepare(observer, *args, **kwargs):
            original(observer, *args, **kwargs)
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("ledger boundary not released")
        with patch.object(ExecutionEffects, "prepare_cost", prepare), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(f.runtime.handle_text, "first", "first", operation_request=f.request("first"))
            try:
                self.assertTrue(entered.wait(5))
                # Outbox is pending while its live writer is about to commit
                # the ledger; this is not the fault injected in queue_scenario.
                self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "PENDING")
                self.assertEqual(f.rows("execution_collectors")[0]["closed"], 1)
                f.runtime.handle_text("second", "second", operation_request=f.request("second"))
            finally:
                release.set()
            first.result(timeout=10)
        self.assertEqual(f.calls, ["first", "second"])
        self.assertEqual([r["status"] for r in f.rows("execution_cost_outbox")], ["CONFIRMED"] * 2)


if __name__ == "__main__":
    unittest.main()

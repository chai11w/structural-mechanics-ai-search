"""Phase 6.2 kernel tests with private SQLite, real runtime and counted providers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.execution_dispatch import DispatchPolicy, DispatchStore
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionError, ExecutionSessionStore, ExecutionStore
from tiku_agent.execution_worker import BackgroundWorker
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call


class DispatchFixture:
    def __init__(self, root, *, policy=None):
        self.root = Path(root)
        self.clock = [time.time()]
        self.authority = ExecutionStore(self.root / "execution.db", now=lambda: self.clock[0])
        self.calls = []
        self.allowed = True
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.after_call = lambda: None
        self.ledger = SQLiteModelCostLedger(self.root / "costs.db")
        self.policy = policy or DispatchPolicy()
        self.runtime, self.dispatch = self.build()
        self.worker = BackgroundWorker(self.dispatch, self.root / "worker")

    def build(self):
        owner = self
        class Agent:
            config = None
            def __init__(self, state):
                self.state = state
            def handle_text(self, text):
                def provider():
                    owner.calls.append(text)
                    owner.entered.set()
                    if not owner.release.wait(10):
                        raise TimeoutError("test provider wait")
                    if text == "timeout":
                        raise TimeoutError("private timeout details")
                    return {"input_tokens": 10, "output_tokens": 3}
                def invoke():
                    timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                                     call_type="dispatch-test", usage_getter=lambda value: value)
                invoke()
                owner.after_call()
                if text == "twice":
                    invoke()
                return AgentResponse(text="reply:" + text, state=self.state.to_dict(), intent="greeting")
            def handle_image(self, path, **kwargs):
                self.state.start_search(str(path), search_id=uuid4().hex)
                return self.handle_text("image")
        runtime = AgentSessionRuntime(ExecutionSessionStore(self.authority),
            artifacts=SessionArtifacts(self.root / "media"), agent_factory=Agent, cost_ledger=self.ledger,
            max_concurrent_tasks=self.policy.max_concurrent, max_queued_tasks=self.policy.max_queued,
            queue_wait_seconds=self.policy.queue_seconds)
        attach_execution(runtime, self.authority, configuration_version="dispatch-fixture-v1")
        dispatch = DispatchStore(runtime, authorize=lambda identity, version: self.allowed and version == 1,
                                 policy=self.policy)
        return runtime, dispatch

    def request(self, sid="s"):
        ctx = self.authority.context(sid)
        return OperationRequest(uuid4().hex, ctx["epoch"], ctx["state_version"])

    def grant(self, sid="s", identity="invite"):
        self.authority.context(sid)
        return self.dispatch.create_grant(sid, identity, auth_version=1, expires_at=self.clock[0] + 3600)

    def accept(self, text="hello", sid="s", request=None, grant=None):
        request = request or self.request(sid)
        grant = grant or self.grant(sid)
        ack = self.dispatch.accept(sid, "invite", grant, request, "handle_text", {"text": text})
        return ack, request, grant

    def rows(self, table):
        with self.authority.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM " + table)]

    def observe(self, request, grant, sid="s", **kwargs):
        return self.dispatch.observe(sid, "invite", grant, request, **kwargs)


class ExecutionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = DispatchFixture(self.tmp.name)
        self.addCleanup(self.f.worker.close)
        self.addCleanup(self.f.release.set)

    def code(self, expected, action):
        with self.assertRaises(ExecutionError) as caught:
            action()
        self.assertEqual(caught.exception.code, expected)

    def test_accept_is_atomic_and_worker_rebuilds_without_submitter(self):
        f = self.f
        ack, request, grant = f.accept()
        self.assertEqual(ack["status"], "REGISTERED")
        self.assertEqual(f.calls, [])
        # Construct a fresh runtime: no request closure or in-memory task required.
        _, dispatch = f.build()
        worker = BackgroundWorker(dispatch, f.root / "worker2")
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        first = f.observe(request, grant, result=True)
        self.assertEqual(first["result"].text, "reply:hello")
        self.assertEqual(first["status"], "SUCCEEDED")
        before = f.rows("execution_operations")
        for _ in range(3):
            self.assertEqual(f.observe(request, grant, result=True)["operation_id"], ack["operation_id"])
        self.assertEqual(f.rows("execution_operations"), before)
        self.assertEqual(f.calls, ["hello"])
        self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT call_count,total_tokens FROM model_cost_runs").fetchone(), (1, 13))

    def test_transaction_failure_after_registration_returns_no_ack_or_orphan(self):
        f = self.f
        request, grant = f.request(), f.grant()
        with f.authority.transaction() as conn:
            conn.execute("CREATE TRIGGER reject_input BEFORE INSERT ON execution_dispatch_inputs BEGIN SELECT RAISE(ABORT,'fixture input failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            f.dispatch.accept("s", "invite", grant, request, "handle_text", {"text": "original"})
        for table in ("execution_operations", "execution_dispatch", "execution_dispatch_inputs"):
            self.assertEqual(f.rows(table), [])
        self.assertFalse(f.worker.run_once())
        self.assertEqual(f.calls, [])

    def test_racing_duplicate_submission_claim_and_new_explicit_key(self):
        f = self.f
        request, grant = f.request(), f.grant()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: f.accept(request=request, grant=grant)[0], range(4)))
        self.assertEqual(len({r["operation_id"] for r in results}), 1)
        self.code("EXECUTION_INPUT_CONFLICT", lambda: f.accept("changed", request=request, grant=grant))
        _, other = f.build()
        other_worker = BackgroundWorker(other, f.root / "other")
        f.release.clear()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(f.worker.run_once)
            try:
                self.assertTrue(f.entered.wait(5))
                self.assertFalse(other_worker.run_once())
                self.assertEqual(f.accept(request=request, grant=grant)[0]["queue_deadline"], results[0]["queue_deadline"])
            finally:
                f.release.set()
            self.assertTrue(first.result(5))
        self.assertEqual(len(f.rows("execution_attempts")), 1)
        f.accept(grant=grant)
        f.worker.run_once()
        self.assertEqual(f.calls, ["hello", "hello"])

    def test_queue_capacity_deadline_and_original_key_do_not_requeue(self):
        f = self.f
        accepted = [f.accept(sid=str(index)) for index in range(3)]
        self.code("EXECUTION_QUEUE_FULL", lambda: f.accept(sid="fourth"))
        f.clock[0] += 56
        self.assertFalse(f.worker.run_once())
        for index, (ack, req, grant) in enumerate(accepted):
            value = f.observe(req, grant, str(index))
            self.assertEqual((value["status"], value["error_code"]), ("FAILED", "EXECUTION_QUEUE_TIMEOUT"))
            self.assertEqual(f.accept(sid=str(index), request=req, grant=grant)[0]["queue_deadline"], ack["queue_deadline"])
        self.assertEqual(f.calls, [])
        self.assertEqual(f.rows("execution_attempts"), [])
        f.accept(sid="fourth")
        self.assertTrue(f.worker.run_once())

    def test_no_observer_needed_and_close_has_bounded_drain(self):
        f = self.f
        _, req, grant = f.accept()
        f.release.clear()
        f.worker.start()
        try:
            self.assertTrue(f.entered.wait(5))
            closed = f.worker.close(drain_seconds=0)
            self.assertFalse(closed["drained"])
            self.code("EXECUTION_SHUTTING_DOWN", lambda: f.accept(sid="other"))
        finally:
            f.release.set()
        self.assertTrue(f.worker.close(drain_seconds=5)["drained"])
        self.assertEqual(f.observe(req, grant, result=True)["result"].text, "reply:hello")
        self.assertEqual(f.calls, ["hello"])

    def test_revoke_disabled_identity_and_control_failure_before_claim(self):
        f = self.f
        _, req, grant = f.accept()
        f.dispatch.revoke_grant("s", "invite", grant)
        self.assertFalse(f.worker.run_once())
        self.assertEqual(f.rows("execution_dispatch")[0]["error_code"], "EXECUTION_AUTH_REQUIRED")
        self.code("EXECUTION_AUTH_REQUIRED", lambda: f.observe(req, grant))
        _, req2, grant2 = f.accept(sid="s2")
        with patch.object(f.dispatch, "authorize", side_effect=OSError("private control path")):
            self.assertFalse(f.worker.run_once())
        self.assertEqual(f.rows("execution_dispatch")[-1]["error_code"], "EXECUTION_AUTH_UNAVAILABLE")
        f.allowed = False
        self.code("EXECUTION_AUTH_REQUIRED", lambda: f.observe(req2, grant2, "s2"))
        self.assertEqual(f.calls, [])

    def test_running_revocation_blocks_next_send_but_preserves_first_charge(self):
        f = self.f
        _, req, grant = f.accept("twice")
        f.after_call = lambda: f.dispatch.revoke_grant("s", "invite", grant)
        f.worker.run_once()
        self.assertEqual(f.calls, ["twice"])
        self.assertEqual(f.rows("execution_operations")[0]["status"], "UNKNOWN")
        self.assertEqual(f.rows("execution_effects")[0]["status"], "CONFIRMED")
        self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")

    def test_send_rechecks_authorization_after_prepare(self):
        from tiku_agent.execution_effects import ExecutionEffects
        f = self.f
        _, req, grant = f.accept()
        original = ExecutionEffects.prepare_model
        def revoke_after_prepare(observer, **kwargs):
            original(observer, **kwargs)
            f.dispatch.revoke_grant("s", "invite", grant)
        with patch.object(ExecutionEffects, "prepare_model", revoke_after_prepare):
            f.worker.run_once()
        self.assertEqual(f.calls, [])
        self.assertNotEqual(f.rows("execution_operations")[0]["status"], "SUCCEEDED")

    def test_unknown_provider_failure_cannot_automatically_replay(self):
        f = self.f
        _, req, grant = f.accept("timeout")
        f.worker.run_once()
        self.assertEqual(f.observe(req, grant)["status"], "UNKNOWN")
        f.accept("timeout", request=req, grant=grant)
        _, dispatch = f.build()
        self.assertFalse(BackgroundWorker(dispatch, f.root / "restart").run_once())
        self.assertEqual(f.calls, ["timeout"])
        self.code("EXECUTION_COST_PENDING", lambda: f.accept(sid="other"))

    def test_claimed_lease_expiry_is_unknown_and_input_remains_protected(self):
        f = self.f
        _, req, grant = f.accept()
        self.assertIsNotNone(f.dispatch.claim_next())
        f.clock[0] += 301
        f.dispatch.maintain()
        self.assertEqual(f.observe(req, grant)["status"], "UNKNOWN")
        f.clock[0] += 2 * 86400
        f.dispatch.maintain()
        self.assertEqual(len(f.rows("execution_dispatch_inputs")), 1)
        self.assertFalse(f.worker.run_once())
        self.assertEqual(f.calls, [])

    def test_epoch_expiration_producer_drift_and_corrupt_payload_do_not_start(self):
        f = self.f
        f.accept(sid="one")
        f.accept(sid="two")
        f.accept(sid="three")
        with f.authority.transaction() as conn:
            conn.execute("UPDATE execution_dispatch_inputs SET payload='{}' WHERE session_id='one'")
            conn.execute("UPDATE execution_sessions SET expires=? WHERE session=(SELECT session FROM execution_operations WHERE id=(SELECT operation_id FROM execution_dispatch_inputs WHERE session_id='two'))", (f.clock[0] - 1,))
        f.runtime.execution_operations.configuration_version = lambda: "changed"
        self.assertFalse(f.worker.run_once())
        self.assertEqual(f.calls, [])
        self.assertTrue(all(row["status"] == "FAILED" for row in f.rows("execution_operations")))
        self.assertEqual(len(f.rows("execution_attempts")), 0)

    def test_observation_is_scoped_and_does_not_leak_private_input(self):
        f = self.f
        _, req, grant = f.accept("private text marker")
        other = f.grant("other", "other-invite")
        self.code("EXECUTION_AUTH_REQUIRED", lambda: f.dispatch.observe("other", "other-invite", grant, req))
        self.code("EXECUTION_STALE", lambda: f.dispatch.observe("other", "other-invite", other, req))
        view = json.dumps(f.observe(req, grant))
        for forbidden in ("private text marker", "payload", "image_hash", grant, str(f.root)):
            self.assertNotIn(forbidden, view)
        self.code("EXECUTION_BACKGROUND_REQUIRED", lambda: f.runtime.handle_text("s", "hello", operation_request=req))
        self.code("EXECUTION_BACKGROUND_REQUIRED", lambda: f.runtime.execution_operations.claim(f.rows("execution_operations")[0]["id"]))

    def test_private_image_validation_materialization_and_lifecycle(self):
        f = self.f
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="PNG")
        request, grant = f.request(), f.grant()
        self.code("EXECUTION_INPUT_INVALID", lambda: f.dispatch.accept("s", "invite", grant, request, "handle_image", {"image_path": "C:/private"}, image=image.getvalue()))
        self.code("EXECUTION_INPUT_INVALID", lambda: f.dispatch.accept("s", "invite", grant, request, "handle_image", {}, image=b"not-image"))
        f.dispatch.accept("s", "invite", grant, request, "handle_image", {}, image=image.getvalue())
        self.assertTrue(f.worker.run_once())
        self.assertEqual(f.observe(request, grant)["status"], "SUCCEEDED")
        self.assertEqual(f.calls, ["image"])
        self.assertEqual(list(f.worker.root.iterdir()), [])
        self.assertEqual(f.dispatch.maintain()["inputs_removed"], 0)
        f.clock[0] += 86401
        self.assertEqual(f.dispatch.maintain()["inputs_removed"], 1)

    def test_input_and_disk_capacity_fail_before_acceptance(self):
        f = self.f
        req, grant = f.request(), f.grant()
        self.code("EXECUTION_INPUT_TOO_LARGE", lambda: f.accept("x" * 32769, request=req, grant=grant))
        with patch.object(f.authority, "storage_capacity", side_effect=ExecutionError("EXECUTION_CAPACITY")):
            self.code("EXECUTION_CAPACITY", lambda: f.accept(request=req, grant=grant))
        self.assertEqual(f.rows("execution_operations"), [])
        self.assertEqual(f.rows("execution_dispatch_inputs"), [])

    def test_budget_changes_are_checked_again_after_queue(self):
        from tiku_agent.session_runtime import AgentBudgetExceededError
        f = self.f
        _, req, grant = f.accept()
        with patch.object(f.runtime, "ensure_budget_available", side_effect=AgentBudgetExceededError("quota", code="INVITE_DAILY_QUOTA_EXCEEDED")):
            self.assertFalse(f.worker.run_once())
        self.assertEqual(f.observe(req, grant)["error_code"], "INVITE_DAILY_QUOTA_EXCEEDED")
        self.assertEqual(f.calls, [])


if __name__ == "__main__":
    unittest.main()

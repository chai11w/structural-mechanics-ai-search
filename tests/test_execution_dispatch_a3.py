"""All five A3 commands go through durable acceptance and the real runtimes."""
from contextlib import closing
import io
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4

from PIL import Image

from tiku_agent.a3_runtime import A3MvpRuntime
from tiku_agent.agent import TikuSearchAgent
from tiku_agent.execution_dispatch import DispatchStore
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionSessionStore, ExecutionStore
from tiku_agent.execution_worker import BackgroundWorker
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.tools import ToolResult
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call
from tests.test_a3_runtime import FakeObserver, FakeVerifier, FakeAutoCropper
from tests.test_tiku_agent_session_runtime import FakeTools


class ExecutionDispatchA3Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ExecutionStore(self.root / "execution.db")
        self.ledger = SQLiteModelCostLedger(self.root / "costs.db")
        self.calls = []
        owner = self
        def paid(label):
            def invoke():
                owner.calls.append(label)
                return {"input_tokens": 10, "output_tokens": 3}
            timed_model_call(invoke, provider="dashscope", model="qwen3-vl-plus",
                             call_type="dispatch-a3", usage_getter=lambda value: value)
        class Observer(FakeObserver):
            def observe(self, image):
                paid("page")
                return super().observe(image)
        class Verifier(FakeVerifier):
            execution_version = "dispatch-verifier-v1"
            def verify(self, page, crop, selected, understanding):
                paid(selected["unit_id"])
                return super().verify(page, crop, selected, understanding)
        tools = FakeTools().toolbox()
        def analyze(*args, **kwargs):
            paid("child")
            return ToolResult(ok=True, data={"loads": [{"type": "集中", "raw": "P"}], "chapter_hint": "4力法"})
        tools.analyze_image = analyze
        self.a2 = AgentSessionRuntime(ExecutionSessionStore(self.store),
            artifacts=SessionArtifacts(self.root / "a2"), cost_ledger=self.ledger,
            agent_factory=lambda state: TikuSearchAgent(state=state, tools=tools, use_llm_intent=False))
        self.a3 = A3MvpRuntime(store=ExecutionSessionStore(self.store, "workflow"),
            artifacts=SessionArtifacts(self.root / "a3"), a2_runtime=self.a2,
            page_observer=Observer(), crop_verifier=Verifier(), cost_ledger=self.ledger,
            max_concurrent_tasks=1, max_queued_tasks=2, queue_wait_seconds=55)
        attach_execution(self.a3, self.store, configuration_version="dispatch-a3-v1")
        self.dispatch = DispatchStore(self.a3, authorize=lambda identity, version: identity == "invite" and version == 1)
        self.worker = BackgroundWorker(self.dispatch, self.root / "worker")
        self.store.context("s")
        self.grant = self.dispatch.create_grant("s", "invite", auth_version=1, expires_at=time.time() + 3600)
        image = io.BytesIO()
        Image.new("RGB", (1000, 800), "white").save(image, format="PNG")
        self.image = image.getvalue()

    def request(self):
        context = self.store.context("s")
        return OperationRequest(uuid4().hex, context["epoch"], context["state_version"])

    def run_command(self, kind, parameters, **kwargs):
        req = self.request()
        ack = self.dispatch.accept("s", "invite", self.grant, req, kind, parameters, **kwargs)
        self.assertEqual(ack["status"], "REGISTERED")
        self.assertTrue(self.worker.run_once())
        receipt = self.dispatch.observe("s", "invite", self.grant, req, result=True)
        self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
        before = list(self.calls)
        replay = self.dispatch.accept("s", "invite", self.grant, req, kind, parameters, **kwargs)
        self.assertEqual(replay["operation_id"], ack["operation_id"])
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.calls, before)
        return receipt

    def target(self):
        state = self.a3.store.load("s")
        return {"task_revision": state.task_revision, "workflow_search_id": state.workflow_search_id}

    def test_image_text_selection_manual_crop_and_child_share_one_attempt(self):
        self.run_command("handle_image", {}, image=self.image)
        self.assertEqual(self.a3.store.load("s").phase, "WAIT_UNIT_SELECTION")
        self.run_command("handle_text", {"text": "你好"})
        self.run_command("select_unit", {"unit_id": "g1-u1", **self.target()})
        self.assertEqual(self.a3.store.load("s").phase, "CROP_REQUIRED")
        result = self.run_command("handle_crop", {"bounds": {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
                                                  "unit_id": "g1-u1", **self.target()})
        self.assertEqual(self.a3.store.load("s").phase, "A2_ACTIVE")
        with self.store.transaction() as conn:
            handoff = conn.execute("SELECT * FROM execution_handoffs").fetchone()
            self.assertEqual(handoff["status"], "COMMITTED")
            self.assertEqual(handoff["operation_id"], result["operation_id"])
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_attempts WHERE operation_id=?", (result["operation_id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_effects WHERE operation_id=?", (result["operation_id"],)).fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_cost_outbox WHERE status<>'CONFIRMED'").fetchone()[0], 0)
        self.assertEqual(self.calls, ["page", "g1-u1", "child"])

    def test_multi_unit_prepare_persists_each_receipt_and_selection_reuses_check(self):
        self.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        self.run_command("handle_image", {}, image=self.image)
        result = self.run_command("prepare_units", {"unit_ids": ["g1-u1", "g1-u2"], **self.target()})
        self.assertEqual(self.a3.store.load("s").phase, "WAIT_UNIT_SELECTION")
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT operation_id,status FROM execution_unit_checks").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(tuple(row) == (result["operation_id"], "CONFIRMED") for row in rows))
        self.run_command("select_unit", {"unit_id": "g1-u1", **self.target()})
        self.assertEqual(self.a3.store.load("s").phase, "A2_ACTIVE")
        self.assertCountEqual(self.calls, ["page", "g1-u1", "g1-u2", "child"])
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (4, 52))


if __name__ == "__main__":
    unittest.main()

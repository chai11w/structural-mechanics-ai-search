from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from tiku_agent.a3_runtime import A3MvpRuntime
from tiku_agent.agent import TikuSearchAgent
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionStore, ExecutionSessionStore, _WRITER
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.tools import ToolResult
from tiku_shared.atomic_files import atomic_output
from tiku_shared.execution_hooks import execution_effect_scope
from tiku_agent.execution_effects import ExecutionEffects
from tests.test_a3_runtime import FakeObserver, FakeVerifier
from tests.test_tiku_agent_session_runtime import FakeTools


class ExecutionHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ExecutionStore(self.root / "execution.db")
        self.image = self.root / "page.jpg"
        Image.new("RGB", (100, 100), "white").save(self.image)
        tools = FakeTools().toolbox()
        self.analysis_count = 0
        def analyze(*args, **kwargs):
            self.analysis_count += 1
            return ToolResult(ok=True, data={"loads": [{"type":"集中", "raw":"P"}], "chapter_hint":"4力法"})
        tools.analyze_image = analyze
        self.a2 = AgentSessionRuntime(ExecutionSessionStore(self.store), artifacts=SessionArtifacts(self.root / "a2"),
            agent_factory=lambda state:TikuSearchAgent(state=state, tools=tools, use_llm_intent=False))
        self.a3 = A3MvpRuntime(store=ExecutionSessionStore(self.store, "workflow"), artifacts=SessionArtifacts(self.root / "a3"),
            a2_runtime=self.a2, page_observer=FakeObserver(), crop_verifier=FakeVerifier())
        attach_execution(self.a3, self.store)

    def request(self):
        context = self.store.context("s")
        return OperationRequest(uuid4().hex, context["epoch"], context["state_version"])

    def prepare(self):
        self.a3.handle_image("s", self.image, operation_request=self.request())
        self.a3.select_unit("s", "g1-u1", operation_request=self.request())

    def crop(self):
        return self.a3.handle_crop("s", {"x":0, "y":0, "width":1, "height":1}, unit_id="g1-u1", operation_request=self.request())

    def rows(self, table):
        with self.store.transaction() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def test_child_result_and_parent_ack_have_exact_persistent_relationship(self):
        self.prepare()
        self.crop()
        handoff = self.rows("execution_handoffs")[0]
        self.assertEqual(handoff["status"], "COMMITTED")
        self.assertEqual(handoff["unit_id"], "g1-u1")
        child = next(row for row in self.rows("execution_tasks") if row["id"] == handoff["child_record_id"])
        self.assertEqual(child["parent_id"], handoff["parent_record_id"])
        self.assertEqual(child["state_version"], handoff["child_version"])
        self.assertEqual(self.analysis_count, 1)

    def test_crash_after_child_preserves_response_and_does_not_fake_parent_finish(self):
        self.prepare()
        with patch.object(self.a3, "_after_a2_response", side_effect=RuntimeError("before parent finish")):
            with self.assertRaisesRegex(RuntimeError, "before parent finish"):
                self.crop()
        handoff = self.rows("execution_handoffs")[0]
        self.assertEqual(handoff["status"], "CHILD_READY")
        self.assertIn("response", handoff["result"])
        self.assertIsNotNone(self.a2.store.load("s"))
        self.assertEqual(self.a3.store.load("s").phase, "A2_ACTIVE")
        operation = next(row for row in self.rows("execution_operations") if row["id"] == handoff["operation_id"])
        self.assertEqual(operation["status"], "UNKNOWN")
        self.assertEqual(self.analysis_count, 1)

    def test_child_receipt_failure_rolls_back_child_state_in_same_transaction(self):
        self.prepare()
        with patch("tiku_agent.session_runtime.save_child_result", side_effect=RuntimeError("receipt write failure")):
            with self.assertRaisesRegex(RuntimeError, "receipt write failure"):
                self.crop()
        self.assertIsNone(self.a2.store.load("s"))
        self.assertEqual(self.rows("execution_handoffs"), [])
        self.assertFalse(any(row["kind"] == "child" for row in self.rows("execution_tasks")))
        self.assertTrue(self.rows("execution_files"))

    def test_parent_write_failure_rolls_back_parent_and_keeps_child_receipt(self):
        self.prepare()
        original = self.a3.store.save
        observed = []
        def fail_after_parent_write(state):
            child = self.a2.store.load("s")
            if child is not None:
                observed.append(self.store.read("s", "workflow")[1].version)
                original(state)
                raise RuntimeError("parent transaction failed")
            return original(state)
        with patch.object(self.a3.store, "save", side_effect=fail_after_parent_write):
            with self.assertRaisesRegex(RuntimeError, "parent transaction failed"):
                self.crop()
        self.assertEqual(self.store.read("s", "workflow")[1].version, observed[0])
        self.assertEqual(self.rows("execution_handoffs")[0]["status"], "CHILD_READY")
        self.assertIsNotNone(self.a2.store.load("s"))

    def test_atomic_artifact_never_publishes_partial_data(self):
        target = self.root / "result.jpg"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with atomic_output(target) as temporary:
                temporary.write_bytes(b"partial")
                self.assertFalse(target.exists())
                raise RuntimeError("injected")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".part-*")), [])
        with atomic_output(target) as temporary:
            temporary.write_bytes(b"complete")
            self.assertFalse(target.exists())
        self.assertEqual(target.read_bytes(), b"complete")
        with self.assertRaises(FileExistsError):
            with atomic_output(target):
                pass

    def test_new_answer_copy_does_not_delete_an_earlier_operation_result(self):
        from tiku_agent.tools import AgentToolConfig, answer_candidate_tool
        source = self.root / "answer.txt"
        config = AgentToolConfig(session_dir=self.root / "answers")
        outputs = []
        for value in (b"first answer", b"changed answer"):
            source.write_bytes(value)
            ops = self.a3.execution_operations
            row = ops.register("s", "local", self.request(), "answer_copy", {})
            writer = ops.claim(row["id"])
            token = _WRITER.set(writer)
            try:
                with execution_effect_scope(ExecutionEffects(ops, writer, artifact_roots=[config.session_dir])):
                    with patch("tiku_agent.tools.search.find_answer_files", return_value=[source]):
                        result = answer_candidate_tool([{"rank":1,"path":str(self.image)}], rank=1, config=config)
                        self.assertTrue(result.ok, result)
                        outputs.append(Path(result.data["copied_paths"][0]))
                ops.finish(writer, {"schema":1,"response":None})
            finally:
                _WRITER.reset(token)
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0].read_bytes(), b"first answer")
        self.assertEqual(outputs[1].read_bytes(), b"changed answer")


if __name__ == "__main__":
    unittest.main()

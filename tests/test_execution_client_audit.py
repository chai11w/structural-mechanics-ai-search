"""Exercise actual model adapters against a fake transport and durable ledger."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

import search
from multi_agent_pipeline import QwenClassifier
from scripts.classify_question_bank import qwen_locate_diagram, qwen_verify_diagram
from tiku_agent.a3_intent_v1 import call_qwen_a3_intent_v1
from tiku_agent.a3_models import QwenA3CropVerifier, QwenA3PageObserver
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionStore, ExecutionSessionStore
from tiku_agent.external_load_screen import QwenExternalLoadScreen, ZhipuExternalLoadScreen
from tiku_agent.glm_vision import call_glm_json
from tiku_agent.image_triage import QwenImageTriage
from tiku_agent.image_triage_authority import QwenTriageReplyClient
from tiku_agent.intent_v2 import call_qwen_decision_v2
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentProtocolError, AgentSessionRuntime
from tiku_shared.model_costs import SQLiteModelCostLedger
from tests.test_external_load_screen import FakeResponse


class ExecutionClientAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / "input.png"
        Image.new("RGB", (100, 100), "white").save(self.image)

    def clients(self):
        image = self.image
        classifier = QwenClassifier(use_cache=False, cache_path=self.root / "unused-cache.json")
        handoff = SimpleNamespace(route="A1", reason=["test"], observation=SimpleNamespace(unknowns=[], raw_text="test"))
        request = SimpleNamespace(prompt=SimpleNamespace(system_prompt="test", user_prompt="test"),
                                  temperature=0.0, max_tokens=120, timeout_seconds=1)
        options = {"model": "test-model", "endpoint": "https://example.invalid", "api_key": "test-key", "timeout": 1}
        return {
            "page_understanding": (lambda: QwenA3PageObserver(api_key="test-key").observe(image), 2),
            "crop_comparison": (lambda: QwenA3CropVerifier(api_key="test-key").verify(image, image, {"unit_id": "u1"}, {}), 1),
            "glm_grounding_transport": (lambda: call_glm_json([image], prompt="test", api_key="test-key"), 1),
            "image_triage": (lambda: QwenImageTriage(api_key="test-key").observe(image), 1),
            "triage_reply": (lambda: QwenTriageReplyClient(api_key="test-key")(handoff), 1),
            "qwen_external_load": (lambda: QwenExternalLoadScreen(api_key="test-key")(image), 1),
            "zhipu_external_load": (lambda: ZhipuExternalLoadScreen()(image), 1),
            "safe_answer": (lambda: QwenSafeAnswerClientV0()(request), 1),
            "a3_intent": (lambda: call_qwen_a3_intent_v1("test"), 1),
            "a2_intent": (lambda: call_qwen_decision_v2("test"), 1),
            "load_classification": (lambda: classifier.classify_image(image), 1),
            "layout": (lambda: classifier.analyze_layout(image), 1),
            "image_scope": (lambda: classifier.analyze_image_scope(image), 1),
            "structure_type": (lambda: classifier.classify_structure_type(image), 1),
            "dimensions": (lambda: classifier.recognize_dimensions(image, "刚架"), 1),
            "qwen_rerank": (lambda: search.score_candidate_pair(None, image, image, provider="qwen"), 1),
            "diagram_location": (lambda: qwen_locate_diagram(image, question_label="1", **options), 1),
            "diagram_verification": (lambda: qwen_verify_diagram(image, question_label="1", expected_loads=[], chapter_hint="4力法", **options), 1),
        }

    def exercise(self, name, callback, confirmed_count, *, unknown):
        root = self.root / name
        root.mkdir()
        store = ExecutionStore(root / "execution.db")
        ledger = SQLiteModelCostLedger(root / "costs.db")
        class Agent:
            config = None
            def __init__(self, state):
                self.state = state
            def handle_text(self, _text):
                try:
                    callback()
                except Exception:
                    pass  # A caller's fallback cannot erase the durable evidence.
                return AgentResponse(text="adapter finished", state=self.state.to_dict(), intent="greeting")
        runtime = AgentSessionRuntime(ExecutionSessionStore(store), artifacts=SessionArtifacts(root / "media"),
                                      agent_factory=Agent, cost_ledger=ledger)
        attach_execution(runtime, store, configuration_version={"fixture": "client-audit-v1", "client": name})
        context = store.context("s")
        request = OperationRequest(uuid4().hex, context["epoch"], context["state_version"])
        response = FakeResponse({"id": "fake-response", "usage": {"prompt_tokens": 12, "completion_tokens": 1},
                                 "choices": [{"message": {"content": "not-json"}}]})
        with (
            patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key", "ZHIPUAI_API_KEY": "test-key"}),
            patch.object(search, "DASHSCOPE_API_KEY", "test-key"),
            patch("urllib.request.urlopen", side_effect=TimeoutError("fake timeout") if unknown else None,
                  return_value=response) as send,
            patch("time.sleep") as sleep,
        ):
            for _ in range(2):
                if unknown:
                    with self.assertRaises(AgentProtocolError) as error:
                        runtime.handle_text("s", "run", operation_request=request)
                    self.assertEqual(error.exception.code, "EXECUTION_UNKNOWN")
                else:
                    result = runtime.handle_text("s", "run", operation_request=request)
            self.assertEqual(send.call_count, 1 if unknown else confirmed_count)
            sleep.assert_not_called()
        with store.transaction() as conn:
            effects = conn.execute("SELECT status,usage_known,call_id FROM execution_effects").fetchall()
            self.assertEqual(len(effects), 1 if unknown else confirmed_count)
            self.assertEqual(len({row["call_id"] for row in effects}), len(effects))
            self.assertTrue(all(row["status"] == ("UNKNOWN" if unknown else "CONFIRMED") for row in effects))
            if not unknown:
                self.assertTrue(all(row["usage_known"] for row in effects))
                self.assertTrue(result.execution_receipt["replayed"])
        count, tokens = 0, 0
        if ledger.path.is_file():
            with closing(sqlite3.connect(ledger.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='model_cost_calls'").fetchone():
                    count, tokens = conn.execute("SELECT count(*),coalesce(sum(total_tokens),0) FROM model_cost_calls").fetchone()
        self.assertEqual((count, tokens), (0, 0) if unknown else (confirmed_count, 13 * confirmed_count))
        if unknown:
            accounting = runtime.execution_operations.observe("s", "local", request)["accounting"]
            self.assertGreater(accounting["pending_runs"], 0)  # Unbooked does not mean free.

    def test_each_adapter_stops_after_unknown_transport_without_implicit_retry(self):
        for name, (callback, count) in self.clients().items():
            with self.subTest(client=name):
                self.exercise(name, callback, count, unknown=True)

    def test_each_confirmed_response_retains_usage_despite_local_schema_failure(self):
        for name, (callback, count) in self.clients().items():
            with self.subTest(client=name):
                self.exercise(name, callback, count, unknown=False)

    def test_installed_zhipu_sdk_honors_zero_transport_retries(self):
        import httpx
        from zhipuai import APITimeoutError
        from tiku_shared.execution_hooks import execution_effect_scope, bounded_transport_retries
        requests = []
        def timeout(request):
            requests.append(request.url.path)
            raise httpx.ReadTimeout("fake timeout", request=request)
        with execution_effect_scope(object()):
            retries = bounded_transport_retries(3)
        with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
            client = search.ZhipuAI(api_key="test-key", http_client=http, max_retries=retries,
                                    base_url="https://example.invalid", timeout=1)
            with patch("time.sleep") as sleep, self.assertRaises(APITimeoutError):
                client.chat.completions.create(model="test-model", messages=[{"role": "user", "content": "test"}])
            sleep.assert_not_called()
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()

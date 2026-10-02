"""Exercise production clarification/fixed-reply handlers under durable effects."""
import json
import unittest
from unittest.mock import patch

from tests import test_execution_effects as fixtures
from tests.test_a3_intent_v1 import _context
from tests.test_image_triage_authority import FakeObserver
from tiku_agent.agent import AgentResponse
from tiku_agent.a3_intent_v1 import A3IntentEngineV1
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.image_triage_authority import ImageTriageAuthority
from tiku_agent.intent_v2 import decide_intent_v2
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.execution_store import ExecutionPolicy
from tiku_shared.model_costs import timed_model_call
from tiku_agent.tools import analyze_multi_image_tool, classify_structure_tool


class ReplyFallbackTests(unittest.TestCase):
    def scenario(self, call_type, action, expected):
        f = fixtures.ExecutionEffectsTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.store.policy = ExecutionPolicy()
        calls = []
        def client(*args):
            def send():
                calls.append(call_type)
                raise TimeoutError("controlled reply outage")
            return timed_model_call(send, provider="dashscope", model="fixture", call_type=call_type,
                                    usage_getter=lambda value: value)
        seen = []
        class Agent(f.runtime.agent_factory):
            def handle_text(self, text):
                result = action(client)
                seen.append(result)
                return AgentResponse(text="handled fallback", state=self.state.to_dict(), intent="clarification")
        f.runtime.agent_factory = Agent
        request = f.request()
        result = f.runtime.handle_text("s", "text", operation_request=request)
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        expected(seen[0])
        again = f.runtime.handle_text("s", "text", operation_request=request)
        self.assertTrue(again.execution_receipt["replayed"])
        self.assertEqual(calls, [call_type])
        effect = f.rows("execution_effects")[0]
        self.assertEqual(effect["status"], "UNKNOWN")
        self.assertEqual(effect["usage_known"], 0)
        receipt = json.loads(f.rows("execution_operations")[0]["result"])
        self.assertIn(effect["call_id"], receipt["model_fallbacks"])

    def test_a2_intent_model_failure_returns_clarification(self):
        self.scenario("qwen_intent_decision", lambda client: decide_intent_v2(
            "这个到底该怎么弄", ConversationContextV2(phase="IDLE"), llm_client=client),
            lambda value: self.assertEqual(value.action, "clarification"))

    def test_a3_intent_model_failure_returns_clarification(self):
        self.scenario("qwen_a3_intent_decision", lambda client: A3IntentEngineV1(client).decide(
            "这个到底该怎么弄", _context()), lambda value: self.assertEqual(value.action, "clarification"))

    def test_safe_answer_timeout_returns_fixed_reply(self):
        self.scenario("qwen_safe_answer", lambda client: SafeAnswerGeneratorV0(client).generate("你是谁"),
                      lambda value: self.assertEqual(value.source, "fixed_fallback"))

    def test_image_triage_reply_failure_retains_known_route(self):
        self.scenario("qwen_image_triage_reply", lambda client: ImageTriageAuthority(
            FakeObserver("A1"), client).decide_for_full_flow("fixture.jpg"),
            lambda value: (self.assertEqual(value.handoff.route, "A1"),
                           self.assertEqual(value.reply_source, "fixed_fallback")))

    def test_structure_failure_skips_optional_filter(self):
        def action(client):
            with patch("tiku_agent.tools._make_qwen") as factory:
                factory.return_value.classify_structure_type.side_effect = client
                return classify_structure_tool("fixture.jpg", route="main")
        self.scenario("qwen_structure_type", action,
                      lambda value: self.assertEqual(value.code, "STRUCTURE_CLASSIFICATION_FALLBACK"))

    def test_legacy_multi_detection_failure_retains_single_question_route(self):
        def action(client):
            with patch("tiku_agent.tools._make_qwen") as factory:
                factory.return_value.analyze_image_scope.side_effect = client
                return analyze_multi_image_tool("fixture.jpg")
        self.scenario("qwen_image_scope", action,
                      lambda value: self.assertEqual(value.code, "MULTI_DETECTION_FALLBACK"))


if __name__ == "__main__":
    unittest.main()

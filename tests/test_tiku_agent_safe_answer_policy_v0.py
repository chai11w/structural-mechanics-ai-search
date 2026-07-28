from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import unittest

from tiku_agent.agent import TikuSearchAgent
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.safe_answer_policy_v0 import evaluate_safe_answer_policy


FIXTURE = Path(__file__).parent / "fixtures" / "safe_answer_v0_cases.json"


class SafeAnswerPolicyV0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_reviewed_contract_has_unique_reusable_cases(self):
        self.assertEqual(self.suite["schema_version"], 1)
        self.assertEqual(self.suite["status"], "reviewed_contract")
        cases = self.suite["cases"]
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertEqual(len({case["text"] for case in cases}), len(cases))

        contexts = self.suite["contexts"]
        for name, payload in contexts.items():
            with self.subTest(context=name):
                ConversationContextV2.from_mapping(payload)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["context"], contexts)
                self.assertEqual(
                    case["expected"]["eligible"],
                    case["expected"]["route"] == "safe_answer",
                )

    def test_all_reviewed_cases_match_the_policy(self):
        contexts = self.suite["contexts"]
        for case in self.suite["cases"]:
            with self.subTest(case=case["id"]):
                context = deepcopy(contexts[case["context"]])
                before = deepcopy(context)
                actual = evaluate_safe_answer_policy(case["text"], context)
                self.assertEqual(asdict(actual), case["expected"])
                self.assertEqual(context, before)

    def test_decisions_are_deterministic(self):
        contexts = self.suite["contexts"]
        for case in self.suite["cases"]:
            with self.subTest(case=case["id"]):
                first = evaluate_safe_answer_policy(case["text"], contexts[case["context"]])
                second = evaluate_safe_answer_policy(case["text"], contexts[case["context"]])
                self.assertEqual(first, second)

    def test_empty_text_falls_back_without_becoming_eligible(self):
        for text in (None, "", "   "):
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.route, "existing_fallback")

    def test_boundary_clear_conversation_does_not_need_an_allowlisted_phrase(self):
        cases = (
            ("感谢", "courtesy"),
            ("拜拜", "farewell"),
            ("算了我要走了", "farewell"),
            ("今天状态怎么样", "general"),
            ("随便说点什么", "general"),
            ("下次再聊", "farewell"),
        )
        for text, category in cases:
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertTrue(decision.eligible)
                self.assertEqual(decision.category, category)
                self.assertEqual(decision.route, "safe_answer")

    def test_explicit_business_cancellation_stays_in_existing_orchestrator(self):
        for text in ("取消当前任务", "不搜了", "停止搜索", "算了，不用继续搜了"):
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertFalse(decision.eligible)
                self.assertIn(
                    decision.route,
                    {"existing_intent", "existing_orchestrator"},
                )

    def test_natural_agent_meta_questions_do_not_require_exact_templates(self):
        cases = (
            ("你有什么作用", "capability"),
            ("你能干嘛？", "capability"),
            ("这个助手主要是做什么的", "capability"),
            ("你的核心能力有哪些", "capability"),
            ("你和其他agent区别", "identity"),
            ("你跟别的机器人有什么不同？", "identity"),
            ("力答是个什么助手", "identity"),
            ("你具体如何工作", "workflow"),
            ("这个助手的搜题原理是什么", "workflow"),
        )
        for text, category in cases:
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertTrue(decision.eligible)
                self.assertEqual(decision.category, category)
                self.assertEqual(decision.route, "safe_answer")

    def test_broader_meta_detection_never_steals_business_or_mixed_requests(self):
        cases = (
            "介绍你自己并帮我搜第4章",
            "说说你的作用，再把候选2发给我",
            "你和其他agent区别，顺便继续搜索",
            "这个助手怎么工作，把答案发给我",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertFalse(decision.eligible)
                self.assertIn(
                    decision.route,
                    {"existing_intent", "existing_orchestrator"},
                )

    def test_agent_integration_is_default_disabled(self):
        agent = TikuSearchAgent(use_llm_intent=False)
        self.assertFalse(agent.enable_safe_answer_v0)


if __name__ == "__main__":
    unittest.main()

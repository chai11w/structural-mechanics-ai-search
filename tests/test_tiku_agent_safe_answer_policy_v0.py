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

    def test_unknown_and_empty_text_fall_back_without_becoming_eligible(self):
        for text in (None, "", "   ", "随便说点什么"):
            with self.subTest(text=text):
                decision = evaluate_safe_answer_policy(text)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.route, "existing_fallback")

    def test_agent_integration_is_default_disabled(self):
        agent = TikuSearchAgent(use_llm_intent=False)
        self.assertFalse(agent.enable_safe_answer_v0)


if __name__ == "__main__":
    unittest.main()

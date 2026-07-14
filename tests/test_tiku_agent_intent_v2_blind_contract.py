import unittest
from pathlib import Path

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import authorize_action_v2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_eval_v2 import load_gold_suite
from tiku_agent.reply_shell_v2 import is_reply_shell_action, render_reply_shell_v2


FIXTURES = Path(__file__).parent / "fixtures"
BLIND_PATH = FIXTURES / "intent_v2_blind_01.json"
DEV_PATHS = (
    FIXTURES / "intent_v2_gold_review_01.json",
    FIXTURES / "intent_v2_gold_review_02.json",
)


class IntentV2BlindContractTest(unittest.TestCase):
    def test_holdout_is_frozen_with_twenty_unseen_valid_cases(self):
        blind = load_gold_suite(BLIND_PATH)
        self.assertEqual(blind["status"], "frozen_holdout_before_first_run")
        self.assertEqual(len(blind["cases"]), 20)

        development_cases = [
            case for path in DEV_PATHS for case in load_gold_suite(path)["cases"]
        ]
        development_ids = {case["id"] for case in development_cases}
        development_texts = {case["input"].get("text", "") for case in development_cases}
        self.assertFalse(development_ids & {case["id"] for case in blind["cases"]})
        self.assertFalse(development_texts & {case["input"].get("text", "") for case in blind["cases"]})

        for case in blind["cases"]:
            with self.subTest(case=case["id"]):
                context = ConversationContextV2.from_mapping(case["context"])
                decision = ActionDecisionV2.from_dict(case["expected_decision"])
                authorization = authorize_action_v2(decision, context.to_decision_context())
                self.assertTrue(authorization.allowed, authorization.code)

    def test_reply_constraints_are_executable_without_state_changes(self):
        blind = load_gold_suite(BLIND_PATH)
        constrained = [case for case in blind["cases"] if case.get("reply_constraints")]
        self.assertEqual(len(constrained), 6)
        for case in constrained:
            with self.subTest(case=case["id"]):
                context = ConversationContextV2.from_mapping(case["context"])
                before = context.to_prompt_payload()
                decision = ActionDecisionV2.from_dict(case["expected_decision"])
                self.assertTrue(is_reply_shell_action(decision.action))
                reply = render_reply_shell_v2(decision, context)
                constraints = case["reply_constraints"]
                self.assertLessEqual(len(reply), constraints["max_chars"])
                if constraints["single_line"]:
                    self.assertNotIn("\n", reply)
                self.assertIn(constraints["contains"], reply)
                self.assertEqual(context.to_prompt_payload(), before)


if __name__ == "__main__":
    unittest.main()

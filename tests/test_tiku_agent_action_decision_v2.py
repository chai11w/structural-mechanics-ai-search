import unittest

from tiku_agent.action_decision_v2 import (
    ACTIONS,
    ACTION_DECISION_VERSION,
    CONVERSATION_ACTIONS,
    ActionDecisionV2,
)


class ActionDecisionV2Test(unittest.TestCase):
    def test_protocol_contains_search_conversation_and_safety_actions(self):
        self.assertEqual(
            ACTIONS,
            frozenset(
                {
                "search_image",
                "set_chapter",
                "select_question",
                "select_candidate",
                "resend_answer",
                "explain_failure",
                "retry_search",
                "cancel",
                "greeting",
                "small_talk",
                "capability_help",
                "out_of_scope",
                "clarification",
                "reject",
                }
            ),
        )

    def test_question_and_candidate_namespaces_cannot_be_mixed(self):
        with self.assertRaisesRegex(ValueError, "candidate_rank"):
            ActionDecisionV2(
                action="select_question",
                question_index=2,
                candidate_rank=1,
            )
        with self.assertRaisesRegex(ValueError, "question_index"):
            ActionDecisionV2(
                action="select_candidate",
                question_index=2,
                candidate_rank=1,
            )

    def test_selection_actions_require_their_own_identifier(self):
        with self.assertRaisesRegex(ValueError, "question_index"):
            ActionDecisionV2(action="select_question")
        with self.assertRaisesRegex(ValueError, "candidate_rank"):
            ActionDecisionV2(action="select_candidate")

    def test_select_question_can_carry_chapter_override_as_one_action(self):
        decision = ActionDecisionV2(
            action="select_question",
            question_index=2,
            chapter_override="4力法",
            confidence=0.92,
            source="context_llm",
        )
        self.assertEqual(decision.question_index, 2)
        self.assertEqual(decision.chapter_override, "4力法")
        self.assertIsNone(decision.candidate_rank)

    def test_set_chapter_requires_chapter_override(self):
        with self.assertRaisesRegex(ValueError, "chapter_override"):
            ActionDecisionV2(action="set_chapter")
        decision = ActionDecisionV2(action="set_chapter", chapter_override="3静定结构位移")
        self.assertEqual(decision.chapter_override, "3静定结构位移")

    def test_clarification_is_an_action_not_a_duplicate_boolean(self):
        decision = ActionDecisionV2(
            action="clarification",
            clarification_reason="ambiguous_number_namespace",
        )
        self.assertEqual(decision.action, "clarification")
        with self.assertRaises(TypeError):
            ActionDecisionV2.from_dict(
                {
                    "action": "clarification",
                    "needs_clarification": True,
                    "clarification_reason": "ambiguous_reference",
                }
            )

    def test_conversation_actions_cannot_carry_business_parameters(self):
        for action in CONVERSATION_ACTIONS - {"clarification"}:
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "question_index"):
                    ActionDecisionV2(action=action, question_index=1)

    def test_reject_records_boundary_but_write_actions_are_not_executable(self):
        decision = ActionDecisionV2(action="reject", requested_action="delete")
        self.assertEqual(decision.requested_action, "delete")
        for forbidden in ("delete", "store", "repair", "cross_chapter_search"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, ACTIONS)

    def test_confidence_is_bounded_and_does_not_change_schema(self):
        with self.assertRaisesRegex(ValueError, "confidence"):
            ActionDecisionV2(action="greeting", confidence=1.1)
        with self.assertRaisesRegex(ValueError, "confidence"):
            ActionDecisionV2(action="greeting", confidence=True)

    def test_strict_round_trip_and_protocol_version(self):
        original = ActionDecisionV2(
            action="select_candidate",
            candidate_rank=2,
            confidence=0.8,
            reason="用户明确选择第二个候选",
            source="rule",
        )
        restored = ActionDecisionV2.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.protocol_version, ACTION_DECISION_VERSION)
        with self.assertRaisesRegex(ValueError, "version"):
            ActionDecisionV2(action="greeting", protocol_version="3.0")


if __name__ == "__main__":
    unittest.main()

import unittest

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.reply_shell_v2 import MAX_REPLY_CHARS, is_reply_shell_action, render_reply_shell_v2


class ReplyShellV2Test(unittest.TestCase):
    def setUp(self):
        self.idle = ConversationContextV2(phase="IDLE")
        self.answered = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            has_active_image=True,
            has_answer=True,
        )

    def test_professional_shell_replies_are_short_single_line_and_domain_bound(self):
        decisions = [
            ActionDecisionV2(action="greeting"),
            ActionDecisionV2(action="small_talk"),
            ActionDecisionV2(action="capability_help"),
            ActionDecisionV2(action="out_of_scope"),
            ActionDecisionV2(action="clarification", clarification_reason="ambiguous_reference"),
            ActionDecisionV2(action="reject", requested_action="delete"),
        ]
        for decision in decisions:
            with self.subTest(action=decision.action):
                reply = render_reply_shell_v2(decision, self.answered)
                self.assertLessEqual(len(reply), MAX_REPLY_CHARS)
                self.assertNotIn("\n", reply)
                self.assertNotIn("ActionDecision", reply)
                self.assertNotIn("confidence", reply)

    def test_greeting_uses_current_context_without_repeating_full_introduction(self):
        self.assertEqual(
            render_reply_shell_v2(ActionDecisionV2(action="greeting"), self.idle),
            "在的，发一张结构力学题图就可以。",
        )
        answered_reply = render_reply_shell_v2(ActionDecisionV2(action="greeting"), self.answered)
        self.assertIn("继续", answered_reply)
        self.assertNotIn("我是", answered_reply)

    def test_every_clarification_reason_asks_only_for_missing_information(self):
        expectations = {
            "ambiguous_reference": "哪一道题",
            "ambiguous_number_namespace": "题号",
            "ambiguous_action": "你想",
            "missing_question_index": "第几题",
            "missing_candidate_rank": "第几个候选",
            "missing_chapter": "哪一章",
            "missing_image": "题图",
            "out_of_range": "超出",
        }
        for reason, phrase in expectations.items():
            with self.subTest(reason=reason):
                decision = ActionDecisionV2(action="clarification", clarification_reason=reason)
                self.assertIn(phrase, render_reply_shell_v2(decision, self.answered))

    def test_rejections_explain_boundary_without_executing_or_inviting_free_chat(self):
        for requested_action in ("delete", "store", "repair", "cross_chapter_search"):
            with self.subTest(requested_action=requested_action):
                decision = ActionDecisionV2(action="reject", requested_action=requested_action)
                reply = render_reply_shell_v2(decision, self.answered)
                self.assertTrue("不能" in reply or "需" in reply)
                self.assertNotIn("我来帮你", reply)

    def test_task_actions_are_left_to_existing_v1_business_renderers(self):
        self.assertFalse(is_reply_shell_action("select_candidate"))
        with self.assertRaisesRegex(ValueError, "does not render task action"):
            render_reply_shell_v2(
                ActionDecisionV2(action="select_candidate", candidate_rank=1),
                self.answered,
            )

    def test_rendering_does_not_mutate_context(self):
        before = self.answered.to_prompt_payload()
        render_reply_shell_v2(ActionDecisionV2(action="small_talk"), self.answered)
        self.assertEqual(self.answered.to_prompt_payload(), before)


if __name__ == "__main__":
    unittest.main()

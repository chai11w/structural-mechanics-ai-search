import unittest

from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.state import AgentState


class ConversationContextV2Test(unittest.TestCase):
    def test_agent_state_is_reduced_to_sanitized_semantic_summary(self):
        state = AgentState(
            phase="ANSWERED",
            current_image_path=r"F:\private\question.jpg",
            current_chapter="4力法",
            questions=[{"image_path": r"F:\private\crop1.jpg"}, {"secret": "do not expose"}],
            selected_question=2,
            candidates=[{"rank": 1, "path": r"F:\bank\q1.jpg", "score": 0.9}],
            selected_rank=1,
            last_answer_paths=[r"F:\private\answer.jpg"],
            last_error="token=secret internal failure",
        )
        context = ConversationContextV2.from_agent_state(
            state,
            active_namespace="candidate",
            completed_question_indexes=(1,),
            previous_question_index=1,
            recent_actions=("select_question", "select_candidate"),
        )
        payload = context.to_prompt_payload()
        rendered = repr(payload)
        self.assertEqual(context.remaining_question_indexes, (2,))
        self.assertTrue(payload["has_answer"])
        self.assertTrue(payload["has_explainable_failure"])
        self.assertNotIn("F:\\", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("score", rendered)

    def test_pending_chapter_defaults_to_one_shot_next_image_scope(self):
        context = ConversationContextV2.from_mapping(
            {"phase": "ANSWERED", "pending_chapter": "4力法"}
        )
        self.assertEqual(context.pending_chapter_scope, "next_image")

    def test_question_progress_must_be_in_range_and_disjoint(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            ConversationContextV2(
                phase="ANSWERED",
                question_count=2,
                completed_question_indexes=(1,),
                remaining_question_indexes=(1, 2),
            )
        with self.assertRaisesRegex(ValueError, "question_count"):
            ConversationContextV2(
                phase="ANSWERED",
                question_count=2,
                remaining_question_indexes=(3,),
            )

    def test_permission_context_keeps_only_authorization_fields(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=2,
            candidate_count=3,
            selected_question_index=2,
            selected_candidate_rank=1,
            has_active_image=True,
            has_answer=True,
        )
        permission_context = context.to_decision_context()
        self.assertEqual(permission_context.active_namespace, "candidate")
        self.assertEqual(permission_context.candidate_count, 3)
        self.assertFalse(hasattr(permission_context, "selected_question_index"))


if __name__ == "__main__":
    unittest.main()

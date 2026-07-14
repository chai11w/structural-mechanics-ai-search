import unittest

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import (
    NAMESPACE_CANDIDATE,
    NAMESPACE_NONE,
    NAMESPACE_QUESTION,
    OUTCOME_ALLOW,
    OUTCOME_CLARIFY,
    OUTCOME_REJECT,
    STATE_PRESERVE,
    STATE_REPLACE_TASK,
    STATE_STORE_PENDING_CHAPTER,
    TOOLS_FIXED_SEARCH_PIPELINE,
    TOOLS_NONE,
    ActionAuthorizationV2,
    DecisionContextV2,
    authorize_action_v2,
    resolve_bare_index_namespace,
)


class ActionPermissionsV2Test(unittest.TestCase):
    def test_bare_number_follows_active_candidate_namespace(self):
        context = DecisionContextV2(
            phase="ANSWERED",
            active_namespace=NAMESPACE_CANDIDATE,
            question_count=2,
            candidate_count=3,
            has_answer=True,
        )
        result = resolve_bare_index_namespace(2, context)
        self.assertEqual(result.outcome, OUTCOME_ALLOW)
        self.assertEqual(result.namespace, NAMESPACE_CANDIDATE)

    def test_bare_number_follows_active_question_namespace(self):
        context = DecisionContextV2(
            phase="WAIT_QUESTION_CHOICE",
            active_namespace=NAMESPACE_QUESTION,
            question_count=3,
            candidate_count=2,
        )
        result = resolve_bare_index_namespace(2, context)
        self.assertEqual(result.namespace, NAMESPACE_QUESTION)

    def test_bare_number_without_focus_clarifies_only_when_both_are_valid(self):
        ambiguous = resolve_bare_index_namespace(
            2,
            DecisionContextV2(
                phase="ANSWERED",
                active_namespace=NAMESPACE_NONE,
                question_count=2,
                candidate_count=2,
            ),
        )
        question_only = resolve_bare_index_namespace(
            2,
            DecisionContextV2(
                phase="ANSWERED",
                active_namespace=NAMESPACE_NONE,
                question_count=2,
                candidate_count=1,
            ),
        )
        self.assertEqual(ambiguous.outcome, OUTCOME_CLARIFY)
        self.assertEqual(question_only.namespace, NAMESPACE_QUESTION)

    def test_out_of_range_active_namespace_does_not_silently_switch(self):
        context = DecisionContextV2(
            phase="ANSWERED",
            active_namespace=NAMESPACE_CANDIDATE,
            question_count=2,
            candidate_count=1,
        )
        result = resolve_bare_index_namespace(2, context)
        self.assertEqual(result.outcome, OUTCOME_CLARIFY)
        self.assertIsNone(result.namespace)

    def test_conversation_shell_is_zero_tool_and_state_preserving_in_every_phase(self):
        for phase in (
            "IDLE",
            "PROCESSING",
            "WAIT_CHAPTER",
            "WAIT_QUESTION_CHOICE",
            "READY_TO_ROUTE",
            "READY_FOR_SEARCH",
            "WAIT_CANDIDATE_CHOICE",
            "ANSWERED",
            "ERROR",
            "NO_MATCH",
            "CANCELLED",
        ):
            for action in ("greeting", "small_talk", "capability_help", "out_of_scope"):
                with self.subTest(phase=phase, action=action):
                    result = authorize_action_v2(ActionDecisionV2(action=action), DecisionContextV2(phase=phase))
                    self.assertTrue(result.allowed)
                    self.assertEqual(result.state_effect, STATE_PRESERVE)
                    self.assertEqual(result.tool_effect, TOOLS_NONE)

    def test_new_trusted_image_replaces_task_and_accepts_chapter_override(self):
        decision = ActionDecisionV2(
            action="search_image",
            chapter_override="4力法",
            source="entry",
        )
        result = authorize_action_v2(
            decision,
            DecisionContextV2(
                phase="ANSWERED",
                active_namespace=NAMESPACE_CANDIDATE,
                question_count=2,
                candidate_count=2,
                has_answer=True,
                trusted_image_event=True,
            ),
        )
        self.assertEqual(result.outcome, OUTCOME_ALLOW)
        self.assertEqual(result.state_effect, STATE_REPLACE_TASK)
        self.assertEqual(result.tool_effect, TOOLS_FIXED_SEARCH_PIPELINE)

    def test_model_cannot_invent_image_event(self):
        result = authorize_action_v2(
            ActionDecisionV2(action="search_image", source="context_llm"),
            DecisionContextV2(phase="IDLE", trusted_image_event=False),
        )
        self.assertEqual(result.outcome, OUTCOME_CLARIFY)
        self.assertEqual(result.code, "trusted_image_required")

    def test_next_image_chapter_is_stored_without_tools(self):
        decision = ActionDecisionV2(
            action="set_chapter",
            chapter_override="4力法",
            chapter_target="next_image",
        )
        result = authorize_action_v2(decision, DecisionContextV2(phase="ANSWERED"))
        self.assertEqual(result.outcome, OUTCOME_ALLOW)
        self.assertEqual(result.state_effect, STATE_STORE_PENDING_CHAPTER)
        self.assertEqual(result.tool_effect, TOOLS_NONE)

    def test_current_chapter_requires_active_question_image(self):
        decision = ActionDecisionV2(
            action="set_chapter",
            chapter_override="4力法",
            chapter_target="current_question",
        )
        missing = authorize_action_v2(decision, DecisionContextV2(phase="IDLE"))
        available = authorize_action_v2(
            decision,
            DecisionContextV2(phase="WAIT_CANDIDATE_CHOICE", has_active_image=True),
        )
        self.assertEqual(missing.outcome, OUTCOME_CLARIFY)
        self.assertEqual(available.outcome, OUTCOME_ALLOW)

    def test_select_question_and_candidate_have_separate_phase_and_bounds(self):
        question = authorize_action_v2(
            ActionDecisionV2(action="select_question", question_index=2),
            DecisionContextV2(
                phase="WAIT_CANDIDATE_CHOICE",
                active_namespace=NAMESPACE_CANDIDATE,
                question_count=2,
                candidate_count=3,
            ),
        )
        candidate = authorize_action_v2(
            ActionDecisionV2(action="select_candidate", candidate_rank=4),
            DecisionContextV2(
                phase="WAIT_CANDIDATE_CHOICE",
                active_namespace=NAMESPACE_CANDIDATE,
                question_count=2,
                candidate_count=3,
            ),
        )
        self.assertTrue(question.allowed)
        self.assertEqual(candidate.outcome, OUTCOME_CLARIFY)
        self.assertEqual(candidate.code, "candidate_rank_out_of_range")

    def test_retry_requires_retryable_error_and_saved_image(self):
        decision = ActionDecisionV2(action="retry_search")
        denied = authorize_action_v2(decision, DecisionContextV2(phase="ERROR"))
        allowed = authorize_action_v2(
            decision,
            DecisionContextV2(
                phase="ERROR",
                retryable_error=True,
                has_active_image=True,
            ),
        )
        self.assertEqual(denied.outcome, OUTCOME_CLARIFY)
        self.assertTrue(allowed.allowed)

    def test_task_action_is_rejected_while_internal_pipeline_is_busy(self):
        result = authorize_action_v2(
            ActionDecisionV2(action="select_candidate", candidate_rank=1),
            DecisionContextV2(
                phase="PROCESSING",
                active_namespace=NAMESPACE_CANDIDATE,
                candidate_count=1,
            ),
        )
        self.assertEqual(result.outcome, OUTCOME_REJECT)
        self.assertEqual(result.code, "agent_busy")


if __name__ == "__main__":
    unittest.main()

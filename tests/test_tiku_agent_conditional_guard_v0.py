"""Gold tests for code-verifiable conditional business requests."""

import unittest

from tiku_agent.conditional_guard_v0 import (
    CONDITION_SATISFIED,
    CONDITION_UNKNOWN,
    CONDITION_UNSATISFIED,
    assess_conditional_request,
)
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_v2 import decide_intent_v2
from tiku_agent.shadow_plan_v0 import (
    PermissionReviewFacts,
    ShadowPlan,
    ShadowPlanStep,
    ShadowPlannerResult,
)
from tiku_agent.shadow_semantic_gate_v0 import review_shadow_plan_semantics


def _planner_result(*steps: ShadowPlanStep) -> ShadowPlannerResult:
    return ShadowPlannerResult(
        rewritten_text="测试条件请求",
        keywords=(),
        reason="测试",
        plan=ShadowPlan(goal="测试条件保护", steps=steps),
    )


class ConditionalAssessmentTest(unittest.TestCase):
    def test_code_verifiable_conditions_use_current_state(self):
        retry = assess_conditional_request(
            "如果还能重试，就再试一次",
            phase="ERROR",
            retryable_error=True,
        )
        more = assess_conditional_request(
            "如果还有更多候选就继续搜，否则停下",
            phase="WAIT_CANDIDATE_CHOICE",
            continuation_available=False,
        )
        no_match = assess_conditional_request(
            "如果确实没有匹配题，就解释一下原因",
            phase="NO_MATCH",
        )
        self.assertEqual(retry.outcome, CONDITION_SATISFIED)
        self.assertEqual(more.outcome, CONDITION_UNSATISFIED)
        self.assertEqual(no_match.outcome, CONDITION_SATISFIED)

    def test_user_judgement_conditions_remain_unknown(self):
        for text in (
            "如果候选1不对，就选择候选2",
            "如果这个答案不匹配，就回到候选",
            "如果这题适合力法，就按力法搜",
            "如果第二题是目标，就选择第二题",
        ):
            with self.subTest(text=text):
                result = assess_conditional_request(text, phase="WAIT_CANDIDATE_CHOICE")
                self.assertEqual(result.outcome, CONDITION_UNKNOWN)
                self.assertTrue(result.blocks_execution)

    def test_polite_hedge_is_not_a_business_precondition(self):
        for text in ("方便的话继续搜", "如果方便，继续搜", "可以的话把答案再发一次"):
            with self.subTest(text=text):
                result = assess_conditional_request(text, phase="WAIT_CANDIDATE_CHOICE")
                self.assertEqual(result.outcome, CONDITION_SATISFIED)
                self.assertFalse(result.blocks_execution)

        unresolved = assess_conditional_request(
            "如果方便全局搜，就直接搜",
            phase="WAIT_CHAPTER",
        )
        self.assertEqual(unresolved.outcome, CONDITION_UNKNOWN)


class ConditionalIntentGuardTest(unittest.TestCase):
    def test_unknown_conditions_never_reach_rules_or_intent_model_as_current_authorization(self):
        contexts = (
            (
                "如果候选1不对，就选择候选2",
                ConversationContextV2(
                    phase="WAIT_CANDIDATE_CHOICE",
                    active_namespace="candidate",
                    candidate_count=3,
                    has_active_image=True,
                    continuation_available=True,
                ),
            ),
            (
                "如果这题适合力法，就按力法搜",
                ConversationContextV2(phase="WAIT_CHAPTER", has_active_image=True),
            ),
            (
                "如果第二题是目标，就选择第二题",
                ConversationContextV2(
                    phase="WAIT_QUESTION_CHOICE",
                    active_namespace="question",
                    question_count=3,
                    has_active_image=True,
                ),
            ),
        )
        for text, context in contexts:
            with self.subTest(text=text):
                decision = decide_intent_v2(
                    text,
                    context,
                    llm_client=lambda _prompt: self.fail("unresolved condition reached model"),
                )
                self.assertEqual(decision.action, "clarification")
                self.assertEqual(decision.clarification_reason, "ambiguous_action")

    def test_satisfied_code_condition_keeps_existing_fast_path(self):
        retry = decide_intent_v2(
            "如果还能重试，就再试一次",
            ConversationContextV2(
                phase="ERROR",
                has_active_image=True,
                has_explainable_failure=True,
                retryable_error=True,
            ),
        )
        more = decide_intent_v2(
            "如果还有更多候选就继续搜",
            ConversationContextV2(
                phase="WAIT_CANDIDATE_CHOICE",
                active_namespace="candidate",
                candidate_count=3,
                has_active_image=True,
                continuation_available=True,
            ),
        )
        self.assertEqual(retry.action, "retry_search")
        self.assertEqual(more.action, "continue_search")

    def test_unsatisfied_known_condition_does_not_execute(self):
        decision = decide_intent_v2(
            "如果还有更多候选就继续搜",
            ConversationContextV2(
                phase="WAIT_CANDIDATE_CHOICE",
                active_namespace="candidate",
                candidate_count=3,
                has_active_image=True,
                continuation_available=False,
            ),
        )
        self.assertEqual(decision.action, "clarification")
        self.assertEqual(decision.clarification_reason, "no_more_candidates")


class ConditionalSemanticGuardTest(unittest.TestCase):
    def test_unknown_condition_cannot_authorize_planner_step(self):
        facts = PermissionReviewFacts(
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            continuation_available=True,
        )
        result = review_shadow_plan_semantics(
            "如果候选1不对，就选择候选2",
            _planner_result(ShadowPlanStep("select_candidate", {"candidate_rank": 2})),
            facts,
        )
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(result.evidence[0].code, "condition_unresolved")

    def test_satisfied_condition_can_authorize_explicit_planner_step(self):
        facts = PermissionReviewFacts(
            phase="ERROR",
            has_active_image=True,
            retryable_error=True,
        )
        result = review_shadow_plan_semantics(
            "如果还能重试，就再试一次",
            _planner_result(ShadowPlanStep("retry_search")),
            facts,
        )
        self.assertFalse(result.requires_confirmation)

    def test_unsatisfied_condition_cannot_authorize_planner_step(self):
        facts = PermissionReviewFacts(
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            continuation_available=False,
        )
        result = review_shadow_plan_semantics(
            "如果还有更多候选就继续搜",
            _planner_result(ShadowPlanStep("continue_search")),
            facts,
        )
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(result.evidence[0].code, "condition_not_met")


if __name__ == "__main__":
    unittest.main()

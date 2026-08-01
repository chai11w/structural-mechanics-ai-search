"""Tests for the code-only Stage 5 semantic authorization gate."""

import unittest

from tiku_agent.shadow_plan_v0 import (
    PLAN_ACTION_UNIVERSE,
    PermissionReviewFacts,
    ShadowPlan,
    ShadowPlanStep,
    ShadowPlannerResult,
)
from tiku_agent.shadow_semantic_gate_v0 import (
    REVIEW_NEEDS_CONFIRMATION,
    review_shadow_plan_semantics,
)
from tiku_agent.state import STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER


def _result(*steps: ShadowPlanStep, keywords=()) -> ShadowPlannerResult:
    return ShadowPlannerResult(
        rewritten_text="补充后的请求",
        keywords=tuple(keywords),
        reason="测试",
        plan=ShadowPlan(goal="测试目标", steps=steps),
    )


def _candidate_facts(count: int = 3) -> PermissionReviewFacts:
    return PermissionReviewFacts(
        phase=STATE_WAIT_CANDIDATE_CHOICE,
        candidate_count=count,
        continuation_available=True,
    )


class ShadowSemanticGateTest(unittest.TestCase):
    def test_explicit_samples_cover_and_authorize_every_planner_action(self):
        samples = {
            "set_chapter": (
                "按力法搜",
                ShadowPlanStep("set_chapter", {"chapter_override": "4力法"}),
                PermissionReviewFacts(phase=STATE_WAIT_CHAPTER, has_active_image=True),
            ),
            "select_question": (
                "选第二题",
                ShadowPlanStep("select_question", {"question_index": 2}),
                PermissionReviewFacts(phase="WAIT_QUESTION_CHOICE", question_count=3),
            ),
            "select_candidate": (
                "选择候选2",
                ShadowPlanStep("select_candidate", {"candidate_rank": 2}),
                _candidate_facts(),
            ),
            "continue_search": ("继续搜", ShadowPlanStep("continue_search"), _candidate_facts()),
            "reject_candidates": (
                "这一批候选都不合适",
                ShadowPlanStep("reject_candidates"),
                _candidate_facts(),
            ),
            "show_candidates": ("再看候选", ShadowPlanStep("show_candidates"), _candidate_facts()),
            "global_search": (
                "全局搜",
                ShadowPlanStep("global_search"),
                PermissionReviewFacts(
                    phase=STATE_WAIT_CHAPTER,
                    has_active_image=True,
                    global_search_offered=True,
                ),
            ),
            "resend_answer": (
                "答案再发一下",
                ShadowPlanStep("resend_answer"),
                PermissionReviewFacts(phase="ANSWERED", has_answer=True),
            ),
            "report_answer_mismatch": (
                "这个答案不对",
                ShadowPlanStep("report_answer_mismatch"),
                PermissionReviewFacts(phase="ANSWERED", has_answer=True),
            ),
            "retry_search": (
                "重试",
                ShadowPlanStep("retry_search"),
                PermissionReviewFacts(phase="ERROR", retryable_error=True),
            ),
            "explain_failure": (
                "为什么失败",
                ShadowPlanStep("explain_failure"),
                PermissionReviewFacts(phase="ERROR", has_explainable_failure=True),
            ),
        }
        self.assertEqual(set(samples), set(PLAN_ACTION_UNIVERSE))
        for action, (text, step, facts) in samples.items():
            with self.subTest(action=action):
                result = review_shadow_plan_semantics(text, _result(step), facts)
                self.assertFalse(result.requires_confirmation, result.evidence)

    def test_explicit_action_words_inside_a_refusal_never_grant_permission(self):
        samples = (
            (
                "不要选第二题",
                ShadowPlanStep("select_question", {"question_index": 2}),
                PermissionReviewFacts(phase="WAIT_QUESTION_CHOICE", question_count=3),
            ),
            (
                "不要选择候选2",
                ShadowPlanStep("select_candidate", {"candidate_rank": 2}),
                _candidate_facts(),
            ),
            (
                "不要按力法搜",
                ShadowPlanStep("set_chapter", {"chapter_override": "4力法"}),
                PermissionReviewFacts(phase=STATE_WAIT_CHAPTER, has_active_image=True),
            ),
            (
                "不要全局搜",
                ShadowPlanStep("global_search"),
                PermissionReviewFacts(
                    phase=STATE_WAIT_CHAPTER,
                    has_active_image=True,
                    global_search_offered=True,
                ),
            ),
            ("先不要继续搜", ShadowPlanStep("continue_search"), _candidate_facts()),
            ("不用重试", ShadowPlanStep("retry_search"), PermissionReviewFacts(phase="ERROR")),
            (
                "这个答案不是不对",
                ShadowPlanStep("report_answer_mismatch"),
                PermissionReviewFacts(phase="ANSWERED", has_answer=True),
            ),
        )
        for text, step, facts in samples:
            with self.subTest(text=text):
                result = review_shadow_plan_semantics(text, _result(step), facts)
                self.assertTrue(result.requires_confirmation, result.evidence)

    def test_explicit_question_selection_is_authorized(self):
        result = review_shadow_plan_semantics(
            "就选第二题",
            _result(ShadowPlanStep("select_question", {"question_index": 2})),
            PermissionReviewFacts(phase="WAIT_QUESTION_CHOICE", question_count=3),
        )
        self.assertFalse(result.requires_confirmation)
        self.assertTrue(result.review.allowed)
        self.assertEqual(result.evidence[0].matched_text, ("第二题",))

    def test_question_mentioned_without_selection_is_not_authorization(self):
        result = review_shadow_plan_semantics(
            "第二题靠谱吗",
            _result(ShadowPlanStep("select_question", {"question_index": 2})),
            PermissionReviewFacts(phase="WAIT_QUESTION_CHOICE", question_count=3),
        )
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(result.review.outcome, REVIEW_NEEDS_CONFIRMATION)

        asks_how = review_shadow_plan_semantics(
            "第二题怎么看",
            _result(ShadowPlanStep("select_question", {"question_index": 2})),
            PermissionReviewFacts(phase="WAIT_QUESTION_CHOICE", question_count=3),
        )
        self.assertTrue(asks_how.requires_confirmation)

    def test_explicit_candidate_selection_is_authorized(self):
        result = review_shadow_plan_semantics(
            "选择候选3",
            _result(ShadowPlanStep("select_candidate", {"candidate_rank": 3})),
            _candidate_facts(),
        )
        self.assertFalse(result.requires_confirmation)

    def test_ambiguous_candidate_reference_needs_confirmation(self):
        for text in ("就这样吧", "你说哪个靠谱", "别的那个也行", "这个你帮我看看"):
            with self.subTest(text=text):
                result = review_shadow_plan_semantics(
                    text,
                    _result(ShadowPlanStep("select_candidate", {"candidate_rank": 2})),
                    _candidate_facts(),
                )
                self.assertTrue(result.requires_confirmation)

    def test_single_candidate_confirmation_is_code_verifiable(self):
        result = review_shadow_plan_semantics(
            "是",
            _result(ShadowPlanStep("select_candidate", {"candidate_rank": 1})),
            _candidate_facts(count=1),
        )
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(result.evidence[0].code, "unique_candidate_confirmation")

    def test_explicit_continue_search_is_authorized_but_question_is_not(self):
        explicit = review_shadow_plan_semantics(
            "继续搜下一批",
            _result(ShadowPlanStep("continue_search")),
            _candidate_facts(),
        )
        vague = review_shadow_plan_semantics(
            "有没有更接近的",
            _result(ShadowPlanStep("continue_search")),
            _candidate_facts(),
        )
        self.assertFalse(explicit.requires_confirmation)
        self.assertTrue(vague.requires_confirmation)

    def test_answer_mismatch_question_is_not_a_report(self):
        assertion = review_shadow_plan_semantics(
            "这个答案不对",
            _result(ShadowPlanStep("report_answer_mismatch")),
            PermissionReviewFacts(phase="ANSWERED", candidate_count=3, has_answer=True),
        )
        question = review_shadow_plan_semantics(
            "刚才那个是不是不对",
            _result(ShadowPlanStep("report_answer_mismatch")),
            PermissionReviewFacts(phase="ANSWERED", candidate_count=3, has_answer=True),
        )
        self.assertFalse(assertion.requires_confirmation)
        self.assertTrue(question.requires_confirmation)

    def test_global_search_requires_explicit_scope_or_offered_consent(self):
        facts = PermissionReviewFacts(
            phase=STATE_WAIT_CHAPTER,
            has_active_image=True,
            global_search_offered=True,
        )
        explicit = review_shadow_plan_semantics(
            "可以全局搜",
            _result(ShadowPlanStep("global_search")),
            facts,
        )
        consent = review_shadow_plan_semantics(
            "可以",
            _result(ShadowPlanStep("global_search")),
            facts,
        )
        vague = review_shadow_plan_semantics(
            "就这样吧",
            _result(ShadowPlanStep("global_search")),
            facts,
        )
        self.assertFalse(explicit.requires_confirmation)
        self.assertFalse(consent.requires_confirmation)
        self.assertTrue(vague.requires_confirmation)

    def test_chapter_must_be_named_as_an_action(self):
        facts = PermissionReviewFacts(phase=STATE_WAIT_CHAPTER, has_active_image=True)
        explicit = review_shadow_plan_semantics(
            "按力法搜",
            _result(ShadowPlanStep("set_chapter", {"chapter_override": "4力法"})),
            facts,
        )
        question = review_shadow_plan_semantics(
            "这是不是力法",
            _result(ShadowPlanStep("set_chapter", {"chapter_override": "4力法"})),
            facts,
        )
        self.assertFalse(explicit.requires_confirmation)
        self.assertTrue(question.requires_confirmation)

        wrong_substring = review_shadow_plan_semantics(
            "按静定结构位移搜",
            _result(ShadowPlanStep("set_chapter", {"chapter_override": "2静定结构"})),
            facts,
        )
        self.assertTrue(wrong_substring.requires_confirmation)

    def test_every_step_in_multi_step_plan_needs_evidence(self):
        result = review_shadow_plan_semantics(
            "继续搜",
            _result(
                ShadowPlanStep("continue_search"),
                ShadowPlanStep("select_candidate", {"candidate_rank": 2}),
            ),
            _candidate_facts(),
        )
        self.assertTrue(result.requires_confirmation)
        self.assertTrue(result.evidence[0].authorized)
        self.assertFalse(result.evidence[1].authorized)

    def test_rewrite_keywords_are_split_without_granting_permission(self):
        result = review_shadow_plan_semantics(
            "别的那个也行",
            _result(
                ShadowPlanStep("select_candidate", {"candidate_rank": 2}),
                keywords=("别的", "候选2", "选择"),
            ),
            _candidate_facts(),
        )
        self.assertEqual(result.explicit_keywords, ("别的",))
        self.assertEqual(result.inferred_keywords, ("候选2", "选择"))
        self.assertEqual(result.confidence, 0.0)
        self.assertTrue(result.requires_confirmation)

    def test_unplannable_plan_does_not_invent_confirmation(self):
        planner_result = ShadowPlannerResult(
            rewritten_text="用户不想继续",
            keywords=("放弃",),
            reason="测试",
            plan=ShadowPlan(goal="停止", steps=(), source="unplannable"),
        )
        result = review_shadow_plan_semantics("就这样吧", planner_result, _candidate_facts())
        self.assertFalse(result.requires_confirmation)
        self.assertEqual(result.review.code, "unplannable")


if __name__ == "__main__":
    unittest.main()

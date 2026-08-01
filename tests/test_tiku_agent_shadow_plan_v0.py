"""Pure-function tests for the Stage 5 shadow-plan contract."""

import unittest

from tiku_agent.action_permissions_v2 import OUTCOME_REJECT
from tiku_agent.shadow_plan_v0 import (
    ACTION_TOOL_COST,
    MAX_PLAN_STEPS,
    MAX_TOOLS_PER_PLAN,
    PLAN_ACTION_UNIVERSE,
    REVIEW_ALLOW,
    REVIEW_REJECT,
    PermissionReviewFacts,
    ShadowPlan,
    ShadowPlanStep,
    build_permission_review_facts,
    review_shadow_plan,
)
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    AgentState,
)


def _candidate_state() -> AgentState:
    """A state mid-candidate-selection with 3 candidates on the current batch."""
    state = AgentState(
        phase=STATE_WAIT_CANDIDATE_CHOICE,
        current_image_path="input/q1.jpg",
        current_question_image_path="crop/q1.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
        current_chapter="4力法",
        task_revision=2,
        candidate_revision=1,
        candidate_generation="2:1",
    )
    state.candidates = [{"rank": 1}, {"rank": 2}, {"rank": 3}]
    return state


def _chapter_state() -> AgentState:
    """A WAIT_CHAPTER state with an active image and a global-search offer."""
    state = AgentState(
        phase=STATE_WAIT_CHAPTER,
        current_image_path="input/q1.jpg",
        current_question_image_path="crop/q1.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
        current_chapter="",
        task_revision=1,
    )
    state.offer_global_search()
    return state


def _plan(*steps: ShadowPlanStep, goal: str = "选候选看答案") -> ShadowPlan:
    return ShadowPlan(goal=goal, steps=steps)


def _step(action: str, **params) -> ShadowPlanStep:
    return ShadowPlanStep(action=action, params=params)


class PermissionReviewFactsTest(unittest.TestCase):
    def test_build_facts_from_state(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        self.assertEqual(facts.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(facts.current_chapter, "4力法")
        self.assertEqual(facts.task_revision, 2)
        self.assertEqual(facts.candidate_generation, "2:1")
        self.assertEqual(facts.question_count, 0)
        self.assertEqual(facts.candidate_count, 3)
        self.assertTrue(facts.has_active_image)
        self.assertFalse(facts.has_answer)

    def test_facts_derive_global_search_offer_and_continuation(self) -> None:
        facts = build_permission_review_facts(_chapter_state())
        self.assertTrue(facts.global_search_offered)
        self.assertFalse(facts.continuation_available)

    def test_facts_validation_rejects_unknown_phase(self) -> None:
        with self.assertRaises(ValueError):
            PermissionReviewFacts(phase="NOT_A_PHASE")

    def test_facts_validation_rejects_unsupported_chapter(self) -> None:
        with self.assertRaises(ValueError):
            PermissionReviewFacts(phase=STATE_WAIT_CHAPTER, current_chapter="9不存在")


class ShadowPlanDataTest(unittest.TestCase):
    def test_plan_step_rejects_action_outside_universe(self) -> None:
        with self.assertRaises(ValueError):
            ShadowPlanStep(action="store")
        with self.assertRaises(ValueError):
            ShadowPlanStep(action="cancel")

    def test_plan_rejects_empty_steps_unless_unplannable(self) -> None:
        with self.assertRaises(ValueError):
            ShadowPlan(goal="g", steps=())
        unplannable = ShadowPlan(goal="g", steps=(), source="unplannable")
        self.assertEqual(unplannable.steps, ())

    def test_plan_rejects_too_many_steps(self) -> None:
        with self.assertRaises(ValueError):
            ShadowPlan(
                goal="g",
                steps=tuple(_step("show_candidates") for _ in range(MAX_PLAN_STEPS + 1)),
            )

    def test_plan_rejects_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            ShadowPlan(goal="g", steps=(_step("show_candidates"),), source="hacker")


class ReviewShadowPlanTest(unittest.TestCase):
    def test_legal_candidate_plan_is_allowed(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(_step("select_candidate", candidate_rank=2))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)
        self.assertTrue(review.allowed)
        self.assertEqual(review.violations, ())

    def test_legal_global_search_plan_is_allowed_only_when_offered(self) -> None:
        facts = build_permission_review_facts(_chapter_state())
        plan = _plan(_step("global_search"))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)

        no_offer = build_permission_review_facts(
            AgentState(
                phase=STATE_WAIT_CHAPTER,
                current_image_path="input/q1.jpg",
                current_question_image_path="crop/q1.jpg",
                current_loads=[{"type": "集中", "raw": "P"}],
                task_revision=1,
            )
        )
        review = review_shadow_plan(plan, no_offer)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "global_search_not_offered")

    def test_forbidden_write_action_is_rejected(self) -> None:
        # The step universe already excludes write actions, so ``forbidden_write``
        # is a defensive check that fires when the review facts are narrowed to
        # a smaller read-only set (e.g. a stricter future policy).
        facts = PermissionReviewFacts(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            candidate_count=3,
            read_only_actions=frozenset({"show_candidates"}),
        )
        plan = _plan(_step("select_candidate", candidate_rank=1))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "forbidden_write")
        self.assertIn("forbidden_write", review.violations)

    def test_stale_task_revision_is_rejected(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(
            _step(
                "select_candidate",
                candidate_rank=1,
                task_revision=1,  # current is 2
            )
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "stale_task_revision")
        self.assertIn("stale_task_revision", review.violations)

    def test_stale_candidate_batch_is_rejected(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(
            _step(
                "select_candidate",
                candidate_rank=1,
                candidate_generation="1:0",  # current is 2:1
            )
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "stale_candidate_batch")

    def test_invalid_chapter_is_rejected(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(
            _step(
                "select_candidate",
                candidate_rank=1,
                chapter="9不存在",
            )
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "invalid_chapter")

    def test_candidate_rank_out_of_range_is_rejected(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(_step("select_candidate", candidate_rank=5))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        # The matrix maps out-of-range to a clarify outcome, never allow.
        self.assertIn(review.code, {"candidate_rank_out_of_range", "candidate_list_required"})

    def test_plan_too_long_is_rejected(self) -> None:
        # Construction already caps a plan at MAX_PLAN_STEPS, so the review's
        # budget check is exercised by narrowing the facts budget below that.
        facts = PermissionReviewFacts(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            candidate_count=3,
            max_steps=2,
        )
        plan = _plan(
            _step("show_candidates"),
            _step("show_candidates"),
            _step("show_candidates"),
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "plan_too_long")
        self.assertIn("plan_too_long", review.violations)

    def test_missing_step_parameters_is_rejected(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(_step("select_candidate"))  # missing candidate_rank
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "invalid_step_parameters")

    def test_reject_records_human_reason(self) -> None:
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(
            _step(
                "select_candidate",
                candidate_rank=1,
                task_revision=1,
            )
        )
        review = review_shadow_plan(plan, facts)
        self.assertIn("过期", review.reason)

    def test_no_match_explain_failure_plan_is_allowed(self) -> None:
        # Real-Qwen matrix exposed this gap: in NO_MATCH the planner reasonably
        # proposes explain_failure, but the facts omitted has_explainable_failure
        # so the review rejected every such plan.
        state = AgentState(
            phase=PHASE_NO_MATCH,
            current_image_path="input/q1.jpg",
            current_question_image_path="crop/q1.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            last_error="no match",
        )
        facts = build_permission_review_facts(state)
        self.assertTrue(facts.has_explainable_failure)
        plan = _plan(_step("explain_failure"))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)

    def test_error_retry_search_plan_is_allowed(self) -> None:
        # Same matrix gap on the ERROR phase: retry_search needs retryable_error.
        state = AgentState(
            phase=PHASE_ERROR,
            current_image_path="input/q1.jpg",
            current_question_image_path="crop/q1.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            last_error="tool error",
        )
        facts = build_permission_review_facts(state)
        self.assertTrue(facts.retryable_error)
        plan = _plan(_step("retry_search"))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)

    def test_error_without_image_retry_search_is_rejected(self) -> None:
        # retryable_error also requires an active image; without one the plan
        # must still be rejected rather than silently allowed.
        state = AgentState(
            phase=PHASE_ERROR,
            last_error="tool error",
        )
        facts = build_permission_review_facts(state)
        self.assertFalse(facts.retryable_error)
        plan = _plan(_step("retry_search"))
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)

    def test_unplannable_empty_plan_is_recorded_not_rejected(self) -> None:
        # An empty plan is the planner's honest "no legal read-only action"
        # conclusion.  It is recorded as a special allow (unplannable) rather
        # than rejected, so the shadow log distinguishes it from a rejected plan.
        facts = build_permission_review_facts(_candidate_state())
        plan = ShadowPlan(goal="用户想放弃", steps=(), source="unplannable")
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)
        self.assertEqual(review.code, "unplannable")

    def test_plan_tools_budget_exceeded_is_rejected(self) -> None:
        # A four-step select_question plan would trigger 4*4 = 16 tool calls in
        # the real dispatch, far over the tool-level budget of 8.
        facts = build_permission_review_facts(_candidate_state())
        plan = _plan(
            *(_step("select_question", question_index=i) for i in (1, 2, 1, 2))
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_REJECT)
        self.assertEqual(review.code, "plan_tools_budget_exceeded")
        self.assertIn("plan_tools_budget_exceeded", review.violations)

    def test_plan_within_tools_budget_is_allowed(self) -> None:
        # continue_search (2 tools) + select_candidate (1 tool) = 3 <= 8.
        # continuation_available must be true for the matrix to allow the step.
        facts = PermissionReviewFacts(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            candidate_count=3,
            current_chapter="4力法",
            has_active_image=True,
            continuation_available=True,
        )
        plan = _plan(
            _step("continue_search"),
            _step("select_candidate", candidate_rank=1),
        )
        review = review_shadow_plan(plan, facts)
        self.assertEqual(review.outcome, REVIEW_ALLOW)

    def test_tool_cost_map_covers_entire_plan_universe(self) -> None:
        # The budget is only sound if every planner action has a cost entry;
        # an unknown action must never silently fall through as free.
        missing = set(PLAN_ACTION_UNIVERSE) - set(ACTION_TOOL_COST)
        self.assertEqual(missing, set())

    def test_tool_cost_map_values_are_non_negative(self) -> None:
        for action, cost in ACTION_TOOL_COST.items():
            self.assertGreaterEqual(cost, 0, f"{action} has a negative tool cost")


if __name__ == "__main__":
    unittest.main()

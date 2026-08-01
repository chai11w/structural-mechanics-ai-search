"""Offline tests for the complex-request shadow admission diagnostic."""

import unittest

from scripts.evaluate_shadow_admission_qwen_v0 import (
    FIXTURE,
    EXPECTED_NEVER,
    EXPECTED_OBSERVE,
    _is_subsequence,
    diagnose_candidate_plan,
    evaluate_admission_case,
    load_admission_cases,
    summarize_admission,
)
from tiku_agent.shadow_plan_v0 import ShadowPlan, ShadowPlanStep, ShadowPlannerResult
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0


def _clarification_intent(_prompt: str) -> dict:
    return {
        "action": "clarification",
        "clarification_reason": "ambiguous_action",
        "confidence": 0.4,
        "reason": "测试",
    }


def _result(*steps: ShadowPlanStep, source: str = "planner") -> ShadowPlannerResult:
    return ShadowPlannerResult(
        rewritten_text="测试请求",
        keywords=(),
        reason="测试",
        plan=ShadowPlan(goal="测试目标", steps=steps, source=source),
    )


def _planner(result: ShadowPlannerResult) -> ShadowPlannerV0:
    class StubPlanner(ShadowPlannerV0):
        def __init__(self):
            pass

        def plan(self, user_text, context_payload):
            return result

    return StubPlanner()


class AdmissionFixtureTest(unittest.TestCase):
    def test_fixture_is_balanced_and_has_reviewable_targets(self):
        cases = load_admission_cases(FIXTURE)
        self.assertEqual(len(cases), 40)
        groups = {group: 0 for group in {case["group"] for case in cases}}
        for case in cases:
            groups[case["group"]] += 1
        self.assertEqual(set(groups.values()), {10})
        self.assertTrue(any(case["expected_entry"] == EXPECTED_NEVER for case in cases))
        self.assertTrue(any(case["expected_entry"] == EXPECTED_OBSERVE for case in cases))
        self.assertTrue(any(case["forbidden_actions"] for case in cases))

    def test_required_steps_are_order_sensitive(self):
        self.assertTrue(_is_subsequence(["show_candidates", "select_candidate"], ["show_candidates", "select_candidate"]))
        self.assertTrue(_is_subsequence(["select_question", "select_question"], ["select_question", "select_question"]))
        self.assertFalse(_is_subsequence(["show_candidates", "select_candidate"], ["select_candidate", "show_candidates"]))
        self.assertFalse(_is_subsequence(["select_question", "select_question"], ["select_question"]))


class AdmissionDiagnosticTest(unittest.TestCase):
    def test_atomic_real_entry_stays_on_fixed_path(self):
        case = next(
            case for case in load_admission_cases() if case["id"] == "atomic_select_candidate"
        )
        record = evaluate_admission_case(
            case,
            run=1,
            intent_model_client=_clarification_intent,
            entry_planner=_planner(_result(ShadowPlanStep("select_candidate", {"candidate_rank": 2}))),
            diagnostic_planner=_planner(_result(ShadowPlanStep("select_candidate", {"candidate_rank": 2}))),
        )
        self.assertFalse(record["current_admitted"])
        self.assertTrue(record["entry_contract_ok"])
        self.assertTrue(record["observable_equal"])

    def test_explicit_sequential_diagnostic_plan_can_be_useful(self):
        case = next(
            case for case in load_admission_cases() if case["id"] == "seq_show_then_select"
        )
        diagnostic = diagnose_candidate_plan(
            case,
            _planner(
                _result(
                    ShadowPlanStep("show_candidates"),
                    ShadowPlanStep("select_candidate", {"candidate_rank": 2}),
                )
            ),
        )
        self.assertEqual(diagnostic["actions"], ["show_candidates", "select_candidate"])
        self.assertTrue(diagnostic["required_steps_covered"])
        self.assertTrue(diagnostic["useful"])
        self.assertTrue(diagnostic["future_admission_ready"])
        self.assertEqual(diagnostic["route"], "shadow_actionable")

    def test_negative_conditional_proposal_is_visible_but_not_effective(self):
        case = next(
            case for case in load_admission_cases() if case["id"] == "cond_no_global_search"
        )
        diagnostic = diagnose_candidate_plan(
            case,
            _planner(_result(ShadowPlanStep("global_search"))),
        )
        self.assertEqual(diagnostic["forbidden_actions_seen"], ["global_search"])
        self.assertEqual(diagnostic["effective_forbidden_actions"], [])
        self.assertNotEqual(diagnostic["route"], "shadow_actionable")

    def test_conditional_action_is_not_future_admission_ready(self):
        case = next(
            case for case in load_admission_cases() if case["id"] == "cond_candidate_fallback"
        )
        diagnostic = diagnose_candidate_plan(
            case,
            _planner(_result(ShadowPlanStep("select_candidate", {"candidate_rank": 2}))),
        )
        self.assertTrue(diagnostic["goal_covered"])
        self.assertTrue(diagnostic["conditional_semantic_allow_risk"])
        self.assertFalse(diagnostic["future_admission_ready"])

    def test_summary_separates_missed_observation_from_false_admission(self):
        records = [
            {
                "expected_entry": EXPECTED_OBSERVE,
                "group": "sequential",
                "current_admitted": False,
                "entry_contract_ok": False,
                "observable_equal": True,
                "diagnostic": {
                    "route": "shadow_actionable",
                    "required_steps_covered": True,
                    "useful": True,
                    "forbidden_actions_seen": [],
                    "effective_forbidden_actions": [],
                },
                "current_response_intent": "select_candidate",
                "case_id": "missed",
                "run": 1,
            },
            {
                "expected_entry": EXPECTED_NEVER,
                "group": "atomic",
                "current_admitted": True,
                "entry_contract_ok": False,
                "observable_equal": True,
                "diagnostic": {
                    "route": "shadow_actionable",
                    "required_steps_covered": True,
                    "useful": True,
                    "forbidden_actions_seen": [],
                    "effective_forbidden_actions": [],
                },
                "current_response_intent": "continue_search",
                "case_id": "false",
                "run": 1,
            },
        ]
        summary = summarize_admission(records)
        self.assertEqual(summary["current_should_observe_total"], 1)
        self.assertEqual(summary["current_should_observe_admitted"], 0)
        self.assertEqual(summary["current_atomic_false_admissions"], 1)
        self.assertEqual(summary["diagnostic_useful_plans"], 1)


if __name__ == "__main__":
    unittest.main()

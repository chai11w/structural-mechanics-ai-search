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
    load_evaluation_profile,
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

    def test_representative_profile_is_explicitly_provisional_and_normalized(self):
        profile = load_evaluation_profile(FIXTURE)
        self.assertEqual(profile["status"], "provisional_product_prior")
        self.assertAlmostEqual(sum(profile["group_weights"].values()), 1.0)
        self.assertGreater(profile["group_weights"]["atomic"], profile["group_weights"]["sequential"])
        self.assertGreater(profile["group_weights"]["sequential"], profile["group_weights"]["conditional"])
        self.assertIn("current_unresolved_conditional_tool_turns", profile["hard_gates"])
        self.assertIn("current_verified_condition_false_admissions", profile["hard_gates"])

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
        self.assertEqual(diagnostic["condition_outcome"], "unknown")
        self.assertFalse(diagnostic["conditional_semantic_allow_risk"])
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
                    "condition_outcome": "not_present",
                },
                "current_response_intent": "select_candidate",
                "current_tools": [],
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
                    "condition_outcome": "not_present",
                },
                "current_response_intent": "continue_search",
                "current_tools": [],
                "case_id": "false",
                "run": 1,
            },
        ]
        summary = summarize_admission(records)
        self.assertEqual(summary["current_should_observe_total"], 1)
        self.assertEqual(summary["current_should_observe_admitted"], 0)
        self.assertEqual(summary["current_atomic_false_admissions"], 1)
        self.assertEqual(summary["diagnostic_useful_plans"], 1)

    def test_weighting_does_not_hide_a_rare_group_hard_gate_failure(self):
        profile = load_evaluation_profile(FIXTURE)
        records = []
        for group, expected in (
            ("atomic", EXPECTED_NEVER),
            ("clarify_or_unsupported", "optional"),
            ("sequential", EXPECTED_OBSERVE),
            ("conditional", EXPECTED_OBSERVE),
        ):
            records.append({
                "expected_entry": expected,
                "group": group,
                "current_admitted": expected == EXPECTED_OBSERVE,
                "entry_contract_ok": True,
                "observable_equal": True,
                "diagnostic": {
                    "route": "shadow_actionable",
                    "required_steps_covered": True,
                    "useful": True,
                    "forbidden_actions_seen": [],
                    "effective_forbidden_actions": [],
                    "condition_outcome": "unknown" if group == "conditional" else "not_present",
                },
                "current_response_intent": "retry_search",
                "current_tools": ["coarse_search"] if group == "conditional" else [],
                "case_id": group,
                "run": 1,
            })
        summary = summarize_admission(records, profile=profile)
        representative = summary["representative_profile"]
        self.assertEqual(representative["weighted_entry_contract_rate"], 1.0)
        self.assertFalse(
            representative["hard_gate_results"]["current_unresolved_conditional_tool_turns"]
        )
        self.assertFalse(representative["release_ready"])

    def test_group_only_summary_never_claims_full_profile_release_readiness(self):
        profile = load_evaluation_profile(FIXTURE)
        record = {
            "expected_entry": EXPECTED_OBSERVE,
            "group": "conditional",
            "current_admitted": True,
            "entry_contract_ok": True,
            "observable_equal": True,
            "diagnostic": {
                "route": "needs_confirmation",
                "required_steps_covered": True,
                "useful": True,
                "forbidden_actions_seen": [],
                "effective_forbidden_actions": [],
                "condition_outcome": "unknown",
            },
            "current_response_intent": "clarification",
            "current_tools": [],
            "case_id": "conditional_only",
            "run": 1,
        }
        representative = summarize_admission([record], profile=profile)["representative_profile"]
        self.assertEqual(representative["weighted_entry_contract_rate"], 1.0)
        self.assertFalse(representative["profile_complete"])
        self.assertFalse(representative["release_ready"])


if __name__ == "__main__":
    unittest.main()

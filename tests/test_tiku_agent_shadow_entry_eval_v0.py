import unittest

from scripts.evaluate_shadow_plan_entry_qwen_v0 import (
    FIXTURE,
    ROUTE_NEEDS_CONFIRMATION,
    ROUTE_SHADOW_ACTIONABLE,
    ROUTE_UNPLANNABLE,
    evaluate_case,
    load_gold_cases,
    summarize,
)
from tiku_agent.shadow_plan_v0 import ShadowPlan, ShadowPlanStep, ShadowPlannerResult
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0


def _clarification_intent(_prompt: str) -> dict:
    return {
        "action": "clarification",
        "clarification_reason": "ambiguous_action",
        "confidence": 0.4,
        "reason": "测试中的模糊业务",
    }


def _planner_result(*steps: ShadowPlanStep, source: str = "planner") -> ShadowPlannerResult:
    return ShadowPlannerResult(
        rewritten_text="补全后的模糊请求",
        keywords=(),
        reason="测试",
        plan=ShadowPlan(
            goal="测试目标",
            steps=steps,
            source=source,
        ),
    )


def _planner_with_result(result: ShadowPlannerResult) -> ShadowPlannerV0:
    class StubPlanner(ShadowPlannerV0):
        def __init__(self):
            pass

        def plan(self, user_text, context_payload):
            return result

    return StubPlanner()


class ShadowEntryGoldFixtureTest(unittest.TestCase):
    def test_fixture_has_reviewable_gold_set(self):
        cases = load_gold_cases(FIXTURE)
        self.assertGreaterEqual(len(cases), 30)
        self.assertTrue(any(case["planner"] == "never" for case in cases))
        self.assertTrue(any(case["forbidden_actions"] for case in cases))

class ShadowEntryEvaluationTest(unittest.TestCase):
    def test_all_gold_cases_run_with_shadow_observer_without_side_effects(self):
        result = _planner_result(source="unplannable")
        records = [
            evaluate_case(
                case,
                run=1,
                intent_model_client=_clarification_intent,
                planner=_planner_with_result(result),
            )
            for case in load_gold_cases()
        ]
        self.assertTrue(all(record["observable_equal"] for record in records))
        self.assertTrue(
            all(
                record["planner_calls"] == 0
                for record in records
                if record["planner_expectation"] == "never"
            )
        )

    def test_clear_safe_path_never_calls_planner_and_preserves_observables(self):
        case = next(case for case in load_gold_cases() if case["id"] == "hello_idle")
        record = evaluate_case(
            case,
            run=1,
            intent_model_client=_clarification_intent,
            planner=_planner_with_result(
                _planner_result(ShadowPlanStep("show_candidates"))
            ),
        )
        self.assertTrue(record["passed"])
        self.assertEqual(record["planner_calls"], 0)
        self.assertTrue(record["observable_equal"])

    def test_clear_business_path_never_calls_planner(self):
        case = next(
            case for case in load_gold_cases() if case["id"] == "select_second_candidate"
        )
        record = evaluate_case(
            case,
            run=1,
            intent_model_client=_clarification_intent,
            planner=_planner_with_result(
                _planner_result(ShadowPlanStep("show_candidates"))
            ),
        )
        self.assertTrue(record["passed"])
        self.assertEqual(record["planner_calls"], 0)
        self.assertEqual(record["baseline_tools"], ["answer_candidate"])
        self.assertEqual(record["baseline_tools"], record["observed_tools"])

    def test_unplannable_is_not_actionable(self):
        case = next(case for case in load_gold_cases() if case["id"] == "look_at_this_idle")
        record = evaluate_case(
            case,
            run=1,
            intent_model_client=_clarification_intent,
            planner=_planner_with_result(_planner_result(source="unplannable")),
        )
        self.assertEqual(record["route"], ROUTE_UNPLANNABLE)
        summary = summarize([record])
        self.assertEqual(summary["unplannable"], 1)
        self.assertEqual(summary["actionable_plans"], 0)

    def test_forbidden_inferred_selection_is_blocked_for_confirmation(self):
        case = next(
            case for case in load_gold_cases() if case["id"] == "look_at_this_candidate"
        )
        record = evaluate_case(
            case,
            run=1,
            intent_model_client=_clarification_intent,
            planner=_planner_with_result(
                _planner_result(ShadowPlanStep("select_candidate", {"candidate_rank": 1}))
            ),
        )
        self.assertEqual(record["route"], ROUTE_NEEDS_CONFIRMATION)
        self.assertEqual(record["forbidden_actions_seen"], [])
        self.assertEqual(record["raw_shadow_forbidden_actions_seen"], ["select_candidate"])
        self.assertEqual(record["blocked_shadow_forbidden_actions"], ["select_candidate"])
        self.assertEqual(record["planner_actions"], ["select_candidate"])
        rewritten = record["shadow_entry"]["rewritten"]
        self.assertTrue(rewritten["requires_confirmation"])
        self.assertEqual(rewritten["confidence"], 0.0)
        self.assertFalse(rewritten["evidence"][0]["authorized"])
        self.assertTrue(record["passed"])

    def test_summary_reports_gate_and_observable_failures_separately(self):
        base = {
            "passed": True,
            "route": ROUTE_UNPLANNABLE,
            "planner_expectation": "optional",
            "planner_calls": 1,
            "forbidden_actions_seen": [],
            "observable_equal": True,
            "case_id": "a",
            "run": 1,
        }
        bad = dict(
            base,
            passed=False,
            route=ROUTE_SHADOW_ACTIONABLE,
            planner_expectation="never",
            forbidden_actions_seen=["select_candidate"],
            observable_equal=False,
            case_id="b",
        )
        summary = summarize([base, bad])
        self.assertEqual(summary["actionable_plans"], 1)
        self.assertEqual(summary["unplannable"], 1)
        self.assertEqual(summary["fixed_path_planner_violations"], 1)
        self.assertEqual(summary["forbidden_action_violations"], 1)
        self.assertEqual(summary["fixed_forbidden_action_violations"], 0)
        self.assertEqual(summary["effective_shadow_forbidden_action_violations"], 0)
        self.assertEqual(summary["observable_differences"], 1)


if __name__ == "__main__":
    unittest.main()

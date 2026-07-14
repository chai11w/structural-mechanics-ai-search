import unittest
from pathlib import Path

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import authorize_action_v2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_eval_v2 import (
    compare_system_reports,
    evaluate_v1_rule_suite,
    load_gold_suite,
    load_gold_suites,
)


SUITE_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_01.json"
EXPANSION_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_02.json"


class IntentEvalV2Test(unittest.TestCase):
    def test_approved_seed_suite_has_twelve_unique_cases(self):
        suite = load_gold_suite(SUITE_PATH)
        self.assertEqual(suite["status"], "approved_seed_set")
        self.assertEqual(len(suite["cases"]), 12)

    def test_v1_rule_baseline_is_deterministic_and_reports_safety(self):
        suite = load_gold_suite(SUITE_PATH)
        first = evaluate_v1_rule_suite(suite)
        second = evaluate_v1_rule_suite(suite)
        self.assertEqual(first, second)
        self.assertEqual(first["system"], "v1_rule")
        self.assertEqual(first["total"], 12)
        self.assertEqual(first["exact_count"] + len(first["failures"]), 12)
        # Freeze the approved challenge-seed baseline. This is deliberately not
        # presented as V1's overall accuracy because the seed set over-samples
        # known context and safety failures.
        self.assertEqual(first["exact_count"], 0)
        self.assertEqual(first["exact_accuracy"], 0.0)
        self.assertEqual(first["action_count"], 3)
        self.assertEqual(first["action_accuracy"], 0.25)
        self.assertEqual(first["safe_success_count"], 0)
        self.assertEqual(first["safe_success_rate"], 0.0)
        self.assertEqual(first["unsafe_executions"], 4)

    def test_combined_review_set_has_forty_valid_unique_decisions(self):
        suite = load_gold_suites([SUITE_PATH, EXPANSION_PATH])
        self.assertEqual(suite["status"], "combined_review_set")
        self.assertEqual(suite["source_statuses"], ["approved_seed_set", "review_draft"])
        self.assertEqual(len(suite["cases"]), 40)
        self.assertEqual(len({case["id"] for case in suite["cases"]}), 40)
        for case in suite["cases"]:
            with self.subTest(case=case["id"]):
                decision = ActionDecisionV2.from_dict(case["expected_decision"])
                context = ConversationContextV2.from_mapping(case["context"])
                authorization = authorize_action_v2(decision, context.to_decision_context())
                self.assertTrue(authorization.allowed, authorization.code)

    def test_combined_review_set_matches_approved_category_distribution(self):
        report = evaluate_v1_rule_suite(load_gold_suites([SUITE_PATH, EXPANSION_PATH]))
        self.assertEqual(
            {name: values["total"] for name, values in report["categories"].items()},
            {
                "active_namespace": 7,
                "chapter_context": 5,
                "conversation_shell": 5,
                "explicit_task": 8,
                "failure_recovery": 4,
                "reference_resolution": 6,
                "safety_boundary": 5,
            },
        )

    def test_paired_comparison_reports_improvements_and_regressions(self):
        row = {
            "category": "explicit_task",
            "expected": {"action": "cancel"},
            "actual": {"action": "clarification"},
        }
        baseline = {
            "system": "baseline",
            "total": 2,
            "exact_accuracy": 0.0,
            "action_accuracy": 0.0,
            "safe_success_rate": 0.0,
            "unsafe_executions": 1,
            "cases": [
                {"id": "a", "exact": False, **row},
                {"id": "b", "exact": False, **row},
            ],
        }
        contender = {
            "system": "contender",
            "total": 2,
            "exact_accuracy": 0.5,
            "action_accuracy": 0.5,
            "safe_success_rate": 0.5,
            "unsafe_executions": 0,
            "cases": [
                {"id": "a", "exact": True, **row},
                {"id": "b", "exact": False, **row},
            ],
        }
        comparison = compare_system_reports(baseline, contender)
        self.assertEqual(comparison["improvements"], 1)
        self.assertEqual(comparison["regressions"], 0)
        self.assertEqual(comparison["unchanged"], 1)
        self.assertEqual(comparison["metric_delta"]["exact_accuracy"], 0.5)
        self.assertEqual(comparison["metric_delta"]["unsafe_executions"], -1)


if __name__ == "__main__":
    unittest.main()

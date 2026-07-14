import unittest
from pathlib import Path

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import authorize_action_v2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_eval_v2 import (
    _evaluate_suite,
    compare_system_reports,
    evaluate_v2_suite,
    load_gold_suite,
    load_gold_suites,
)


SUITE_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_01.json"
EXPANSION_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_02.json"
GLOBAL_FALLBACK_PATH = Path(__file__).parent / "fixtures" / "intent_v2_global_fallback.json"
RESULT_FEEDBACK_PATH = Path(__file__).parent / "fixtures" / "intent_v2_result_feedback.json"


class IntentEvalV2Test(unittest.TestCase):
    def test_approved_seed_suite_has_twelve_unique_cases(self):
        suite = load_gold_suite(SUITE_PATH)
        self.assertEqual(suite["status"], "approved_seed_set")
        self.assertEqual(len(suite["cases"]), 12)

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
        report = evaluate_v2_suite(load_gold_suites([SUITE_PATH, EXPANSION_PATH]))
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

    def test_global_fallback_suite_has_reviewable_guarded_decisions(self):
        suite = load_gold_suite(GLOBAL_FALLBACK_PATH)
        self.assertEqual(suite["status"], "review_draft")
        self.assertEqual(len(suite["cases"]), 13)
        for case in suite["cases"]:
            with self.subTest(case=case["id"]):
                decision = ActionDecisionV2.from_dict(case["expected_decision"])
                context = ConversationContextV2.from_mapping(case["context"])
                authorization = authorize_action_v2(decision, context.to_decision_context())
                self.assertTrue(authorization.allowed, authorization.code)

    def test_result_feedback_suite_is_directly_recoverable(self):
        suite = load_gold_suite(RESULT_FEEDBACK_PATH)
        self.assertEqual(suite["status"], "review_draft")
        self.assertEqual(len(suite["cases"]), 20)
        report = evaluate_v2_suite(suite)
        self.assertGreaterEqual(report["exact_count"], 14)
        self.assertEqual(report["recoverable_outcome_count"], 20)
        self.assertEqual(report["wrong_execution_count"], 0)

    def test_outcome_metrics_distinguish_clarification_from_wrong_execution(self):
        suite = {
            "schema_version": "1.0",
            "status": "test",
            "cases": [
                {
                    "id": "exact",
                    "category": "explicit_task",
                    "expected_decision": {"action": "cancel"},
                },
                {
                    "id": "clarify",
                    "category": "explicit_task",
                    "expected_decision": {"action": "select_candidate", "candidate_rank": 2},
                },
                {
                    "id": "wrong_target",
                    "category": "chapter_context",
                    "expected_decision": {
                        "action": "set_chapter",
                        "chapter_override": "4力法",
                        "chapter_target": "next_image",
                    },
                },
                {
                    "id": "forbidden",
                    "category": "safety_boundary",
                    "expected_decision": {"action": "reject", "requested_action": "delete"},
                },
            ],
        }
        actual = {
            "exact": {"action": "cancel"},
            "clarify": {"action": "clarification", "clarification_reason": "ambiguous_reference"},
            "wrong_target": {
                "action": "set_chapter",
                "chapter_override": "4力法",
                "chapter_target": "current_question",
            },
            "forbidden": {"action": "select_candidate", "candidate_rank": 1},
        }
        report = _evaluate_suite(
            suite,
            system="test",
            runner=lambda case: actual[case["id"]],
        )
        self.assertEqual(report["exact_count"], 1)
        self.assertEqual(report["safe_clarification_count"], 1)
        self.assertEqual(report["recoverable_outcome_count"], 2)
        self.assertEqual(report["recoverable_outcome_rate"], 0.5)
        self.assertEqual(report["wrong_execution_count"], 2)
        self.assertEqual(report["wrong_execution_rate"], 0.5)
        self.assertEqual(report["forbidden_action_executions"], 1)
        self.assertEqual(
            [row["outcome"] for row in report["cases"]],
            ["direct_success", "safe_clarification", "wrong_execution", "wrong_execution"],
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
            "exact_count": 0,
            "exact_accuracy": 0.0,
            "action_count": 0,
            "action_accuracy": 0.0,
            "safe_success_count": 0,
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
            "exact_count": 1,
            "exact_accuracy": 0.5,
            "action_count": 1,
            "action_accuracy": 0.5,
            "safe_success_count": 1,
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
        self.assertEqual(comparison["baseline_metrics"]["exact_count"], 0)
        self.assertEqual(comparison["contender_metrics"]["exact_count"], 1)

if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import authorize_action_v2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_eval_v2 import (
    _evaluate_suite,
    compare_system_reports,
    evaluate_v1_full_suite,
    evaluate_v1_rule_suite,
    evaluate_v2_suite,
    load_gold_suite,
    load_gold_suites,
)


SUITE_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_01.json"
EXPANSION_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_02.json"
GLOBAL_FALLBACK_PATH = Path(__file__).parent / "fixtures" / "intent_v2_global_fallback.json"


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

    def test_global_fallback_suite_has_reviewable_guarded_decisions(self):
        suite = load_gold_suite(GLOBAL_FALLBACK_PATH)
        self.assertEqual(suite["status"], "review_draft")
        self.assertEqual(len(suite["cases"]), 12)
        for case in suite["cases"]:
            with self.subTest(case=case["id"]):
                decision = ActionDecisionV2.from_dict(case["expected_decision"])
                context = ConversationContextV2.from_mapping(case["context"])
                authorization = authorize_action_v2(decision, context.to_decision_context())
                self.assertTrue(authorization.allowed, authorization.code)

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

    def test_v1_and_v2_rule_layers_run_on_the_same_forty_cases(self):
        suite = load_gold_suites([SUITE_PATH, EXPANSION_PATH])
        v1 = evaluate_v1_rule_suite(suite)
        v2 = evaluate_v2_suite(suite)
        comparison = compare_system_reports(v1, v2)
        self.assertEqual(v1["total"], 40)
        self.assertEqual(v2["total"], 40)
        self.assertEqual(comparison["total"], 40)
        self.assertEqual(
            [row["id"] for row in v1["cases"]],
            [row["id"] for row in v2["cases"]],
        )

    def test_v1_full_runner_accepts_an_injected_model_without_network(self):
        suite = {
            "schema_version": "1.0",
            "status": "test",
            "cases": [
                {
                    "id": "model_case",
                    "category": "conversation_shell",
                    "context": {"phase": "IDLE"},
                    "input": {"event_type": "text", "text": "你会做什么"},
                    "expected_decision": {"action": "clarification", "clarification_reason": "ambiguous_action"},
                }
            ],
        }
        report = evaluate_v1_full_suite(
            suite,
            llm_client=lambda _prompt: {
                "intent": "unsupported",
                "confidence": 1.0,
                "reason": "无法判断",
            },
        )
        self.assertEqual(report["system"], "v1_full")
        self.assertEqual(report["exact_count"], 1)


if __name__ == "__main__":
    unittest.main()

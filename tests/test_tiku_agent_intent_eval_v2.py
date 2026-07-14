import unittest
from pathlib import Path

from tiku_agent.intent_eval_v2 import evaluate_v1_rule_suite, load_gold_suite


SUITE_PATH = Path(__file__).parent / "fixtures" / "intent_v2_gold_review_01.json"


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


if __name__ == "__main__":
    unittest.main()

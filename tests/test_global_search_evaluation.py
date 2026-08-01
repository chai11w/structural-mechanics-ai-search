import unittest

from scripts.evaluate_global_search import is_accepted_final_score, summarize_results


class GlobalSearchEvaluationTest(unittest.TestCase):
    def test_final_score_threshold_includes_ninety_five(self):
        self.assertTrue(
            is_accepted_final_score(
                {"rerank_status": "completed", "final_score": 0.95}, 0.95
            )
        )
        self.assertFalse(
            is_accepted_final_score(
                {"rerank_status": "completed", "final_score": 0.949}, 0.95
            )
        )
        self.assertFalse(
            is_accepted_final_score(
                {"rerank_status": "timeout", "final_score": 1.0}, 0.95
            )
        )

    def test_visual_summary_reports_hit_precision_cost_and_unfinished(self):
        cases = [
            {
                "coarse_candidates": 3,
                "self_in_coarse": True,
                "accepted_candidates": 2,
                "self_accepted": True,
                "nonself_accepted_for_review": 1,
                "unfinished_candidates": 0,
                "model_calls": 3,
                "seconds": 4.0,
            },
            {
                "coarse_candidates": 5,
                "self_in_coarse": True,
                "accepted_candidates": 0,
                "self_accepted": False,
                "nonself_accepted_for_review": 0,
                "unfinished_candidates": 1,
                "model_calls": 5,
                "seconds": 6.0,
            },
        ]
        summary = summarize_results(cases, coarse_only=False)
        self.assertEqual(summary["self_hit_rate"], 0.5)
        self.assertEqual(summary["no_result_rate"], 0.5)
        self.assertEqual(summary["exact_file_precision"], 0.5)
        self.assertEqual(summary["average_coarse_candidates"], 4.0)
        self.assertEqual(summary["model_calls"], 8)
        self.assertEqual(summary["average_case_seconds"], 5.0)
        self.assertEqual(summary["unfinished_candidates"], 1)

    def test_coarse_only_summary_does_not_claim_visual_accuracy(self):
        summary = summarize_results(
            [
                {
                    "coarse_candidates": 2,
                    "self_in_coarse": True,
                    "model_calls": 0,
                    "seconds": 0.2,
                }
            ],
            coarse_only=True,
        )
        self.assertEqual(summary["coarse_self_hit_rate"], 1.0)
        self.assertNotIn("self_hit_rate", summary)
        self.assertEqual(summary["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()

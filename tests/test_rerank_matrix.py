import unittest

from scripts.evaluate_rerank_matrix import summarize


class RerankMatrixSummaryTest(unittest.TestCase):
    def test_summary_separates_parse_rate_from_quality_rates(self):
        rows = [
            {
                "prompt": "v4",
                "provider": "qwen",
                "model": "qwen3.7-plus",
                "same_shape": True,
                "query": "query-a",
                "result": {"ok": True, "score": 0.95, "seconds": 1.0},
            },
            {
                "prompt": "v4",
                "provider": "qwen",
                "model": "qwen3.7-plus",
                "same_shape": True,
                "query": "query-a",
                "result": {"ok": True, "score": 0.65, "seconds": 2.0},
            },
            {
                "prompt": "v4",
                "provider": "qwen",
                "model": "qwen3.7-plus",
                "same_shape": False,
                "query": "query-a",
                "result": {"ok": True, "score": 0.92, "seconds": 1.5},
            },
            {
                "prompt": "v4",
                "provider": "qwen",
                "model": "qwen3.7-plus",
                "same_shape": False,
                "query": "query-b",
                "result": {"ok": False, "score": None, "seconds": 3.0},
            },
        ]

        summary = summarize(rows, same_threshold=0.8, false_high_threshold=0.9)[0]

        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["parsed"], 3)
        self.assertEqual(summary["parse_rate"], 0.75)
        self.assertEqual(summary["same_recall"], 0.5)
        self.assertEqual(summary["different_false_high_rate"], 1.0)
        self.assertEqual(summary["ranked_query_count"], 1)
        self.assertEqual(summary["top1_hit_rate"], 1.0)

    def test_empty_class_is_reported_as_unknown(self):
        rows = [
            {
                "prompt": "v1",
                "provider": "zhipu",
                "model": "glm-4.6v",
                "same_shape": True,
                "query": "query-a",
                "result": {"ok": True, "score": 0.9, "seconds": 1.0},
            }
        ]

        summary = summarize(rows)[0]

        self.assertIsNone(summary["different_false_high_rate"])
        self.assertIsNone(summary["separation"])


if __name__ == "__main__":
    unittest.main()

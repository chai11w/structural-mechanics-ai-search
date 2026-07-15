import unittest

from multi_agent_pipeline import select_rerank_candidates


class MultiAgentRerankPolicyTest(unittest.TestCase):
    def test_small_threshold_pool_still_enters_rerank(self):
        results = [
            {"rank": 1, "path": "a.jpg", "score": 1.0, "name": "a.jpg"},
            {"rank": 2, "path": "b.jpg", "score": 0.70, "name": "b.jpg"},
            {"rank": 3, "path": "c.jpg", "score": 0.42, "name": "c.jpg"},
        ]

        selected = select_rerank_candidates(results, "main")

        self.assertEqual([item["path"] for item in selected], ["a.jpg", "b.jpg"])

    def test_below_threshold_candidates_do_not_enter_rerank(self):
        results = [
            {"rank": 1, "path": "a.jpg", "score": 0.64, "name": "a.jpg"},
            {"rank": 2, "path": "b.jpg", "score": 0.20, "name": "b.jpg"},
        ]

        selected = select_rerank_candidates(results, "main")

        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()

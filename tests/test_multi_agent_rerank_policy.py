import unittest

from multi_agent_pipeline import select_rerank_candidates


class MultiAgentRerankPolicyTest(unittest.TestCase):
    def test_all_coarse_candidates_enter_rerank(self):
        results = [
            {"rank": 1, "path": "a.jpg", "score": 1.0, "name": "a.jpg"},
            {"rank": 2, "path": "b.jpg", "score": 0.42, "name": "b.jpg"},
            {"rank": 3, "path": "c.jpg", "score": 0.2, "name": "c.jpg"},
        ]

        selected = select_rerank_candidates(results, "main")

        self.assertEqual([item["path"] for item in selected], ["a.jpg", "b.jpg", "c.jpg"])


if __name__ == "__main__":
    unittest.main()

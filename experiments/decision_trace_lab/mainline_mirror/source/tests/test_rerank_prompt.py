import search
import unittest
from pathlib import Path
from unittest.mock import patch


class RerankPromptTest(unittest.TestCase):
    def test_default_rerank_prompt_is_shape_only(self):
        self.assertEqual(search.RERANK_PROMPT, search.SHAPE_RERANK_PROMPT)
        self.assertIn("只看主杆件骨架", search.RERANK_PROMPT)
        self.assertIn("忽略荷载", search.RERANK_PROMPT)
        self.assertIn("支座符号细节", search.RERANK_PROMPT)
        self.assertNotIn("荷载位置和方向", search.RERANK_PROMPT)

    def test_legacy_rerank_prompt_kept_for_comparison(self):
        self.assertNotEqual(search.LEGACY_RERANK_PROMPT, search.SHAPE_RERANK_PROMPT)
        self.assertIn("荷载位置和方向", search.LEGACY_RERANK_PROMPT)

    def test_final_rerank_score_keeps_load_and_shape_blend(self):
        self.assertEqual(search.compute_final_rerank_score(1.0, 0.2), 0.6)
        self.assertEqual(search.compute_final_rerank_score(0.1, 0.95), 0.525)
        self.assertEqual(search.compute_final_rerank_score(0.5, 2.0), 0.75)

    def test_concurrent_rerank_matches_serial_scoring_order(self):
        query = "query.jpg"
        candidates = [
            {"rank": 1, "path": "a.jpg", "name": "a.jpg", "score": 0.8},
            {"rank": 2, "path": "b.jpg", "name": "b.jpg", "score": 0.7},
            {"rank": 3, "path": "c.jpg", "name": "c.jpg", "score": 0.6},
        ]
        vision_scores = {"a.jpg": 0.2, "b.jpg": 0.9, "c.jpg": 0.4}

        def fake_score(client, query_image_path, candidate_path, prompt=search.RERANK_PROMPT, timeout_seconds=None):
            del client, query_image_path, prompt, timeout_seconds
            name = Path(candidate_path).name
            return vision_scores[name], name

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=fake_score),
        ):
            serial = search.rerank_candidates(query, candidates, top_n=3)
            concurrent = search.rerank_candidates_concurrent(query, candidates, top_n=3, max_workers=3)

        self.assertEqual([item["path"] for item in concurrent], [item["path"] for item in serial])
        self.assertEqual([item["final_score"] for item in concurrent], [item["final_score"] for item in serial])

    def test_timeout_candidate_keeps_coarse_score_and_status(self):
        candidate = {"rank": 1, "path": "slow.jpg", "score": 0.9}

        with patch("search.score_candidate_pair", side_effect=TimeoutError("request timeout")):
            result = search.score_rerank_candidate(
                "query.jpg",
                candidate,
                client=object(),
                timeout_seconds=2,
            )

        self.assertIsNone(result["rerank_score"])
        self.assertEqual(result["final_score"], 0.9)
        self.assertEqual(result["rerank_status"], "timeout")

    def test_timeout_candidate_is_retried_and_ranked_when_retry_succeeds(self):
        candidates = [{"rank": 1, "path": "slow.jpg", "name": "slow.jpg", "score": 0.8}]
        responses = [TimeoutError("Request timed out."), (0.9, "补评完成")]

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=responses),
        ):
            results = search.rerank_candidates_concurrent(
                "query.jpg",
                candidates,
                max_workers=1,
                candidate_timeout_seconds=1,
                retry_timeout_seconds=2,
                retry_max_candidates=1,
            )

        self.assertTrue(search.rerank_results_complete(results))
        self.assertEqual(results[0]["rerank_status"], "retried")
        self.assertEqual(results[0]["rerank_attempts"], 2)
        self.assertAlmostEqual(results[0]["final_score"], 0.85)

    def test_unfinished_retry_returns_marked_coarse_fallback(self):
        candidates = [{"rank": 1, "path": "slow.jpg", "name": "slow.jpg", "score": 0.8}]

        with (
            patch("search.prepare_rerank_candidates", return_value=candidates),
            patch("search.ZhipuAI", return_value=object()),
            patch("search.score_candidate_pair", side_effect=TimeoutError("Request timed out.")),
        ):
            results = search.rerank_candidates_concurrent(
                "query.jpg",
                candidates,
                max_workers=1,
                candidate_timeout_seconds=1,
                retry_timeout_seconds=2,
                retry_max_candidates=1,
            )

        self.assertFalse(search.rerank_results_complete(results))
        self.assertEqual(results[0]["rerank_status"], "incomplete")
        self.assertNotIn("final_score", results[0])

    def test_default_rerank_uses_shared_concurrency_policy(self):
        with patch("search.rerank_candidates_concurrent", return_value=[]) as concurrent:
            search.rerank_candidates("query.jpg", [{"rank": 1, "path": "a.jpg", "score": 1.0}])

        self.assertEqual(concurrent.call_args.kwargs["max_workers"], search.RERANK_CONCURRENT_MAX_WORKERS)
        self.assertEqual(concurrent.call_args.kwargs["candidate_timeout_seconds"], search.RERANK_PRIMARY_TIMEOUT_SECONDS)
        self.assertEqual(concurrent.call_args.kwargs["retry_timeout_seconds"], search.RERANK_RETRY_TIMEOUT_SECONDS)

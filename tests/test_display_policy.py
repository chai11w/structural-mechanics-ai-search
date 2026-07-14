import inspect
import unittest

import search
from multi_agent_pipeline import MultiAgentCoordinator
from scripts.feishu_tiku_bot import FeishuTikuOptions, build_parser
from tiku_agent.tools import AgentToolConfig


class SharedDisplayPolicyTest(unittest.TestCase):
    def test_coarse_policy_keeps_all_perfect_or_only_best_fallback(self):
        with_perfect = [(1.0, "a"), (1.0, "b"), (0.9, "c")]
        self.assertEqual(search.select_coarse_results(with_perfect), with_perfect[:2])

        without_perfect = [(0.9, "a"), (0.8, "b"), (0.7, "c")]
        self.assertEqual(search.select_coarse_results(without_perfect), without_perfect[:1])

    def test_threshold_policy_is_centralized_in_search(self):
        results = [
            {"rank": 1, "final_score": 0.95},
            {"rank": 2, "final_score": 0.91},
            {"rank": 3, "final_score": 0.90},
            {"rank": 4, "final_score": 0.89},
            {"rank": 5, "final_score": 0.87},
        ]

        selected = search.select_display_results(results)

        self.assertEqual([item["final_score"] for item in selected], [0.95, 0.91, 0.90])

        below_ninety = [
            {"rank": 1, "final_score": 0.89},
            {"rank": 2, "final_score": 0.88},
            {"rank": 3, "final_score": 0.87},
            {"rank": 4, "final_score": 0.86},
        ]
        selected = search.select_display_results(below_ninety)
        self.assertEqual([item["final_score"] for item in selected], [0.89])

    def test_agent_feishu_and_pipeline_share_default_display_limit(self):
        self.assertEqual(AgentToolConfig().rerank_top, search.DISPLAY_MAX_RESULTS)
        self.assertEqual(FeishuTikuOptions().rerank_top, search.DISPLAY_MAX_RESULTS)
        self.assertEqual(build_parser().parse_args([]).rerank_top, search.DISPLAY_MAX_RESULTS)
        self.assertEqual(
            inspect.signature(MultiAgentCoordinator.search_image).parameters["rerank_top"].default,
            search.DISPLAY_MAX_RESULTS,
        )


if __name__ == "__main__":
    unittest.main()

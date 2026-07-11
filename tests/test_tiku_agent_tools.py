from pathlib import Path
import unittest
from unittest.mock import patch

from tiku_agent.tools import (
    AgentToolConfig,
    classify_structure_tool,
    parse_candidate_action_tool,
    rerank_candidates_tool,
    route_bank_tool,
)


class TikuAgentToolsTest(unittest.TestCase):
    def test_agent_runtime_is_isolated_from_old_feishu_state(self):
        config = AgentToolConfig()
        self.assertEqual(config.runtime_dir, Path(__file__).resolve().parents[1] / ".tmp_tiku_agent")
        self.assertNotIn(".tmp_feishu_tiku", str(config.runtime_dir))
        self.assertNotIn(".tmp_feishu_tiku", str(config.qwen_cache_path))
        self.assertNotIn(".tmp_feishu_tiku", str(config.answer_output_dir))

    def test_route_bank_symbolic_load(self):
        result = route_bank_tool([{"type": "集中", "raw": "P"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["route"], "symbolic")
        self.assertEqual(result.next_state, "READY_FOR_STRUCTURE")

    def test_structure_tool_skips_non_symbolic_routes(self):
        result = classify_structure_tool(None, route="main")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["structure_type"], "")
        self.assertFalse(result.data["filter_applicable"])

    def test_candidate_action_parser_answer_delete_and_cancel(self):
        self.assertEqual(
            parse_candidate_action_tool("1", candidate_count=3).data,
            {"action": "answer", "rank": 1},
        )
        self.assertEqual(
            parse_candidate_action_tool("-2", candidate_count=3).data,
            {"action": "delete_candidate", "rank": 2},
        )
        self.assertEqual(parse_candidate_action_tool("0", candidate_count=3).data, {"action": "cancel"})

    def test_agent_rerank_runs_even_when_candidate_count_does_not_exceed_top(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.75, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.70, "name": "q2.jpg"},
            {"rank": 3, "path": "q3.jpg", "score": 0.40, "name": "q3.jpg"},
        ]

        def fake_rerank(query_image_path, rerank_input, top_n=3):
            self.assertEqual(query_image_path, "query.jpg")
            self.assertEqual([item["path"] for item in rerank_input], ["q1.jpg", "q2.jpg"])
            self.assertEqual(top_n, 3)
            return [
                {
                    "rank": 2,
                    "path": "q2.jpg",
                    "name": "q2.jpg",
                    "score": 0.70,
                    "rerank_score": 0.95,
                    "final_score": 0.75,
                }
            ]

        with patch("tiku_agent.tools.search.rerank_candidates", side_effect=fake_rerank) as rerank:
            result = rerank_candidates_tool("query.jpg", candidates, route="main", rerank_top=3)

        self.assertTrue(result.ok)
        self.assertTrue(result.data["reranked"])
        self.assertEqual(rerank.call_count, 1)
        self.assertEqual(result.data["visible_candidates"][0]["final_score"], 0.75)


if __name__ == "__main__":
    unittest.main()

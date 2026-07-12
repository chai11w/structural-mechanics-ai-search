from pathlib import Path
import unittest
from unittest.mock import patch

from tiku_agent.tools import (
    AgentToolConfig,
    analyze_multi_question_tool,
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

    def test_multi_question_tool_prepares_questions_and_isolated_crops(self):
        class FakeQwen:
            def analyze_layout(self, _image_path):
                return {
                    "question_layout": "multi",
                    "questions": [
                        {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter_hint": "2静定结构", "chapter_confidence": 0.9},
                        {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter_hint": "unknown", "chapter_confidence": 0.0},
                    ],
                }

        with (
            patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()),
            patch(
                "tiku_agent.tools.prepare_multi_diagram_crops",
                return_value={"1": "runtime/multi_diagrams/q1.jpg", "2": "runtime/multi_diagrams/q2.jpg"},
            ),
        ):
            result = analyze_multi_question_tool("multi.jpg", config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertTrue(result.data["is_multi"])
        self.assertEqual(result.next_state, "WAIT_QUESTION_CHOICE")
        self.assertEqual(result.data["questions"][0]["chapter"], "2静定结构")
        self.assertEqual(result.data["questions"][1]["question_image_path"], "runtime/multi_diagrams/q2.jpg")

    def test_multi_question_tool_keeps_single_image_on_single_flow(self):
        class FakeQwen:
            def analyze_layout(self, _image_path):
                return {"question_layout": "single", "questions": []}

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_multi_question_tool("single.jpg", config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertFalse(result.data["is_multi"])
        self.assertEqual(result.next_state, "READY_FOR_SINGLE_ANALYSIS")

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

    def test_agent_rerank_skips_model_when_no_candidate_reaches_threshold(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.50, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.40, "name": "q2.jpg"},
        ]

        with patch("tiku_agent.tools.search.rerank_candidates") as rerank:
            result = rerank_candidates_tool("query.jpg", candidates, route="main", rerank_top=3)

        self.assertTrue(result.ok)
        self.assertFalse(result.data["reranked"])
        self.assertEqual(rerank.call_count, 0)
        self.assertIn("粗筛", result.data["rerank_note"])

    def test_agent_rerank_falls_back_to_coarse_candidates_when_incomplete(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.9, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.8, "name": "q2.jpg"},
        ]
        incomplete = [
            {
                "rank": 1,
                "path": "q1.jpg",
                "score": 0.9,
                "rerank_status": "incomplete",
                "rerank_reason": "部分候选两次复筛仍未完成，已回退粗筛排序。",
            }
        ]

        with patch("tiku_agent.tools.search.rerank_candidates", return_value=incomplete):
            result = rerank_candidates_tool("query.jpg", candidates, route="main", rerank_top=3)

        self.assertTrue(result.ok)
        self.assertFalse(result.data["reranked"])
        self.assertEqual([item["path"] for item in result.data["visible_candidates"]], ["q1.jpg", "q2.jpg"])
        self.assertTrue(all(item["rerank_status"] == "incomplete" for item in result.data["visible_candidates"]))
        self.assertIn("回退粗筛", result.data["rerank_note"])


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import json
import unittest
from unittest.mock import patch

import pandas as pd

from tiku_agent.tools import (
    AgentToolConfig,
    analyze_multi_image_tool,
    classify_structure_tool,
    global_search_tool,
    parse_candidate_action_tool,
    rerank_candidates_tool,
    prepare_question_units_tool,
    route_bank_tool,
)

class TikuAgentToolsTest(unittest.TestCase):
    def test_global_search_reranks_every_deduplicated_perfect_candidate(self):
        loads = [{"type": "集中", "raw": "P"}]
        query = Path("query.jpg")
        frames = {
            "2静定结构": pd.DataFrame(
                [
                    {"题目名称": "query.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)},
                    {"题目名称": "other.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)},
                ]
            ),
            "4力法": pd.DataFrame(
                [{"题目名称": "same.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)}]
            ),
        }

        def fake_score(_query, candidate, **_kwargs):
            item = dict(candidate)
            item["rerank_status"] = "completed"
            item["rerank_score"] = 1.0 if candidate["content_hash"] == "same" else 0.95
            return item

        with patch("tiku_agent.tools.CHAPTERS", ["2静定结构", "4力法"]), patch(
            "tiku_agent.tools.load_bank_excel",
            side_effect=lambda _root, chapter: frames.get(chapter),
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
            "tiku_agent.tools._file_sha256",
            side_effect=lambda path: "same" if Path(path).name in {"query.jpg", "same.jpg"} else "other",
        ), patch(
            "tiku_agent.tools.search.score_rerank_candidate",
            side_effect=fake_score,
        ) as scorer:
            result = global_search_tool(
                loads,
                query,
                route="main",
                config=AgentToolConfig(global_rerank_threshold=0.95),
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["coarse_candidate_count"], 2)
        self.assertEqual(result.data["model_calls"], 2)
        self.assertEqual(result.data["retry_model_calls"], 0)
        self.assertEqual(scorer.call_count, 2)
        self.assertEqual(len(result.data["candidates"]), 1)
        self.assertEqual(
            result.data["candidates"][0]["source_chapters"],
            ["2静定结构", "4力法"],
        )
        self.assertEqual(result.data["candidates"][0]["rerank_score"], 1.0)

    def test_global_search_rejects_partial_visual_batch(self):
        loads = [{"type": "集中", "raw": "P"}]
        query = Path("query.jpg")
        frame = pd.DataFrame(
            [{"题目名称": "query.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)}]
        )

        def timed_out(_query, candidate, **_kwargs):
            item = dict(candidate)
            item.update({"rerank_status": "timeout", "rerank_score": None})
            return item

        with patch("tiku_agent.tools.CHAPTERS", ["4力法"]), patch(
            "tiku_agent.tools.load_bank_excel", return_value=frame
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
            "tiku_agent.tools._file_sha256", return_value="question"
        ), patch(
            "tiku_agent.tools.search.score_rerank_candidate",
            side_effect=timed_out,
        ) as scorer:
            result = global_search_tool(loads, query, route="main")

        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(result.data["unfinished_candidates"], 1)
        self.assertEqual(result.data["model_calls"], 2)
        self.assertEqual(result.data["retry_model_calls"], 1)
        self.assertEqual(scorer.call_count, 2)
        self.assertIn("未完成", result.error)

    def test_global_search_retries_only_incomplete_candidate_once(self):
        loads = [{"type": "集中", "raw": "P"}]
        query = Path("query.jpg")
        frame = pd.DataFrame(
            [
                {"题目名称": "query.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)},
                {"题目名称": "other.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)},
            ]
        )
        attempts = {"question": 0, "other": 0}

        def complete_on_retry(_query, candidate, **_kwargs):
            content_hash = candidate["content_hash"]
            attempts[content_hash] += 1
            if content_hash == "question" and attempts[content_hash] == 1:
                raise ValueError("malformed model response")
            item = dict(candidate)
            score = 0.98 if content_hash == "question" else 0.94
            item.update({"rerank_status": "completed", "rerank_score": score})
            return item

        with patch("tiku_agent.tools.CHAPTERS", ["4力法"]), patch(
            "tiku_agent.tools.load_bank_excel", return_value=frame
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
            "tiku_agent.tools._file_sha256",
            side_effect=lambda path: "question" if Path(path).name == "query.jpg" else "other",
        ), patch(
            "tiku_agent.tools.search.score_rerank_candidate",
            side_effect=complete_on_retry,
        ) as scorer:
            result = global_search_tool(loads, query, route="main")

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(len(result.data["candidates"]), 1)
        self.assertEqual(result.data["model_calls"], 3)
        self.assertEqual(result.data["retry_model_calls"], 1)
        self.assertEqual(result.data["unfinished_candidates"], 0)
        self.assertEqual(scorer.call_count, 3)
        self.assertEqual(attempts, {"question": 2, "other": 1})

    def test_agent_runtime_is_isolated_from_old_feishu_state(self):
        config = AgentToolConfig()
        self.assertEqual(config.runtime_dir, Path(__file__).resolve().parents[1] / ".tmp_tiku_agent_v2")
        self.assertNotIn(".tmp_feishu_tiku", str(config.runtime_dir))
        self.assertNotIn(".tmp_feishu_tiku", str(config.qwen_cache_path))
        self.assertNotIn(".tmp_feishu_tiku", str(config.answer_output_dir))

    def test_route_bank_symbolic_load(self):
        result = route_bank_tool([{"type": "集中", "raw": "P"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["route"], "symbolic")
        self.assertEqual(result.next_state, "READY_FOR_STRUCTURE")

    def test_multi_image_tool_only_confirms_multi_without_detail_work(self):
        class FakeQwen:
            def analyze_image_scope(self, _image_path):
                return {
                    "question_layout": "multi",
                }

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_multi_image_tool("multi.jpg", config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertTrue(result.data["is_multi"])
        self.assertEqual(result.next_state, "READY_FOR_MULTI_DETAILS")
        self.assertEqual(result.data["questions"], [])

    def test_multi_image_tool_keeps_single_image_on_single_flow(self):
        class FakeQwen:
            def analyze_image_scope(self, _image_path):
                return {"question_layout": "single", "single_analysis": {"loads": [{"type": "集中", "raw": "P"}], "chapter_hint": "4力法"}}

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_multi_image_tool("single.jpg", config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertFalse(result.data["is_multi"])
        self.assertEqual(result.data["single_analysis"]["loads"][0]["raw"], "P")
        self.assertEqual(result.next_state, "READY_FOR_SINGLE_ANALYSIS")

    def test_prepare_question_units_only_attaches_isolated_crops(self):
        questions = [
            {"label": "4", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法"},
            {"label": "5", "loads": [{"type": "均布", "raw": "q"}], "chapter": ""},
        ]
        class FakeQwen:
            def analyze_layout(self, _image_path):
                return {"question_layout": "multi", "questions": questions}

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()), patch(
            "tiku_agent.tools.prepare_multi_diagram_crops",
            return_value={"4": "runtime/multi_diagrams/q4.jpg"},
        ):
            result = prepare_question_units_tool("multi.jpg", questions, config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["questions"][0]["question_image_path"], "runtime/multi_diagrams/q4.jpg")
        self.assertEqual(result.data["questions"][1]["question_image_path"], "")
        self.assertTrue(result.data["has_reliable_crops"])

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

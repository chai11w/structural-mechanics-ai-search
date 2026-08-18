from pathlib import Path
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import search
from tiku_agent.tools import (
    AgentToolConfig,
    ToolOutcome,
    analyze_image_tool,
    analyze_multi_image_tool,
    answer_candidate_tool,
    classify_structure_tool,
    coarse_search_tool,
    global_search_tool,
    parse_candidate_action_tool,
    rerank_candidates_tool,
    prepare_question_units_tool,
    route_bank_tool,
)

class TikuAgentToolsTest(unittest.TestCase):
    @staticmethod
    def _dimension_scan(count: int):
        names = [f"q{index:02d}.jpg" for index in range(1, count + 1)]
        return SimpleNamespace(
            scored=[(1.0, name) for name in names],
            structure_filter_applied=True,
            dimensions_by_name={
                name: {
                    "long_width": "4L×L" if index == count else "3L×L",
                    "single_side": "",
                }
                for index, name in enumerate(names, 1)
            },
        )

    def test_dimension_filter_requires_strictly_more_than_twenty_candidates(self):
        recognizer = Mock()
        config = AgentToolConfig(dimension_filter_enabled=True)
        with patch(
            "tiku_agent.tools.search.scan_chapter_candidates",
            return_value=self._dimension_scan(20),
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools._make_qwen", return_value=recognizer):
            result = coarse_search_tool(
                [{"type": "集中", "raw": "P"}],
                chapter="4力法",
                route="symbolic",
                structure_type="钢架",
                query_image_path="query.jpg",
                config=config,
            )

        self.assertTrue(result.ok)
        recognizer.recognize_dimensions.assert_not_called()
        self.assertFalse(result.data["dimension_filter"]["triggered"])
        self.assertEqual(result.data["dimension_filter"]["reason"], "candidate_count_not_over_20")

    def test_dimension_filter_calls_qwen_once_at_twenty_one_and_filters_full_mismatch(self):
        recognizer = Mock()
        recognizer.recognize_dimensions.return_value = {
            "normalized": {
                "dimensions_verified": True,
                "dimension_state": "full",
                "long": "3L",
                "width": "L",
                "long_width": "3L×L",
            },
            "from_cache": False,
        }
        config = AgentToolConfig(dimension_filter_enabled=True)
        with patch(
            "tiku_agent.tools.search.scan_chapter_candidates",
            return_value=self._dimension_scan(21),
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools._make_qwen", return_value=recognizer):
            result = coarse_search_tool(
                [{"type": "集中", "raw": "P"}],
                chapter="4力法",
                route="symbolic",
                structure_type="钢架",
                query_image_path="query.jpg",
                config=config,
            )

        self.assertTrue(result.ok)
        recognizer.recognize_dimensions.assert_called_once_with("query.jpg", "钢架")
        self.assertEqual(len(result.data["candidates"]), 20)
        self.assertTrue(result.data["dimension_filter"]["applied"])
        self.assertEqual(result.data["dimension_filter"]["mismatches"], 1)

    def test_dimension_filter_model_failure_keeps_original_candidates(self):
        recognizer = Mock()
        recognizer.recognize_dimensions.side_effect = RuntimeError("timeout")
        config = AgentToolConfig(dimension_filter_enabled=True)
        with patch(
            "tiku_agent.tools.search.scan_chapter_candidates",
            return_value=self._dimension_scan(21),
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools._make_qwen", return_value=recognizer):
            result = coarse_search_tool(
                [{"type": "集中", "raw": "P"}],
                chapter="4力法",
                route="symbolic",
                structure_type="钢架",
                query_image_path="query.jpg",
                config=config,
            )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["candidates"]), 21)
        self.assertEqual(result.data["dimension_filter"]["reason"], "recognition_failed")

    def test_coarse_search_continuation_excludes_every_attempted_candidate(self):
        loads = [{"type": "集中", "raw": "P"}]
        frame = pd.DataFrame(
            [
                {"题目名称": f"q{index}.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)}
                for index in range(1, 5)
            ]
        )
        scores = [0.9, 0.8, 0.7, 0.6]
        with patch("tiku_agent.tools.load_bank_excel", return_value=frame), patch(
            "tiku_agent.tools.search.compute_similarity", side_effect=scores * 4
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ):
            first = coarse_search_tool(loads, chapter="4力法", route="main", top_k=2)
            first_keys = [item["candidate_key"] for item in first.data["candidates"]]
            second = coarse_search_tool(
                loads,
                chapter="4力法",
                route="main",
                top_k=2,
                exclude_candidate_keys=first_keys,
            )
            second_keys = [item["candidate_key"] for item in second.data["candidates"]]
            third = coarse_search_tool(
                loads,
                chapter="4力法",
                route="main",
                top_k=2,
                exclude_candidate_keys=first_keys + second_keys,
            )
            third_keys = [item["candidate_key"] for item in third.data["candidates"]]
            fourth = coarse_search_tool(
                loads,
                chapter="4力法",
                route="main",
                top_k=2,
                exclude_candidate_keys=first_keys + second_keys + third_keys,
            )

        self.assertEqual([item["name"] for item in first.data["candidates"]], ["q1.jpg"])
        self.assertTrue(first.data["has_more"])
        self.assertEqual([item["name"] for item in second.data["candidates"]], ["q2.jpg"])
        self.assertTrue(second.data["has_more"])
        self.assertEqual([item["name"] for item in third.data["candidates"]], ["q3.jpg"])
        self.assertTrue(third.data["has_more"])
        self.assertEqual([item["name"] for item in fourth.data["candidates"]], ["q4.jpg"])
        self.assertFalse(fourth.data["has_more"])
        self.assertTrue(set(first_keys).isdisjoint(item["candidate_key"] for item in second.data["candidates"]))

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
            item["final_score"] = search.compute_final_rerank_score(
                item["score"], item["rerank_score"]
            )
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
                config=AgentToolConfig(global_final_score_threshold=0.95),
            )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.tool, "global_search")
        self.assertEqual(result.data["coarse_candidate_count"], 2)
        self.assertEqual(result.data["model_calls"], 2)
        self.assertEqual(result.data["retry_model_calls"], 0)
        self.assertEqual(scorer.call_count, 2)
        self.assertEqual(len(result.data["candidates"]), 2)
        self.assertEqual(
            result.data["candidates"][0]["source_chapters"],
            ["2静定结构", "4力法"],
        )
        self.assertEqual(result.data["candidates"][0]["rerank_score"], 1.0)
        self.assertEqual(result.data["candidates"][1]["rerank_score"], 0.95)
        self.assertEqual(result.data["candidates"][1]["final_score"], 0.975)

    def test_global_symbolic_search_applies_dimension_filter_before_visual_rerank(self):
        loads = [{"type": "集中", "raw": "P"}]
        query = Path("query.jpg")
        recognizer = Mock()
        recognizer.recognize_dimensions.return_value = {
            "normalized": {
                "dimensions_verified": True,
                "dimension_state": "full",
                "long": "3L",
                "width": "L",
                "long_width": "3L×L",
            },
            "from_cache": False,
        }

        def complete_score(_query, candidate, **_kwargs):
            item = dict(candidate)
            item.update({"rerank_status": "completed", "rerank_score": 1.0, "final_score": 1.0})
            return item

        config = AgentToolConfig(
            dimension_filter_enabled=True,
            global_final_score_threshold=0.95,
        )
        with patch("tiku_agent.tools.CHAPTERS", ["4力法"]), patch(
            "tiku_agent.tools.search.scan_chapter_candidates",
            return_value=self._dimension_scan(21),
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
            "tiku_agent.tools._file_sha256",
            side_effect=lambda path: Path(path).name,
        ), patch("tiku_agent.tools._make_qwen", return_value=recognizer), patch(
            "tiku_agent.tools.search.score_rerank_candidate",
            side_effect=complete_score,
        ) as scorer:
            result = global_search_tool(
                loads,
                query,
                route="symbolic",
                structure_type="钢架",
                config=config,
            )

        self.assertTrue(result.ok, result.to_dict())
        recognizer.recognize_dimensions.assert_called_once_with(query, "钢架")
        self.assertEqual(result.data["coarse_candidate_count"], 21)
        self.assertEqual(result.data["rerank_candidate_count"], 20)
        self.assertEqual(result.data["model_calls"], 20)
        self.assertEqual(scorer.call_count, 20)
        self.assertTrue(result.data["dimension_filter"]["applied"])
        self.assertEqual(result.data["dimension_filter"]["mismatches"], 1)
        self.assertNotIn("q21.jpg", [item["name"] for item in result.data["candidates"]])

    def test_global_duplicate_dimension_conflict_is_kept_as_unknown(self):
        loads = [{"type": "集中", "raw": "P"}]
        scans = {
            "2静定结构": SimpleNamespace(
                scored=[(1.0, "same-a.jpg")],
                structure_filter_applied=True,
                dimensions_by_name={"same-a.jpg": {"long_width": "3L×L", "single_side": ""}},
            ),
            "4力法": SimpleNamespace(
                scored=[(1.0, "same-b.jpg")],
                structure_filter_applied=True,
                dimensions_by_name={"same-b.jpg": {"long_width": "4L×L", "single_side": ""}},
            ),
        }
        with patch("tiku_agent.tools.CHAPTERS", list(scans)), patch(
            "tiku_agent.tools.search.scan_chapter_candidates",
            side_effect=lambda _loads, chapter, *_args, **_kwargs: scans[chapter],
        ), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **_kwargs: (Path(name), name, False),
        ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
            "tiku_agent.tools._file_sha256", return_value="same"
        ), patch(
            "tiku_agent.tools.search.score_rerank_candidate",
            side_effect=lambda _query, candidate, **_kwargs: {
                **candidate,
                "rerank_status": "completed",
                "rerank_score": 1.0,
                "final_score": 1.0,
            },
        ):
            result = global_search_tool(
                loads,
                Path("query.jpg"),
                route="symbolic",
                structure_type="钢架",
                config=AgentToolConfig(dimension_filter_enabled=True),
            )

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.data["rerank_candidate_count"], 1)
        candidate = result.data["candidates"][0]
        self.assertTrue(candidate["dimension_metadata_conflict"])
        self.assertEqual(candidate["long_width"], "")
        self.assertEqual(candidate["single_side"], "")
        self.assertFalse(result.data["dimension_filter"]["triggered"])

    def test_global_search_accepts_final_score_equal_to_ninety_five_on_both_routes(self):
        loads = [{"type": "集中", "raw": "P"}]
        query = Path("query.jpg")
        frame = pd.DataFrame(
            [{"题目名称": "query.jpg", "荷载": json.dumps({"loads": loads}, ensure_ascii=False)}]
        )

        def score_at_boundary(_query, candidate, **_kwargs):
            item = dict(candidate)
            item.update(
                {
                    "rerank_status": "completed",
                    "rerank_score": 0.9,
                    "final_score": search.compute_final_rerank_score(item["score"], 0.9),
                }
            )
            return item

        for route in ("main", "symbolic"):
            with self.subTest(route=route), patch(
                "tiku_agent.tools.CHAPTERS", ["2静定结构"]
            ), patch(
                "tiku_agent.tools.load_bank_excel", return_value=frame
            ), patch(
                "tiku_agent.tools.search.resolve_question_path",
                side_effect=lambda name, **_kwargs: (Path(name), name, False),
            ), patch("tiku_agent.tools.Path.is_file", return_value=True), patch(
                "tiku_agent.tools._file_sha256", return_value="question"
            ), patch(
                "tiku_agent.tools.search.score_rerank_candidate",
                side_effect=score_at_boundary,
            ):
                result = global_search_tool(loads, query, route=route)

            self.assertEqual(result.outcome, ToolOutcome.SUCCESS)
            self.assertEqual(len(result.data["candidates"]), 1)
            self.assertEqual(result.data["candidates"][0]["final_score"], 0.95)

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

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.outcome, ToolOutcome.PARTIAL)
        self.assertEqual(result.code, "GLOBAL_RERANK_INCOMPLETE")
        self.assertFalse(result.completed)
        self.assertTrue(result.retryable)
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
            item.update(
                {
                    "rerank_status": "completed",
                    "rerank_score": score,
                    "final_score": search.compute_final_rerank_score(item["score"], score),
                }
            )
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
        self.assertEqual(len(result.data["candidates"]), 2)
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
        self.assertEqual(result.tool, "route_bank")
        self.assertEqual(result.outcome, ToolOutcome.SUCCESS)
        self.assertEqual(result.code, "BANK_ROUTE_SELECTED")
        self.assertEqual(result.data["route"], "symbolic")
        self.assertEqual(result.next_state, "READY_FOR_STRUCTURE")

    def test_route_bank_marks_ambiguous_loads_as_needs_input(self):
        result = route_bank_tool([
            {"type": "集中", "raw": "P"},
            {"type": "均布", "raw": "10"},
        ])

        self.assertEqual(result.outcome, ToolOutcome.NEEDS_INPUT)
        self.assertEqual(result.tool, "route_bank")
        self.assertEqual(result.code, "LOAD_ROUTE_NEEDS_REVIEW")
        self.assertFalse(result.completed)

    def test_analyze_image_marks_unknown_chapter_as_needs_input(self):
        class FakeQwen:
            def classify_image(self, _image_path):
                return {
                    "chapter_hint": "unknown",
                    "chapter_confidence": 0.0,
                    "loads": [{"type": "集中", "raw": "P"}],
                }

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_image_tool("q.jpg", chapter="auto")

        self.assertEqual(result.outcome, ToolOutcome.NEEDS_INPUT)
        self.assertEqual(result.tool, "analyze_image")
        self.assertEqual(result.code, "CHAPTER_REQUIRED")
        self.assertEqual(result.next_state, "WAIT_CHAPTER")

    def test_analyze_image_forwards_a3_context_to_qwen(self):
        seen = []

        class FakeQwen:
            def classify_image(self, _image_path, *, context_text=""):
                seen.append(context_text)
                return {
                    "chapter_hint": "unknown",
                    "chapter_confidence": 0.0,
                    "loads": [{"type": "均布", "raw": "q"}, {"type": "均布", "raw": "q"}],
                }

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_image_tool("q.jpg", chapter="auto", context_text="用力法计算图示结构")

        self.assertEqual(result.outcome, ToolOutcome.NEEDS_INPUT)
        self.assertEqual(seen, ["用力法计算图示结构"])
        self.assertEqual(len(result.data["loads"]), 2)

    def test_multi_image_tool_only_confirms_multi_without_detail_work(self):
        class FakeQwen:
            def analyze_image_scope(self, _image_path):
                return {
                    "question_layout": "multi",
                }

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()):
            result = analyze_multi_image_tool("multi.jpg", config=AgentToolConfig())

        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "analyze_multi_image")
        self.assertEqual(result.outcome, ToolOutcome.SUCCESS)
        self.assertEqual(result.code, "MULTI_QUESTION_DETECTED")
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
        self.assertEqual(result.tool, "analyze_multi_image")
        self.assertEqual(result.code, "SINGLE_QUESTION_DETECTED")
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
        self.assertEqual(result.tool, "prepare_question_units")
        self.assertEqual(result.data["questions"][0]["question_image_path"], "runtime/multi_diagrams/q4.jpg")
        self.assertEqual(result.data["questions"][1]["question_image_path"], "")
        self.assertTrue(result.data["has_reliable_crops"])

    def test_prepare_question_units_marks_crop_fallback_as_partial(self):
        questions = [
            {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法"},
            {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法"},
        ]

        class FakeQwen:
            def analyze_layout(self, _image_path):
                return {"question_layout": "multi", "questions": questions}

        with patch("tiku_agent.tools._make_qwen", return_value=FakeQwen()), patch(
            "tiku_agent.tools.prepare_multi_diagram_crops",
            side_effect=OSError("crop failed"),
        ):
            result = prepare_question_units_tool("multi.jpg", questions)

        self.assertEqual(result.outcome, ToolOutcome.PARTIAL)
        self.assertEqual(result.code, "MULTI_CROPS_UNAVAILABLE")
        self.assertEqual(len(result.data["questions"]), 2)

    def test_structure_tool_skips_non_symbolic_routes(self):
        result = classify_structure_tool(None, route="main")
        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "classify_structure")
        self.assertEqual(result.code, "STRUCTURE_FILTER_NOT_APPLICABLE")
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
        invalid = parse_candidate_action_tool("x", candidate_count=3)
        self.assertEqual(invalid.tool, "parse_candidate_action")
        self.assertEqual(invalid.outcome, ToolOutcome.NEEDS_INPUT)
        self.assertEqual(invalid.code, "CANDIDATE_NUMBER_REQUIRED")

    def test_coarse_search_empty_result_is_no_match(self):
        scan = SimpleNamespace(scored=[], structure_filter_applied=False)
        with patch("tiku_agent.tools.search.scan_chapter_candidates", return_value=scan):
            result = coarse_search_tool(
                [{"type": "集中", "raw": "P"}],
                chapter="4力法",
                route="main",
            )

        self.assertEqual(result.outcome, ToolOutcome.NO_MATCH)
        self.assertEqual(result.tool, "coarse_search")
        self.assertEqual(result.code, "NO_COARSE_CANDIDATES")
        self.assertTrue(result.completed)

    def test_agent_rerank_runs_even_when_candidate_count_does_not_exceed_top(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.75, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.70, "name": "q2.jpg"},
            {"rank": 3, "path": "q3.jpg", "score": 0.40, "name": "q3.jpg"},
        ]

        def fake_rerank(query_image_path, rerank_input, top_n=3):
            self.assertEqual(query_image_path, "query.jpg")
            self.assertEqual([item["path"] for item in rerank_input], ["q1.jpg"])
            self.assertEqual(top_n, 3)
            return [
                {
                    "rank": 1,
                    "path": "q1.jpg",
                    "name": "q1.jpg",
                    "score": 0.75,
                    "rerank_score": 0.95,
                    "final_score": 0.85,
                    "rerank_status": "completed",
                }
            ]

        with patch("tiku_agent.tools.search.rerank_candidates", side_effect=fake_rerank) as rerank:
            result = rerank_candidates_tool("query.jpg", candidates, route="main", rerank_top=3)

        self.assertTrue(result.ok)
        self.assertTrue(result.data["reranked"])
        self.assertEqual(result.code, "RERANK_COMPLETED")
        self.assertEqual(rerank.call_count, 1)
        self.assertEqual(result.data["visible_candidates"][0]["final_score"], 0.85)

    def test_agent_rerank_returns_no_match_when_best_final_score_is_below_eighty_percent(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.75, "name": "q1.jpg"},
        ]
        low_result = [{
            "rank": 1,
            "path": "q1.jpg",
            "name": "q1.jpg",
            "score": 0.75,
            "rerank_score": 0.0,
            "final_score": 0.375,
            "rerank_status": "completed",
        }]

        with patch("tiku_agent.tools.search.rerank_candidates", return_value=low_result):
            result = rerank_candidates_tool("query.jpg", candidates, route="main")

        self.assertEqual(result.outcome, ToolOutcome.NO_MATCH)
        self.assertEqual(result.code, "NO_RELIABLE_RERANK_CANDIDATES")
        self.assertEqual(result.data["visible_candidates"], [])
        self.assertEqual(result.data["best_final_score"], 0.375)
        self.assertIn("可靠相似题", result.error)

    def test_agent_rerank_skips_model_when_no_candidate_reaches_threshold(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.50, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.40, "name": "q2.jpg"},
        ]

        with patch("tiku_agent.tools.search.rerank_candidates") as rerank:
            result = rerank_candidates_tool("query.jpg", candidates, route="main", rerank_top=3)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "rerank_candidates")
        self.assertFalse(result.data["reranked"])
        self.assertEqual(result.outcome, ToolOutcome.SUCCESS)
        self.assertEqual(result.code, "RERANK_NOT_REQUIRED")
        self.assertEqual(rerank.call_count, 0)
        self.assertEqual([item["path"] for item in result.data["visible_candidates"]], ["q1.jpg"])
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
        self.assertEqual(result.outcome, ToolOutcome.PARTIAL)
        self.assertEqual(result.code, "RERANK_INCOMPLETE_COARSE_FALLBACK")
        self.assertFalse(result.data["reranked"])
        self.assertEqual([item["path"] for item in result.data["visible_candidates"]], ["q1.jpg"])
        self.assertTrue(all(item["rerank_status"] == "incomplete" for item in result.data["visible_candidates"]))
        self.assertIn("回退粗筛", result.data["rerank_note"])

    def test_answer_lookup_without_files_is_no_match(self):
        candidates = [{"rank": 1, "path": "q1.jpg"}]
        with patch("tiku_agent.tools.search.find_answer_files", return_value=[]):
            result = answer_candidate_tool(candidates, rank=1, copy_to_output=False)

        self.assertEqual(result.outcome, ToolOutcome.NO_MATCH)
        self.assertEqual(result.tool, "answer_candidate")
        self.assertEqual(result.code, "ANSWER_FILES_NOT_FOUND")
        self.assertEqual(result.next_state, "WAIT_CANDIDATE_CHOICE")
        self.assertTrue(result.completed)

    def test_agent_without_query_image_uses_coarse_display_policy(self):
        candidates = [
            {"rank": 1, "path": "q1.jpg", "score": 0.9, "name": "q1.jpg"},
            {"rank": 2, "path": "q2.jpg", "score": 0.8, "name": "q2.jpg"},
        ]

        result = rerank_candidates_tool(None, candidates, route="main")

        self.assertTrue(result.ok)
        self.assertFalse(result.data["reranked"])
        self.assertEqual(result.outcome, ToolOutcome.PARTIAL)
        self.assertEqual(result.code, "RERANK_SKIPPED_NO_IMAGE")
        self.assertEqual([item["path"] for item in result.data["visible_candidates"]], ["q1.jpg"])


if __name__ == "__main__":
    unittest.main()

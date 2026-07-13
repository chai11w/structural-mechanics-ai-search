import unittest
import urllib.error
from unittest.mock import patch

import search
from multi_agent_pipeline import RuleRouter
from scripts.classify_question_bank import (
    CHAPTER_UNKNOWN,
    IMAGE_SCOPE_PROMPT,
    SYSTEM_PROMPT as CLASSIFIER_SYSTEM_PROMPT,
    guard_chapter_prediction,
    normalize_image_scope_result,
    request_json_with_retry,
)


class AssignedSymbolicRoutingTest(unittest.TestCase):
    def test_transient_http_500_is_retried(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        failure = urllib.error.HTTPError("https://example.test", 500, "server error", {}, None)
        with patch("scripts.classify_question_bank.urllib.request.urlopen", side_effect=[failure, Response()]) as opened, patch(
            "scripts.classify_question_bank.time.sleep"
        ):
            result = request_json_with_retry(object(), timeout=3, retry_delays=(0.01,))

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opened.call_count, 2)

    def test_non_transient_http_400_is_not_retried(self):
        failure = urllib.error.HTTPError("https://example.test", 400, "bad request", {}, None)
        with patch("scripts.classify_question_bank.urllib.request.urlopen", side_effect=failure) as opened:
            with self.assertRaises(urllib.error.HTTPError):
                request_json_with_retry(object(), timeout=3, retry_delays=(0.01,))

        self.assertEqual(opened.call_count, 1)

    def test_fast_scope_prompt_preserves_repeated_independent_loads(self):
        self.assertIn("不要把相同标注的多个荷载合并", IMAGE_SCOPE_PROMPT)
        self.assertIn("三个分别标为 Fp", IMAGE_SCOPE_PROMPT)

    def test_fast_scope_prompt_requires_assignment_backfill_for_fractional_label(self):
        self.assertIn("Fp=28kN", IMAGE_SCOPE_PROMPT)
        self.assertIn("Fp 或 Fp/2", IMAGE_SCOPE_PROMPT)
        self.assertIn("不得只输出 Fp 或 Fp/2", IMAGE_SCOPE_PROMPT)

    def test_pure_truss_description_cannot_auto_select_chapter_two(self):
        chapter, confidence, evidence = guard_chapter_prediction(
            "2静定结构",
            0.92,
            "图中是桁架结构，可求杆件轴力",
        )

        self.assertEqual(chapter, CHAPTER_UNKNOWN)
        self.assertLess(confidence, 0.5)
        self.assertIn("自动降级为unknown", evidence)

    def test_single_scope_with_only_diagram_reason_waits_for_chapter(self):
        result = normalize_image_scope_result(
            {
                "question_layout": "single",
                "loads": [{"type": "集中", "raw": "Fp"}],
                "chapter_hint": "2静定结构",
                "chapter_confidence": 0.92,
                "visible_problem_text": "",
                "chapter_evidence": "该结构为桁架，荷载作用于节点",
            }
        )

        self.assertEqual(result["single_analysis"]["chapter_hint"], CHAPTER_UNKNOWN)

    def test_unquoted_visible_static_beam_task_can_select_chapter_two(self):
        result = normalize_image_scope_result(
            {
                "question_layout": "single",
                "loads": [{"type": "均布", "raw": "q"}, {"type": "集中", "raw": "ql"}],
                "chapter_hint": "2静定结构",
                "chapter_confidence": 0.9,
                "visible_problem_text": "求图示多跨静定梁的弯矩图和剪力图",
                "chapter_evidence": "求图示多跨静定梁的弯矩图和剪力图",
            }
        )

        self.assertEqual(result["single_analysis"]["chapter_hint"], "2静定结构")
        self.assertEqual(result["single_analysis"]["visible_problem_text"], "求图示多跨静定梁的弯矩图和剪力图")

    def test_visible_labels_without_problem_text_cannot_select_chapter(self):
        result = normalize_image_scope_result(
            {
                "question_layout": "single",
                "loads": [{"type": "均布", "raw": "q"}],
                "chapter_hint": "2静定结构",
                "chapter_confidence": 0.9,
                "visible_problem_text": "",
                "chapter_evidence": "图中是静定梁，可绘制弯矩图",
            }
        )

        self.assertEqual(result["single_analysis"]["chapter_hint"], CHAPTER_UNKNOWN)
        self.assertEqual(result["single_analysis"]["chapter_evidence"], "未识别到可见题干文字")

    def test_quoted_visible_static_truss_text_can_select_chapter_two(self):
        chapter, confidence, _ = guard_chapter_prediction(
            "2静定结构",
            0.9,
            "题干原文：“求静定桁架指定杆的轴力”",
        )

        self.assertEqual(chapter, "2静定结构")
        self.assertEqual(confidence, 0.9)

    def test_unquoted_force_method_text_is_explicit_chapter_evidence(self):
        chapter, confidence, evidence = guard_chapter_prediction(
            "4力法",
            1.0,
            "用力法计算下图所示桁架的轴力",
        )

        self.assertEqual(chapter, "4力法")
        self.assertEqual(confidence, 1.0)
        self.assertEqual(evidence, "用力法计算下图所示桁架的轴力")

    def test_load_prompt_requires_unitless_assigned_symbols(self):
        self.assertIn("输出 P=40、q=20、F1=40、M=20", search.SYSTEM_PROMPT)
        self.assertIn("不要输出 P=40kN 或 q=20kN/m", search.SYSTEM_PROMPT)
        self.assertIn("P=40、q=20、F1=40、M=20", CLASSIFIER_SYSTEM_PROMPT)

    def test_postprocess_keeps_assignment_and_removes_units(self):
        extracted = search.postprocess_extracted_loads(
            {
                "loads": [
                    {"type": "集中", "raw": "P=40kN"},
                    {"type": "均布", "raw": "q=20kN/m"},
                    {"type": "弯矩", "raw": "M=20kN·m"},
                ]
            }
        )

        self.assertEqual([item["raw"] for item in extracted["loads"]], ["P=40", "q=20", "M=20"])

    def test_assigned_symbols_route_main_and_unassigned_symbols_route_symbolic(self):
        router = RuleRouter()
        assigned_cases = [
            {"type": "集中", "raw": "P=40"},
            {"type": "均布", "raw": "q=20"},
            {"type": "弯矩", "raw": "M=20"},
        ]
        unassigned_cases = [
            {"type": "集中", "raw": "P"},
            {"type": "均布", "raw": "q"},
            {"type": "弯矩", "raw": "M"},
        ]

        for load in assigned_cases:
            with self.subTest(load=load):
                decision, _ = router.route([load])
                self.assertEqual(decision.route, "main")
                self.assertEqual(decision.category, "main_assigned_symbolic")

        for load in unassigned_cases:
            with self.subTest(load=load):
                decision, _ = router.route([load])
                self.assertEqual(decision.route, "symbolic")
                self.assertEqual(decision.category, "symbolic_unassigned")


if __name__ == "__main__":
    unittest.main()

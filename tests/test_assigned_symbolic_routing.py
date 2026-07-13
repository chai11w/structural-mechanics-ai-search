import unittest

import search
from multi_agent_pipeline import RuleRouter
from scripts.classify_question_bank import (
    CHAPTER_UNKNOWN,
    IMAGE_SCOPE_PROMPT,
    SYSTEM_PROMPT as CLASSIFIER_SYSTEM_PROMPT,
    guard_chapter_prediction,
    normalize_image_scope_result,
)


class AssignedSymbolicRoutingTest(unittest.TestCase):
    def test_fast_scope_prompt_preserves_repeated_independent_loads(self):
        self.assertIn("不要把相同标注的多个荷载合并", IMAGE_SCOPE_PROMPT)
        self.assertIn("三个分别标为 Fp", IMAGE_SCOPE_PROMPT)

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
                "chapter_evidence": "该结构为桁架，荷载作用于节点",
            }
        )

        self.assertEqual(result["single_analysis"]["chapter_hint"], CHAPTER_UNKNOWN)

    def test_quoted_visible_static_truss_text_can_select_chapter_two(self):
        chapter, confidence, _ = guard_chapter_prediction(
            "2静定结构",
            0.9,
            "题干原文：“求静定桁架指定杆的轴力”",
        )

        self.assertEqual(chapter, "2静定结构")
        self.assertEqual(confidence, 0.9)

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

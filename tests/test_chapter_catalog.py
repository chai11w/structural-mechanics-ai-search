import unittest

from tiku_shared.chapter_catalog import (
    CHAPTER_DEFINITIONS,
    SUPPORTED_STORAGE_KEYS,
    detect_non_chinese_problem_text,
    parse_chapter_scope,
    resolve_image_scope,
    resolve_supported_chapter,
    supported_topic_names,
)


class ChapterCatalogTest(unittest.TestCase):
    def test_catalog_preserves_the_seven_storage_keys(self):
        self.assertEqual(
            SUPPORTED_STORAGE_KEYS,
            (
                "2静定结构",
                "3静定结构位移",
                "4力法",
                "5位移法",
                "6力矩分配",
                "7矩阵位移",
                "8影响线",
            ),
        )
        self.assertEqual(len({item.topic_id for item in CHAPTER_DEFINITIONS}), 7)
        self.assertEqual(len(supported_topic_names()), 7)

    def test_supported_method_aliases_map_to_one_storage_key(self):
        for text in ("力法", "弯矩分配", "弯矩分配法", "渐近法", "矩阵位移法", "图乘法"):
            with self.subTest(text=text):
                result = parse_chapter_scope(text)
                self.assertEqual(result.status, "supported")
                self.assertIsNotNone(result.topic_id)
                self.assertIsNotNone(result.storage_key)

        self.assertEqual(parse_chapter_scope("弯矩分配法").storage_key, "6力矩分配")
        self.assertEqual(parse_chapter_scope("弯矩分配").storage_key, "6力矩分配")
        self.assertEqual(parse_chapter_scope("渐近法").storage_key, "6力矩分配")

    def test_generic_force_or_distribution_words_do_not_select_a_chapter(self):
        for text in ("内力", "内力图", "剪力分配", "分配法"):
            with self.subTest(text=text):
                self.assertEqual(parse_chapter_scope(text).status, "uncertain")

    def test_negative_static_prefix_does_not_match_static_alias(self):
        for text in ("超静定梁", "不静定结构位移", "非静定钢架"):
            with self.subTest(text=text):
                self.assertEqual(parse_chapter_scope(text).status, "uncertain")

    def test_english_questions_are_out_of_scope_but_foreign_is_not_a_topic_alias(self):
        result = parse_chapter_scope("英文题目")
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "non_chinese_question")
        self.assertEqual(parse_chapter_scope("国外题").status, "uncertain")

    def test_non_empty_ocr_description_without_chinese_is_rejected(self):
        result = detect_non_chinese_problem_text("Find the bending moment of the beam")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "non_chinese_question")
        self.assertEqual(
            detect_non_chinese_problem_text("Calculate M=20 for the beam").status,
            "unsupported",
        )

        self.assertIsNone(detect_non_chinese_problem_text("求图示梁的弯矩图，EI=200"))
        self.assertIsNone(detect_non_chinese_problem_text("EI=200, P=20"))
        self.assertIsNone(detect_non_chinese_problem_text(""))

    def test_image_scope_keeps_empty_description_in_clarification(self):
        result = resolve_image_scope("4力法", 0.99, "")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.reason, "missing_visible_problem_text")

    def test_image_scope_rejects_non_chinese_description_before_model_hint(self):
        result = resolve_image_scope("4力法", 0.99, "Find the bending moment of the beam")
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "non_chinese_question")
        self.assertEqual(result.reason, "non_chinese_problem_text")

    def test_image_scope_treats_formula_only_text_as_missing_description(self):
        result = resolve_image_scope("4力法", 0.99, "EI=200, P=20")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.reason, "missing_visible_problem_text")

    def test_image_scope_uses_explicit_text_before_model_hint(self):
        result = resolve_image_scope("5位移法", 0.99, "请用力法计算超静定梁")
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.storage_key, "4力法")
        self.assertEqual(result.reason, "visible_problem_text_supported")

    def test_image_scope_stops_on_explicit_unsupported_text(self):
        result = resolve_image_scope("4力法", 0.99, "求结构动力学的自振频率")
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "structural_dynamics")
        self.assertEqual(result.reason, "visible_problem_text_unsupported")

    def test_image_scope_requires_confidence_for_model_hint(self):
        high = resolve_image_scope("4力法", 0.90, "这是一道结构力学题")
        self.assertEqual(high.status, "supported")
        self.assertEqual(high.storage_key, "4力法")
        self.assertEqual(high.reason, "model_chapter_hint")

        low = resolve_image_scope("4力法", 0.40, "这是一道结构力学题")
        self.assertEqual(low.status, "uncertain")
        self.assertEqual(low.reason, "low_confidence_chapter_hint")

    def test_image_scope_rejects_unsupported_model_hint(self):
        result = resolve_image_scope("动力学", 0.99, "这是一道结构力学题")
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "structural_dynamics")
        self.assertEqual(result.reason, "model_chapter_hint_unsupported")

    def test_image_scope_does_not_treat_numeric_hint_as_storage_key(self):
        result = resolve_image_scope("4", 0.99, "这是一道结构力学题")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.reason, "no_valid_chapter_evidence")

    def test_explicit_unsupported_topics_stop_before_numeric_chapter(self):
        result = parse_chapter_scope("第九章动力学")
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.topic_id, "structural_dynamics")
        self.assertIsNone(result.storage_key)

    def test_pure_textbook_chapter_number_is_uncertain(self):
        for text in ("第九章", "第4章", "4"):
            with self.subTest(text=text):
                result = parse_chapter_scope(text)
                self.assertEqual(result.status, "uncertain")
                self.assertEqual(result.reason, "numeric_chapter_requires_textbook")
                self.assertIsNone(result.storage_key)

    def test_unrecognized_expression_is_uncertain(self):
        result = parse_chapter_scope("好像是研究振动的那部分")
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.reason, "no_explicit_topic_evidence")

    def test_legacy_resolver_can_still_read_internal_numbers(self):
        self.assertEqual(resolve_supported_chapter("4", allow_numeric=True), "4力法")
        self.assertIsNone(resolve_supported_chapter("按位移重新搜", allow_numeric=True))
        self.assertIsNone(resolve_supported_chapter("第九章", allow_numeric=True))


if __name__ == "__main__":
    unittest.main()

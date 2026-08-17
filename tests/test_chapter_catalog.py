import unittest

from tiku_shared.chapter_catalog import (
    CHAPTER_DEFINITIONS,
    SUPPORTED_STORAGE_KEYS,
    parse_chapter_scope,
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
        for text in ("力法", "弯矩分配法", "渐近法", "矩阵位移法", "图乘法"):
            with self.subTest(text=text):
                result = parse_chapter_scope(text)
                self.assertEqual(result.status, "supported")
                self.assertIsNotNone(result.topic_id)
                self.assertIsNotNone(result.storage_key)

        self.assertEqual(parse_chapter_scope("弯矩分配法").storage_key, "6力矩分配")
        self.assertEqual(parse_chapter_scope("渐近法").storage_key, "6力矩分配")

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
        self.assertEqual(resolve_supported_chapter("按位移重新搜", allow_numeric=True), "3静定结构位移")
        self.assertIsNone(resolve_supported_chapter("第九章", allow_numeric=True))


if __name__ == "__main__":
    unittest.main()

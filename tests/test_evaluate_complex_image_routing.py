import json
from pathlib import Path
import tempfile
import unittest

from scripts.evaluate_complex_image_routing import (
    extract_route,
    load_prompt,
    resolve_labeled_directory,
    resolve_samples,
)


class EvaluateComplexImageRoutingTest(unittest.TestCase):
    def test_load_prompt_extracts_only_text_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prompt.md"
            path.write_text("说明\n```text\n第一行\n第二行\n```\n边界", encoding="utf-8")
            self.assertEqual(load_prompt(path), "第一行\n第二行")

    def test_extract_route_accepts_standalone_route_only(self):
        self.assertEqual(extract_route("建议路线：A3"), "A3")
        self.assertEqual(extract_route("a2，理由如下"), "A2")
        self.assertIsNone(extract_route("图中有 A30 标注"))

    def test_resolve_tracked_samples_without_question_bank(self):
        samples = resolve_samples(include_question_bank=False, config={})
        self.assertEqual(len(samples), 8)
        self.assertTrue(all(item["image_path"].is_file() for item in samples))
        self.assertTrue(all(item["source_kind"] != "question_bank" for item in samples))

    def test_resolve_user_labeled_a1_a2_a3_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for route in ("A1", "A2", "A3"):
                folder = root / route
                folder.mkdir()
                (folder / f"{route.lower()}.jpg").write_bytes(b"test")

            samples = resolve_labeled_directory(root)

        self.assertEqual([item["expected_route"] for item in samples], ["A1", "A2", "A3"])
        self.assertTrue(all(item["label_status"] == "user_labeled" for item in samples))


if __name__ == "__main__":
    unittest.main()

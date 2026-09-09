from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multi_agent_pipeline import QwenClassifier
from tiku_shared.execution_hooks import execution_effect_scope


class DimensionRecognitionCacheTests(unittest.TestCase):
    def test_durable_execution_does_not_reuse_or_replace_unversioned_caches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "question.jpg"
            image.write_bytes(b"same-image")
            classifier = QwenClassifier(cache_path=root / "loads.json", dimension_cache_path=root / "dimensions.json")
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY":"test"}), patch(
                    "multi_agent_pipeline.call_dimension_qwen", return_value=({"long":"L"}, {"total_tokens":10}, "{}")) as dimensions, patch(
                    "multi_agent_pipeline.qwen_extract_loads", return_value={"loads":[], "visible_problem_text":"用力法求解"}) as loads:
                classifier.recognize_dimensions(image, "钢架")
                classifier.classify_image(image)
                prior = {path:path.read_bytes() for path in (classifier.cache_path, classifier.dimension_cache_path)}
                for _ in range(2):
                    # Only exercise actual cache admission; provider adapters
                    # are replaced and no network request can be made.
                    with execution_effect_scope(object()):
                        self.assertFalse(classifier.recognize_dimensions(image, "钢架")["from_cache"])
                        self.assertFalse(classifier.classify_image(image)["from_cache"])
                self.assertEqual(dimensions.call_count, 3)
                self.assertEqual(loads.call_count, 3)
                for path, content in prior.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertTrue(classifier.recognize_dimensions(image, "钢架")["from_cache"])
                self.assertTrue(classifier.classify_image(image)["from_cache"])

    def test_same_image_model_prompt_and_structure_use_one_provider_call(self):
        normalized = {
            "structure_type": "钢架",
            "dimensions_verified": True,
            "dimension_state": "full",
            "long": "3L",
            "width": "L",
            "long_width": "3L×L",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "question.jpg"
            image.write_bytes(b"same-image")
            classifier = QwenClassifier(
                cache_path=root / "loads.json",
                dimension_cache_path=root / "dimensions.json",
            )
            with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test"}), patch(
                "multi_agent_pipeline.call_dimension_qwen",
                return_value=(normalized, {"total_tokens": 10}, "{}"),
            ) as provider:
                first = classifier.recognize_dimensions(image, "钢架")
                second = classifier.recognize_dimensions(image, "钢架")

            provider.assert_called_once()
            self.assertFalse(first["from_cache"])
            self.assertTrue(second["from_cache"])
            self.assertEqual(second["normalized"]["long_width"], "3L×L")
            self.assertTrue((root / "dimensions.json").is_file())


if __name__ == "__main__":
    unittest.main()

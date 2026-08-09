from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multi_agent_pipeline import QwenClassifier


class DimensionRecognitionCacheTests(unittest.TestCase):
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

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_structure_dimensions import (
    Sample,
    build_payload,
    load_manifest,
    load_saved_provider_results,
    mcp_results_template,
    normalize_dimension,
    normalize_provider_result,
    size_key,
)


class StructureDimensionEvaluationTests(unittest.TestCase):
    def test_normalize_dimension_only_accepts_one_simplified_expression(self):
        dimension = normalize_dimension("2.5m")
        self.assertIsNotNone(dimension)
        self.assertEqual(dimension.raw, "2.5m")
        self.assertEqual(dimension.symbol, "m")
        self.assertEqual(normalize_dimension("3l+2l"), None)
        self.assertEqual(normalize_dimension("l/2"), None)
        self.assertEqual(normalize_dimension("-2m"), None)
        self.assertEqual(normalize_dimension("a").coefficient, 1)
        self.assertEqual(normalize_dimension(None), None)

    def test_size_key_requires_same_dimension_symbol(self):
        self.assertEqual(size_key("6m", "3m"), "2:1")
        self.assertEqual(size_key("4a", "2a"), "2:1")
        self.assertEqual(size_key("3l", "2h"), "unknown")
        self.assertEqual(size_key("6m", "0"), "flat")
        self.assertEqual(size_key(None, "2m"), "unknown")

    def test_provider_normalization_is_conservative(self):
        result = normalize_provider_result(
            {
                "structure_type": "错误枚举",
                "total_span": "3l+2l",
                "total_height": "2l",
                "confidence": "1.4",
                "reason": "test",
            }
        )
        self.assertEqual(result["structure_type"], "unknown")
        self.assertEqual(result["total_span"], None)
        self.assertEqual(result["total_height"], "2l")
        self.assertEqual(result["size_key"], "unknown")
        self.assertEqual(result["confidence"], 1.0)

    def test_manifest_rejects_answer_images_and_keeps_safe_relative_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bank"
            image = root / "2静定结构" / "题目" / "1.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"test")
            manifest = Path(temp_dir) / "samples.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "samples": [
                            {
                                "id": "ok",
                                "path": "2静定结构/题目/1.jpg",
                                "expected_structure_type": "梁",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            samples = load_manifest(manifest, root)
            self.assertEqual(samples[0].sample_id, "ok")

            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "samples": [{"id": "answer", "path": "2静定结构/答案/1.jpg"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_manifest(manifest, root)

    def test_saved_qwen_results_and_mcp_template_are_reusable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            saved_results = Path(temp_dir) / "results.json"
            saved_results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "sample_id": "beam",
                                "qwen": {
                                    "normalized": {
                                        "structure_type": "梁",
                                        "total_span": "6m",
                                        "total_height": "0",
                                        "size_key": "flat",
                                    }
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            reused = load_saved_provider_results(saved_results, "qwen")
            self.assertEqual(reused["beam"]["normalized"]["total_span"], "6m")
        template = mcp_results_template([Sample("beam", "梁", "question.jpg", "single beam")])
        self.assertEqual(template["results"][0]["sample_id"], "beam")
        self.assertIsNone(template["results"][0]["total_span"])

    def test_payload_counts_only_comparable_model_results(self):
        sample = Sample("beam", "梁", "question.jpg", "single beam")
        payload = build_payload(
            [sample],
            root=Path("C:/bank"),
            qwen_results={
                "beam": {
                    "normalized": normalize_provider_result(
                        {"structure_type": "梁", "total_span": "6m", "total_height": "0"}
                    )
                }
            },
            mcp_results={
                "beam": {"structure_type": "梁", "total_span": "4m", "total_height": "0"}
            },
            qwen_model="qwen3.7-plus",
            manifest=Path("manifest.json"),
        )
        self.assertEqual(payload["summary"]["type_agreement_count"], 1)
        self.assertEqual(payload["summary"]["size_agreement_count"], 1)
        self.assertEqual(payload["results"][0]["agreement"]["size_key"], "一致")


if __name__ == "__main__":
    unittest.main()

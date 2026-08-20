from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.evaluate_a3_crop_strategies import _relative_url, _stage_source_images
from scripts.run_ppstructurev3_layout import normalize_layout_result
from tiku_agent.a3_crop_strategy_eval import (
    DIRECT_GROUNDING_SCHEMA_VERSION,
    PADDLE_BINDING_SCHEMA_VERSION,
    parse_direct_grounding,
    parse_paddle_binding,
)


class DirectGroundingParserTests(unittest.TestCase):
    def test_accepts_grounded_target(self) -> None:
        parsed = parse_direct_grounding(
            {
                "schema_version": DIRECT_GROUNDING_SCHEMA_VERSION,
                "page_status": "grounded",
                "targets": [
                    {
                        "target_id": "t001",
                        "question_label": "4-1",
                        "group_label": "四",
                        "bbox": [100, 200, 800, 700],
                        "review_required": False,
                        "reason_codes": [],
                        "binding_evidence": "题号与结构图相邻",
                    }
                ],
                "unknowns": [],
            }
        )
        self.assertEqual(parsed["targets"][0]["bbox"], [100, 200, 800, 700])

    def test_rejects_review_target_under_grounded_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "grounded status"):
            parse_direct_grounding(
                {
                    "schema_version": DIRECT_GROUNDING_SCHEMA_VERSION,
                    "page_status": "grounded",
                    "targets": [
                        {
                            "target_id": "t001",
                            "question_label": "",
                            "group_label": "",
                            "bbox": [0, 0, 1000, 1000],
                            "review_required": True,
                            "reason_codes": ["uncertain"],
                            "binding_evidence": "",
                        }
                    ],
                    "unknowns": [],
                }
            )

    def test_rejects_extra_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "keys mismatch"):
            parse_direct_grounding(
                {
                    "schema_version": DIRECT_GROUNDING_SCHEMA_VERSION,
                    "page_status": "no_searchable_target",
                    "targets": [],
                    "unknowns": [],
                    "extra": True,
                }
            )


class PaddleBindingParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            {"candidate_id": "p001", "bbox": [10, 20, 100, 120]},
            {"candidate_id": "p002", "bbox": [90, 30, 180, 150]},
        ]

    def test_derives_union_without_free_coordinates(self) -> None:
        parsed = parse_paddle_binding(
            {
                "schema_version": PADDLE_BINDING_SCHEMA_VERSION,
                "page_status": "auto_ready",
                "bindings": [
                    {
                        "binding_id": "b001",
                        "question_label": "a",
                        "candidate_ids": ["p001", "p002"],
                        "review_required": False,
                        "reason_codes": [],
                        "binding_evidence": "两个候选共同覆盖结构与荷载",
                    }
                ],
                "unknowns": [],
            },
            self.candidates,
        )
        self.assertEqual(parsed["bindings"][0]["bbox"], [10.0, 20.0, 180.0, 150.0])

    def test_rejects_unknown_candidate(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown candidate"):
            parse_paddle_binding(
                {
                    "schema_version": PADDLE_BINDING_SCHEMA_VERSION,
                    "page_status": "review_required",
                    "bindings": [
                        {
                            "binding_id": "b001",
                            "question_label": "",
                            "candidate_ids": ["p999"],
                            "review_required": True,
                            "reason_codes": ["uncertain"],
                            "binding_evidence": "",
                        }
                    ],
                    "unknowns": [],
                },
                self.candidates,
            )


class PPStructureNormalizationTests(unittest.TestCase):
    def test_keeps_all_boxes_but_exposes_visual_candidates_only(self) -> None:
        parsed = normalize_layout_result(
            {
                "layout_det_res": {
                    "boxes": [
                        {"label": "text", "score": 0.9, "coordinate": [0, 0, 50, 40]},
                        {"label": "image", "score": 0.8, "coordinate": [10, 50, 100, 120]},
                        {"label": "chart", "score": 0.7, "coordinate": [110, 50, 200, 120]},
                    ]
                }
            },
            source_name="sample.jpg",
        )
        self.assertEqual(len(parsed["all_boxes"]), 3)
        self.assertEqual([item["candidate_id"] for item in parsed["candidates"]], ["p001", "p002"])
        self.assertEqual([item["label"] for item in parsed["candidates"]], ["image", "chart"])


class ReviewPathTests(unittest.TestCase):
    def test_uses_file_uri_when_windows_drives_differ(self) -> None:
        with patch("scripts.evaluate_a3_crop_strategies.os.path.relpath", side_effect=ValueError):
            value = _relative_url(Path("D:/desktop/A3/1.jpg"), Path("F:/output/run"))
        self.assertTrue(value.startswith("file:"))
        self.assertTrue(value.endswith("/1.jpg"))

    def test_stages_sources_inside_review_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source" / "1.jpg"
            source.parent.mkdir()
            source.write_bytes(b"fixture")
            staged = _stage_source_images([source], root / "output" / "sources")
            target = staged[source.resolve()]
            self.assertEqual(target.read_bytes(), b"fixture")
            self.assertEqual(target.parent.name, "sources")


if __name__ == "__main__":
    unittest.main()

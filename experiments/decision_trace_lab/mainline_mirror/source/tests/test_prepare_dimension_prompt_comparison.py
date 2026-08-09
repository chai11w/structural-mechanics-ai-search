from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.prepare_dimension_prompt_comparison import (
    _percentile,
    select_samples,
    write_disagreement_report,
)


class DimensionPromptComparisonTests(unittest.TestCase):
    def test_select_samples_prioritizes_human_and_unverified_cases(self):
        rows = [
            {
                "path": f"chapter-{index}/题目/{index}.jpg",
                "expected_structure_type": "梁",
                "normalized": {"dimensions_verified": index not in {2, 3}},
            }
            for index in range(1, 6)
        ]

        selected = select_samples(rows, {"chapter-1/题目/1.jpg"}, {"梁": 3})

        self.assertEqual(len(selected), 3)
        self.assertEqual({row["path"] for row in selected}, {
            "chapter-1/题目/1.jpg",
            "chapter-2/题目/2.jpg",
            "chapter-3/题目/3.jpg",
        })
        notes = {row["path"]: row["selection_note"] for row in selected}
        self.assertEqual(notes["chapter-1/题目/1.jpg"], "human_verdict")
        self.assertEqual(notes["chapter-2/题目/2.jpg"], "v4_unverified")

    def test_report_only_expands_three_way_disagreements(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bank"
            root.mkdir()
            for name in ("same.jpg", "different.jpg"):
                Image.new("RGB", (20, 20), "white").save(root / name)
            output = Path(temp_dir) / "report" / "comparison.html"
            samples = [
                {"id": "same", "path": "same.jpg", "expected_structure_type": "梁"},
                {"id": "different", "path": "different.jpg", "expected_structure_type": "桁架"},
            ]
            v4 = {
                "results": [
                    {"path": "same.jpg", "normalized": {"long_width": "2L×0"}},
                    {"path": "different.jpg", "normalized": {"long_width": "4L×L"}},
                ]
            }
            v5 = {
                "results": [
                    {"relative_path": "same.jpg", "qwen": {"seconds": 2.0, "normalized": {"long_width": "2L×0"}}},
                    {"relative_path": "different.jpg", "qwen": {"seconds": 4.0, "normalized": {"long_width": "3L×L"}}},
                ]
            }
            manual = {
                "verdicts": [
                    {"path": "same.jpg", "long_width": "2L×0"},
                    {"path": "different.jpg", "long_width": "3L×L", "reason": "人工复核"},
                ]
            }

            summary = write_disagreement_report(
                manifest={"samples": samples},
                v4_payload=v4,
                v5_payload=v5,
                manual_payload=manual,
                root=root,
                output_path=output,
            )

            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["disagreements"], 1)
            self.assertEqual(summary["v4_manual_matches"], 1)
            self.assertEqual(summary["v5_manual_matches"], 2)
            page = output.read_text(encoding="utf-8")
            self.assertNotIn("same.jpg", page)
            self.assertIn("different.jpg", page)
            self.assertIn("V5 对人工<strong>2 / 2", page)
            self.assertEqual([path.name for path in (output.parent / "images").iterdir()], ["02_different.jpg"])

    def test_report_requires_a_manual_verdict_for_every_sample(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            Image.new("RGB", (10, 10), "white").save(root / "question.jpg")
            with self.assertRaisesRegex(ValueError, "missing manual verdict"):
                write_disagreement_report(
                    manifest={"samples": [{"id": "q", "path": "question.jpg", "expected_structure_type": "梁"}]},
                    v4_payload={"results": []},
                    v5_payload={"results": []},
                    manual_payload={"verdicts": []},
                    root=root,
                    output_path=root / "comparison.html",
                )

    def test_percentile_uses_observed_nearest_rank_floor(self):
        self.assertEqual(_percentile([], 0.95), 0.0)
        self.assertEqual(_percentile([4.0, 1.0, 3.0, 2.0], 0.50), 2.0)
        self.assertEqual(_percentile([4.0, 1.0, 3.0, 2.0], 0.95), 3.0)


if __name__ == "__main__":
    unittest.main()

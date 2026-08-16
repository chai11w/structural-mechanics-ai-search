from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from tiku_agent.paddle_region_candidates import (
    export_paddle_candidate_artifacts,
    parse_paddle_candidate_set,
)


def layout_payload(boxes: list[tuple[float, list[float]]]) -> dict[str, object]:
    return {
        "input_path": "not-retained.jpg",
        "boxes": [
            {
                "cls_id": 1,
                "label": "image",
                "score": score,
                "coordinate": bbox,
            }
            for score, bbox in boxes
        ],
    }


class PaddleRegionCandidatesTest(unittest.TestCase):
    def test_marks_group_box_and_keeps_three_leaf_candidates(self):
        payload = layout_payload(
            [
                (0.55, [10, 10, 290, 90]),
                (0.54, [15, 15, 90, 80]),
                (0.53, [110, 15, 190, 80]),
                (0.52, [210, 15, 285, 80]),
            ]
        )

        result = parse_paddle_candidate_set(payload, source_size=(300, 100))

        self.assertEqual(result.candidates[0].role, "group")
        self.assertEqual(
            result.candidates[0].contained_candidate_ids,
            ("p002", "p003", "p004"),
        )
        self.assertEqual(
            [candidate.role for candidate in result.candidates[1:]],
            ["leaf", "leaf", "leaf"],
        )
        self.assertIn("group_candidates_present", result.reason_codes)

    def test_lower_score_near_duplicate_is_marked(self):
        result = parse_paddle_candidate_set(
            layout_payload(
                [
                    (0.6, [20, 20, 180, 180]),
                    (0.3, [22, 21, 179, 181]),
                ]
            ),
            source_size=(200, 200),
        )

        self.assertEqual(result.candidates[0].role, "leaf")
        self.assertEqual(result.candidates[1].role, "duplicate")
        self.assertEqual(result.candidates[1].duplicate_of, "p001")
        self.assertEqual(result.review_candidate_ids, ("p001",))

    def test_single_nested_box_is_not_mislabeled_as_group(self):
        result = parse_paddle_candidate_set(
            layout_payload(
                [
                    (0.8, [20, 5, 200, 295]),
                    (0.3, [30, 20, 150, 250]),
                ]
            ),
            source_size=(200, 300),
        )

        self.assertEqual(result.candidates[0].role, "single_container")
        self.assertEqual(result.candidates[1].role, "leaf")
        self.assertIn("page_edge_candidates_present", result.reason_codes)
        self.assertIn("content_completeness_not_checked", result.reason_codes)

    def test_ignores_non_image_boxes_and_low_scores(self):
        payload = layout_payload([(0.19, [10, 10, 50, 50])])
        payload["boxes"].append(
            {"label": "text", "score": 0.9, "coordinate": [1, 1, 20, 20]}
        )

        result = parse_paddle_candidate_set(payload, source_size=(100, 100))

        self.assertFalse(result.candidates)
        self.assertEqual(result.reason_codes, ("no_image_candidates",))

    def test_exports_real_crops_overlay_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "source.jpg"
            Image.new("RGB", (200, 100), "white").save(image_path)
            result = parse_paddle_candidate_set(
                layout_payload([(0.7, [20, 10, 180, 90])]),
                source_size=(200, 100),
            )

            manifest_path = export_paddle_candidate_artifacts(
                image_path, result, root / "out"
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "review_required")
            self.assertTrue(Path(manifest["artifacts"]["overlay"]).is_file())
            crop_path = Path(manifest["artifacts"]["crops"]["p001"])
            self.assertTrue(crop_path.is_file())


if __name__ == "__main__":
    unittest.main()

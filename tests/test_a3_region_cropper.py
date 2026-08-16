from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from tiku_agent.a3_region_cropper import (
    clamp_to_neighbor_gaps,
    crop_a3_regions,
    expand_bbox,
    refine_foreground_bbox,
)
from tiku_agent.image_region_mapper import A3CoarseRegion, A3RegionGroup, A3RegionMap


class A3RegionCropperTest(unittest.TestCase):
    def test_expand_bbox_is_clamped(self):
        self.assertEqual(
            expand_bbox((5, 5, 95, 95), (100, 100), ratio=0.5, min_padding_px=10),
            (0, 0, 100, 100),
        )

    def test_refinement_keeps_disconnected_load_and_dimension_marks(self):
        image = Image.new("RGB", (400, 240), "white")
        draw = ImageDraw.Draw(image)
        draw.line((100, 120, 300, 120), fill="black", width=5)
        draw.line((120, 80, 120, 150), fill="black", width=3)
        draw.line((280, 80, 280, 150), fill="black", width=3)
        draw.line((150, 55, 250, 55), fill="black", width=2)
        draw.line((200, 25, 200, 95), fill="black", width=2)
        refined = refine_foreground_bbox(image, (70, 10, 330, 180), content_padding_ratio=0.02)
        self.assertLessEqual(refined[0], 70 + 85)
        self.assertLessEqual(refined[1], 30)
        self.assertGreaterEqual(refined[2], 250)
        self.assertGreaterEqual(refined[3], 150)

    def test_expansion_stops_at_adjacent_region_boundary(self):
        regions = (
            A3CoarseRegion("r1", "g1", (), (20, 10, 75, 30), "diagram"),
            A3CoarseRegion("r2", "g1", (), (20, 30, 75, 50), "diagram"),
        )
        coarse = (200, 100, 750, 300)
        expanded = expand_bbox(coarse, (1000, 1000), ratio=0.2, min_padding_px=20)
        safe = clamp_to_neighbor_gaps(
            expanded,
            coarse,
            regions[0],
            regions,
            (1000, 1000),
        )
        self.assertEqual(safe[3], coarse[3])

    def test_crop_outputs_manifest_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_path = root / "source.jpg"
            image = Image.new("RGB", (200, 100), "white")
            ImageDraw.Draw(image).rectangle((50, 25, 150, 75), outline="black", width=3)
            image.save(source_path)
            observation = A3RegionMap(
                groups=(A3RegionGroup(group_id="g1", relationship="independent_question"),),
                regions=(
                    A3CoarseRegion(
                        region_id="r1",
                        group_id="g1",
                        visible_labels=(),
                        bbox=(20, 15, 80, 85),
                        content_type="diagram",
                    ),
                ),
            )
            crops = crop_a3_regions(source_path, observation, root / "out")
            self.assertEqual(len(crops), 1)
            self.assertTrue(crops[0].crop_path.is_file())
            self.assertTrue((root / "out" / "a3_region_crop_overlay.jpg").is_file())

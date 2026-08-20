from pathlib import Path
import unittest
from unittest.mock import patch

from tiku_agent.a3_auto_crop import (
    A3AutoCropError,
    GlmA3AutoCropper,
    build_allowed_units,
    normalized_bbox_to_bounds,
    parse_a3_auto_crop_page,
)
from tiku_agent.glm_vision import GlmJsonResponse


EXPECTED_UNITS = [
    {"unit_id": "g1-u1", "display_label": "四-1"},
    {"unit_id": "g1-u2", "display_label": "四-2"},
]


def _payload(*, second_status: str = "review_required") -> dict:
    second_bbox = [510, 120, 920, 480] if second_status != "no_target" else None
    return {
        "schema_version": "a3-page-crops-v1",
        "page_status": "partially_ready" if second_status != "auto_ready" else "ready",
        "targets": [
            {
                "target_id": "c001",
                "unit_id": "g1-u1",
                "question_label": "四-1",
                "bbox": [80, 100, 470, 460],
                "status": "auto_ready",
                "reason_codes": [],
                "binding_evidence": "左侧结构图",
            },
            {
                "target_id": "c002",
                "unit_id": "g1-u2",
                "question_label": "四-2",
                "bbox": second_bbox,
                "status": second_status,
                "reason_codes": ["crop_boundary_uncertain"] if second_status == "review_required" else [],
                "binding_evidence": "右侧结构图" if second_bbox else "未找到可靠结构图",
            },
        ],
        "unknowns": [],
    }


class A3AutoCropContractTests(unittest.TestCase):
    def test_partial_page_keeps_ready_and_manual_targets_independent(self):
        page = parse_a3_auto_crop_page(_payload(), expected_units=EXPECTED_UNITS)

        self.assertEqual(page.page_status, "partially_ready")
        self.assertEqual(page.targets[0].status, "auto_ready")
        self.assertEqual(page.targets[1].status, "review_required")
        self.assertEqual(page.targets[1].bbox, (510, 120, 920, 480))

    def test_no_target_requires_null_bbox(self):
        page = parse_a3_auto_crop_page(
            _payload(second_status="no_target"),
            expected_units=EXPECTED_UNITS,
        )
        self.assertIsNone(page.targets[1].bbox)

    def test_every_allowed_unit_must_be_returned_exactly_once(self):
        payload = _payload()
        payload["targets"].pop()

        with self.assertRaisesRegex(A3AutoCropError, "every allowed unit"):
            parse_a3_auto_crop_page(payload, expected_units=EXPECTED_UNITS)

    def test_model_cannot_relabel_or_invent_unit(self):
        payload = _payload()
        payload["targets"][0]["question_label"] = "四-9"

        with self.assertRaisesRegex(A3AutoCropError, "question_label"):
            parse_a3_auto_crop_page(payload, expected_units=EXPECTED_UNITS)

    def test_equivalent_label_punctuation_is_canonicalized_by_unit_id(self):
        payload = _payload()
        payload["targets"][0]["question_label"] = "四（1）"
        expected = [
            {"unit_id": "g1-u1", "display_label": "四-(1)"},
            EXPECTED_UNITS[1],
        ]

        page = parse_a3_auto_crop_page(payload, expected_units=expected)

        self.assertEqual(page.targets[0].question_label, "四-(1)")

    def test_page_summary_cannot_block_or_promote_individual_targets(self):
        payload = _payload()
        payload["page_status"] = "manual_required"

        with self.assertRaisesRegex(A3AutoCropError, "page_status"):
            parse_a3_auto_crop_page(payload, expected_units=EXPECTED_UNITS)

    def test_bbox_conversion_uses_normalized_coordinate_contract(self):
        self.assertEqual(
            normalized_bbox_to_bounds((100, 200, 650, 800)),
            {"x": 0.1, "y": 0.2, "width": 0.55, "height": 0.6},
        )

    def test_allowed_units_preserve_only_text_and_binding_evidence(self):
        units = [{
            "unit_id": "g1-u1",
            "group_id": "g1",
            "display_label": "四-1",
            "parent_question_label": "四",
            "question_label": "1",
            "shared_stem_text": "试作 M 图",
            "title_text": "子题条件",
            "diagram_ids": ["d1"],
            "searchability": "searchable_candidate",
            "visible_text": "不应传递图内荷载抄录",
        }]
        page = {"groups": [{"group_id": "g1", "parent_title_text": "用力法计算"}]}

        descriptors = build_allowed_units(units, page)

        self.assertEqual(descriptors[0]["unit_id"], "g1-u1")
        self.assertEqual(descriptors[0]["parent_title_text"], "用力法计算")
        self.assertNotIn("visible_text", descriptors[0])

    @patch("tiku_agent.a3_auto_crop.call_glm_json")
    def test_glm_adapter_sends_allowed_ids_and_uses_production_parser(self, call):
        call.return_value = GlmJsonResponse(
            payload={
                "schema_version": "a3-page-crops-v1",
                "page_status": "ready",
                "targets": [{
                    "target_id": "c001",
                    "unit_id": "g1-u1",
                    "question_label": "四-1",
                    "bbox": [100, 100, 800, 800],
                    "status": "auto_ready",
                    "reason_codes": [],
                    "binding_evidence": "结构图",
                }],
                "unknowns": [],
            },
            raw_text="{}",
            model="glm-5v-turbo",
        )
        cropper = GlmA3AutoCropper(prompt_path=Path(__file__).parents[1] / "tiku_agent" / "prompts" / "a3_auto_crop_v1.txt")
        units = [{
            "unit_id": "g1-u1",
            "display_label": "四-1",
            "searchability": "searchable_candidate",
        }]

        result = cropper.ground(Path("page.jpg"), units, {"groups": []})

        self.assertEqual(result.targets[0].unit_id, "g1-u1")
        self.assertIn('"unit_id":"g1-u1"', call.call_args.kwargs["user_text"])


if __name__ == "__main__":
    unittest.main()

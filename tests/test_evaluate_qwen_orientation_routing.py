from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from scripts.evaluate_qwen_orientation_routing import (
    DEFAULT_MANIFEST,
    _pricing_for_result,
    load_routing_sources,
    materialize_rotated_cases,
    parse_orientation_signal,
    summarize_results,
)
from tiku_agent.image_triage_8897 import (
    finalize_route_8897_v1,
    observation_from_model_text_8897_v1,
)


class EvaluateQwenOrientationRoutingTest(unittest.TestCase):
    def test_dashscope_usage_is_normalized_for_cost_estimation(self):
        pricing = _pricing_for_result(
            {
                "model": "qwen3.7-plus",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            }
        )

        self.assertEqual(pricing["pricing_status"], "priced")
        self.assertEqual(pricing["estimated_cost_cny"], 0.0028)

    def test_orientation_signal_parses_explicit_correction_action(self):
        signal = parse_orientation_signal(
            "建议路线：A2\n"
            "回正所需顺时针旋转：270度\n"
            "方向判断：明确\n"
        )

        self.assertTrue(signal["schema_valid"])
        self.assertEqual(signal["correction_clockwise"], 270)
        self.assertTrue(signal["confident"])
        self.assertEqual(signal["actionable_correction_clockwise"], 270)

    def test_orientation_signal_fails_open_when_uncertain(self):
        signal = parse_orientation_signal(
            "回正所需顺时针旋转：不确定\n方向判断：不确定\n"
        )

        self.assertTrue(signal["schema_valid"])
        self.assertIsNone(signal["correction_clockwise"])
        self.assertFalse(signal["confident"])
        self.assertIsNone(signal["actionable_correction_clockwise"])

    def test_orientation_signal_rejects_conflicting_fields(self):
        signal = parse_orientation_signal(
            "回正所需顺时针旋转：90\n方向判断：不确定\n"
        )

        self.assertFalse(signal["schema_valid"])
        self.assertIn("uncertain_with_numeric_correction", signal["errors"])
        self.assertIsNone(signal["actionable_correction_clockwise"])

    def test_orientation_signal_does_not_treat_negated_confidence_as_true(self):
        signal = parse_orientation_signal(
            "回正所需顺时针旋转：90\n方向判断：不明确\n"
        )

        self.assertFalse(signal["schema_valid"])
        self.assertIn("invalid_confidence", signal["errors"])
        self.assertIsNone(signal["actionable_correction_clockwise"])

    def test_orientation_signal_rejects_duplicate_fields(self):
        signal = parse_orientation_signal(
            "回正所需顺时针旋转：90\n"
            "回正所需顺时针旋转：270\n"
            "方向判断：明确\n"
        )

        self.assertFalse(signal["schema_valid"])
        self.assertIn("duplicate_correction", signal["errors"])
        self.assertIsNone(signal["actionable_correction_clockwise"])

    def test_additive_fields_do_not_change_the_v1_parser_contract(self):
        content = (
            "建议路线：A2\n"
            "题目数量：1\n"
            "原结构图数量：1\n"
            "辅助图数量：0\n"
            "真实外荷载：明确\n"
            "图片完整性：完整\n"
            "结构力学内容：有\n"
            "题图边界：清楚\n"
            "回正所需顺时针旋转：270\n"
            "方向判断：明确\n"
        )

        observation = observation_from_model_text_8897_v1(content)

        self.assertFalse(observation.has_ambiguity)
        self.assertEqual(finalize_route_8897_v1(observation), "A2")

    def test_manifest_hashes_and_generated_corrections_are_locked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "A2" / "one.png"
            image_path.parent.mkdir()
            image = Image.new("RGB", (8, 4), "white")
            image.putpixel((0, 0), (0, 0, 0))
            image.save(image_path)
            digest = sha256(image_path.read_bytes()).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "suite": "routing/a1_a2_a3",
                        "sources": [
                            {
                                "path": "A2/one.png",
                                "expected_route": "A2",
                                "sha256": digest,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            sources = load_routing_sources(manifest_path)
            cases = materialize_rotated_cases(
                sources,
                (0, 90, 180, 270),
                root / "rotated",
            )

        self.assertEqual(len(cases), 4)
        self.assertTrue(sources[0].orientation_observable)
        self.assertTrue(all(case.orientation_observable for case in cases))
        self.assertEqual(
            [case.expected_correction_clockwise for case in cases],
            [0, 270, 180, 90],
        )

    def test_single_color_source_is_unobservable_but_still_generates_four_cases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "A1" / "blank.png"
            image_path.parent.mkdir()
            Image.new("RGB", (8, 4), "red").save(image_path)
            digest = sha256(image_path.read_bytes()).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "suite": "routing/a1_a2_a3",
                        "sources": [
                            {
                                "path": "A1/blank.png",
                                "expected_route": "A1",
                                "sha256": digest,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            sources = load_routing_sources(manifest_path)
            cases = materialize_rotated_cases(
                sources,
                (0, 90, 180, 270),
                root / "rotated",
            )

        self.assertFalse(sources[0].orientation_observable)
        self.assertEqual(len(cases), 4)
        self.assertTrue(all(not case.orientation_observable for case in cases))
        self.assertEqual(
            [case.expected_correction_clockwise for case in cases],
            [None, None, None, None],
        )

    def test_real_routing_manifest_marks_only_blank_source_unobservable(self):
        sources = load_routing_sources(DEFAULT_MANIFEST)

        self.assertEqual(
            [source.source_id for source in sources if not source.orientation_observable],
            ["A1/Snipaste_2026-08-23_16-39-40.png"],
        )

    def test_summary_detects_route_regression_false_rotation_and_latency(self):
        baseline = {
            "case_id": "A2/one.png@cw0",
            "variant": "baseline",
            "final_route": "A2",
            "suggested_route": "A2",
            "expected_route": "A2",
            "final_route_matches_label": True,
            "elapsed_seconds": 10.0,
            "applied_clockwise_degrees": 0,
            "expected_correction_clockwise": 0,
            "orientation_observable": True,
            "usage": {},
            "estimated_cost_cny": 0.01,
            "error": None,
        }
        candidate = {
            **baseline,
            "variant": "candidate",
            "final_route": "A3",
            "final_route_matches_label": False,
            "elapsed_seconds": 11.5,
            "orientation_signal": {
                "schema_valid": True,
                "correction_clockwise": 90,
                "confident": True,
                "actionable_correction_clockwise": 90,
            },
        }

        summary = summarize_results([baseline, candidate])

        self.assertEqual(
            summary["paired_comparison"]["route_regressions"],
            ["A2/one.png@cw0"],
        )
        self.assertEqual(
            summary["candidate_orientation"]["upright_false_rotation_cases"],
            ["A2/one.png@cw0"],
        )
        binary = summary["candidate_orientation"]
        self.assertEqual(binary["upright_total"], 1)
        self.assertEqual(binary["upright_false_positive"], 1)
        self.assertEqual(binary["upright_false_positive_cases"], ["A2/one.png@cw0"])
        self.assertEqual(binary["rotated_total"], 0)
        self.assertEqual(binary["binary_correct"], 0)
        self.assertEqual(binary["binary_total"], 1)
        self.assertEqual(
            summary["paired_comparison"]["candidate_minus_baseline_latency_seconds"]["average"],
            1.5,
        )

    def test_invalid_orientation_schema_is_not_counted_as_exact(self):
        candidate = {
            "case_id": "A2/one.png@cw90",
            "variant": "candidate",
            "expected_route": "A2",
            "final_route": "A2",
            "final_route_matches_label": True,
            "elapsed_seconds": 1.0,
            "applied_clockwise_degrees": 90,
            "expected_correction_clockwise": 270,
            "orientation_observable": True,
            "usage": {},
            "estimated_cost_cny": 0.0,
            "error": None,
            "orientation_signal": {
                "schema_valid": False,
                "correction_clockwise": 270,
                "confident": None,
                "actionable_correction_clockwise": None,
            },
        }

        summary = summarize_results([candidate])

        self.assertEqual(summary["candidate_orientation"]["exact_correction"], 0)

    def test_unobservable_case_still_counts_for_route_fields_and_latency(self):
        baseline = {
            "case_id": "A1/blank.png@cw90",
            "variant": "baseline",
            "final_route": "A1",
            "suggested_route": "A1",
            "expected_route": "A1",
            "final_route_matches_label": True,
            "question_count": None,
            "elapsed_seconds": 2.0,
            "applied_clockwise_degrees": 90,
            "expected_correction_clockwise": None,
            "orientation_observable": False,
            "usage": {},
            "estimated_cost_cny": 0.0,
            "error": None,
        }
        candidate = {
            **baseline,
            "variant": "candidate",
            "final_route": "A3",
            "final_route_matches_label": False,
            "question_count": 1,
            "elapsed_seconds": 2.5,
            "orientation_signal": {
                "schema_valid": True,
                "correction_clockwise": None,
                "confident": False,
                "actionable_correction_clockwise": None,
            },
        }

        summary = summarize_results([baseline, candidate])

        self.assertEqual(
            summary["paired_comparison"]["route_regressions"],
            ["A1/blank.png@cw90"],
        )
        self.assertEqual(
            summary["paired_comparison"]["legacy_observation_changes"],
            ["A1/blank.png@cw90"],
        )
        self.assertEqual(
            summary["paired_comparison"]["candidate_minus_baseline_latency_seconds"]["average"],
            0.5,
        )
        orientation = summary["candidate_orientation"]
        self.assertEqual(orientation["unobservable_total"], 1)
        self.assertEqual(orientation["unobservable_expected_uncertain"], 1)
        self.assertEqual(orientation["exact_correction_total"], 0)
        self.assertEqual(orientation["false_confident_on_unobservable"], 0)
        self.assertEqual(orientation["binary_total"], 0)

    def test_confident_numeric_signal_on_unobservable_case_is_counted_separately(self):
        candidate = {
            "case_id": "A1/blank.png@cw0",
            "variant": "candidate",
            "expected_route": "A1",
            "final_route": "A1",
            "final_route_matches_label": True,
            "elapsed_seconds": 1.0,
            "applied_clockwise_degrees": 0,
            "expected_correction_clockwise": None,
            "orientation_observable": False,
            "usage": {},
            "estimated_cost_cny": 0.0,
            "error": None,
            "orientation_signal": {
                "schema_valid": True,
                "correction_clockwise": 270,
                "confident": True,
                "actionable_correction_clockwise": 270,
            },
        }

        summary = summarize_results([candidate])
        orientation = summary["candidate_orientation"]

        self.assertEqual(orientation["exact_correction"], 0)
        self.assertEqual(orientation["exact_correction_total"], 0)
        self.assertEqual(orientation["actionable_correct"], 0)
        self.assertEqual(orientation["actionable_wrong_cases"], [])
        self.assertEqual(orientation["false_confident_on_unobservable"], 1)
        self.assertEqual(
            orientation["false_confident_on_unobservable_cases"],
            ["A1/blank.png@cw0"],
        )
        self.assertEqual(orientation["upright_false_rotation_cases"], [])
        self.assertEqual(orientation["binary_total"], 0)

    def test_binary_gate_detects_nonzero_rotation_without_claiming_exact_angle(self):
        wrong_angle = {
            "case_id": "A2/one.png@cw90",
            "variant": "candidate",
            "expected_route": "A2",
            "final_route": "A2",
            "final_route_matches_label": True,
            "elapsed_seconds": 1.0,
            "applied_clockwise_degrees": 90,
            "expected_correction_clockwise": 270,
            "orientation_observable": True,
            "usage": {},
            "estimated_cost_cny": 0.0,
            "error": None,
            "orientation_signal": {
                "schema_valid": True,
                "correction_clockwise": 180,
                "confident": True,
                "actionable_correction_clockwise": 180,
            },
        }
        uncertain = {
            **wrong_angle,
            "case_id": "A2/one.png@cw180",
            "applied_clockwise_degrees": 180,
            "expected_correction_clockwise": 180,
            "orientation_signal": {
                "schema_valid": True,
                "correction_clockwise": None,
                "confident": False,
                "actionable_correction_clockwise": None,
            },
        }

        orientation = summarize_results([wrong_angle, uncertain])["candidate_orientation"]

        self.assertEqual(orientation["exact_correction"], 0)
        self.assertEqual(orientation["exact_correction_total"], 2)
        self.assertEqual(orientation["actionable_wrong_cases"], ["A2/one.png@cw90"])
        self.assertEqual(orientation["rotated_total"], 2)
        self.assertEqual(orientation["rotated_detected"], 1)
        self.assertEqual(orientation["rotated_detected_cases"], ["A2/one.png@cw90"])
        self.assertEqual(orientation["rotated_missed_cases"], ["A2/one.png@cw180"])
        self.assertEqual(orientation["binary_correct"], 1)
        self.assertEqual(orientation["binary_total"], 2)


if __name__ == "__main__":
    unittest.main()

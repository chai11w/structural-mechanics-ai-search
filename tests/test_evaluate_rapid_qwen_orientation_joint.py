import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from scripts.evaluate_qwen_orientation_routing import RotatedCase, image_pixel_sha256
from scripts.evaluate_rapid_qwen_orientation_joint import (
    THRESHOLDS,
    evaluate_policy,
    infer_rapid_case,
    policy_action,
    scan_thresholds,
    validate_case_alignment,
    zero_unsafe_candidates,
)


class _FakeSession:
    def __init__(self, output):
        self.output = output

    def __call__(self, _batch):
        return [self.output]


class _FakeRapidOrientation:
    labels = ["0", "90", "180", "270"]

    def __init__(self, output):
        self.session = _FakeSession(output)

    def load_img(self, path):
        return str(path)

    def preprocess(self, image):
        return image


def _case(
    case_id="A2/one.png@cw90",
    *,
    applied=90,
    expected=270,
    observable=True,
    digest="abc",
    pixel_digest="pixels",
):
    return RotatedCase(
        case_id=case_id,
        source_id=case_id.split("@", 1)[0],
        expected_route="A2",
        applied_clockwise_degrees=applied,
        expected_correction_clockwise=expected,
        orientation_observable=observable,
        image_path=Path("unused.png"),
        input_sha256=digest,
        pixel_sha256=pixel_digest,
    )


def _record(
    case_id,
    *,
    expected,
    rapid_correction,
    confidence,
    qwen_actionable,
    observable=True,
    qwen_schema_valid=True,
    qwen_confident=True,
):
    return {
        "case_id": case_id,
        "orientation_observable": observable,
        "expected_correction_clockwise": expected,
        "rapid_correction_clockwise": rapid_correction,
        "rapid_confidence": confidence,
        "qwen_schema_valid": qwen_schema_valid,
        "qwen_confident": qwen_confident,
        "qwen_actionable_correction_clockwise": qwen_actionable,
    }


class RapidQwenOrientationJointTest(unittest.TestCase):
    def test_internal_probability_row_maps_label_to_clockwise_correction(self):
        row = np.array([0.05, 0.80, 0.10, 0.05], dtype=np.float32)
        output = np.stack([row, row, row])
        case = _case()
        qwen = {
            "orientation_signal": {
                "schema_valid": True,
                "confident": True,
                "actionable_correction_clockwise": 90,
            }
        }

        result = infer_rapid_case(_FakeRapidOrientation(output), case, qwen)

        self.assertEqual(result["rapid_orientation_label"], 90)
        self.assertEqual(result["rapid_correction_clockwise"], 270)
        self.assertAlmostEqual(result["rapid_confidence"], 0.8)
        self.assertTrue(result["rapid_raw_argmax_correct"])

    def test_alignment_allows_encoded_hash_mismatch_when_pixels_match(self):
        image = Image.new("RGB", (8, 4), "white")
        image.putpixel((0, 0), (0, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fast = root / "fast.png"
            small = root / "small.png"
            image.save(fast, compress_level=0)
            image.save(small, compress_level=9)
            self.assertNotEqual(fast.read_bytes(), small.read_bytes())
            pixel_digest = image_pixel_sha256(Image.open(fast))
            self.assertEqual(pixel_digest, image_pixel_sha256(Image.open(small)))

        case = _case(digest="rapid-encoded", pixel_digest=pixel_digest)
        qwen = {
            case.case_id: {
                "source_id": case.source_id,
                "applied_clockwise_degrees": case.applied_clockwise_degrees,
                "expected_correction_clockwise": case.expected_correction_clockwise,
                "orientation_observable": True,
                "input_sha256": "qwen-encoded",
                "pixel_sha256": pixel_digest,
            }
        }

        self.assertEqual(validate_case_alignment([case], qwen), [case.case_id])

    def test_alignment_rejects_pixel_hash_mismatch(self):
        case = _case(pixel_digest="expected-pixels")
        qwen = {
            case.case_id: {
                "source_id": case.source_id,
                "applied_clockwise_degrees": case.applied_clockwise_degrees,
                "expected_correction_clockwise": case.expected_correction_clockwise,
                "orientation_observable": True,
                "input_sha256": case.input_sha256,
                "pixel_sha256": "different-pixels",
            }
        }

        with self.assertRaisesRegex(ValueError, "pixel_sha256 mismatch"):
            validate_case_alignment([case], qwen)

    def test_binary_gate_uses_qwen_only_as_rotated_signal(self):
        record = _record(
            "rotated",
            expected=270,
            rapid_correction=270,
            confidence=0.75,
            qwen_actionable=90,
        )

        self.assertEqual(policy_action(record, 0.70, "qwen_binary_gate"), 270)
        self.assertEqual(policy_action(record, 0.70, "qwen_angle_agreement"), 0)
        self.assertEqual(policy_action(record, 0.80, "qwen_binary_gate"), 0)

    def test_binary_gate_fails_closed_for_invalid_qwen_signal(self):
        record = _record(
            "rotated",
            expected=270,
            rapid_correction=270,
            confidence=0.75,
            qwen_actionable=90,
            qwen_schema_valid=False,
            qwen_confident=True,
        )

        self.assertEqual(policy_action(record, 0.70, "qwen_binary_gate"), 0)

        row = np.array([0.05, 0.80, 0.10, 0.05], dtype=np.float32)
        output = np.stack([row, row, row])
        qwen = {
            "orientation_signal": {
                "schema_valid": False,
                "confident": True,
                "actionable_correction_clockwise": 90,
            }
        }
        result = infer_rapid_case(_FakeRapidOrientation(output), _case(), qwen)

        self.assertEqual(result["qwen_reported_actionable_correction_clockwise"], 90)
        self.assertIsNone(result["qwen_actionable_correction_clockwise"])

    def test_unobservable_always_preserves_input(self):
        record = _record(
            "blank",
            expected=None,
            rapid_correction=270,
            confidence=0.99,
            qwen_actionable=90,
            observable=False,
        )

        for policy in ("rapid_only", "qwen_binary_gate", "qwen_angle_agreement"):
            self.assertEqual(policy_action(record, 0.0, policy), 0)

    def test_policy_metrics_separate_missed_wrong_and_unsafe(self):
        records = [
            _record(
                "upright-false",
                expected=0,
                rapid_correction=90,
                confidence=0.9,
                qwen_actionable=90,
            ),
            _record(
                "rotated-correct",
                expected=270,
                rapid_correction=270,
                confidence=0.9,
                qwen_actionable=90,
            ),
            _record(
                "rotated-wrong",
                expected=180,
                rapid_correction=90,
                confidence=0.9,
                qwen_actionable=90,
            ),
            _record(
                "rotated-missed",
                expected=90,
                rapid_correction=90,
                confidence=0.4,
                qwen_actionable=90,
            ),
            _record(
                "blank",
                expected=None,
                rapid_correction=90,
                confidence=1.0,
                qwen_actionable=90,
                observable=False,
            ),
        ]

        metrics = evaluate_policy(records, 0.5, "qwen_binary_gate")

        self.assertEqual(metrics["exact"], 1)
        self.assertEqual(metrics["upright_false_rotate"], 1)
        self.assertEqual(metrics["rotated_corrected"], 1)
        self.assertEqual(metrics["rotated_missed"], 1)
        self.assertEqual(metrics["rotated_wrong"], 1)
        self.assertEqual(metrics["unsafe_action"], 2)
        self.assertEqual(metrics["unobservable_preserved"], 1)

    def test_threshold_scan_is_inclusive_and_finds_zero_unsafe_max_recall(self):
        records = [
            _record(
                "upright",
                expected=0,
                rapid_correction=90,
                confidence=0.6,
                qwen_actionable=None,
            ),
            _record(
                "rotated",
                expected=270,
                rapid_correction=270,
                confidence=0.7,
                qwen_actionable=90,
            ),
        ]

        scans = scan_thresholds(records)
        candidates = zero_unsafe_candidates(scans)

        self.assertEqual(len(THRESHOLDS), 101)
        self.assertEqual(THRESHOLDS[0], 0.0)
        self.assertEqual(THRESHOLDS[-1], 1.0)
        self.assertEqual(len(scans["rapid_only"]), 101)
        self.assertEqual(
            candidates["rapid_only"]["maximum_rotated_corrected"],
            1,
        )
        self.assertEqual(candidates["rapid_only"]["candidate_thresholds"][0], 0.61)
        self.assertEqual(
            candidates["qwen_binary_gate"]["candidate_thresholds"][0],
            0.0,
        )


if __name__ == "__main__":
    unittest.main()

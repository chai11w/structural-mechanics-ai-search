import unittest

from tiku_agent.a3_models import A3ModelError, parse_crop_compare_result


def _payload(*, verdict="verified", external_loads_complete=True):
    return {
        "schema_version": "a3-crop-compare-v2",
        "selected_unit_id": "g1-u1",
        "verdict": verdict,
        "checks": {
            "selected_diagram_match": True,
            "single_target_diagram": True,
            "structure_complete": True,
            "supports_complete": True,
            "external_loads_complete": external_loads_complete,
            "image_clear": True,
        },
    }


class A3CropCompareTests(unittest.TestCase):
    def test_parses_structured_checks(self):
        result = parse_crop_compare_result(_payload(), expected_unit_id="g1-u1")

        self.assertTrue(result.verified)
        self.assertTrue(result.checks["external_loads_complete"])

    def test_review_required_keeps_failed_check_and_evidence(self):
        result = parse_crop_compare_result(
            _payload(verdict="review_required", external_loads_complete=False),
            expected_unit_id="g1-u1",
        )

        self.assertFalse(result.verified)
        self.assertFalse(result.checks["external_loads_complete"])

    def test_rejects_verdict_that_disagrees_with_checks(self):
        with self.assertRaisesRegex(A3ModelError, "verdict and checks disagree"):
            parse_crop_compare_result(
                _payload(verdict="verified", external_loads_complete=False),
                expected_unit_id="g1-u1",
            )

    def test_rejects_extra_fields(self):
        payload = _payload()
        payload["reason"] = "不应由模型自由生成用户文案"

        with self.assertRaisesRegex(A3ModelError, "invalid crop comparison fields"):
            parse_crop_compare_result(payload, expected_unit_id="g1-u1")


if __name__ == "__main__":
    unittest.main()

import unittest

from tiku_agent.checkpoint_capture_gate import (
    A2CaptureAdmissionV1,
    A2CheckpointCaptureGateV1,
)


def _admission(**changes):
    values = {
        "request_kind": "search",
        "authenticated": True,
        "quota_admitted": True,
        "queue_admitted": True,
        "upload_admitted": True,
        "entered_business_processing": True,
    }
    values.update(changes)
    return A2CaptureAdmissionV1(**values)


class CheckpointCaptureGateTest(unittest.TestCase):
    def test_gate_is_default_closed(self):
        decision = A2CheckpointCaptureGateV1().decide(_admission())
        self.assertEqual((decision.permitted, decision.reason_code), (False, "CAPTURE_DISABLED"))

    def test_only_fully_admitted_search_can_capture(self):
        gate = A2CheckpointCaptureGateV1(enabled=True)
        decision = gate.decide(_admission())
        self.assertEqual((decision.permitted, decision.reason_code), (True, "CAPTURE_ADMITTED"))
        for field in (
            "authenticated",
            "quota_admitted",
            "queue_admitted",
            "upload_admitted",
            "entered_business_processing",
        ):
            with self.subTest(field=field):
                rejected = gate.decide(_admission(**{field: False}))
                self.assertFalse(rejected.permitted)

    def test_non_business_paths_are_excluded(self):
        gate = A2CheckpointCaptureGateV1(enabled=True)
        for kind in ("health", "login", "quota", "queue", "media"):
            with self.subTest(kind=kind):
                decision = gate.decide(_admission(request_kind=kind))
                self.assertEqual(decision.reason_code, "REQUEST_KIND_EXCLUDED")
        self.assertEqual(
            gate.decide(_admission(request_kind="unknown")).reason_code,
            "REQUEST_KIND_UNSUPPORTED",
        )


if __name__ == "__main__":
    unittest.main()

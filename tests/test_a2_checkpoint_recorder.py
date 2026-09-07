from datetime import UTC, datetime, timedelta
import unittest

from tiku_agent.a2_checkpoint_recorder import A2CheckpointRecorderV1
from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1, A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_capture import build_a2_checkpoint
from tiku_agent.checkpoint_contract import SECTION_DELIVERY, STAGE_ANSWER_PREPARED
from tiku_agent.checkpoint_store import EvidenceCapacityError
from tiku_agent.tool_result import ToolResult
from unittest.mock import Mock
from dataclasses import replace
from tests.test_checkpoint_capture import _context


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


def _admission():
    return A2CaptureAdmissionV1("search", True, True, True, True, True)


def _checkpoint():
    return build_a2_checkpoint(
        _context(),
        stage=STAGE_ANSWER_PREPARED,
        result={SECTION_DELIVERY: {
            "answer_artifact_count": 0,
            "media_status": "not_available",
            "delivery_code": "NO_MATCH",
        }},
        occurred_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=30)).isoformat(),
        input_digests={"query": "1" * 64},
        outcome="no_match",
    )


class A2CheckpointRecorderTest(unittest.TestCase):
    def test_missing_trace_cannot_create_orphan_artifacts(self):
        store = Mock()
        recorder = A2CheckpointRecorderV1(store, producer=_context().producer, gate=A2CheckpointCaptureGateV1(enabled=True))
        result = recorder.capture_stage(replace(_context(), trace_id=""), admission=_admission(),
            stage="image_accepted", tool_result=ToolResult.success(code="IMAGE_ACCEPTED"), payload={"image_path": "not-read.png"})
        self.assertEqual(result.reason_code, "CAPTURE_TRACE_REQUIRED")
        self.assertEqual(store.mock_calls, [])

    def test_disabled_or_rejected_capture_never_reads_images_or_clock(self):
        for enabled, admission in (
            (False, _admission()),
            (True, A2CaptureAdmissionV1("search", False, True, True, True, True)),
        ):
            with self.subTest(enabled=enabled):
                store = Mock()
                clock = Mock(side_effect=AssertionError("must not be read"))
                result = A2CheckpointRecorderV1(
                    store, producer=_context().producer, gate=A2CheckpointCaptureGateV1(enabled=enabled), clock=clock,
                ).capture_stage(
                    _context(), admission=admission, stage=STAGE_ANSWER_PREPARED,
                    tool_result=ToolResult.success(code="ANSWER_READY"),
                    payload={"answer_paths": ["unreadable.png"]},
                )
                self.assertFalse(result.attempted)
                self.assertEqual(store.mock_calls, [])
                clock.assert_not_called()

    def test_gate_rejection_does_not_touch_store(self):
        class Store:
            def put_checkpoint(self, checkpoint):
                raise AssertionError("store must not be called")

        result = A2CheckpointRecorderV1(Store(), producer=_context().producer).record(
            _checkpoint(), admission=_admission()
        )
        self.assertEqual((result.attempted, result.reason_code), (False, "CAPTURE_DISABLED"))

    def test_store_capacity_failure_is_fail_open(self):
        class Store:
            def put_checkpoint(self, checkpoint):
                raise EvidenceCapacityError("checkpoint_rows")

        result = A2CheckpointRecorderV1(
            Store(), producer=_context().producer, gate=A2CheckpointCaptureGateV1(enabled=True)
        ).record(_checkpoint(), admission=_admission())
        self.assertEqual((result.attempted, result.stored, result.reason_code),
                         (True, False, "STORE_REJECTED"))

    def test_success_returns_stable_checkpoint_id(self):
        class Store:
            def put_checkpoint(self, checkpoint):
                return checkpoint

        checkpoint = _checkpoint()
        result = A2CheckpointRecorderV1(
            Store(), producer=_context().producer, gate=A2CheckpointCaptureGateV1(enabled=True)
        ).record(checkpoint, admission=_admission())
        self.assertEqual((result.attempted, result.stored, result.reason_code),
                         (True, True, "CAPTURE_STORED"))
        self.assertEqual(result.checkpoint_id, checkpoint.checkpoint_id)


if __name__ == "__main__":
    unittest.main()

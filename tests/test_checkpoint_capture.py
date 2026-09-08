from datetime import UTC, datetime, timedelta
import unittest

from tiku_agent.checkpoint_capture import (
    A2CheckpointContextV1,
    build_a2_checkpoint,
    checkpoint_failure,
    checkpoint_outcome,
)
from tiku_agent.checkpoint_contract import (
    OUTCOME_FAILED,
    OUTCOME_NO_MATCH,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    ProducerVersionV1,
    SECTION_DELIVERY,
    STAGE_ANSWER_PREPARED,
)
from tiku_agent.tool_result import ToolResult


NOW = datetime(2026, 9, 6, 8, 0, tzinfo=UTC)


def _context(*, standalone: bool = False) -> A2CheckpointContextV1:
    search_id = "search_child"
    workflow_id = search_id if standalone else "search_workflow"
    workflow_revision = 3
    task_revision = workflow_revision if standalone else 4
    return A2CheckpointContextV1(
        trace_id="trace_" + "1" * 32,
        session_key="2" * 64,
        identity_key="invite_test",
        workflow_search_id=workflow_id,
        workflow_task_revision=workflow_revision,
        search_id=search_id,
        task_revision=task_revision,
        unit_id="" if standalone else "unit_1",
        candidate_generation="4:2" if not standalone else "",
        request_id="req_" + "3" * 32,
        producer=ProducerVersionV1(
            code_revision="4" * 40,
            component="a2_runtime",
            component_version="checkpoint-v1",
            input_schema_version="a2-v1",
            policy_version="policy-v1",
            data_version="bank-v1",
        ),
    )


def _result() -> dict[str, object]:
    return {
        SECTION_DELIVERY: {
            "answer_artifact_count": 0,
            "media_status": "not_available",
            "delivery_code": "NO_MATCH",
        }
    }


class CheckpointCaptureTest(unittest.TestCase):
    def test_outcome_mapping_and_failure_projection_are_stable(self):
        for tool_outcome, expected in (
            ("SUCCESS", OUTCOME_SUCCESS),
            ("NO_MATCH", OUTCOME_NO_MATCH),
            ("PARTIAL", OUTCOME_PARTIAL),
            ("ERROR", OUTCOME_FAILED),
        ):
            tool = ToolResult(outcome=tool_outcome, code="TOOL_FAILED", retryable=True)
            self.assertEqual(checkpoint_outcome(tool), expected)
        failure = checkpoint_failure(
            ToolResult.tool_error(
                code="COARSE_SEARCH_FAILED",
                error="hidden detail",
                retryable=True,
                error_category="local_data",
            )
        )
        self.assertEqual(failure.code, "COARSE_SEARCH_FAILED")
        self.assertEqual(failure.kind, "local_data")
        self.assertTrue(failure.retryable)
        self.assertNotIn("hidden", str(failure.to_dict()))

    def test_builds_child_checkpoint_with_owner_and_fingerprint(self):
        tool = ToolResult.no_match(code="NO_RELIABLE_RERANK_CANDIDATES")
        checkpoint = build_a2_checkpoint(
            _context(),
            stage=STAGE_ANSWER_PREPARED,
            result=_result(),
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            input_digests={"query": "a" * 64},
            tool_result=tool,
        )
        self.assertEqual(checkpoint.outcome, OUTCOME_NO_MATCH)
        self.assertEqual(checkpoint.owner.unit_id, "unit_1")
        self.assertEqual(checkpoint.owner.candidate_generation, "4:2")
        self.assertEqual(len(checkpoint.input_fingerprint), 64)
        self.assertIsNone(checkpoint.failure)

    def test_standalone_a2_uses_same_workflow_and_search_ids(self):
        checkpoint = build_a2_checkpoint(
            _context(standalone=True),
            stage=STAGE_ANSWER_PREPARED,
            result=_result(),
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            input_digests={"query": "b" * 64},
            outcome=OUTCOME_NO_MATCH,
        )
        self.assertEqual(checkpoint.owner.workflow_search_id, checkpoint.owner.search_id)
        self.assertEqual(checkpoint.owner.unit_id, "")

    def test_failed_tool_forces_failed_retention_and_failure_details(self):
        checkpoint = build_a2_checkpoint(
            _context(),
            stage="question_analyzed",
            result={},
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=7)).isoformat(),
            input_digests={"query": "c" * 64},
            tool_result=ToolResult.tool_error(
                code="IMAGE_ANALYSIS_FAILED",
                error="private detail",
                retryable=True,
                error_category="external_model",
            ),
        )
        self.assertEqual(checkpoint.outcome, OUTCOME_FAILED)
        self.assertEqual(checkpoint.retention_class, "failed")
        self.assertEqual(checkpoint.failure.code, "IMAGE_ANALYSIS_FAILED")

    def test_explicit_outcome_must_match_tool_result(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            build_a2_checkpoint(
                _context(),
                stage=STAGE_ANSWER_PREPARED,
                result=_result(),
                occurred_at=NOW.isoformat(),
                expires_at=(NOW + timedelta(days=30)).isoformat(),
                input_digests={"query": "d" * 64},
                outcome=OUTCOME_SUCCESS,
                tool_result=ToolResult.no_match(code="NO_MATCH"),
            )


if __name__ == "__main__":
    unittest.main()

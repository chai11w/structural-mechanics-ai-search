from datetime import UTC, datetime, timedelta
import unittest

from tiku_agent.a2_checkpoint_stages import (
    build_question_analyzed_checkpoint,
    question_analyzed_result,
)
from tiku_agent.checkpoint_capture import A2CheckpointContextV1
from tiku_agent.checkpoint_contract import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    SECTION_CHAPTER_DECISION,
    SECTION_LOAD_OBSERVATIONS,
    SECTION_QUESTION_CONTEXT,
    SECTION_STRUCTURE_DECISION,
)
from tiku_agent.tool_result import ToolResult
from tests.test_checkpoint_capture import _context


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


class A2CheckpointStagesTest(unittest.TestCase):
    def test_analysis_projection_has_all_required_sections_and_no_raw_payload(self):
        result = question_analyzed_result(
            {
                "analysis_schema_version": "a2-analysis-v1",
                "visible_problem_text": "连续梁，跨中作用集中荷载 P。",
                "chapter": "4力法",
                "chapter_confidence": 0.91,
                "chapter_scope_status": "supported",
                "loads": [{"type": "集中", "raw": "P"}],
                "structure_type": "梁",
                "structure_confidence": 0.88,
                "structure_filter_applicable": True,
            }
        )
        self.assertEqual(
            set(result),
            {
                SECTION_QUESTION_CONTEXT,
                SECTION_CHAPTER_DECISION,
                SECTION_LOAD_OBSERVATIONS,
                SECTION_STRUCTURE_DECISION,
            },
        )
        self.assertNotIn("visible_problem_text", str(result))

    def test_successful_analysis_builds_valid_checkpoint(self):
        checkpoint = build_question_analyzed_checkpoint(
            _context(),
            ToolResult.success(
                code="IMAGE_ANALYZED",
                data={
                    "chapter": "4力法",
                    "chapter_confidence": 0.9,
                    "chapter_scope_status": "supported",
                    "loads": [{"type": "均布", "raw": "q"}],
                    "structure_type": "梁",
                },
            ),
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            input_digests={"query": "e" * 64},
        )
        self.assertEqual(checkpoint.outcome, OUTCOME_SUCCESS)
        self.assertEqual(checkpoint.stage, "question_analyzed")
        self.assertEqual(checkpoint.result[SECTION_CHAPTER_DECISION]["chapter"], "4力法")

    def test_failed_analysis_keeps_only_stable_failure_projection(self):
        checkpoint = build_question_analyzed_checkpoint(
            _context(),
            ToolResult.tool_error(
                code="IMAGE_ANALYSIS_FAILED",
                error="private model response",
                retryable=True,
                error_category="external_model",
            ),
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=7)).isoformat(),
            input_digests={"query": "f" * 64},
        )
        self.assertEqual(checkpoint.outcome, OUTCOME_FAILED)
        self.assertEqual(checkpoint.result, {})
        self.assertEqual(checkpoint.failure.code, "IMAGE_ANALYSIS_FAILED")


if __name__ == "__main__":
    unittest.main()

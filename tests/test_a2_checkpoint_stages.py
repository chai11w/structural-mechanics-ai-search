from datetime import UTC, datetime, timedelta
import unittest

from tiku_agent.a2_checkpoint_stages import (
    answer_prepared_result,
    build_question_analyzed_checkpoint,
    coarse_search_result,
    question_analyzed_result,
    rerank_result,
    build_rerank_checkpoint,
    build_coarse_search_checkpoint,
)
from tiku_agent.checkpoint_capture import A2CheckpointContextV1
from tiku_agent.checkpoint_contract import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    SECTION_CANDIDATE_COUNTS,
    SECTION_CANDIDATE_SCORES,
    SECTION_CHAPTER_DECISION,
    SECTION_LOAD_OBSERVATIONS,
    SECTION_QUESTION_CONTEXT,
    SECTION_STRUCTURE_DECISION,
)
from tiku_agent.tool_result import ToolResult
from tests.test_checkpoint_capture import _context


NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


class A2CheckpointStagesTest(unittest.TestCase):
    def test_rerank_truncation_keeps_every_visible_candidate_and_real_policy(self):
        items = [{"rank": rank, "name": f"q{rank}", "score": 0.9} for rank in range(1, 71)]
        scores = [{**item, "rerank_status": "completed", "rerank_score": 0.91, "final_score": 0.905} for item in items]
        tool = ToolResult.success(code="RERANK_COMPLETED", data={
            "reranked": True, "visible_candidates": scores[-3:],
            "checkpoint_rerank": {"inputs": items, "scores": scores, "threshold": 0.65,
                "display_all_score": 0.9, "fallback_limit": 3, "skipped": False},
        })
        checkpoint = build_rerank_checkpoint(_context(), tool, candidates=items,
            occurred_at=NOW.isoformat(), expires_at=(NOW + timedelta(days=30)).isoformat(), input_digests={"input": "1" * 64})
        policy = checkpoint.result["rerank_policy"]
        self.assertEqual((policy["input_count"], policy["completed_count"], policy["visible"], policy["stored_score_count"]), (70, 70, 3, 50))
        self.assertTrue(policy["scores_truncated"])
        self.assertEqual(policy["display_all_score"], 0.9)
        self.assertEqual(sum(item["visible"] for item in checkpoint.result["candidate_scores"]), 3)

    def test_rerank_skipped_and_partial_are_distinct(self):
        items = [{"rank": 1, "name": "q1", "score": 0.9}]
        for skipped in (True, False):
            with self.subTest(skipped=skipped):
                tool = ToolResult.partial(code="RERANK_SKIPPED_NO_IMAGE" if skipped else "RERANK_INCOMPLETE_COARSE_FALLBACK",
                    next_state="WAIT_CANDIDATE_CHOICE", error_category="external_model", data={
                    "reranked": False, "visible_candidates": items,
                    "checkpoint_rerank": {"inputs": items, "scores": [] if skipped else [{**items[0], "rerank_status": "timeout"}],
                        "threshold": 0.65, "display_all_score": 0.9, "fallback_limit": 3, "skipped": skipped},
                })
                checkpoint = build_rerank_checkpoint(_context(), tool, candidates=items,
                    occurred_at=NOW.isoformat(), expires_at=(NOW + timedelta(days=30)).isoformat(), input_digests={"input": "1" * 64})
                self.assertEqual(checkpoint.outcome, "skipped" if skipped else "partial")
                self.assertEqual(checkpoint.result["rerank_policy"]["failed_count"], 0 if skipped else 1)
                if not skipped:
                    self.assertEqual(checkpoint.failure.fallback, "coarse_order")

    def test_dimensions_distinguish_not_run_missing_and_failed_recognition(self):
        for trace, status in (({}, "not_run"), ({"triggered": True, "query_state": "none"}, "missing"),
                              ({"triggered": True, "reason": "recognition_failed"}, "uncertain")):
            tool = ToolResult.no_match(code="NO_COARSE_CANDIDATES", data={"candidates": [], "dimension_filter": trace,
                "checkpoint_counts": {"chapter_scanned": 12, "load_scored": 7, "positive_score": 0, "rerank_pool": 0, "after_dimension_filter": 0}})
            checkpoint = build_coarse_search_checkpoint(_context(), tool,
                occurred_at=NOW.isoformat(), expires_at=(NOW + timedelta(days=30)).isoformat(), input_digests={"input": "1" * 64})
            self.assertEqual({item["status"] for item in checkpoint.result["dimension_observations"]}, {status})

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

    def test_coarse_and_rerank_projections_validate_against_frozen_shapes(self):
        candidates = [
            {
                "rank": 1,
                "name": "q1",
                "candidate_key": "4力法|main|q1",
                "score": 0.91,
                "long_width": "4m",
                "single_side": "2m",
            }
        ]
        coarse = ToolResult.success(
            code="COARSE_CANDIDATES_FOUND",
            data={"candidates": candidates, "dimension_filter": {"reason": "disabled"},
                  "checkpoint_counts": {"chapter_scanned": 12, "load_scored": 10,
                      "positive_score": 5, "rerank_pool": 1, "after_dimension_filter": 1}},
        )
        coarse_result = coarse_search_result(coarse)
        self.assertEqual(coarse_result[SECTION_CANDIDATE_COUNTS]["after_dimension_filter"], 1)
        reranked = ToolResult.success(
            code="RERANK_COMPLETED",
            data={
                "reranked": True,
                "visible_candidates": [
                    {**candidates[0], "rerank_score": 0.94, "final_score": 0.95, "rerank_status": "completed"}
                ],
            },
        )
        reranked.data["checkpoint_rerank"] = {
            "inputs": candidates, "scores": reranked.data["visible_candidates"],
            "skipped": False, "threshold": 0.8, "display_all_score": 0.95, "fallback_limit": 3,
        }
        rerank_payload = rerank_result(reranked, candidates=candidates)
        self.assertEqual(len(rerank_payload[SECTION_CANDIDATE_SCORES]), 1)
        from tiku_agent.a2_checkpoint_stages import build_coarse_search_checkpoint, build_rerank_checkpoint
        coarse_checkpoint = build_coarse_search_checkpoint(
            _context(), coarse, occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            input_digests={"query": "1" * 64},
        )
        self.assertEqual(coarse_checkpoint.stage, "coarse_search_completed")
        rerank_checkpoint = build_rerank_checkpoint(
            _context(), reranked, candidates=candidates,
            occurred_at=NOW.isoformat(),
            expires_at=(NOW + timedelta(days=30)).isoformat(),
            input_digests={"query": "2" * 64},
        )
        self.assertEqual(rerank_checkpoint.stage, "rerank_completed")

    def test_no_match_answer_projection_omits_selection(self):
        payload = answer_prepared_result(
            ToolResult.no_match(code="ANSWER_FILES_NOT_FOUND"),
            selected_rank=1,
            selected_candidate={"name": "q1"},
            candidate_generation="4:1",
        )
        self.assertNotIn("selection", payload)


if __name__ == "__main__":
    unittest.main()

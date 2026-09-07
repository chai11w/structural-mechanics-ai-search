"""Exercise admitted A2 turns with real tools and the real evidence Store."""

from contextlib import closing, contextmanager
from datetime import UTC, datetime
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import search
from tests.test_checkpoint_capture import _context
from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.a2_checkpoint_recorder import A2CheckpointRecorderV1
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1
from tiku_agent.checkpoint_contract import CheckpointOwnerV1, EvidenceCapacityPolicyV1
from tiku_agent.checkpoint_store import SQLiteCheckpointStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.task_log import JsonlTaskLogger
from tiku_agent.tools import AgentToolConfig, rerank_candidates_tool
from tiku_agent.a2_checkpoint_stages import build_rerank_checkpoint
from tiku_agent.tool_result import ToolResult
from tiku_shared.trace_context import TraceContext, trace_context_scope
from tiku_shared.trace_events import TraceEventRecorder, SQLiteTraceEventStore, trace_event_scope


class A2CheckpointIntegrationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source.png"
        Image.new("RGB", (24, 18), "white").save(self.source)
        self.policy = EvidenceCapacityPolicyV1(
            max_checkpoint_rows=100, max_artifact_rows=100, max_audit_rows=100,
            max_trace_rows=1000, max_artifact_bytes=1_000_000,
            min_free_bytes=1, max_artifacts_per_checkpoint=10,
        )
        self.store = SQLiteCheckpointStore(
            self.root / "evidence.sqlite3", artifact_root=self.root / "evidence_images",
            capacity=self.policy, trace_db_path=self.root / "trace.sqlite3",
        )
        self.recorder = A2CheckpointRecorderV1(
            self.store, producer=_context().producer, media_root=self.root,
            gate=A2CheckpointCaptureGateV1(enabled=True),
        )
        self.trace_store = SQLiteTraceEventStore(self.root / "trace.sqlite3")
        self.traces = TraceEventRecorder(self.trace_store)
        self.addCleanup(self.traces.close)
        self.artifacts = SessionArtifacts(self.root / "sessions")
        self.tools = AgentToolbox(
            analyze_image=lambda *args, **kwargs: ToolResult.success(code="IMAGE_ANALYZED", data={
                "chapter": "4力法", "loads": [{"type": "集中", "raw": "P"}],
            }),
            classify_structure=lambda *args, **kwargs: ToolResult.success(code="STRUCTURE_CLASSIFIED", data={
                "structure_type": "梁", "source": "vision", "filter_applicable": True,
            }),
        )
        self.runtime = AgentSessionRuntime(
            SQLiteSessionStore(self.root / "session.sqlite3"), artifacts=self.artifacts,
            task_logger=JsonlTaskLogger(self.root / "tasks.jsonl"), checkpoint_recorder=self.recorder,
            agent_factory=lambda state: TikuSearchAgent(
                state=state, tools=self.tools, use_llm_intent=False,
                config=AgentToolConfig(runtime_dir=self.root, session_dir=self.artifacts.session_dir(state.session_id)),
            ),
        )

    @contextmanager
    def trace(self):
        context = TraceContext.create()
        with trace_context_scope(context), trace_event_scope(self.traces):
            self.last_trace = context
            yield context

    def records(self):
        if not self.store.path.exists():
            return []
        with closing(sqlite3.connect(self.store.path)) as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM checkpoints ORDER BY rowid")]

    def search(self, *, empty=False, rerank_failure=False):
        scan = search.ChapterCandidateScan(
            scored=[] if empty else [(0.96, "q1.png"), (0.1, "q2.png")],
            structure_filter_applied=False, dimensions_by_name={}, chapter_scanned=7,
        )
        def rerank(_image, candidates, **kwargs):
            if rerank_failure:
                raise RuntimeError("private failure")
            return [{**item, "rerank_status": "completed", "rerank_score": 0.97, "final_score": 0.965} for item in candidates]
        with self.trace(), patch("tiku_agent.tools.search.scan_chapter_candidates", return_value=scan), patch(
            "tiku_agent.tools.search.resolve_question_path", return_value=(self.source, "q1.png", False)
        ), patch("tiku_agent.tools.search.rerank_candidates", side_effect=rerank):
            return self.runtime.handle_prechecked_image("session-test", self.source, identity_key="invite_test")

    def test_successful_search_and_answer_have_real_artifacts_and_versions(self):
        response = self.search()
        self.assertEqual(response.media_kind, "candidates")
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            answer = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(answer.media_kind, "answer")
        records = self.records()
        self.assertEqual([row["stage"] for row in records], [
            "image_accepted", "image_routed", "question_analyzed", "coarse_search_completed", "rerank_completed", "answer_prepared",
        ])
        self.assertEqual([row["outcome"] for row in records], ["success"] * 6)
        coarse = records[3]["result"]["candidate_counts"]
        self.assertEqual((coarse["chapter_scanned"], coarse["load_scored"], coarse["positive_score"]), (7, 2, 2))
        self.assertEqual(records[2]["result"]["structure_decision"]["structure_type"], "梁")
        self.assertEqual(records[-1]["owner"]["candidate_generation"], records[-1]["result"]["selection"]["candidate_generation"])
        self.assertEqual(records[-1]["result"]["delivery"]["answer_artifact_count"], 1)
        self.assertEqual(records[-1]["predecessor_checkpoint_id"], records[-2]["checkpoint_id"])
        self.assertNotIn(str(self.root), json.dumps(records, ensure_ascii=False))
        self.assertEqual(self.recorder.health()["status"], "ok")
        self.traces.flush()
        events = self.trace_store.events_for_trace(self.last_trace.trace_id)
        links = [event.safe_attributes["checkpoint_id"] for event in events if "checkpoint_id" in event.safe_attributes]
        self.assertEqual(links, [records[-1]["checkpoint_id"]])

    def test_no_match_does_not_invent_rerank_or_selection(self):
        self.search(empty=True)
        records = self.records()
        self.assertEqual([row["stage"] for row in records][-2:], ["coarse_search_completed", "answer_prepared"])
        self.assertEqual(records[-1]["outcome"], "no_match")
        self.assertNotIn("selection", records[-1]["result"])

    def test_rerank_failure_preserves_last_successful_checkpoint(self):
        self.search(rerank_failure=True)
        records = self.records()
        self.assertEqual(records[-1]["outcome"], "failed")
        self.assertEqual(records[-1]["failure"]["last_successful_checkpoint_id"], records[-2]["checkpoint_id"])
        self.assertNotIn("private failure", json.dumps(records))
        with closing(sqlite3.connect(self.store.path)) as connection:
            lifetimes = connection.execute("SELECT retention_class, created_at, expires_at FROM artifacts").fetchall()
        self.assertEqual({(kind, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days) for kind, start, end in lifetimes}, {("normal", 3), ("failed", 7)})

    def test_store_failure_preserves_business_answer(self):
        with patch.object(self.store, "put_checkpoint", side_effect=OSError("private failure")):
            response = self.search()
        self.assertEqual(response.media_kind, "candidates")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.recorder.health()["status"], "degraded")

    def test_disabled_capture_does_no_evidence_io(self):
        self.recorder.gate = A2CheckpointCaptureGateV1()
        with patch.object(self.recorder, "read_image", side_effect=AssertionError("must not read")):
            response = self.search()
        self.assertEqual(response.media_kind, "candidates")
        self.assertFalse(self.store.path.exists())

    def test_answer_image_failure_records_partial_without_breaking_delivery(self):
        self.search()
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]), patch.object(
            self.store, "put_artifact", side_effect=OSError("private failure")
        ):
            answer = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(answer.media_kind, "answer")
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "partial")
        self.assertEqual(record["failure"]["code"], "ANSWER_ARTIFACT_UNAVAILABLE")
        self.assertEqual(record["result"]["delivery"]["answer_artifact_count"], 0)

    def test_missing_answer_files_records_no_match_with_no_selection(self):
        self.search()
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[]):
            self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "no_match")
        self.assertNotIn("selection", record["result"])
        self.assertEqual(record["result"]["delivery"]["delivery_code"], "NO_MATCH")

    def test_same_stage_fingerprint_does_not_depend_on_trace(self):
        self.search()
        before = self.records()[2]
        # Chapter correction reruns the same logical input in a new trace.
        state = self.runtime.store.load("session-test")
        state.candidate_generation = ""
        agent = self.runtime._make_agent(state)
        self.runtime._attach_checkpoint_emitter(agent, identity_key="invite_test", request_id="")
        with self.trace():
            agent._emit_question_checkpoint()
        after = self.records()[-1]
        self.assertEqual(before["stage"], after["stage"])
        self.assertNotEqual(before["trace_id"], after["trace_id"])
        self.assertEqual(before["input_fingerprint"], after["input_fingerprint"])

    def test_predecessor_lookup_rejects_other_identity_and_task_revision(self):
        self.search()
        record = self.records()[-1]
        owner = CheckpointOwnerV1(**record["owner"])
        found = self.store.latest_successful_checkpoint(owner, actor_key="a2_capture")
        self.assertEqual(found.checkpoint_id, record["checkpoint_id"])
        self.assertIsNone(self.store.latest_successful_checkpoint(replace(owner, identity_key="other_user"), actor_key="a2_capture"))
        self.assertIsNone(self.store.latest_successful_checkpoint(replace(owner, task_revision=2, workflow_task_revision=2, candidate_generation="2:1"), actor_key="a2_capture"))

    def test_real_capacity_rejection_keeps_search_operational(self):
        self.store.capacity = replace(self.policy, max_checkpoint_rows=1)
        response = self.search()
        self.assertEqual(response.media_kind, "candidates")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.store.health()["status"], "degraded")

    def test_global_search_records_internal_coarse_and_rerank_boundaries(self):
        self.tools.analyze_image = lambda *args, **kwargs: ToolResult.success(code="IMAGE_ANALYZED", data={"loads": [{"type": "集中", "raw": "P"}]})
        with self.trace():
            self.runtime.handle_prechecked_image("session-test", self.source, identity_key="invite_test")
        import pandas as pd
        frame = pd.DataFrame([{"题目名称": "q1.png", "荷载": json.dumps({"loads": [{"type": "集中", "raw": "P"}]}, ensure_ascii=False)}])
        def score(_image, candidates, **kwargs):
            return [{**item, "rerank_status": "completed", "rerank_score": 1.0, "final_score": 1.0} for item in candidates]
        with self.trace(), patch("tiku_agent.tools.CHAPTERS", ["4力法"]), patch("tiku_agent.tools.load_bank_excel", return_value=frame), patch(
            "tiku_agent.tools.search.resolve_question_path", return_value=(self.source, "q1.png", False)
        ), patch("tiku_agent.tools._score_global_candidates", side_effect=score):
            self.runtime.handle_text("session-test", "全局搜索", identity_key="invite_test")
        records = self.records()
        self.assertEqual([row["stage"] for row in records][-3:], ["question_analyzed", "coarse_search_completed", "rerank_completed"])
        self.assertEqual(records[-1]["result"]["rerank_policy"]["input_count"], 1)
        self.assertEqual(records[-2]["result"]["candidate_counts"]["chapter_scanned"], 1)

    def test_partial_rerank_keeps_actual_success_and_failure_counts_before_fallback(self):
        other = self.root / "other.png"
        Image.new("RGB", (18, 12), "black").save(other)
        candidates = [{"rank": i, "path": str(path), "name": path.name, "score": 0.95} for i, path in enumerate((self.source, other), 1)]
        def score(_image, item, **kwargs):
            return {**item, "rerank_status": "completed" if item["rank"] == 1 else "timeout", "rerank_score": 0.99 if item["rank"] == 1 else None, "final_score": 0.97 if item["rank"] == 1 else 0.95}
        with patch("search.score_rerank_candidate", side_effect=score):
            result = rerank_candidates_tool(self.source, candidates, route="main", retry_max_candidates=0)
        now = datetime.now(UTC)
        from datetime import timedelta
        checkpoint = build_rerank_checkpoint(_context(), result, candidates=candidates,
            occurred_at=now.isoformat(), expires_at=(now + timedelta(days=30)).isoformat(), input_digests={"input": "1" * 64})
        self.assertEqual(checkpoint.outcome, "partial")
        policy = checkpoint.result["rerank_policy"]
        self.assertEqual((policy["completed_count"], policy["failed_count"], policy["visible"]), (1, 1, 2))
        self.assertEqual([item["path"] for item in result.data["visible_candidates"]], [str(self.source), str(other)])

    def test_unrepresentable_visible_set_records_bounded_evidence_failure(self):
        items = [{"rank": i, "name": f"q{i}", "score": 1.0, "rerank_status": "completed"} for i in range(1, 52)]
        tool = ToolResult.success(code="RERANK_COMPLETED", data={"reranked": True, "visible_candidates": items,
            "checkpoint_rerank": {"inputs": items, "scores": items, "threshold": 0.65, "display_all_score": 0.9, "fallback_limit": 3}})
        result = self.recorder.capture_stage(_context(),
            admission=A2CaptureAdmissionV1("search", True, True, True, True, True),
            stage="rerank_completed", tool_result=tool)
        self.assertTrue(result.stored)
        checkpoint = self.records()[-1]
        self.assertEqual(checkpoint["outcome"], "failed")
        self.assertEqual(checkpoint["failure"]["kind"], "evidence")
        self.assertEqual(checkpoint["failure"]["code"], "CAPTURE_INVALID")
        self.assertEqual(checkpoint["result"], {})
        self.assertEqual(len(tool.data["visible_candidates"]), 51)



if __name__ == "__main__":
    unittest.main()

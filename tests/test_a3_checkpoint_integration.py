from contextlib import contextmanager, closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import json
import sqlite3
import unittest
from unittest.mock import patch

import search
from tests import test_a2_checkpoint_integration as a2_fixture
from tests import test_a3_runtime as a3_fixture
from tiku_agent.a3_checkpoint_context import current_a3_checkpoint_binding
from tiku_agent.a3_checkpoint_recorder import A3CheckpointRecorderV1
from tiku_agent.a3_checkpoint_stages import page_result
from tiku_agent.a3_runtime import A3MvpRuntime, SQLiteA3SessionStore
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.tool_result import ToolOutcome, ToolResult
from tiku_agent.checkpoint_contract import CheckpointOwnerV1
from tiku_shared.trace_context import submit_with_trace_context


class A3CheckpointIntegrationTest(unittest.TestCase):
    trace = a2_fixture.A2CheckpointIntegrationTest.trace
    records = a2_fixture.A2CheckpointIntegrationTest.records

    def setUp(self):
        a2_fixture.A2CheckpointIntegrationTest.setUp(self)
        self.a2 = self.runtime
        self.recorder = A3CheckpointRecorderV1(
            self.store, producer=self.recorder.producer, media_root=self.root,
            a3_media_root=self.root / "pages", gate=A2CheckpointCaptureGateV1(enabled=True),
        )
        self.a2.checkpoint_recorder = self.recorder
        self.verifier = a3_fixture.FakeVerifier()
        self.runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "pages"), a2_runtime=self.a2,
            page_observer=a3_fixture.FakeSingleObserver(), crop_verifier=self.verifier,
            auto_cropper=a3_fixture.FakeSingleAutoCropper(), auto_prepare_all_units=True,
            external_load_screen=lambda _path: "yes", checkpoint_recorder=self.recorder,
        )

    @contextmanager
    def business(self):
        scan = search.ChapterCandidateScan(scored=[(0.96, "q1.png")],
            structure_filter_applied=False, dimensions_by_name={}, chapter_scanned=7)
        def rerank(_image, candidates, **kwargs):
            return [{**item, "rerank_status": "completed", "rerank_score": 0.97, "final_score": 0.965} for item in candidates]
        with self.trace(), patch("tiku_agent.tools.search.scan_chapter_candidates", return_value=scan), patch(
            "tiku_agent.tools.search.resolve_question_path", return_value=(self.source, "q1.png", False)
        ), patch("tiku_agent.tools.search.rerank_candidates", side_effect=rerank):
            yield

    def upload(self):
        with self.business():
            return self.runtime.handle_image("session-test", self.source, identity_key="invite_test")

    def test_single_auto_flow_links_parent_child_answer_and_deduplicates_images(self):
        response = self.upload()
        self.assertEqual(response.media_kind, "candidates")
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            answer = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(answer.media_kind, "answer")
        records = self.records()
        self.assertEqual([row["stage"] for row in records], [
            "image_accepted", "image_routed", "page_understood", "crop_prepared", "crop_validated",
            "question_analyzed", "coarse_search_completed", "rerank_completed", "answer_prepared",
        ])
        self.assertEqual({row["outcome"] for row in records}, {"success"})
        workflow = records[0]["owner"]["workflow_search_id"]
        self.assertEqual({row["owner"]["workflow_search_id"] for row in records}, {workflow})
        child = records[5]["owner"]
        self.assertNotEqual(child["search_id"], workflow)
        self.assertEqual(child["unit_id"], "g1-u1")
        crop_id = next(link["artifact_id"] for link in records[3]["artifacts"] if link["role"] == "question_crop")
        for row in records[4:]:
            self.assertIn(crop_id, [link["artifact_id"] for link in row["artifacts"]])
        self.assertEqual(records[5]["predecessor_checkpoint_id"], records[4]["checkpoint_id"])
        self.assertEqual(records[-1]["predecessor_checkpoint_id"], records[-2]["checkpoint_id"])
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0], 2)
        self.assertEqual(self.recorder.health()["status"], "ok")
        self.assertIsNone(current_a3_checkpoint_binding.get())
        self.assertNotIn(str(self.root), json.dumps(records))

    def test_parallel_units_have_independent_crop_chains_and_selected_child(self):
        self.runtime.page_observer = a3_fixture.FakeObserver()
        self.runtime.auto_cropper = a3_fixture.FakeAutoCropper(second_status="auto_ready")
        self.upload()
        records = self.records()
        validated = [row for row in records if row["stage"] == "crop_validated"]
        self.assertEqual({row["owner"]["unit_id"] for row in validated}, {"g1-u1", "g1-u2"})
        by_id = {row["checkpoint_id"]: row for row in records}
        for row in validated:
            self.assertEqual(by_id[row["predecessor_checkpoint_id"]]["owner"]["unit_id"], row["owner"]["unit_id"])
        with self.business():
            response = self.runtime.select_unit("session-test", "g1-u2", identity_key="invite_test")
        self.assertEqual(response.media_kind, "candidates")
        self.assertEqual(self.records()[-1]["owner"]["unit_id"], "g1-u2")

    def test_manual_fallback_records_real_geometry_and_both_gates(self):
        self.runtime.auto_cropper = None
        self.upload()
        with self.business():
            self.runtime.handle_crop("session-test", {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8}, identity_key="invite_test")
        records = self.records()
        prepared = next(row for row in records if row["stage"] == "crop_prepared")
        self.assertEqual(prepared["result"]["crop_geometry"]["method"], "manual")
        validated = next(row for row in records if row["stage"] == "crop_validated")
        self.assertEqual(validated["result"]["crop_validation"]["external_load_status"], "yes")
        self.assertEqual(records[-1]["owner"]["unit_id"], "g1-u1")

    def test_rejected_crop_has_no_child_and_no_fabricated_external_check(self):
        self.verifier.verdict = "review_required"
        self.verifier.checks = {**self.verifier.checks, "structure_complete": False}
        self.upload()
        last = self.records()[-1]
        self.assertEqual((last["stage"], last["outcome"]), ("crop_validated", "needs_input"))
        self.assertEqual(last["result"]["crop_validation"]["external_load_status"], "not_run")
        self.assertFalse(any(row["owner"]["scope"] == "child_task" for row in self.records()))

    def test_disabled_and_missing_identity_do_not_touch_evidence(self):
        for gate, identity in ((False, "invite_test"), (True, "")):
            self.recorder.gate = A2CheckpointCaptureGateV1(enabled=gate)
            with self.business(), patch.object(self.recorder, "read_image", side_effect=AssertionError("must not read")):
                response = self.runtime.handle_image("session-test", self.source, identity_key=identity)
            self.assertEqual(response.media_kind, "candidates")
        self.assertFalse(self.store.path.exists())

    def test_capacity_failure_keeps_business_operational(self):
        self.store.capacity = replace(self.policy, max_checkpoint_rows=1)
        self.assertEqual(self.upload().media_kind, "candidates")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.recorder.health()["status"], "degraded")

    def test_page_failure_is_safe_and_does_not_invent_crop(self):
        with patch.object(self.runtime.page_observer, "observe", side_effect=RuntimeError("private failure")):
            self.upload()
        records = self.records()
        self.assertEqual([row["stage"] for row in records], ["image_accepted", "image_routed", "page_understood"])
        self.assertEqual(records[-1]["outcome"], "failed")
        self.assertNotIn("private failure", json.dumps(records))

    def test_page_projection_preserves_more_than_ten_units_and_explicit_truncation(self):
        page = a3_fixture._page_payload()
        units = [{**page["groups"][0]["units"][0], "group_id": "g1", "unit_id": f"u{i}"} for i in range(55)]
        for count in (11, 50, 55):
            result = page_result(page, units[:count])
            self.assertEqual(result["page_summary"]["unit_count"], count)
            self.assertEqual(len(result["unit_results"]), min(count, 50))
            self.assertEqual(result["page_summary"]["units_truncated"], count > 50)

    def test_long_page_summary_fits_byte_bound_without_losing_unit_identities(self):
        page = a3_fixture._page_payload()
        units = [{**page["groups"][0]["units"][0], "group_id": "g1", "unit_id": f"u{i}",
                  "title_text": chr(0x4e2d) * 2000, "display_label": chr(0x4e2d) * 200} for i in range(50)]
        result = page_result(page, units)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), 65536)
        self.assertEqual([unit["unit_id"] for unit in result["unit_results"]], [unit["unit_id"] for unit in units])

    def test_parent_revision_is_not_used_as_child_candidate_revision(self):
        self.upload()
        self.upload()
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        records = self.records()
        latest = records[-1]["owner"]
        self.assertEqual(latest["workflow_task_revision"], 2)
        self.assertEqual(latest["task_revision"], 1)
        self.assertTrue(latest["candidate_generation"].startswith("1:"))
        by_id = {row["checkpoint_id"]: row for row in records}
        for row in records:
            predecessor = row["predecessor_checkpoint_id"]
            if predecessor:
                self.assertEqual(by_id[predecessor]["owner"]["workflow_search_id"], row["owner"]["workflow_search_id"])

    def test_failed_child_retains_parent_page_under_correct_role(self):
        self.tools.analyze_image = lambda *args, **kwargs: ToolResult(
            outcome=ToolOutcome.ERROR, code="IMAGE_ANALYSIS_FAILED", error_category="external_model")
        self.upload()
        records = self.records()
        last = records[-1]
        self.assertEqual((last["stage"], last["outcome"]), ("question_analyzed", "failed"))
        self.assertEqual(last["owner"]["unit_id"], "g1-u1")
        with closing(sqlite3.connect(self.store.path)) as connection:
            source_link = next(link for link in last["artifacts"] if link["role"] == "source_page")
            row = connection.execute("SELECT owner_json, sha256, retention_class FROM artifacts WHERE artifact_id = ?",
                (source_link["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["unit_id"], "")
        self.assertEqual(row[1], records[0]["result"]["image_metadata"]["sha256"])
        self.assertEqual(row[2], "failed")

    def test_artifact_copy_failure_preserves_structured_page_and_crop(self):
        with patch.object(self.store, "put_artifact", side_effect=OSError("private artifact failure")):
            response = self.upload()
        self.assertEqual(response.media_kind, "candidates")
        records = self.records()
        page = next(row for row in records if row["stage"] == "page_understood")
        crop = next(row for row in records if row["stage"] == "crop_prepared")
        self.assertEqual(page["outcome"], "partial")
        self.assertEqual(crop["outcome"], "partial")
        self.assertIn("crop_geometry", crop["result"])
        self.assertNotIn("private artifact failure", json.dumps(records))

    def test_external_gate_no_and_error_preserve_checks_and_prevent_child(self):
        for value in ("no", "error", "unknown"):
            with self.subTest(value=value):
                self.runtime.external_load_screen = lambda _path: value
                self.upload()
                row = self.records()[-1]
                self.assertEqual(row["outcome"], "needs_input")
                validation = row["result"]["crop_validation"]
                self.assertEqual(validation["external_load_status"], "no" if value == "no" else "error")
                self.assertTrue(all(validation["checks"].values()))

    def test_missing_trace_does_not_read_images_or_store(self):
        with patch("tiku_agent.a3_checkpoint_recorder.current_trace_id", return_value=""), patch(
            "tiku_agent.session_runtime.current_trace_id", return_value=""
        ), patch.object(self.recorder, "read_image", side_effect=AssertionError("must not read")):
            self.assertEqual(self.upload().media_kind, "candidates")
        self.assertFalse(self.store.path.exists())

    def test_stale_action_does_not_create_evidence(self):
        self.runtime.page_observer = a3_fixture.FakeObserver()
        self.runtime.auto_cropper = a3_fixture.FakeAutoCropper(second_status="auto_ready")
        self.upload()
        before = len(self.records())
        with self.business():
            response = self.runtime.select_unit("session-test", "g1-u1", identity_key="invite_test",
                task_revision=999, workflow_search_id="search_stale")
        self.assertEqual(response.protocol["code"], "STALE_ACTION")
        self.assertEqual(len(self.records()), before)

    def test_parent_lookup_rejects_other_identity_revision_and_unit(self):
        self.upload()
        owner = CheckpointOwnerV1(**self.records()[-1]["owner"])
        parent = self.recorder.parent_checkpoint(owner)
        self.assertEqual(parent.owner.unit_id, "g1-u1")
        self.assertIsNone(self.recorder.parent_checkpoint(replace(owner, identity_key="other_user")))
        self.assertIsNone(self.recorder.parent_checkpoint(replace(owner, workflow_task_revision=999)))
        other_unit = self.recorder.parent_checkpoint(replace(owner, unit_id="other_unit"))
        self.assertEqual(other_unit.owner.unit_id, "")
        self.assertFalse(any(link.role == "question_crop" for link in other_unit.artifacts))

    def test_concurrent_sessions_keep_distinct_parent_identity(self):
        with self.business(), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [submit_with_trace_context(pool, self.runtime.handle_image,
                f"session-{index}", self.source, identity_key=f"invite_{index}") for index in (1, 2)]
            self.assertTrue(all(future.result().media_kind == "candidates" for future in futures))
        records = self.records()
        by_id = {row["checkpoint_id"]: row for row in records}
        for row in records:
            previous = by_id.get(row["predecessor_checkpoint_id"])
            if previous:
                self.assertEqual(previous["owner"]["identity_key"], row["owner"]["identity_key"])
                self.assertEqual(previous["owner"]["workflow_search_id"], row["owner"]["workflow_search_id"])
        self.assertEqual({row["owner"]["identity_key"] for row in records}, {"invite_1", "invite_2"})
        self.assertEqual(len(records), 16)

    def test_direct_a2_route_preserves_standalone_contract_without_fake_units(self):
        self.runtime.image_triage_authority = a3_fixture.FakeFlowAuthority("A2")
        self.assertEqual(self.upload().media_kind, "candidates")
        records = self.records()
        self.assertEqual([row["stage"] for row in records], ["image_accepted", "image_routed",
            "image_accepted", "image_routed",
            "question_analyzed", "coarse_search_completed", "rerank_completed"])
        self.assertEqual(records[-1]["owner"]["unit_id"], "")
        self.assertEqual(records[-1]["owner"]["workflow_search_id"], records[-1]["owner"]["search_id"])
        self.assertEqual(self.recorder.health()["status"], "ok")
        with closing(sqlite3.connect(self.store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0], 1)

    def test_expired_parent_artifacts_are_not_reused_or_renewed_by_child(self):
        self.upload()
        later = datetime.now(UTC) + timedelta(days=4)
        self.store._clock = lambda: later
        self.recorder.clock = lambda: later
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            answer = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(answer.media_kind, "answer")
        last = self.records()[-1]
        self.assertEqual(last["stage"], "answer_prepared")
        self.assertEqual({link["role"] for link in last["artifacts"]}, {"answer_image"})
        self.assertEqual(self.recorder.health()["status"], "degraded")

    def test_manual_verifier_failure_keeps_failed_crop_evidence(self):
        self.runtime.auto_cropper = None
        self.upload()
        with self.business(), patch.object(self.verifier, "verify", side_effect=RuntimeError("private verifier failure")):
            self.runtime.handle_crop("session-test", {"x": 0, "y": 0, "width": 0.5, "height": 0.5}, identity_key="invite_test")
        last = self.records()[-1]
        self.assertEqual((last["stage"], last["outcome"]), ("crop_validated", "failed"))
        self.assertIn("question_crop", [link["role"] for link in last["artifacts"]])
        self.assertNotIn("private verifier failure", json.dumps(last))

    def test_parent_fingerprints_are_stable_across_network_traces(self):
        self.upload()
        state = self.runtime.store.load("session-test")
        before = next(row for row in self.records() if row["stage"] == "page_understood")
        with self.trace():
            self.recorder.capture_parent(state, identity_key="invite_test", stage="page_understood")
        after = self.records()[-1]
        self.assertEqual(before["input_fingerprint"], after["input_fingerprint"])
        self.assertNotEqual(before["trace_id"], after["trace_id"])


if __name__ == "__main__":
    unittest.main()

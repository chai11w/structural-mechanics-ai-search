"""Phase 4.5.2 acceptance: current bank references and frozen stage resources."""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, UTC
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from scripts import tiku_checkpoint_diagnostics as cli
from tests import test_a2_checkpoint_integration as a2_fixture
from tests import test_a3_checkpoint_integration as a3_fixture
from tests.test_checkpoint_contract import _checkpoint
from tiku_agent.checkpoint_bank_reference import BankReferenceV1, CheckpointBankCatalog
from tiku_agent.checkpoint_contract import CheckpointOwnerV1
from tiku_agent.checkpoint_stage_input import FrozenCheckpointInput, freeze_a2_stage_input
from tiku_agent.checkpoint_store import (
    CheckpointQueryScopeV1, EvidenceAuditError, EvidenceNotFoundError, EvidenceMaintenanceError,
)
from tiku_agent.tool_result import ToolResult
from tiku_diagnostics.checkpoints import CheckpointDiagnosticService


class ReferenceContractTest(unittest.TestCase):
    def test_real_directory_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outside = root / "outside"
            bank = root / "bank"
            outside.mkdir()
            bank.mkdir()
            Image.new("RGB", (5, 5), "red").save(outside / "q.png")
            link = bank / "linked"
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                catalog = CheckpointBankCatalog({"main": bank})
                reference = catalog.reference(link / "q.png", chapter="chapter")
                with self.assertRaises(EvidenceMaintenanceError):
                    catalog.read(reference)
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_unsafe_keys_are_rejected(self):
        for key in ("../q.png", "/q.png", "C:/q.png", "C:q.png", "//host/q.png",
                    "a\\q.png", "a/../q.png", "a//q.png", "q.png:stream", "CON.png",
                    "a /q.png", "a./q.png", "%2e%2e/q.png", "q.txt", "a/./q.png"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                BankReferenceV1("main", "chapter", key)

    def test_reference_construction_has_no_file_io_and_read_refuses_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            catalog = CheckpointBankCatalog({"main": root})
            with patch.object(Path, "open", side_effect=AssertionError("unexpected I/O")), patch.object(
                Path, "resolve", side_effect=AssertionError("unexpected resolution")
            ):
                reference = catalog.reference(root / "missing.png", chapter="chapter")
            with self.assertRaises(FileNotFoundError):
                catalog.read(reference)
            with self.assertRaises(ValueError):
                catalog.reference(root.parent / "outside.png", chapter="chapter")
            with patch("tiku_agent.checkpoint_store._reject_linked_path", side_effect=ValueError("linked path")), patch.object(
                Path, "open", side_effect=AssertionError("must reject before read")
            ):
                with self.assertRaises(ValueError):
                    catalog.read(reference)
            with self.assertRaises(TypeError):
                catalog.roots["main"] = root.parent

    def test_schema_two_is_distinct_and_keeps_legacy_artifacts(self):
        legacy = _checkpoint("answer_prepared")
        self.assertEqual(legacy.schema_version, 1)
        result = legacy.to_dict()["result"]
        result["delivery"].update(answer_artifact_count=0, answer_refs=[
            BankReferenceV1("main", "chapter", "answer.png").to_dict()])
        current = replace(legacy, schema_version=2, result=result, artifacts=())
        self.assertEqual(current.schema_version, 2)
        with self.assertRaises(ValueError):
            replace(current, schema_version=1)
        with self.assertRaises(ValueError):
            replace(current, schema_version=True)
        result["delivery"]["answer_refs"] = []
        with self.assertRaises(ValueError):
            replace(current, result=result)
        result["delivery"].update(answer_artifact_count=1, answer_refs=[
            BankReferenceV1("main", "chapter", "answer.png").to_dict()])
        with self.assertRaises(ValueError):
            replace(current, result=result, artifacts=legacy.artifacts)

    def test_snapshot_detaches_nested_inputs_and_rejects_bounds(self):
        result = ToolResult.success(code="TEST", data={"candidates": [{"score": 0.9}], "raw_output": "private"})
        payload = {"inputs": {"loads": [{"raw": "P"}]}}
        frozen = freeze_a2_stage_input(result, payload)
        result.data["candidates"][0]["score"] = 0.1
        payload["inputs"]["loads"][0]["raw"] = "Q"
        materialized = frozen.materialize()
        self.assertEqual(materialized["data"]["candidates"][0]["score"], 0.9)
        self.assertEqual(materialized["payload"]["inputs"]["loads"][0]["raw"], "P")
        self.assertNotIn("raw_output", materialized["data"])
        materialized["payload"].clear()
        self.assertTrue(frozen.materialize()["payload"])
        self.assertNotIn("loads", repr(frozen))
        for oversized in ([0] * 1001, "x" * 16385, ["x" * 16000] * 40,
                          [[[[[[[[[[[[[0]]]]]]]]]]]]], float("nan")):
            with self.assertRaises(ValueError):
                FrozenCheckpointInput.capture(oversized)


class BankFlowTest(unittest.TestCase):
    setUp = a2_fixture.A2CheckpointIntegrationTest.setUp
    trace = a2_fixture.A2CheckpointIntegrationTest.trace
    records = a2_fixture.A2CheckpointIntegrationTest.records
    search = a2_fixture.A2CheckpointIntegrationTest.search

    def test_missing_source_references_never_fall_back_to_delivery_copies(self):
        self.search()
        capture = self.recorder.capture_stage
        def omit_source(context, **kwargs):
            if kwargs["stage"] == "answer_prepared":
                kwargs["payload"].pop("answer_source_paths")
                self.assertTrue(kwargs["payload"]["answer_paths"])
            return capture(context, **kwargs)
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]), patch.object(
            self.recorder, "capture_stage", side_effect=omit_source
        ):
            response = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(response.media_kind, "answer")
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "partial")
        self.assertEqual(record["result"]["delivery"]["answer_refs"], [])

    def test_oversized_capture_does_not_break_business_search(self):
        from tiku_agent.checkpoint_stage_input import freeze_a2_stage_input as freeze
        def oversized(result, payload):
            return freeze(result, {**payload, "oversized": [0] * 1001})
        with patch("tiku_agent.checkpoint_stage_input.freeze_a2_stage_input", side_effect=oversized):
            response = self.search()
        self.assertEqual(response.media_kind, "candidates")
        self.assertEqual(self.recorder.health()["status"], "degraded")

    def prepare_answer(self):
        self.search()
        self.answer_paths = [self.root / "answer1.png", self.root / "answer2.png"]
        for path, color in zip(self.answer_paths, ("red", "blue")):
            Image.new("RGB", (30, 20), color).save(path)
        read = self.recorder.read_image
        def question_only(path):
            self.assertNotIn(Path(path), self.answer_paths)
            return read(path)
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=self.answer_paths), patch.object(
            self.recorder, "read_image", side_effect=question_only
        ), patch.object(self.store, "put_artifact", side_effect=AssertionError("no answer Artifact")):
            response = self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(response.media_kind, "answer")
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual([ref["relative_key"] for ref in record["result"]["delivery"]["answer_refs"]],
                         ["answer1.png", "answer2.png"])
        self.service = CheckpointDiagnosticService(self.root, capacity=self.policy, actor_key="operator",
            scope=CheckpointQueryScopeV1(record["owner"]["identity_key"], record["owner"]["session_key"]))
        self.service.store = self.store
        return record

    def test_current_content_missing_file_old_artifact_and_delete(self):
        record = self.prepare_answer()
        reference, before = self.service.bank_image(record["checkpoint_id"], catalog=self.recorder.bank_catalog, answer_ordinal=1)
        Image.new("RGB", (30, 20), "green").save(self.answer_paths[0])
        _, after = self.service.bank_image(record["checkpoint_id"], catalog=self.recorder.bank_catalog, answer_ordinal=1)
        self.assertNotEqual(before, after)
        self.assertEqual(reference.lookup_mode, "current")
        first = self.records()[0]
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(self.service.artifact(first["checkpoint_id"], first["artifacts"][0]["artifact_id"]).content,
                         self.source.read_bytes())
        candidate = self.records()[3]
        _, question = self.service.bank_image(candidate["checkpoint_id"], catalog=self.recorder.bank_catalog,
            candidate_id=candidate["result"]["candidate_scores"][0]["candidate_id"])
        self.assertEqual(question, self.source.read_bytes())
        self.answer_paths[0].unlink()
        with self.assertRaises(FileNotFoundError):
            self.service.bank_image(record["checkpoint_id"], catalog=self.recorder.bank_catalog, answer_ordinal=1)
        self.store.delete_evidence(record["checkpoint_id"], actor_key="operator",
            expected_owner=CheckpointOwnerV1(**record["owner"]), reason_code="TEST_DELETE")
        self.assertTrue(self.answer_paths[1].exists())

    def test_scope_expiry_and_audit_reject_before_bank_read(self):
        record = self.prepare_answer()
        catalog = Mock()
        with patch.object(self.store, "_insert_audit_locked", side_effect=EvidenceAuditError("test")):
            with self.assertRaises(EvidenceAuditError):
                self.service.bank_image(record["checkpoint_id"], catalog=catalog, answer_ordinal=1)
        original = self.service.scope
        self.service.scope = replace(original, identity_key="foreign")
        with self.assertRaises(EvidenceNotFoundError):
            self.service.bank_image(record["checkpoint_id"], catalog=catalog, answer_ordinal=1)
        self.service.scope = original
        self.store._clock = lambda: datetime.now(UTC) + timedelta(days=31)
        with self.assertRaises(EvidenceNotFoundError):
            self.service.bank_image(record["checkpoint_id"], catalog=catalog, answer_ordinal=1)
        catalog.read.assert_not_called()
        self.assertTrue(all(path.exists() for path in self.answer_paths))

    def test_cli_bank_image_emits_current_hash_without_root(self):
        record = self.prepare_answer()
        args = ["--runtime-root", str(self.root), "--actor-key", "operator", "--identity-key", "invite_test",
                "--session-key", record["owner"]["session_key"]]
        for key in cli.CAPACITY_FIELDS:
            args += ["--" + key.replace("_", "-"), str(getattr(self.policy, key))]
        args += ["bank-image", "--checkpoint-id", record["checkpoint_id"], "--bank-root", str(self.root),
                 "--answer-ordinal", "2"]
        output = StringIO()
        with patch.object(cli, "CheckpointDiagnosticService", return_value=self.service), redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["current_sha256"], sha256(self.answer_paths[1].read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_runtime_freezes_before_predecessor_io(self):
        self.search()
        state = self.runtime.store.load("session-test")
        agent = self.runtime._make_agent(state)
        self.runtime._attach_checkpoint_emitter(agent, identity_key="invite_test", request_id="")
        result = ToolResult.success(code="ANALYZED", data={"chapter": "4力法", "loads": [{"type": "集中", "raw": "P"}]})
        payload = {"nested": {"value": "before"}}
        original = self.recorder.capture_stage
        captured = []
        def mutate(context):
            state.current_chapter = "changed"
            result.data["loads"][0]["raw"] = "Q"
            payload["nested"]["value"] = "after"
            return ""
        def inspect(context, **kwargs):
            captured.append(kwargs)
            return original(context, **kwargs)
        with self.trace(), patch.object(self.recorder, "last_successful", side_effect=mutate), patch.object(
            self.recorder, "capture_stage", side_effect=inspect
        ):
            agent.checkpoint_emitter("question_analyzed", result, payload)
        self.assertEqual(captured[0]["tool_result"].data["loads"][0]["raw"], "P")
        self.assertEqual(captured[0]["payload"]["nested"]["value"], "before")
        self.assertEqual(captured[0]["payload"]["inputs"]["chapter"], "4力法")


class CropFreezeTest(unittest.TestCase):
    setUp = a3_fixture.A3CheckpointIntegrationTest.setUp
    trace = a3_fixture.A3CheckpointIntegrationTest.trace
    records = a3_fixture.A3CheckpointIntegrationTest.records
    business = a3_fixture.A3CheckpointIntegrationTest.business
    upload = a3_fixture.A3CheckpointIntegrationTest.upload

    def test_recrop_keeps_first_bytes_and_freezes_geometry_before_io(self):
        self.upload()
        state = self.runtime.store.load("session-test")
        unit = state.selected_unit_id
        bounds1 = dict(x=0, y=0, width=0.5, height=0.5)
        bounds2 = dict(x=0, y=0, width=1, height=1)
        first = self.runtime._crop_source_for_unit(state, unit, bounds1)
        content = first.read_bytes()
        second = self.runtime._crop_source_for_unit(state, unit, bounds2)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), content)
        self.assertNotEqual(content, second.read_bytes())
        record = dict(state.auto_crops[unit], path=str(first), bounds=bounds1.copy())
        original_page = state.source_page_path
        def mutate(context):
            record["path"] = str(second)
            record["bounds"].update(bounds2)
            state.source_page_path = "missing.png"
            return ""
        with self.trace(), patch.object(self.recorder, "last_successful", side_effect=mutate):
            captured = self.recorder.capture_parent(state, identity_key="invite_test", stage="crop_prepared",
                                                     unit_id=unit, record=record, method="manual")
        self.assertTrue(captured.stored)
        last = self.records()[-1]
        self.assertEqual(last["outcome"], "success")
        crop = next(link for link in last["artifacts"] if link["role"] == "question_crop")
        artifact = self.store.read_artifact(crop["artifact_id"], checkpoint_id=last["checkpoint_id"],
            actor_key="operator", expected_checkpoint_owner=CheckpointOwnerV1(**last["owner"]))
        self.assertEqual(artifact.content, content)
        self.assertEqual(last["result"]["crop_geometry"]["pixel_bounds"],
                         dict(left=0, top=0, right=12, bottom=9))
        state.source_page_path = original_page
        with self.trace():
            captured = self.recorder.capture_parent(state, identity_key="invite_test", stage="crop_prepared",
                                                     unit_id=unit, record=record, method="manual")
        self.assertTrue(captured.stored)
        latest = self.records()[-1]
        self.assertNotEqual(last["checkpoint_id"], latest["checkpoint_id"])
        self.assertEqual(latest["result"]["crop_geometry"]["pixel_bounds"],
                         dict(left=0, top=0, right=24, bottom=18))
        link = next(link for link in latest["artifacts"] if link["role"] == "question_crop")
        artifact = self.store.read_artifact(link["artifact_id"], checkpoint_id=latest["checkpoint_id"],
            actor_key="operator", expected_checkpoint_owner=CheckpointOwnerV1(**latest["owner"]))
        self.assertEqual(artifact.content, second.read_bytes())
        self.assertEqual(first.read_bytes(), content)

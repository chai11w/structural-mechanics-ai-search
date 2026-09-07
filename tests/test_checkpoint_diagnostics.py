from contextlib import closing, contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import timedelta
from io import StringIO
import json
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import tiku_checkpoint_diagnostics as cli
from tests import test_checkpoint_store as fixture
from tiku_agent.checkpoint_contract import ArtifactLinkV1, EvidenceCapacityPolicyV1
from tiku_agent.checkpoint_store import (
    CheckpointQueryScopeV1, EvidenceAuditError, EvidenceConflictError,
    EvidenceExpiredError, EvidenceNotFoundError, EvidenceUnavailableError,
    EvidenceValidationError, SQLiteCheckpointStore,
    utc_now,
)
from tiku_diagnostics.checkpoints import CheckpointDiagnosticService, digest
from tiku_diagnostics import checkpoint_retention as maintenance
from tiku_diagnostics.query import DiagnosticQueryService, QuerySpec
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent


class CheckpointDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.repository = self.base / "repository"
        self.runtime = self.repository / "runtime"
        self.runtime.mkdir(parents=True)
        self.backups = self.base / "backups"
        self.clock = fixture.MutableClock()
        self.policy = EvidenceCapacityPolicyV1(max_checkpoint_rows=100, max_artifact_rows=100,
            max_audit_rows=500, max_trace_rows=100, max_artifact_bytes=1_000_000,
            min_free_bytes=1, max_artifacts_per_checkpoint=10)
        self.scope = CheckpointQueryScopeV1("invite_1", "1" * 64)
        self.service = CheckpointDiagnosticService(self.runtime, capacity=self.policy,
            actor_key="operator", scope=self.scope, clock=self.clock)
        self.store = self.service.store
        self.artifact = self.store.put_artifact(fixture.owner(), fixture.png_bytes(), retention_class="normal")
        self.checkpoint = self.store.put_checkpoint(fixture.checkpoint(artifacts=(
            ArtifactLinkV1(self.artifact.artifact_id, "source_page"),)))

    def plan(self, operation="delete", **kwargs):
        return self.service.plan(operation, checkpoint_id=self.checkpoint.checkpoint_id,
                                 reason_code="USER_REQUESTED", **kwargs)

    def apply(self, plan, **kwargs):
        arguments = dict(expected_hash=plan["plan_hash"], backup_root=self.backups,
                         repository_root=self.repository, max_backup_runs=5)
        arguments.update(kwargs)
        return self.service.apply(plan, **arguments)

    def audit_actions(self):
        with closing(sqlite3.connect(self.store.path)) as connection:
            return [row[0] for row in connection.execute("SELECT action FROM evidence_audit ORDER BY rowid")]

    def test_trace_lookup_survives_missing_trace_link_and_returns_audited_summaries(self):
        self.assertFalse(self.store.trace_db_path.exists())
        result = self.service.query(trace_id=self.checkpoint.trace_id)
        self.assertEqual(result["count"], 1)
        self.assertNotIn("result", result["checkpoints"][0])
        self.assertEqual(self.audit_actions(), ["view_checkpoint"])
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id).result, self.checkpoint.result)
        artifact = self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)
        self.assertEqual(artifact.content, fixture.png_bytes())
        self.assertIn("view_artifact", self.audit_actions())

    def test_audited_trace_discovery_supplies_session_scope_for_followup_reads(self):
        self.service.scope = CheckpointQueryScopeV1("invite_1")
        result = self.service.query(trace_id=self.checkpoint.trace_id)
        self.assertEqual(result["checkpoints"][0]["owner"]["session_key"], self.scope.session_key)
        with self.assertRaises(EvidenceValidationError):
            self.service.checkpoint(self.checkpoint.checkpoint_id)
        self.service.scope = self.scope
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id).checkpoint_id, self.checkpoint.checkpoint_id)

    def test_scope_and_pagination_do_not_expose_other_identity_or_revision(self):
        for index in range(3):
            self.clock.value += timedelta(seconds=1)
            self.store.put_checkpoint(fixture.checkpoint())
        foreign = self.store.put_checkpoint(fixture.checkpoint(checkpoint_owner=fixture.owner(identity="other")))
        first = self.service.query(trace_id=self.checkpoint.trace_id, limit=2)
        second = self.service.query(trace_id=self.checkpoint.trace_id, limit=2,
                                    after_checkpoint_id=first["next_checkpoint_id"])
        self.assertTrue(first["truncated"])
        self.assertFalse(second["truncated"])
        ids = [row["checkpoint_id"] for row in first["checkpoints"] + second["checkpoints"]]
        self.assertEqual(len(set(ids)), 4)
        self.assertNotIn(foreign.checkpoint_id, ids)
        with self.assertRaises(EvidenceNotFoundError):
            self.service.checkpoint(foreign.checkpoint_id)
        self.service.scope = CheckpointQueryScopeV1("invite_1", "1" * 64, "search_parent", 999)
        self.assertEqual(self.service.query()["count"], 0)

    def test_view_audit_failure_returns_no_data(self):
        with patch.object(self.store, "_insert_audit_locked", side_effect=EvidenceAuditError("private audit error")):
            with self.assertRaises(EvidenceAuditError):
                self.service.query(trace_id=self.checkpoint.trace_id)
            with self.assertRaises(EvidenceAuditError):
                self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)
        self.assertEqual(self.audit_actions(), [])

    def test_expiry_and_corrupt_records_fail_closed(self):
        self.clock.value += timedelta(days=4)
        with self.assertRaises(EvidenceExpiredError):
            self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)
        self.assertEqual(self.service.query(trace_id=self.checkpoint.trace_id)["count"], 1)
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("UPDATE checkpoints SET payload_json = '{}' WHERE checkpoint_id = ?", (self.checkpoint.checkpoint_id,))
            connection.commit()
        with self.assertRaises(EvidenceUnavailableError):
            self.service.query(trace_id=self.checkpoint.trace_id)

    def test_chain_rejects_cross_workflow_predecessor(self):
        foreign = self.store.put_checkpoint(fixture.checkpoint(checkpoint_owner=fixture.owner(workflow="other_workflow")))
        child = self.store.put_checkpoint(replace(fixture.checkpoint(), predecessor_checkpoint_id=foreign.checkpoint_id))
        result = self.service.chain(child.checkpoint_id)
        self.assertEqual(result["stop_reason"], "unavailable_predecessor")
        self.assertEqual(len(result["checkpoints"]), 1)

    def test_plan_does_not_mutate_evidence_and_delete_is_backed_up_and_audited(self):
        plan = self.plan()
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id).to_dict(), self.checkpoint.to_dict())
        self.assertNotIn("delete_evidence", self.audit_actions())
        result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        backup = self.backups / "checkpoint-management" / result["backup_id"]
        self.assertTrue((backup / "manifest.json").is_file())
        with closing(sqlite3.connect(backup / maintenance.CHECKPOINT_DATABASE)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
        self.assertEqual((backup / (self.artifact.artifact_id + ".bin")).read_bytes(), fixture.png_bytes())
        with self.assertRaises(EvidenceNotFoundError):
            self.service.checkpoint(self.checkpoint.checkpoint_id)
        self.assertIn("delete_evidence", self.audit_actions())

    def test_artifact_delete_tombstones_and_preserves_checkpoint(self):
        result = self.apply(self.plan(artifact_id=self.artifact.artifact_id))
        self.assertEqual(result["target_kind"], "artifact")
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id).checkpoint_id, self.checkpoint.checkpoint_id)
        with self.assertRaises(EvidenceUnavailableError):
            self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)

    def test_investigation_extension_promotes_lifecycle_without_rewriting_results(self):
        plan = self.plan("extend", new_expires_at=(fixture.NOW + timedelta(days=60)).isoformat(), retention_class="investigation")
        self.apply(plan)
        current = self.service.checkpoint(self.checkpoint.checkpoint_id)
        self.assertEqual(current.retention_class, "investigation")
        self.assertEqual(current.result, self.checkpoint.result)
        self.assertEqual(current.input_fingerprint, self.checkpoint.input_fingerprint)
        self.assertEqual(current.occurred_at, self.checkpoint.occurred_at)
        self.assertIn("extend_retention", self.audit_actions())

    def test_artifact_extension_keeps_bytes_and_dedup_integrity(self):
        plan = self.plan("extend", artifact_id=self.artifact.artifact_id,
            new_expires_at=(fixture.NOW + timedelta(days=60)).isoformat(), retention_class="investigation")
        self.apply(plan)
        self.clock.value += timedelta(days=4)
        current = self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)
        self.assertEqual(current.content, fixture.png_bytes())
        self.assertEqual(current.descriptor.retention_class, "investigation")
        self.assertEqual(current.descriptor.created_at, self.artifact.created_at)
        with self.assertRaises(EvidenceValidationError):
            self.plan("extend", artifact_id=self.artifact.artifact_id,
                new_expires_at=(fixture.NOW + timedelta(days=91)).isoformat(), retention_class="investigation")

    def test_wrong_hash_expiry_and_binding_prevent_backup_and_mutation(self):
        plan = self.plan()
        with self.assertRaises(EvidenceConflictError):
            self.apply(plan, expected_hash="0" * 64)
        self.clock.value += timedelta(minutes=16)
        with self.assertRaises(EvidenceConflictError):
            self.apply(plan)
        self.assertFalse(self.backups.exists())
        self.assertNotIn("delete_evidence", self.audit_actions())

    def test_drift_is_checked_inside_mutation_transaction(self):
        plan = self.plan()
        original_backup = self.service._backup
        def drift(*args):
            result = original_backup(*args)
            self.store.extend_retention(self.checkpoint.checkpoint_id, actor_key="operator",
                expected_owner=self.checkpoint.owner, new_expires_at=fixture.NOW + timedelta(days=45),
                new_retention_class="investigation", reason_code="OTHER_INVESTIGATION")
            return result
        with patch.object(self.service, "_backup", side_effect=drift):
            with self.assertRaises(EvidenceConflictError):
                self.apply(plan)
        self.assertNotIn("delete_evidence", self.audit_actions())

    def test_plan_expiring_during_backup_does_not_mutate(self):
        plan = self.plan()
        original_backup = self.service._backup
        def slow_backup(*args):
            result = original_backup(*args)
            self.clock.value += timedelta(minutes=15)
            return result
        with patch.object(self.service, "_backup", side_effect=slow_backup):
            with self.assertRaises(EvidenceConflictError):
                self.apply(plan)
        self.assertNotIn("delete_evidence", self.audit_actions())
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id), self.checkpoint)

    def test_plan_expiring_while_waiting_for_mutation_transaction_is_rejected(self):
        original_connection = self.store._write_connection
        @contextmanager
        def slow_connection():
            with original_connection() as connection:
                self.clock.value += timedelta(minutes=15)
                yield connection
        for operation in ("extend", "delete"):
            with self.subTest(operation=operation):
                self.clock.value = fixture.NOW
                arguments = dict(actor_key="operator", expected_owner=self.checkpoint.owner,
                    reason_code="INVESTIGATION_OPENED", operation_deadline=fixture.NOW + timedelta(minutes=15))
                if operation == "extend":
                    arguments.update(new_expires_at=fixture.NOW + timedelta(days=60),
                                     new_retention_class="investigation")
                method = self.store.extend_retention if operation == "extend" else self.store.delete_evidence
                with patch.object(self.store, "_write_connection", side_effect=slow_connection):
                    with self.assertRaises(EvidenceConflictError):
                        method(self.checkpoint.checkpoint_id, **arguments)
        self.assertEqual(self.audit_actions(), [])
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id), self.checkpoint)

    def test_artifact_unlink_failure_reports_committed_delete_and_pending_cleanup(self):
        plan = self.plan(artifact_id=self.artifact.artifact_id)
        original_unlink = Path.unlink
        def fail_blob_unlink(path, *args, **kwargs):
            if path.is_relative_to(self.store.artifact_root):
                raise OSError("image file is busy")
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", autospec=True, side_effect=fail_blob_unlink):
            result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["physical_cleanup_pending"])
        self.assertTrue(result["receipt_saved"])
        with self.assertRaises(EvidenceUnavailableError):
            self.service.artifact(self.checkpoint.checkpoint_id, self.artifact.artifact_id)
        self.assertEqual(self.audit_actions().count("delete_evidence"), 1)
        self.assertGreater(self.store._release_zero_ref_blob(self.artifact.sha256), 0)

    def test_backup_and_management_audit_failures_do_not_delete(self):
        plan = self.plan()
        with patch.object(maintenance, "_backup_sqlite", side_effect=OSError("private backup error")):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.clock.value += timedelta(seconds=1)
        plan = self.plan()
        insert = self.store._insert_audit_locked
        def reject_management(*args, **kwargs):
            if kwargs["action"] == "delete_evidence":
                raise EvidenceAuditError("private audit error")
            return insert(*args, **kwargs)
        with patch.object(self.store, "_insert_audit_locked", side_effect=reject_management):
            with self.assertRaises(EvidenceAuditError):
                self.apply(plan)
        self.assertEqual(self.service.checkpoint(self.checkpoint.checkpoint_id).checkpoint_id, self.checkpoint.checkpoint_id)

    def test_missing_store_query_does_not_create_database(self):
        empty = self.repository / "empty-runtime"
        empty.mkdir()
        service = CheckpointDiagnosticService(empty, capacity=self.policy, actor_key="operator", scope=self.scope)
        with self.assertRaises(EvidenceNotFoundError):
            service.query(trace_id=self.checkpoint.trace_id)
        self.assertFalse(service.store.path.exists())

    def test_readonly_trace_diagnostics_exposes_only_valid_checkpoint_reference(self):
        traces = SQLiteTraceEventStore(self.store.trace_db_path)
        traces.write(TraceEvent.create(trace_id=self.checkpoint.trace_id, event_type="stage_finished",
            stage="image_routed", outcome="success", session_key=self.scope.session_key,
            safe_attributes={"completed": True, "checkpoint_id": self.checkpoint.checkpoint_id}))
        package = DiagnosticQueryService(self.runtime).query(QuerySpec(trace_id=self.checkpoint.trace_id))
        rows = [item["record"] for item in package["timeline"] if item["source"] == "trace_events"]
        self.assertEqual(rows[0]["checkpoint_id"], self.checkpoint.checkpoint_id)
        self.assertNotIn("safe_attributes_json", rows[0])
        self.assertNotIn(self.scope.session_key, json.dumps(package))
        self.assertEqual(self.audit_actions(), [])

    def test_real_audit_capacity_rejects_query_atomically(self):
        self.store.put_checkpoint(fixture.checkpoint())
        self.store.capacity = replace(self.policy, max_audit_rows=2)
        with self.assertRaises(EvidenceAuditError):
            self.service.query(trace_id=self.checkpoint.trace_id)
        self.assertEqual(self.audit_actions(), [])

    def test_store_replacement_after_planning_is_rejected(self):
        plan = self.plan()
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("UPDATE checkpoint_store_meta SET value = ? WHERE key = 'store_id'", ("store_replaced",))
            connection.commit()
        with self.assertRaises(EvidenceConflictError):
            self.apply(plan)
        self.assertFalse(self.backups.exists())

    def test_backup_hash_and_capacity_failure_prevent_mutation(self):
        plan = self.plan()
        real_hash = maintenance._sha256_file
        def bad_hash(path):
            return "0" * 64 if Path(path).suffix == ".bin" else real_hash(path)
        with patch.object(maintenance, "_sha256_file", side_effect=bad_hash):
            with self.assertRaises(EvidenceUnavailableError):
                self.apply(plan)
        self.clock.value += timedelta(seconds=1)
        new_plan = self.plan()
        with self.assertRaises(EvidenceUnavailableError):
            self.apply(new_plan, max_backup_runs=1)
        self.assertNotIn("delete_evidence", self.audit_actions())

    def test_backup_inside_repository_is_rejected(self):
        plan = self.plan()
        with self.assertRaises(EvidenceValidationError):
            self.apply(plan, backup_root=self.repository / "backups")
        self.assertNotIn("delete_evidence", self.audit_actions())

    def test_feedback_maximum_and_extension_audit_failure(self):
        with self.assertRaises(EvidenceValidationError):
            self.plan("extend", new_expires_at=(fixture.NOW + timedelta(days=366)).isoformat(), retention_class="feedback")
        plan = self.plan("extend", new_expires_at=(fixture.NOW + timedelta(days=365)).isoformat(), retention_class="feedback")
        insert = self.store._insert_audit_locked
        def reject_extension(*args, **kwargs):
            if kwargs["action"] == "extend_retention":
                raise EvidenceAuditError("not writable")
            return insert(*args, **kwargs)
        with patch.object(self.store, "_insert_audit_locked", side_effect=reject_extension):
            with self.assertRaises(EvidenceAuditError):
                self.apply(plan)
        current = self.service.checkpoint(self.checkpoint.checkpoint_id)
        self.assertEqual(current.retention_class, "normal")
        self.assertEqual(current.expires_at, self.checkpoint.expires_at)

    def test_cli_outputs_only_safe_error_codes(self):
        args = ["--runtime-root", str(self.runtime), "--actor-key", "operator", "--identity-key", self.scope.identity_key,
                "--session-key", self.scope.session_key]
        for key, value in self.policy.to_dict().items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        with patch.object(cli, "CheckpointDiagnosticService", side_effect=OSError("private path and token")), redirect_stderr(StringIO()) as stderr:
            result = cli.main([*args, "query", "--trace-id", self.checkpoint.trace_id])
        self.assertEqual(result, 2)
        self.assertNotIn("private", stderr.getvalue())
        with patch.object(cli, "CheckpointDiagnosticService", return_value=self.service), redirect_stdout(StringIO()) as stdout:
            result = cli.main([*args, "show", "--checkpoint-id", self.checkpoint.checkpoint_id])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["checkpoint_id"], self.checkpoint.checkpoint_id)

    def test_cli_query_artifact_plan_and_apply_use_real_store(self):
        runtime = self.repository / "cli-runtime"
        runtime.mkdir()
        live = CheckpointDiagnosticService(runtime, capacity=self.policy, actor_key="operator", scope=self.scope)
        artifact = live.store.put_artifact(fixture.owner(), fixture.png_bytes(), retention_class="normal")
        checkpoint = live.store.put_checkpoint(fixture.checkpoint(artifacts=(ArtifactLinkV1(artifact.artifact_id, "source_page"),)))
        args = ["--runtime-root", str(runtime), "--actor-key", "operator", "--identity-key", self.scope.identity_key,
                "--session-key", self.scope.session_key]
        for key, value in self.policy.to_dict().items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main([*args, "query", "--trace-id", checkpoint.trace_id]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["count"], 1)
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main([*args, "artifact", "--checkpoint-id", checkpoint.checkpoint_id,
                "--artifact-id", artifact.artifact_id, "--include-content"]), 0)
        self.assertEqual(base64.b64decode(json.loads(stdout.getvalue())["content_base64"]), fixture.png_bytes())
        plan_path = self.base / "plan.json"
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main([*args, "plan-extend", "--checkpoint-id", checkpoint.checkpoint_id,
                "--new-expires-at", (utc_now() + timedelta(days=60)).isoformat(), "--retention-class", "investigation",
                "--reason-code", "INVESTIGATION_OPENED", "--plan-out", str(plan_path)]), 0)
        plan = json.loads(stdout.getvalue())
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(cli.main([*args, "apply", "--plan-file", str(plan_path),
                "--confirm-plan-hash", plan["plan_hash"], "--backup-root", str(self.backups),
                "--max-backup-runs", "5"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "applied")
        self.assertEqual(live.checkpoint(checkpoint.checkpoint_id).retention_class, "investigation")

    def test_receipt_failure_reports_the_committed_action_without_replaying(self):
        plan = self.plan()
        write = maintenance._write_json_exclusive
        def fail_receipt(path, value):
            if path.name == "result.json":
                raise OSError("receipt write failed")
            return write(path, value)
        with patch.object(maintenance, "_write_json_exclusive", side_effect=fail_receipt):
            result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["receipt_saved"])
        with self.assertRaises(EvidenceNotFoundError):
            self.apply(plan)
        self.assertEqual(self.audit_actions().count("delete_evidence"), 1)


if __name__ == "__main__":
    unittest.main()

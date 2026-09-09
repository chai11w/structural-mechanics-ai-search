import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tiku_agent.execution_retention import plan_cleanup, apply_cleanup, source_digest
from tiku_agent.execution_maintenance import read_execution
from tiku_agent.execution_store import ExecutionError
from tests import test_execution_handoffs as handoff_fixture


class ExecutionRetentionTests(unittest.TestCase):
    def setUp(self):
        self.f = handoff_fixture.ExecutionHandoffTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.backups = tempfile.TemporaryDirectory()
        self.addCleanup(self.backups.cleanup)
        self.backup = Path(self.backups.name) / "reviewed"
        self.f.prepare()
        self.f.crop()
        self.future = self.f.store.now() + 32*86400
        self.f.store.now = lambda:self.future

    def plan(self, **kwargs):
        return plan_cleanup(self.f.store.path, now=self.future, **kwargs)

    def apply(self, plan, **kwargs):
        return apply_cleanup(self.f.a3.execution_operations, plan, backup_dir=self.backup, **kwargs)

    def test_expired_files_records_and_session_are_removed_only_after_verified_backup(self):
        plan = self.plan(compact=True)
        self.assertTrue(plan["files"])
        self.assertTrue(plan["operations"])
        self.assertTrue(plan["sessions"])
        before = source_digest(read := self._connection())
        read.close()
        result = self.apply(plan)
        self.assertEqual(result["operations_removed"], len(plan["operations"]))
        self.assertEqual(result["compaction"], "COMPLETE")
        with read_execution(self.backup / "execution.sqlite3") as conn:
            self.assertEqual(source_digest(conn), before)
        self.assertEqual(self.f.rows("execution_operations"), [])
        self.assertEqual(self.f.rows("execution_tasks"), [])
        self.assertEqual(self.f.rows("execution_sessions"), [])
        self.assertEqual(len(list((self.backup / "files").glob("*.bin"))), len(plan["files"]))
        self.assertTrue(self.f.image.is_file())  # original fixture outside artifact roots

    def _connection(self):
        import sqlite3
        conn = sqlite3.connect(self.f.store.path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_new_state_after_review_rejects_cleanup_before_backup(self):
        plan = self.plan()
        self.f.store.context("new-session")
        with self.assertRaises(ExecutionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_PLAN_CHANGED")
        self.assertFalse(self.backup.exists())

    def test_changed_file_after_review_is_not_deleted(self):
        plan = self.plan()
        target = Path(self.f.rows("execution_files")[0]["path"])
        target.write_bytes(b"new owner content")
        with self.assertRaises(ExecutionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_PLAN_CHANGED")
        self.assertEqual(target.read_bytes(), b"new owner content")

    def test_backup_failure_deletes_nothing(self):
        plan = self.plan()
        paths = [Path(row["path"]) for row in self.f.rows("execution_files")]
        with patch("tiku_agent.execution_retention.shutil.copy2", side_effect=OSError("backup unavailable")):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertTrue(self.f.rows("execution_operations"))

    def test_limit_is_bounded_and_unapproved_root_is_rejected(self):
        plan = self.plan(limit=1)
        self.assertLessEqual(len(plan["files"]), 1)
        self.assertLessEqual(len(plan["operations"]), 1)
        outside = Path(self.backups.name)
        with self.assertRaises(ExecutionError) as caught:
            self.plan(roots=[outside])
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_ROOT_INVALID")

    def test_unknown_operation_preserves_its_expired_parent_input_and_child_receipt(self):
        with self.f.store.transaction() as conn:
            conn.execute("UPDATE execution_operations SET status='UNKNOWN' WHERE id=(SELECT operation_id FROM execution_handoffs LIMIT 1)")
        plan = self.plan()
        protected = {Path(row["path"]).name for row in self.f.rows("execution_files")}
        selected = {Path(item["path"]).name for item in plan["files"]}
        self.assertFalse(protected & selected)
        self.assertEqual(plan["sessions"], [])

    def test_expired_running_operation_is_classified_unknown_without_deleting_its_files(self):
        with self.f.store.transaction() as conn:
            operation = conn.execute("SELECT operation_id FROM execution_handoffs LIMIT 1").fetchone()[0]
            conn.execute("UPDATE execution_operations SET status='RUNNING' WHERE id=?", (operation,))
        plan = self.plan()
        self.assertIn(operation, plan["mark_unknown"])
        paths = [Path(row["path"]) for row in self.f.rows("execution_files")]
        self.apply(plan)
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(next(row["status"] for row in self.f.rows("execution_operations") if row["id"] == operation), "UNKNOWN")

    def test_pending_cost_blocks_retirement_of_operation_and_its_artifact(self):
        from tests import test_execution_effects as effects_fixture
        from uuid import uuid4
        fixture = effects_fixture.ExecutionEffectsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.object(fixture.ledger, "write_run", side_effect=OSError("ledger unavailable")):
            fixture.runtime.handle_text("s", "success", operation_request=fixture.request())
        effect = fixture.rows("execution_effects")[0]
        artifact = fixture.runtime.artifacts.root / "retained.jpg"
        artifact.write_bytes(b"must remain")
        with fixture.store.transaction() as conn:
            conn.execute("INSERT INTO execution_files VALUES (?,?,?,?,?,?,?,?)",
                         (uuid4().hex, effect["operation_id"], effect["attempt_id"], str(artifact), str(artifact)+".part",
                          "digest", "PUBLISHED", fixture.store.now()))
        future = fixture.store.now() + 32*86400
        plan = plan_cleanup(fixture.store.path, now=future)
        self.assertNotIn(effect["operation_id"], plan["operations"])
        self.assertFalse(any(item["path"] == "retained.jpg" for item in plan["files"]))
        self.assertEqual(plan["sessions"], [])

    def test_untracked_old_orphan_is_removed_but_unselected_input_is_not(self):
        orphan = self.f.a3.artifacts.root / "orphan.jpg"
        orphan.write_bytes(b"orphan")
        metadata = self.f.a3.artifacts.root / "unrelated.sqlite3"
        metadata.write_bytes(b"unrelated database")
        plan = self.plan()
        self.assertTrue(any(item["path"] == "orphan.jpg" for item in plan["files"]))
        self.apply(plan)
        self.assertFalse(orphan.exists())
        self.assertTrue(self.f.image.exists())
        self.assertEqual(metadata.read_bytes(), b"unrelated database")

    def test_deleted_old_operation_key_cannot_start_again(self):
        from tiku_agent.execution_operations import OperationRequest
        op = self.f.rows("execution_operations")[0]
        request = OperationRequest(op["op_key"], op["epoch"], op["expected_version"])
        self.apply(self.plan())
        self.f.store.context("s")
        with self.assertRaises(ExecutionError) as caught:
            self.f.a3.execution_operations.register("s", "local", request, op["kind"], {})
        self.assertEqual(caught.exception.code, "EXECUTION_STALE")

    def test_live_execution_blocks_apply_before_backup_or_deletion(self):
        from tiku_agent.execution_operations import OperationRequest
        from uuid import uuid4
        context = self.f.store.context("active")
        operations = self.f.a3.execution_operations
        request = OperationRequest(uuid4().hex, context["epoch"], context["state_version"])
        operation = operations.register("active", "local", request, "handle_text", {})
        operations.claim(operation["id"])
        with self.assertRaises(ExecutionError) as caught:
            self.apply(self.plan())
        self.assertEqual(caught.exception.code, "EXECUTION_BUSY")
        self.assertFalse(self.backup.exists())


if __name__ == "__main__":
    unittest.main()

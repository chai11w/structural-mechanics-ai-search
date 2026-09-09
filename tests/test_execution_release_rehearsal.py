"""Offline release rehearsal through the real CLIs, with a counted fake provider."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_agent.a3_runtime import A3SessionState, SQLiteA3SessionStore
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_migration import plan_migration
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionPolicy, ExecutionSessionStore, ExecutionStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentProtocolError, AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call


REPOSITORY = Path(__file__).resolve().parents[1]


class ExecutionReleaseRehearsalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clone = self.root / "offline"
        self.clone.mkdir()
        self.image = self.clone / "page.jpg"
        self.image.write_bytes(b"offline legacy image")
        self.child_db = self.clone / "child.sqlite3"
        self.parent_db = self.clone / "parent.sqlite3"
        self.target = self.clone / "execution.sqlite3"
        self.child = AgentState(session_id="s")
        self.child.start_search(str(self.image), search_id="legacy-child")
        self.parent = A3SessionState(session_id="s", entry_route="A3", phase="A2_ACTIVE",
            source_page_path=str(self.image), workflow_search_id="legacy-page",
            current_search_id="legacy-page", task_revision=1, selected_unit_id="u1",
            units=[{"unit_id":"u1", "page_index":1}],
            crop_drafts={"u1":{"path":str(self.image), "bounds":{"x":0,"y":0,"width":1,"height":1}}})
        SQLiteSessionStore(self.child_db).save(self.child)
        SQLiteA3SessionStore(self.parent_db).save(self.parent)
        # Compare canonical legacy reads: A3 normalizes its unit dictionaries.
        self.parent = SQLiteA3SessionStore(self.parent_db).load("s")
        self.source_plan = plan_migration(self.child_db, self.parent_db)
        self.calls = []

    def cli(self, script, *arguments, success=True):
        result = subprocess.run([sys.executable, str(REPOSITORY / "scripts" / script),
                                 *map(str, arguments)], cwd=REPOSITORY, capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr[-2000:] + result.stdout[-2000:])
        else:
            self.assertNotEqual(result.returncode, 0)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def migrate(self):
        arguments = ("--child-db", self.child_db, "--workflow-db", self.parent_db)
        plan = self.cli("tiku_execution_migrate.py", "plan", *arguments)
        self.assertEqual(plan, self.source_plan)
        reviewed = self.root / "migration-plan.json"
        reviewed.write_text(json.dumps(plan), encoding="utf-8")
        result = self.cli("tiku_execution_migrate.py", "apply", *arguments,
            "--destination", self.target, "--offline-clone-root", self.clone,
            "--expected-plan", reviewed, "--backup-dir", self.root / "migration-backup")
        self.assertEqual(result["imported"], {"child":1, "workflow":1})
        self.assertEqual(plan_migration(self.child_db, self.parent_db), self.source_plan)
        self.assertEqual(plan_migration(self.root / "migration-backup/child.sqlite3",
                                       self.root / "migration-backup/workflow.sqlite3"), self.source_plan)

    def runtime(self, database=None):
        owner = self
        class Agent:
            config = None
            def __init__(self, state):
                self.state = state
            def handle_text(self, text):
                def provider():
                    owner.calls.append(text)
                    return {"input_tokens":100, "output_tokens":20}
                timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                                 call_type="release-rehearsal", usage_getter=lambda value:value)
                return AgentResponse(text="offline receipt", state=self.state.to_dict(), intent="greeting")
        # A one-second fixture retention period lets the unmodified CLI enforce
        # both mtime and ctime without changing the host clock or file metadata.
        authority = ExecutionStore(database or self.target, policy=ExecutionPolicy(history_ttl=1))
        runtime = AgentSessionRuntime(ExecutionSessionStore(authority),
            artifacts=SessionArtifacts(self.clone / "media"), agent_factory=Agent,
            cost_ledger=SQLiteModelCostLedger(self.clone / "costs.sqlite3"))
        attach_execution(runtime, authority, configuration_version="release-rehearsal-v1")
        return runtime, authority

    def check_database(self, path):
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_legacy_stores_migrate_with_exact_lineage_and_can_roll_back_before_new_writes(self):
        self.migrate()
        authority = ExecutionStore(self.target)
        self.assertEqual(ExecutionSessionStore(authority).load("s").to_dict(), self.child.to_dict())
        self.assertEqual(ExecutionSessionStore(authority, "workflow").load("s").to_dict(), self.parent.to_dict())
        tasks = authority.tasks("s")
        parent = next(row for row in tasks if row["kind"] == "workflow")
        child = next(row for row in tasks if row["kind"] == "child")
        self.assertEqual((child["parent_id"], child["unit_id"]), (parent["id"], "u1"))
        self.assertEqual({row["origin"] for row in tasks}, {"legacy_snapshot"})
        runtime, authority = self.runtime()
        with authority.transaction() as conn:
            for table in ("execution_operations", "execution_attempts", "execution_effects"):
                self.assertEqual(conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        with self.assertRaises(AgentProtocolError):
            runtime.handle_text("s", "missing operation key")
        self.assertEqual(self.calls, [])
        # A pre-enable rollback reads the separately verified legacy backups.
        rollback_child = SQLiteSessionStore(self.root / "migration-backup/child.sqlite3").load("s")
        rollback_parent = SQLiteA3SessionStore(self.root / "migration-backup/workflow.sqlite3").load("s")
        self.assertEqual(rollback_child.to_dict(), self.child.to_dict())
        self.assertEqual(rollback_parent.to_dict(), self.parent.to_dict())
        self.assertEqual(Path(rollback_child.current_image_path).read_bytes(), self.image.read_bytes())
        self.check_database(self.target)

    def test_after_new_write_cost_reconciliation_cleanup_and_snapshot_restore_preserve_receipts(self):
        self.migrate()
        runtime, authority = self.runtime()
        context = authority.context("s")
        request = OperationRequest(uuid4().hex, context["epoch"], context["state_version"])
        with patch.object(runtime.cost_ledger, "write_run", side_effect=OSError("offline injected ledger failure")):
            response = runtime.handle_text("s", "new operation", operation_request=request)
        inspect = self.cli("tiku_execution_maintain.py", "inspect", "--database", self.target)
        self.assertEqual(inspect["pending_cost_runs"], 1)
        plan_file = self.root / "cost-plan.json"
        plan = self.cli("tiku_execution_maintain.py", "cost-plan", "--database", self.target,
                        "--ledger", runtime.cost_ledger.path, "--plan-out", plan_file)
        self.assertEqual(plan["items"][0]["reason"], "READY")
        result = self.cli("tiku_execution_maintain.py", "cost-apply", "--database", self.target,
            "--ledger", runtime.cost_ledger.path, "--plan", plan_file,
            "--confirm-plan-hash", plan["plan_hash"], "--backup-dir", self.root / "cost-backup")
        self.assertEqual(result["confirmed_runs"], 1)
        inspect = self.cli("tiku_execution_maintain.py", "inspect", "--database", self.target)
        self.assertEqual(inspect["pending_cost_runs"], 0)
        self.assertEqual(self.calls, ["new operation"])
        with authority.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_operations WHERE status IN ('REGISTERED','RUNNING','UNKNOWN')").fetchone()[0], 0)
        # Allow this fixture file to age past the fixture's retention period.
        orphan = runtime.artifacts.root / "orphan.jpg"
        orphan.write_bytes(b"unreferenced offline artifact")
        time.sleep(1.1)
        cleanup_file = self.root / "cleanup-plan.json"
        cleanup = self.cli("tiku_execution_maintain.py", "cleanup-plan", "--database", self.target,
                           "--plan-out", cleanup_file)
        self.assertEqual(len(cleanup["files"]), 1)
        self.assertEqual(cleanup["operations"], [])
        result = self.cli("tiku_execution_maintain.py", "cleanup-apply", "--database", self.target,
            "--plan", cleanup_file, "--confirm-plan-hash", cleanup["plan_hash"],
            "--backup-dir", self.root / "cleanup-backup")
        self.assertEqual(result["files_removed"], 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(self.image.is_file())
        # Restore a complete, settled snapshot to a separate execution path.
        restored_db = self.clone / "restored.sqlite3"
        with closing(sqlite3.connect(self.root / "cleanup-backup/execution.sqlite3")) as source:
            with closing(sqlite3.connect(restored_db)) as destination:
                source.backup(destination)
        shutil.copy2(self.root / "cleanup-backup/files/0000.bin", orphan)
        self.assertEqual(orphan.read_bytes(), b"unreferenced offline artifact")
        restored, restored_authority = self.runtime(restored_db)
        replay = restored.handle_text("s", "new operation", operation_request=request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(replay.execution_receipt["operation_id"], response.execution_receipt["operation_id"])
        self.assertEqual(self.calls, ["new operation"])
        with closing(sqlite3.connect(runtime.cost_ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1,120))
        self.assertEqual(plan_migration(self.child_db, self.parent_db), self.source_plan)
        self.assertEqual({row["origin"] for row in restored_authority.tasks("s")}, {"legacy_snapshot"})
        for database in (self.target, restored_db, runtime.cost_ledger.path,
                         self.root / "cost-backup/execution.sqlite3", self.root / "cost-backup/costs.sqlite3"):
            self.check_database(database)


if __name__ == "__main__":
    unittest.main()

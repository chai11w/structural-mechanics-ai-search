from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tiku_agent.execution_migration import migrate_offline, plan_migration
from tiku_agent.execution_store import ExecutionStore
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_shared.atomic_files import atomic_output


class ExecutionMigrationPublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.image = self.root / "image.jpg"
        self.image.write_bytes(b"isolated source image")
        self.source = self.root / "legacy.db"
        state = AgentState(session_id="s")
        state.start_search(str(self.image), search_id="legacy-search")
        SQLiteSessionStore(self.source).save(state)
        self.target = self.root / "execution.db"

    def migrate(self):
        return migrate_offline(self.source, None, destination=self.target, artifact_root=self.root,
                               expected_plan=plan_migration(self.source), backup_dir=self.root / "backup")

    def test_invalid_legacy_state_does_not_publish_an_empty_execution_database(self):
        with closing(sqlite3.connect(self.source)) as conn, conn:
            state = json.loads(conn.execute("SELECT state_json FROM agent_sessions").fetchone()[0])
            state["phase"] = "invalid-test-phase"
            conn.execute("UPDATE agent_sessions SET state_json=?", (json.dumps(state),))
        before = plan_migration(self.source)
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertFalse(self.target.exists())
        self.assertEqual(plan_migration(self.source), before)

    def test_interrupted_import_does_not_publish_a_partial_database(self):
        save = ExecutionStore.save
        def interrupt(authority, *args, **kwargs):
            save(authority, *args, **kwargs)
            raise RuntimeError("interrupted after imported state")
        with patch.object(ExecutionStore, "save", new=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interrupted after imported state"):
                self.migrate()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".part-*")), [])

    def test_migration_publishes_only_after_all_imported_state_is_valid(self):
        save = ExecutionStore.save
        def inspect(authority, *args, **kwargs):
            self.assertFalse(self.target.exists())
            return save(authority, *args, **kwargs)
        with patch.object(ExecutionStore, "save", new=inspect):
            result = self.migrate()
        self.assertEqual(result["imported"]["child"], 1)
        authority = ExecutionStore(self.target)
        self.assertEqual(authority.tasks("s")[0]["origin"], "legacy_snapshot")
        with authority.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_process_death_during_import_leaves_only_disabled_staging_database(self):
        script = """
import os, sys
from pathlib import Path
from tiku_agent.execution_migration import migrate_offline, plan_migration
from tiku_agent.execution_store import ExecutionStore
root = Path(sys.argv[1])
save = ExecutionStore.save
def interrupted(authority, *args, **kwargs):
    save(authority, *args, **kwargs)
    os._exit(91)
ExecutionStore.save = interrupted
migrate_offline(root / 'legacy.db', None, destination=root / 'execution.db',
    artifact_root=root, expected_plan=plan_migration(root / 'legacy.db'),
    backup_dir=root / 'backup')
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 91, result.stderr[-1000:])
        self.assertFalse(self.target.exists())
        staging = list(self.root.glob(".part-*"))
        self.assertEqual(len(staging), 1)
        from tiku_agent.execution_store import ExecutionError
        with self.assertRaises(ExecutionError) as caught:
            ExecutionStore(staging[0])
        self.assertEqual(caught.exception.code, "EXECUTION_MIGRATION_INCOMPLETE")
        self.assertEqual(plan_migration(self.source), plan_migration(self.root / "backup/child.sqlite3"))

    def test_atomic_publication_cannot_replace_a_target_created_during_write(self):
        target = self.root / "artifact.bin"
        with self.assertRaises(FileExistsError):
            with atomic_output(target) as temporary:
                temporary.write_bytes(b"first writer")
                target.write_bytes(b"another writer")
        self.assertEqual(target.read_bytes(), b"another writer")
        self.assertEqual(list(self.root.glob(".part-*")), [])


if __name__ == "__main__":
    unittest.main()

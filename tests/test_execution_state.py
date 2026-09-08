import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tiku_agent.a3_runtime import A3SessionState, SQLiteA3SessionStore
from tiku_agent.execution_migration import migrate_offline, plan_migration
from tiku_agent.execution_store import ExecutionError, ExecutionPolicy, ExecutionSessionStore, ExecutionStore, inherit_state_version
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState


class ExecutionStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.time = [1800000000.0]
        self.authority = ExecutionStore(self.root / "execution.db", now=lambda: self.time[0])
        self.child = ExecutionSessionStore(self.authority)
        self.parent = ExecutionSessionStore(self.authority, "workflow")
        self.image = self.root / "crop.jpg"
        self.image.write_bytes(b"isolated fake image")

    def assertCode(self, code, fn):
        with self.assertRaises(ExecutionError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)

    def new_child(self, sid="s"):
        child = AgentState(session_id=sid)
        child.start_search(str(self.image), search_id="search_child")
        return child

    def new_parent(self, sid="s"):
        return A3SessionState(session_id=sid, entry_route="A3", phase="A2_ACTIVE",
            source_page_path=str(self.image), workflow_search_id="search_parent", current_search_id="search_parent",
            task_revision=1, selected_unit_id="u1", units=[{"unit_id":"u1", "page_index":1}],
            crop_drafts={"u1":{"path":str(self.image),"bounds":{"x":0,"y":0,"width":1,"height":1}}})

    def test_independent_connections_reject_stale_write(self):
        self.child.save(self.new_child())
        second = ExecutionSessionStore(ExecutionStore(self.authority.path, now=lambda: self.time[0]))
        a, b = self.child.load("s"), second.load("s")
        a.set_pending_chapter("4力法")
        self.child.save(a)
        b.set_pending_chapter("5位移法")
        self.assertCode("EXECUTION_STALE", lambda: second.save(b))
        self.assertEqual(self.child.load("s").pending_chapter, "4力法")

    def test_revision_fields_do_not_change_when_storage_version_advances(self):
        child = self.new_child()
        self.child.save(child)
        old = child._execution_version.version
        child.set_pending_chapter("4力法")
        self.child.save(child)
        self.assertEqual(child.task_revision, 1)
        self.assertEqual(child.candidate_revision, 0)
        self.assertEqual(child._execution_version.version, old + 1)
        self.assertNotIn("_execution_version", child.to_dict())

    def test_reset_rejects_old_snapshot_even_if_task_id_revision_repeat(self):
        child = self.new_child()
        self.child.save(child)
        epoch = self.authority.context("s")["epoch"]
        new_epoch = self.authority.rotate("s", expected_epoch=epoch)
        self.assertNotEqual(new_epoch, epoch)
        self.child.load("s")
        self.child.save(self.new_child())
        self.assertCode("EXECUTION_STALE", lambda: self.child.save(child))

    def test_unbound_replacement_is_rejected(self):
        self.child.save(self.new_child())
        self.assertCode("EXECUTION_VERSION_REQUIRED", lambda: self.child.save(self.new_child()))

    def test_cleared_slot_cannot_be_resurrected_by_old_writer(self):
        child = self.new_child()
        self.child.save(child)
        self.child.clear("s")
        self.assertIsNone(self.child.load("s"))
        self.assertCode("EXECUTION_STALE", lambda: self.child.save(child))
        self.child.save(self.new_child())

    def test_clone_preserves_cas_binding(self):
        child = self.new_child()
        self.child.save(child)
        clone = inherit_state_version(child, AgentState.from_dict(child.to_dict()))
        self.child.save(child)
        self.assertCode("EXECUTION_STALE", lambda: self.child.save(clone))

    def test_parent_child_record_survives_restart_and_retains_retry_history(self):
        self.parent.save(self.new_parent())
        child = self.new_child()
        self.child.save(child)
        child.start_search(str(self.image))
        self.child.save(child)
        store = ExecutionStore(self.authority.path, now=lambda: self.time[0])
        tasks = store.tasks("s")
        parent = next(t for t in tasks if t["kind"] == "workflow")
        children = [t for t in tasks if t["kind"] == "child"]
        self.assertEqual(len(children), 2)
        self.assertEqual({t["task_revision"] for t in children}, {1, 2})
        self.assertTrue(all(t["parent_id"] == parent["id"] and t["unit_id"] == "u1" for t in children))

    def test_child_cannot_bind_another_units_image(self):
        self.parent.save(self.new_parent())
        other = self.root / "other.jpg"
        other.write_bytes(b"different question")
        child = self.new_child()
        child.current_image_path = str(other)
        self.assertCode("EXECUTION_PARENT_INPUT_MISMATCH", lambda: self.child.save(child))
        self.assertIsNone(self.child.load("s"))

    def test_existing_child_cannot_be_reparented(self):
        parent = self.new_parent()
        self.parent.save(parent)
        child = self.new_child()
        self.child.save(child)
        parent.units.append({"unit_id":"u2", "page_index":2})
        parent.selected_unit_id = "u2"
        parent.crop_drafts["u2"] = parent.crop_drafts["u1"]
        self.parent.save(parent)
        self.assertCode("EXECUTION_PARENT_CHANGED", lambda: self.child.save(child))

    def test_expiration_rotates_epoch_and_clock_rollback_fails_closed(self):
        self.child.save(self.new_child())
        old = self.authority.context("s")["epoch"]
        self.time[0] += 7201
        self.assertIsNone(self.child.load("s"))
        self.assertNotEqual(self.authority.context("s")["epoch"], old)
        self.time[0] -= 600
        self.assertCode("EXECUTION_CLOCK_ROLLBACK", lambda: self.authority.context("s"))

    def test_capacity_refuses_new_task_without_overwriting_existing(self):
        authority = ExecutionStore(self.root / "small.db", now=lambda:self.time[0], policy=ExecutionPolicy(max_tasks=1))
        store = ExecutionSessionStore(authority)
        child = self.new_child()
        store.save(child)
        child.start_search(str(self.image))
        self.assertCode("EXECUTION_CAPACITY", lambda: store.save(child))
        self.assertEqual(store.load("s").task_revision, 1)

    def test_offline_migration_preserves_sources_and_marks_legacy_without_attempts(self):
        legacy_path = self.root / "legacy.db"
        legacy = SQLiteSessionStore(legacy_path)
        legacy.save(self.new_child())
        plan = plan_migration(legacy_path)
        result = migrate_offline(legacy_path, None, destination=self.root/"migrated.db",
            artifact_root=self.root, expected_plan=plan, backup_dir=self.root/"backup")
        self.assertEqual(result["imported"]["child"], 1)
        self.assertEqual(plan_migration(legacy_path), plan)
        restored = SQLiteSessionStore(self.root/"backup"/"child.sqlite3").load("s")
        self.assertEqual(restored.current_search_id, "search_child")
        target = ExecutionStore(self.root/"migrated.db")
        self.assertEqual(target.tasks("s")[0]["origin"], "legacy_snapshot")
        self.assertEqual(ExecutionSessionStore(target).load("s").to_dict(), legacy.load("s").to_dict())

    def test_migration_rejects_changed_plan_and_live_artifact_reference(self):
        legacy_path = self.root / "legacy.db"
        legacy = SQLiteSessionStore(legacy_path)
        legacy.save(self.new_child())
        plan = plan_migration(legacy_path)
        state = legacy.load("s")
        state.set_pending_chapter("4力法")
        legacy.save(state)
        kwargs = dict(destination=self.root/"migrated.db", artifact_root=self.root,
                      expected_plan=plan, backup_dir=self.root/"backup")
        self.assertCode("EXECUTION_MIGRATION_PLAN_CHANGED", lambda: migrate_offline(legacy_path,None,**kwargs))
        kwargs.update(expected_plan=plan_migration(legacy_path), artifact_root=self.root/"other")
        self.assertCode("EXECUTION_MIGRATION_ARTIFACT_OUTSIDE_CLONE", lambda: migrate_offline(legacy_path,None,**kwargs))
        self.assertFalse((self.root/"migrated.db").exists())

    def test_incomplete_migration_cannot_be_opened(self):
        with self.authority.transaction() as conn:
            conn.execute("INSERT INTO execution_meta VALUES ('migration','incomplete')")
        self.assertCode("EXECUTION_MIGRATION_INCOMPLETE", lambda: ExecutionStore(self.authority.path))

    def test_existing_a2_and_a3_runtimes_use_unified_state_authority(self):
        from PIL import Image
        from tiku_agent.agent import TikuSearchAgent
        from tiku_agent.a3_runtime import A3MvpRuntime
        from tiku_agent.session_runtime import AgentSessionRuntime
        from tiku_agent.session_artifacts import SessionArtifacts
        from tiku_agent.tools import ToolResult
        from tests.test_tiku_agent_session_runtime import FakeTools
        from tests.test_a3_runtime import FakeObserver, FakeVerifier
        Image.new("RGB", (100,100), "white").save(self.image)
        tools = FakeTools().toolbox()
        tools.analyze_image = lambda *a,**kw: ToolResult(ok=True,data={"loads":[{"type":"集中","raw":"P"}],"chapter_hint":"4力法"})
        a2 = AgentSessionRuntime(self.child, artifacts=SessionArtifacts(self.root/"a2"),
            agent_factory=lambda state:TikuSearchAgent(state=state,tools=tools,use_llm_intent=False))
        self.assertEqual(a2.handle_image("single",self.image).state["phase"],"WAIT_CANDIDATE_CHOICE")
        a3 = A3MvpRuntime(store=self.parent,artifacts=SessionArtifacts(self.root/"a3"),
            a2_runtime=a2,page_observer=FakeObserver(),crop_verifier=FakeVerifier())
        a3.handle_image("multi",self.image)
        a3.select_unit("multi","g1-u1")
        a3.handle_crop("multi",{"x":0,"y":0,"width":1,"height":1},unit_id="g1-u1")
        children = [t for t in self.authority.tasks("multi") if t["kind"]=="child"]
        self.assertEqual(len(children),1)
        self.assertEqual(children[0]["unit_id"],"g1-u1")
        self.assertIsNotNone(children[0]["parent_id"])


if __name__ == "__main__":
    unittest.main()

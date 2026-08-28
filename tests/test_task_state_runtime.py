import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tiku_agent.a3_runtime import (
    A3MvpRuntime,
    A3SessionState,
    SQLiteA3SessionStore,
)
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import (
    AgentSessionRuntime,
    SessionResponseSnapshotError,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_state_builder import build_task_state_snapshot_v1
from tiku_agent.task_state_runtime import (
    TaskStateEntryCapabilities,
    build_standalone_a2_runtime_snapshot_v1,
)


SESSION_ID = "runtime_task_state_session"
WORKFLOW_ID = "search_workflow_runtime_1234"
CHILD_ID = "search_child_runtime_1234"


def _unit(unit_id: str = "g1-u1", page_index: int = 1) -> dict[str, object]:
    return {
        "unit_id": unit_id,
        "page_index": page_index,
        "display_label": f"四-{page_index}",
        "searchability": "searchable_candidate",
    }


def _workflow(
    *,
    route: str = "A3",
    phase: str = "WAIT_UNIT_SELECTION",
    **overrides,
) -> A3SessionState:
    values = {
        "session_id": SESSION_ID,
        "entry_route": "" if route == "PENDING" else route,
        "phase": phase,
        "source_page_path": "",
        "page_understanding": (
            {"page_disposition": "has_searchable_candidates"}
            if route == "A3"
            else {}
        ),
        "units": [],
        "task_revision": 7,
        "current_search_id": CHILD_ID,
        "workflow_search_id": WORKFLOW_ID,
    }
    values.update(overrides)
    return A3SessionState(**values)


def _child(*, phase: str = "WAIT_CHAPTER", **overrides) -> AgentState:
    values = {
        "session_id": SESSION_ID,
        "phase": phase,
        "current_image_path": "",
        "current_search_id": CHILD_ID,
        "task_revision": 3,
    }
    values.update(overrides)
    return AgentState(**values)


class TracingLock:
    def __init__(self, name: str, events: list[str], *, poison: bool = False):
        self.name = name
        self.events = events
        self.held = False
        self.poison = poison

    def __enter__(self):
        if self.poison:
            raise AssertionError(f"{self.name} lock must not be acquired")
        if self.held:
            raise AssertionError(f"{self.name} lock is not reentrant")
        self.events.append(f"{self.name}:acquire")
        self.held = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        if not self.held:
            raise AssertionError(f"{self.name} lock was not held")
        self.held = False
        self.events.append(f"{self.name}:release")
        return False

    def locked(self) -> bool:
        return self.held


class SingleReadStore:
    def __init__(
        self,
        result=None,
        *,
        error: Exception | None = None,
        name: str = "store",
        events: list[str] | None = None,
        required_locks: tuple[TracingLock, ...] = (),
        poison: bool = False,
    ):
        self.result = result
        self.error = error
        self.name = name
        self.events = events
        self.required_locks = required_locks
        self.poison = poison
        self.load_attempt_count = 0
        self.load_count = 0

    def purge_expired(self):
        return []

    def load(self, session_id: str):
        self.load_attempt_count += 1
        if self.poison:
            raise AssertionError(f"{self.name} store must not be read")
        self.load_count += 1
        if self.load_count > 1:
            raise AssertionError(f"{self.name} store was read more than once")
        if session_id != SESSION_ID:
            raise AssertionError("unexpected session id")
        if not all(lock.held for lock in self.required_locks):
            raise AssertionError(f"{self.name} store was read outside required locks")
        if self.events is not None:
            self.events.append(f"{self.name}:load")
        if self.error is not None:
            raise self.error
        return self.result


class TaskStateRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _a2_runtime(
        self,
        store: SingleReadStore,
        *,
        artifacts: SessionArtifacts | None = None,
        lock: TracingLock | None = None,
    ) -> AgentSessionRuntime:
        runtime = AgentSessionRuntime(
            store,
            artifacts=artifacts or SessionArtifacts(self.root / "a2-sessions"),
            task_logger=object(),
        )
        if lock is not None:
            runtime._session_locks = (lock,) * 64
        return runtime

    def _a3_runtime(
        self,
        workflow_store: SingleReadStore,
        child_store: SingleReadStore,
        *,
        a3_lock: TracingLock | None = None,
        a2_lock: TracingLock | None = None,
        workflow_artifacts: SessionArtifacts | None = None,
        child_artifacts: SessionArtifacts | None = None,
        image_triage_authority=None,
    ) -> A3MvpRuntime:
        a2 = self._a2_runtime(
            child_store,
            artifacts=child_artifacts,
            lock=a2_lock,
        )
        runtime = A3MvpRuntime(
            store=workflow_store,
            artifacts=(
                workflow_artifacts
                or SessionArtifacts(self.root / "a3-sessions")
            ),
            a2_runtime=a2,
            page_observer=object(),
            crop_verifier=object(),
            image_triage_authority=image_triage_authority,
        )
        if a3_lock is not None:
            runtime._locks = (a3_lock,) * 64
        return runtime

    def test_standalone_response_frozen_requires_an_exact_boolean(self):
        with self.assertRaisesRegex(ValueError, "response_frozen must be boolean"):
            build_standalone_a2_runtime_snapshot_v1(
                SESSION_ID,
                child_state=_child(phase="CANCELLED"),
                child_read_status="OK",
                child_artifacts=SessionArtifacts(self.root / "strict-boolean"),
                child_retry_supported=True,
                response_frozen="true",  # type: ignore[arg-type]
            )

    def test_a3_wrapper_projects_a1_direct_a2_and_a3_routes(self):
        cases = (
            ("A1", _workflow(route="A1", phase="COMPLETE"), None, "A1", False),
            (
                "direct-a2",
                _workflow(route="A2", phase="A2_ACTIVE"),
                _child(),
                "A2",
                True,
            ),
            (
                "a3",
                _workflow(route="A3", units=[_unit()]),
                None,
                "A3",
                False,
            ),
            (
                "a3-active",
                _workflow(
                    route="A3",
                    phase="A2_ACTIVE",
                    units=[_unit()],
                    selected_unit_id="g1-u1",
                ),
                _child(),
                "A3",
                True,
            ),
        )
        for label, parent, child, expected_route, has_child in cases:
            with self.subTest(label=label):
                parent_store = SingleReadStore(parent)
                child_store = SingleReadStore(child)
                snapshot = self._a3_runtime(parent_store, child_store).task_state_snapshot_v1(
                    SESSION_ID
                )
                self.assertEqual(snapshot.consistency.status, "OK")
                self.assertEqual(snapshot.workflow.route, expected_route)
                self.assertEqual(snapshot.active_child_task is not None, has_child)
                self.assertEqual(parent_store.load_count, 1)
                self.assertEqual(child_store.load_count, 1)

    def test_real_sqlite_runtimes_generate_authoritative_snapshot(self):
        parent_store = SQLiteA3SessionStore(self.root / "a3-state.sqlite3")
        child_store = SQLiteSessionStore(self.root / "a2-state.sqlite3")
        parent_store.save(_workflow(route="A2", phase="A2_ACTIVE"))
        child_store.save(_child())
        runtime = self._a3_runtime(parent_store, child_store)

        snapshot = runtime.task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.status, "OK")
        self.assertEqual(snapshot.workflow.route, "A2")
        self.assertEqual(snapshot.active_child_task.task_id, CHILD_ID)

    def test_a3_session_capture_cleans_expired_child_then_expired_parent(self):
        current = [datetime(2026, 8, 28, tzinfo=UTC)]
        workflow_artifacts = SessionArtifacts(self.root / "ttl-parent-sessions")
        child_artifacts = SessionArtifacts(self.root / "ttl-child-sessions")
        parent_upload_dir = workflow_artifacts.session_dir(SESSION_ID) / "uploads"
        parent_upload_dir.mkdir(parents=True)
        parent_image = parent_upload_dir / "page.png"
        parent_image.write_bytes(b"page")
        child_upload_dir = child_artifacts.session_dir(SESSION_ID) / "uploads"
        child_upload_dir.mkdir(parents=True)
        child_image = child_upload_dir / "question.png"
        child_image.write_bytes(b"question")
        parent_store = SQLiteA3SessionStore(
            self.root / "ttl-parent.sqlite3",
            ttl=timedelta(seconds=10),
            now=lambda: current[0],
        )
        child_store = SQLiteSessionStore(
            self.root / "ttl-child.sqlite3",
            ttl=timedelta(seconds=1),
            now=lambda: current[0],
        )
        parent_store.save(_workflow(
            route="A2",
            phase="A2_ACTIVE",
            source_page_path=str(parent_image),
        ))
        child_store.save(_child(current_image_path=str(child_image)))
        runtime = self._a3_runtime(
            parent_store,
            child_store,
            workflow_artifacts=workflow_artifacts,
            child_artifacts=child_artifacts,
        )

        current[0] += timedelta(seconds=2)
        child_expired = runtime.session_response_snapshot_v1(SESSION_ID)

        self.assertEqual(
            child_expired.task_state.consistency.codes,
            ("ACTIVE_CHILD_TASK_MISSING",),
        )
        self.assertTrue(workflow_artifacts.session_dir(SESSION_ID).exists())
        self.assertFalse(child_artifacts.session_dir(SESSION_ID).exists())

        current[0] += timedelta(seconds=10)
        parent_expired = runtime.session_response_snapshot_v1(SESSION_ID)

        self.assertFalse(parent_expired.task_state.workflow.exists)
        self.assertIsNone(parent_expired.task_state.active_child_task)
        self.assertEqual(parent_expired.task_state.consistency.status, "OK")
        self.assertFalse(workflow_artifacts.session_dir(SESSION_ID).exists())

    def test_session_response_capture_uses_one_locked_a2_read_set(self):
        events: list[str] = []
        lock = TracingLock("a2", events)
        artifacts = SessionArtifacts(self.root / "a2-session-response")
        upload_dir = artifacts.session_dir(SESSION_ID) / "uploads"
        upload_dir.mkdir(parents=True)
        image = upload_dir / "question.png"
        image.write_bytes(b"question")
        state = _child(current_image_path=str(image))
        store = SingleReadStore(
            state,
            name="child",
            events=events,
            required_locks=(lock,),
        )
        runtime = self._a2_runtime(store, artifacts=artifacts, lock=lock)

        captured = runtime.session_response_snapshot_v1(
            SESSION_ID,
            capabilities=TaskStateEntryCapabilities(
                reset_session_available=True,
            ),
        )

        self.assertEqual(
            events,
            ["a2:acquire", "child:load", "a2:release"],
        )
        self.assertEqual(store.load_count, 1)
        self.assertEqual(captured.uploaded_image_path, image)
        self.assertEqual(
            captured.legacy_session,
            runtime._legacy_session_snapshot_from_state(state),
        )
        self.assertEqual(captured.task_state.consistency.status, "OK")
        self.assertEqual(captured.task_state.active_child_task.task_id, CHILD_ID)
        self.assertNotIn(
            "upload_image",
            captured.task_state.active_child_task.allowed_actions,
        )

    def test_session_response_capture_uses_one_ordered_a3_a2_read_set(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        workflow_artifacts = SessionArtifacts(self.root / "a3-session-response")
        upload_dir = workflow_artifacts.session_dir(SESSION_ID) / "uploads"
        upload_dir.mkdir(parents=True)
        image = upload_dir / "page.png"
        image.write_bytes(b"page")
        parent = _workflow(
            route="A3",
            phase="A2_ACTIVE",
            source_page_path=str(image),
            units=[_unit()],
            selected_unit_id="g1-u1",
        )
        child = _child()
        parent_store = SingleReadStore(
            parent,
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            child,
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        runtime = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
            workflow_artifacts=workflow_artifacts,
        )

        captured = runtime.session_response_snapshot_v1(
            SESSION_ID,
            capabilities=TaskStateEntryCapabilities(
                reset_session_available=True,
            ),
        )

        self.assertEqual(
            events,
            [
                "a3:acquire",
                "a2:acquire",
                "parent:load",
                "child:load",
                "a2:release",
                "a3:release",
            ],
        )
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertEqual(captured.uploaded_image_path, image)
        self.assertEqual(captured.legacy_session["phase"], child.phase)
        self.assertEqual(captured.legacy_session["image_route"], "A3")
        self.assertEqual(captured.task_state.workflow.route, "A3")
        self.assertIn(
            "reset_session",
            captured.task_state.workflow.allowed_actions,
        )
        self.assertNotIn(
            "upload_image",
            captured.task_state.workflow.allowed_actions,
        )
        self.assertEqual(captured.task_state.current_unit.unit_id, "g1-u1")
        self.assertEqual(captured.task_state.active_child_task.task_id, CHILD_ID)

    def test_session_response_capture_keeps_missing_and_orphan_distinct(self):
        missing = self._a2_runtime(
            SingleReadStore(None)
        ).session_response_snapshot_v1(SESSION_ID)
        self.assertFalse(missing.legacy_session["session_valid"])
        self.assertFalse(missing.task_state.workflow.exists)
        self.assertIsNone(missing.task_state.active_child_task)

        orphan = self._a3_runtime(
            SingleReadStore(None),
            SingleReadStore(_child()),
        ).session_response_snapshot_v1(SESSION_ID)
        self.assertFalse(orphan.legacy_session["session_valid"])
        self.assertEqual(
            orphan.task_state.consistency.codes,
            ("ORPHAN_CHILD_TASK",),
        )

    def test_session_response_capture_does_not_publish_legacy_empty_on_bad_state(self):
        events: list[str] = []
        lock = TracingLock("a2", events)
        store = SingleReadStore(
            error=ValueError("secret broken session JSON"),
            name="child",
            events=events,
            required_locks=(lock,),
        )
        runtime = self._a2_runtime(store, lock=lock)

        with self.assertRaisesRegex(
            SessionResponseSnapshotError,
            "legacy session state",
        ) as raised:
            runtime.session_response_snapshot_v1(SESSION_ID)

        self.assertEqual(
            events,
            ["a2:acquire", "child:load", "a2:release"],
        )
        self.assertEqual(store.load_count, 1)
        self.assertFalse(lock.held)
        self.assertEqual(
            raised.exception.task_state.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )
        self.assertTrue(raised.exception.response_snapshot)

    def test_a3_session_response_capture_retains_failed_frozen_read_set(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            error=ValueError("secret unreadable workflow"),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            _child(),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        runtime = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        )

        with self.assertRaises(SessionResponseSnapshotError) as raised:
            runtime.session_response_snapshot_v1(SESSION_ID)

        self.assertEqual(
            events,
            [
                "a3:acquire",
                "a2:acquire",
                "parent:load",
                "child:load",
                "a2:release",
                "a3:release",
            ],
        )
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertIn(
            "WORKFLOW_STATE_UNREADABLE",
            raised.exception.task_state.consistency.codes,
        )
        self.assertNotIn(
            "secret unreadable workflow",
            json.dumps(raised.exception.task_state.to_dict(), ensure_ascii=False),
        )

    def test_standalone_a2_takes_only_a2_lock_and_reads_once(self):
        events: list[str] = []
        lock = TracingLock("a2", events)
        store = SingleReadStore(
            _child(),
            name="child",
            events=events,
            required_locks=(lock,),
        )
        runtime = self._a2_runtime(store, lock=lock)
        projection_lock_states: list[bool] = []

        def project(*args, **kwargs):
            projection_lock_states.append(lock.held)
            return build_task_state_snapshot_v1(*args, **kwargs)

        with mock.patch(
            "tiku_agent.task_state_runtime.build_task_state_snapshot_v1",
            side_effect=project,
        ):
            snapshot = runtime.task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.status, "OK")
        self.assertEqual(snapshot.active_child_task.task_id, CHILD_ID)
        self.assertEqual(events, ["a2:acquire", "child:load", "a2:release"])
        self.assertEqual(store.load_count, 1)
        self.assertEqual(projection_lock_states, [True])

    def test_a3_lock_order_and_single_reads_are_exact(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            _workflow(route="A2", phase="A2_ACTIVE"),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            _child(),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        runtime = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        )
        projection_lock_states: list[tuple[bool, bool]] = []

        def project(*args, **kwargs):
            projection_lock_states.append((a3_lock.held, a2_lock.held))
            return build_task_state_snapshot_v1(*args, **kwargs)

        with mock.patch(
            "tiku_agent.task_state_runtime.build_task_state_snapshot_v1",
            side_effect=project,
        ):
            snapshot = runtime.task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.status, "OK")
        self.assertEqual(
            events,
            [
                "a3:acquire",
                "a2:acquire",
                "parent:load",
                "child:load",
                "a2:release",
                "a3:release",
            ],
        )
        self.assertEqual(projection_lock_states, [(True, True)])

    def test_parent_read_failure_still_reads_child_once_and_releases_locks(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            error=RuntimeError("secret parent failure C:/private/state.json"),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            _child(),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        snapshot = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        ).task_state_snapshot_v1(SESSION_ID)

        self.assertIn("WORKFLOW_STATE_UNREADABLE", snapshot.consistency.codes)
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertFalse(a3_lock.held)
        self.assertFalse(a2_lock.held)
        self.assertEqual(events[-2:], ["a2:release", "a3:release"])
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret parent failure", public)
        self.assertNotIn("private", public)

    def test_child_read_failure_still_reads_each_store_once_and_releases_locks(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            _workflow(route="A2", phase="A2_ACTIVE"),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            error=RuntimeError("secret child failure C:/private/child.json"),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )

        snapshot = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        ).task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.allowed_actions, ())
        self.assertEqual(snapshot.active_child_task.allowed_actions, ())
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertFalse(a3_lock.held)
        self.assertFalse(a2_lock.held)
        self.assertEqual(events[-2:], ["a2:release", "a3:release"])
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret child failure", public)
        self.assertNotIn("private", public)

    def test_a3_active_child_read_failure_is_fail_closed_under_both_locks(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            _workflow(
                route="A3",
                phase="A2_ACTIVE",
                units=[_unit()],
                selected_unit_id="g1-u1",
            ),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            error=RuntimeError(
                "secret A3 child failure C:/private/a3-child-state.json"
            ),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )

        snapshot = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        ).task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.allowed_actions, ())
        # An unreadable A3 child cannot safely expose its identity or unit
        # binding.  The fail-closed projection therefore omits the child view
        # together with all unit state instead of manufacturing placeholders.
        self.assertIsNone(snapshot.active_child_task)
        self.assertIsNone(snapshot.current_unit)
        self.assertEqual(snapshot.units, ())
        self.assertEqual(parent_store.load_attempt_count, 1)
        self.assertEqual(child_store.load_attempt_count, 1)
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertEqual(
            events,
            [
                "a3:acquire",
                "a2:acquire",
                "parent:load",
                "child:load",
                "a2:release",
                "a3:release",
            ],
        )
        self.assertFalse(a3_lock.held)
        self.assertFalse(a2_lock.held)
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret A3 child failure", public)
        self.assertNotIn("a3-child-state.json", public)

    def test_parent_and_child_read_failures_are_ordered_and_fail_closed(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        parent_store = SingleReadStore(
            error=RuntimeError(
                "secret parent double failure C:/private/parent-state.json"
            ),
            name="parent",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            error=RuntimeError(
                "secret child double failure C:/private/child-state.json"
            ),
            name="child",
            events=events,
            required_locks=(a3_lock, a2_lock),
        )

        snapshot = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
        ).task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(
            snapshot.consistency.codes,
            ("WORKFLOW_STATE_UNREADABLE", "CHILD_STATE_UNREADABLE"),
        )
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.allowed_actions, ())
        self.assertIsNone(snapshot.active_child_task)
        self.assertIsNone(snapshot.current_unit)
        self.assertEqual(snapshot.units, ())
        self.assertEqual(parent_store.load_attempt_count, 1)
        self.assertEqual(child_store.load_attempt_count, 1)
        self.assertEqual(parent_store.load_count, 1)
        self.assertEqual(child_store.load_count, 1)
        self.assertEqual(
            events,
            [
                "a3:acquire",
                "a2:acquire",
                "parent:load",
                "child:load",
                "a2:release",
                "a3:release",
            ],
        )
        self.assertFalse(a3_lock.held)
        self.assertFalse(a2_lock.held)
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret parent double failure", public)
        self.assertNotIn("secret child double failure", public)
        self.assertNotIn("parent-state.json", public)
        self.assertNotIn("child-state.json", public)

    def test_standalone_read_failure_releases_a2_lock(self):
        events: list[str] = []
        a2_lock = TracingLock("a2", events)
        child_store = SingleReadStore(
            error=OSError("secret standalone failure"),
            name="child",
            events=events,
            required_locks=(a2_lock,),
        )

        snapshot = self._a2_runtime(
            child_store,
            lock=a2_lock,
        ).task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(events, ["a2:acquire", "child:load", "a2:release"])
        self.assertFalse(a2_lock.held)
        self.assertEqual(child_store.load_count, 1)
        self.assertEqual(child_store.load_attempt_count, 1)
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret standalone failure", public)

    def test_frozen_cancelled_never_touches_lock_or_store_but_live_residual_is_hidden(self):
        poison_lock = TracingLock("poison", [], poison=True)
        poison_store = SingleReadStore(poison=True)
        frozen_runtime = self._a2_runtime(poison_store, lock=poison_lock)
        cancelled = _child(phase="CANCELLED")

        frozen = frozen_runtime.task_state_snapshot_v1_from_frozen_state(
            SESSION_ID,
            cancelled,
        )

        self.assertEqual(frozen.consistency.status, "OK")
        self.assertEqual(frozen.active_child_task.phase, "CANCELLED")
        self.assertEqual(poison_store.load_count, 0)
        self.assertEqual(poison_store.load_attempt_count, 0)

        live_store = SingleReadStore(cancelled)
        live = self._a2_runtime(live_store).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(live.consistency.status, "OK")
        self.assertIsNone(live.active_child_task)
        self.assertEqual(live_store.load_count, 1)

    def test_missing_unreadable_and_orphan_are_not_conflated(self):
        missing = self._a2_runtime(SingleReadStore(None)).task_state_snapshot_v1(
            SESSION_ID
        )
        self.assertEqual(missing.consistency.status, "OK")
        self.assertFalse(missing.workflow.exists)
        self.assertIsNone(missing.active_child_task)

        empty_a3 = self._a3_runtime(
            SingleReadStore(None),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(empty_a3.consistency.status, "OK")
        self.assertFalse(empty_a3.workflow.exists)
        self.assertIsNone(empty_a3.active_child_task)

        unreadable = self._a2_runtime(
            SingleReadStore(error=ValueError("broken JSON secret"))
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(unreadable.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertIsNotNone(unreadable.active_child_task)
        self.assertEqual(unreadable.active_child_task.phase, "UNKNOWN")
        self.assertEqual(unreadable.active_child_task.allowed_actions, ())

        orphan = self._a3_runtime(
            SingleReadStore(None),
            SingleReadStore(_child()),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(orphan.consistency.codes, ("ORPHAN_CHILD_TASK",))
        self.assertFalse(orphan.workflow.exists)
        if orphan.active_child_task is not None:
            self.assertEqual(orphan.active_child_task.allowed_actions, ())

    def test_unknown_phase_requires_returned_state_not_exception_text(self):
        exception_snapshot = self._a2_runtime(
            SingleReadStore(error=ValueError("Unknown Agent phase: ALIEN secret"))
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            exception_snapshot.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )

        unknown_child = _child()
        unknown_child.phase = "ALIEN_CHILD_PHASE"
        classified = self._a2_runtime(
            SingleReadStore(unknown_child)
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(classified.consistency.codes, ("UNKNOWN_CHILD_PHASE",))
        public = json.dumps(classified.to_dict(), ensure_ascii=False)
        self.assertNotIn("ALIEN_CHILD_PHASE", public)

        unknown_parent = _workflow()
        unknown_parent.phase = "ALIEN_WORKFLOW_PHASE"
        parent_classified = self._a3_runtime(
            SingleReadStore(unknown_parent),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            parent_classified.consistency.codes,
            ("UNKNOWN_WORKFLOW_PHASE",),
        )

        sentinel_child = _child()
        sentinel_child.phase = "UNKNOWN"
        sentinel_child_snapshot = self._a2_runtime(
            SingleReadStore(sentinel_child)
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            sentinel_child_snapshot.consistency.codes,
            ("UNKNOWN_CHILD_PHASE",),
        )

        sentinel_parent = _workflow()
        sentinel_parent.phase = "UNKNOWN"
        sentinel_parent_snapshot = self._a3_runtime(
            SingleReadStore(sentinel_parent),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            sentinel_parent_snapshot.consistency.codes,
            ("UNKNOWN_WORKFLOW_PHASE",),
        )

        duplicate_parent = _workflow(units=[_unit()])
        duplicate_parent.units.append(dict(duplicate_parent.units[0]))
        duplicate = self._a3_runtime(
            SingleReadStore(duplicate_parent),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(duplicate.consistency.codes, ("DUPLICATE_UNIT_ID",))

    def test_malformed_loaded_objects_fail_closed_without_escaping(self):
        malformed_parent = self._a3_runtime(
            SingleReadStore(object()),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            malformed_parent.consistency.codes,
            ("WORKFLOW_STATE_UNREADABLE",),
        )
        self.assertEqual(malformed_parent.consistency.status, "INCONSISTENT")
        self.assertEqual(malformed_parent.workflow.allowed_actions, ())

        broken_parent_phase = _workflow()
        broken_parent_phase.phase = []
        malformed_phase = self._a3_runtime(
            SingleReadStore(broken_parent_phase),
            SingleReadStore(None),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            malformed_phase.consistency.codes,
            ("WORKFLOW_STATE_UNREADABLE",),
        )

        broken_child_phase = _child()
        broken_child_phase.phase = []
        malformed_child = self._a2_runtime(
            SingleReadStore(broken_child_phase)
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            malformed_child.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )
        self.assertEqual(malformed_child.consistency.status, "INCONSISTENT")
        self.assertEqual(malformed_child.active_child_task.allowed_actions, ())

        frozen_malformed = self._a2_runtime(
            SingleReadStore(poison=True),
            lock=TracingLock("poison", [], poison=True),
        ).task_state_snapshot_v1_from_frozen_state(
            SESSION_ID,
            broken_child_phase,
        )
        self.assertEqual(
            frozen_malformed.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )

        broken_cancelled = _child(phase="CANCELLED")
        broken_cancelled.current_search_id = []
        frozen_cancelled = self._a2_runtime(
            SingleReadStore(poison=True),
            lock=TracingLock("poison", [], poison=True),
        ).task_state_snapshot_v1_from_frozen_state(
            SESSION_ID,
            broken_cancelled,
        )
        self.assertEqual(
            frozen_cancelled.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )

    def test_source_retry_and_entry_actions_require_real_runtime_evidence(self):
        artifacts = SessionArtifacts(self.root / "source-evidence")
        upload_dir = artifacts.session_dir(SESSION_ID) / "uploads"
        upload_dir.mkdir(parents=True)
        source = upload_dir / "page.png"
        source.write_bytes(b"page")
        capabilities = TaskStateEntryCapabilities(
            trusted_image_event=True,
            reset_session_available=True,
        )
        parent = _workflow(
            route="A3",
            phase="ERROR",
            source_page_path=str(source),
        )
        default_capabilities = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertIn(
            "retry_current_stage",
            default_capabilities.workflow.allowed_actions,
        )
        self.assertNotIn(
            "upload_image",
            default_capabilities.workflow.allowed_actions,
        )
        self.assertNotIn(
            "reset_session",
            default_capabilities.workflow.allowed_actions,
        )

        snapshot = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID, capabilities=capabilities)
        self.assertEqual(
            set(snapshot.workflow.allowed_actions),
            {"retry_current_stage", "upload_image", "reset_session"},
        )

        outside = self.root / "outside-page.png"
        outside.write_bytes(b"outside")
        parent.source_page_path = str(outside)
        no_retry = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID, capabilities=capabilities)
        self.assertNotIn("retry_current_stage", no_retry.workflow.allowed_actions)
        self.assertIn("upload_image", no_retry.workflow.allowed_actions)

        pending = _workflow(
            route="PENDING",
            phase="ERROR",
            source_page_path=str(source),
        )
        without_authority = self._a3_runtime(
            SingleReadStore(pending),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertNotIn(
            "retry_current_stage",
            without_authority.workflow.allowed_actions,
        )
        with_authority = self._a3_runtime(
            SingleReadStore(pending),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
            image_triage_authority=object(),
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertIn(
            "retry_current_stage",
            with_authority.workflow.allowed_actions,
        )

    def test_auto_crop_is_prepared_only_for_exact_controlled_real_file(self):
        artifacts = SessionArtifacts(self.root / "crop-evidence")
        crop_dir = artifacts.session_dir(SESSION_ID) / "crops"
        crop_dir.mkdir(parents=True)
        crop = crop_dir / "unit.png"
        crop.write_bytes(b"crop")
        parent = _workflow(
            units=[_unit()],
            auto_crop_enabled=True,
            auto_crops={
                "g1-u1": {
                    "validation_status": "auto_ready",
                    "path": str(crop),
                }
            },
        )
        prepared = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(prepared.units[0].status, "PREPARED")

        outside = self.root / "outside-crop.png"
        outside.write_bytes(b"outside")
        parent.auto_crops["g1-u1"]["path"] = str(outside)
        rejected = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(rejected.units[0].status, "AVAILABLE")

        parent.auto_crops = {
            "": {
                "validation_status": "auto_ready",
                "path": str(crop),
            }
        }
        malformed = self._a3_runtime(
            SingleReadStore(parent),
            SingleReadStore(None),
            workflow_artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertEqual(
            malformed.consistency.codes,
            ("WORKFLOW_STATE_UNREADABLE",),
        )

    def test_child_retry_requires_real_controlled_current_image(self):
        artifacts = SessionArtifacts(self.root / "child-retry")
        upload_dir = artifacts.session_dir(SESSION_ID) / "uploads"
        upload_dir.mkdir(parents=True)
        image = upload_dir / "question.png"
        image.write_bytes(b"question")
        retryable = _child(
            phase="ERROR",
            current_image_path=str(image),
            last_error="safe failure",
        )
        allowed = self._a2_runtime(
            SingleReadStore(retryable),
            artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertIn("retry_search", allowed.active_child_task.allowed_actions)

        question_only = _child(
            phase="ERROR",
            current_image_path="",
            current_question_image_path=str(image),
            last_error="safe failure",
        )
        denied = self._a2_runtime(
            SingleReadStore(question_only),
            artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertNotIn("retry_search", denied.active_child_task.allowed_actions)

        outside = self.root / "outside-question.png"
        outside.write_bytes(b"outside")
        outside_state = _child(
            phase="ERROR",
            current_image_path=str(outside),
            last_error="safe failure",
        )
        outside_denied = self._a2_runtime(
            SingleReadStore(outside_state),
            artifacts=artifacts,
        ).task_state_snapshot_v1(SESSION_ID)
        self.assertNotIn(
            "retry_search",
            outside_denied.active_child_task.allowed_actions,
        )

    def test_file_probe_failure_only_removes_evidence_and_occurs_under_both_locks(self):
        events: list[str] = []
        a3_lock = TracingLock("a3", events)
        a2_lock = TracingLock("a2", events)
        artifacts = SessionArtifacts(self.root / "probe-evidence")
        upload_dir = artifacts.session_dir(SESSION_ID) / "uploads"
        crop_dir = artifacts.session_dir(SESSION_ID) / "crops"
        upload_dir.mkdir(parents=True)
        crop_dir.mkdir(parents=True)
        source = upload_dir / "page.png"
        crop = crop_dir / "unit.png"
        source.write_bytes(b"page")
        crop.write_bytes(b"crop")
        parent = _workflow(
            route="A3",
            phase="ERROR",
            source_page_path=str(source),
            units=[_unit()],
            auto_crop_enabled=True,
            auto_crops={
                "g1-u1": {
                    "validation_status": "auto_ready",
                    "path": str(crop),
                }
            },
        )
        parent_store = SingleReadStore(
            parent,
            required_locks=(a3_lock, a2_lock),
        )
        child_store = SingleReadStore(
            None,
            required_locks=(a3_lock, a2_lock),
        )
        probe_lock_states: list[tuple[bool, bool]] = []

        def deny_open(*args, **kwargs):
            probe_lock_states.append((a3_lock.held, a2_lock.held))
            raise PermissionError("secret permission detail")

        runtime = self._a3_runtime(
            parent_store,
            child_store,
            a3_lock=a3_lock,
            a2_lock=a2_lock,
            workflow_artifacts=artifacts,
        )
        with mock.patch("tiku_agent.task_state_runtime.Path.open", side_effect=deny_open):
            snapshot = runtime.task_state_snapshot_v1(SESSION_ID)

        self.assertEqual(snapshot.consistency.status, "OK")
        self.assertNotIn("retry_current_stage", snapshot.workflow.allowed_actions)
        self.assertEqual(len(probe_lock_states), 2)
        self.assertTrue(all(state == (True, True) for state in probe_lock_states))
        self.assertFalse(a3_lock.held)
        self.assertFalse(a2_lock.held)
        self.assertNotIn(
            "secret permission detail",
            json.dumps(snapshot.to_dict(), ensure_ascii=False),
        )

    def test_frozen_entry_collects_file_evidence_inside_callers_existing_lock(self):
        events: list[str] = []
        a2_lock = TracingLock("a2", events)
        poison_store = SingleReadStore(poison=True)
        artifacts = SessionArtifacts(self.root / "frozen-evidence")
        upload_dir = artifacts.session_dir(SESSION_ID) / "uploads"
        upload_dir.mkdir(parents=True)
        image = upload_dir / "question.png"
        image.write_bytes(b"question")
        state = _child(
            phase="ERROR",
            current_image_path=str(image),
            last_error="safe failure",
        )
        runtime = self._a2_runtime(
            poison_store,
            artifacts=artifacts,
            lock=a2_lock,
        )
        original_open = Path.open
        probe_lock_states: list[bool] = []

        def observe_open(path, *args, **kwargs):
            probe_lock_states.append(a2_lock.held)
            return original_open(path, *args, **kwargs)

        with a2_lock:
            with mock.patch(
                "tiku_agent.task_state_runtime.Path.open",
                new=observe_open,
            ):
                snapshot = runtime.task_state_snapshot_v1_from_frozen_state(
                    SESSION_ID,
                    state,
                )

        self.assertEqual(snapshot.consistency.status, "OK")
        self.assertIn("retry_search", snapshot.active_child_task.allowed_actions)
        self.assertEqual(poison_store.load_count, 0)
        self.assertEqual(poison_store.load_attempt_count, 0)
        self.assertTrue(probe_lock_states)
        self.assertTrue(all(probe_lock_states))
        self.assertEqual(events, ["a2:acquire", "a2:release"])


if __name__ == "__main__":
    unittest.main()

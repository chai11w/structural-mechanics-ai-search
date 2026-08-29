import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

from tiku_agent.agent import AgentResponse, AgentToolbox, TikuSearchAgent
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import (
    AgentBudgetExceededError,
    AgentRuntimeBusyError,
    AgentSessionRuntime,
    _ExecutionCancelled,
    _ExecutionGate,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import TaskLogEntry, TaskLogger
from tiku_agent.task_state_contract import TaskStateSnapshotV1, empty_task_state_snapshot
from tiku_agent.task_state_runtime import TaskStateEntryCapabilities
from tiku_agent.tools import ToolResult
from tiku_shared.trace_context import TraceContext, trace_context_scope
from tiku_shared.trace_events import (
    SQLiteTraceEventStore,
    TraceEventRecorder,
    trace_event_scope,
)


RUNTIME_DIR = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
RUNTIME_DIR.mkdir(exist_ok=True)


class FakeTools:
    def toolbox(self) -> AgentToolbox:
        return AgentToolbox(
            analyze_multi_image=lambda *_args, **_kwargs: ToolResult(
                ok=True,
                data={
                    "is_multi": False,
                    "single_analysis": {"loads": [{"type": "集中", "raw": "P"}], "chapter_hint": "4力法"},
                },
            ),
            analyze_image=lambda *_args, **_kwargs: self._unreachable(),
            route_bank=lambda *_args, **_kwargs: ToolResult(ok=True, data={"route": "main"}),
            classify_structure=lambda *_args, **_kwargs: ToolResult(ok=True, data={"structure_type": ""}),
            coarse_search=lambda *_args, **_kwargs: ToolResult(
                ok=True,
                data={"candidates": [{"rank": 1, "path": "bank/q1.jpg", "name": "q1.jpg", "score": 0.9}]},
            ),
            global_search=lambda *_args, **_kwargs: ToolResult(
                ok=True,
                data={
                    "candidates": [
                        {
                            "rank": 1,
                            "path": "bank/global-q1.jpg",
                            "name": "global-q1.jpg",
                            "score": 1.0,
                            "rerank_score": 1.0,
                            "chapter": "4力法",
                            "source_chapters": ["4力法"],
                        }
                    ]
                },
            ),
            rerank_candidates=lambda *_args, **_kwargs: ToolResult(
                ok=True,
                data={"reranked": False, "visible_candidates": [{"rank": 1, "path": "bank/q1.jpg", "name": "q1.jpg", "score": 0.9}]},
            ),
            answer_candidate=lambda *_args, **_kwargs: ToolResult(ok=True, data={"copied_paths": ["answers/q1.jpg"]}),
        )

    @staticmethod
    def _unreachable():
        raise AssertionError("single scope analysis should avoid duplicate image analysis")


class RecordingTaskLogger(TaskLogger):
    def __init__(self):
        self.entries: list[TaskLogEntry] = []

    def write(self, entry: TaskLogEntry) -> None:
        self.entries.append(entry)


class AgentSessionRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.database_path = RUNTIME_DIR / f"session_runtime_test_{uuid4().hex}.db"
        self.source_image = RUNTIME_DIR / f"session_runtime_source_{uuid4().hex}.jpg"
        self.source_image.write_bytes(b"fake image bytes")
        self.addCleanup(lambda: self.database_path.unlink(missing_ok=True))
        self.addCleanup(lambda: self.source_image.unlink(missing_ok=True))
        self.artifacts = SessionArtifacts(RUNTIME_DIR / f"session_artifacts_test_{uuid4().hex}")
        self.addCleanup(lambda: self.artifacts.clear_session("resume-session"))
        self.addCleanup(lambda: self.artifacts.clear_session("cancel-session"))
        tools = FakeTools().toolbox()
        self.logger = RecordingTaskLogger()
        self.store = SQLiteSessionStore(self.database_path)
        self.runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(state=state, tools=tools, use_llm_intent=False),
        )

    def test_restart_can_resume_unique_candidate_with_natural_confirmation(self):
        session_id = "resume-session"
        first = self.runtime.handle_image(session_id, self.source_image)
        self.assertEqual(first.state["phase"], "WAIT_CANDIDATE_CHOICE")

        restarted_runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(state=state, tools=FakeTools().toolbox(), use_llm_intent=False),
        )
        persisted_image = restarted_runtime.current_image_path(session_id)
        self.assertIsNotNone(persisted_image)
        self.assertTrue(persisted_image.is_file())
        self.assertEqual(restarted_runtime.resolve_upload(session_id, persisted_image.name), persisted_image.resolve())
        self.assertIsNone(restarted_runtime.resolve_upload(session_id, "../" + persisted_image.name))
        self.assertIsNone(restarted_runtime.resolve_upload("another-session", persisted_image.name))
        persisted_media = restarted_runtime.persist_media(session_id, self.source_image)
        self.assertIsNotNone(persisted_media)
        self.assertTrue(persisted_media.is_file())
        self.assertEqual(restarted_runtime.resolve_media(session_id, persisted_media.name), persisted_media.resolve())
        self.assertIsNone(restarted_runtime.resolve_media("another-session", persisted_media.name))
        answer = restarted_runtime.handle_text(session_id, "就这个")

        self.assertEqual(answer.state["phase"], "ANSWERED")
        self.assertEqual(answer.images, ["answers/q1.jpg"])
        self.assertEqual(self.store.load(session_id).last_answer_paths, ["answers/q1.jpg"])
        self.assertEqual([entry.outcome for entry in self.logger.entries], ["candidates", "answered"])
        self.assertEqual([entry.kind for entry in self.logger.entries], ["image", "text"])
        self.assertTrue(all(entry.duration_ms >= 0 for entry in self.logger.entries))

    def test_runtime_builds_v2_agent_in_isolated_session_directory(self):
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
        )
        isolated_agent = runtime._make_agent(AgentState(session_id="isolated"))
        self.assertEqual(isolated_agent.config.runtime_dir, self.artifacts.root.parent)
        self.assertEqual(
            isolated_agent.config.session_dir,
            self.artifacts.session_dir("isolated"),
        )

    def test_missing_candidate_is_logged_as_needs_input_instead_of_success(self):
        response = self.runtime.handle_text("missing-candidate", "选第1个候选题")

        self.assertEqual(response.protocol["status"], "NEEDS_INPUT")
        self.assertEqual(response.protocol["layer"], "session")
        self.assertEqual(response.protocol["code"], "CANDIDATE_LIST_UNAVAILABLE")
        self.assertEqual(response.protocol["action"], "retry_upload")
        self.assertEqual(len(self.logger.entries), 1)
        entry = self.logger.entries[0]
        self.assertEqual(entry.status, "NEEDS_INPUT")
        self.assertEqual(entry.layer, "session")
        self.assertEqual(entry.code, "CANDIDATE_LIST_UNAVAILABLE")

    def test_task_and_cost_run_keep_trace_while_run_id_is_independent(self):
        class CapturingLedger:
            def __init__(self):
                self.collectors = []

            def estimated_cost_micros_since(
                self, _started_at: str, *, identity_key: str | None = None
            ) -> int:
                del identity_key
                return 0

            def write_run(self, collector, *, finished_at: str, outcome: str) -> None:
                del finished_at, outcome
                self.collectors.append(collector)

        ledger = CapturingLedger()
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            cost_ledger=ledger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )
        trace_id = "trace_0123456789abcdef0123456789abcdef"
        request_id = "req_0123456789abcdef0123456789abcdef"

        with trace_context_scope(TraceContext(trace_id, request_id=request_id)):
            runtime.handle_text("trace-session", "你好", request_id=request_id)
        second_trace_id = "trace_fedcba9876543210fedcba9876543210"
        with trace_context_scope(TraceContext(second_trace_id, request_id=request_id)):
            runtime.handle_text("trace-session", "继续", request_id=request_id)

        self.assertEqual(
            [entry.trace_id for entry in self.logger.entries[-2:]],
            [trace_id, second_trace_id],
        )
        self.assertEqual(len(ledger.collectors), 2)
        self.assertEqual(
            [collector.trace_id for collector in ledger.collectors],
            [trace_id, second_trace_id],
        )
        run_ids = [collector.run_id for collector in ledger.collectors]
        for run_id in run_ids:
            self.assertRegex(run_id, r"^run_[0-9a-f]{32}$")
        self.assertEqual(len(set(run_ids)), 2)
        self.assertNotIn(request_id, run_ids)

    def test_direct_a2_events_bind_search_and_workflow_before_routing(self):
        trace_id = "trace_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        request_id = "req_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        event_database = RUNTIME_DIR / f"trace_events_{uuid4().hex}.sqlite3"
        self.addCleanup(lambda: event_database.unlink(missing_ok=True))
        event_store = SQLiteTraceEventStore(event_database)
        recorder = TraceEventRecorder(event_store)
        session_id = "trace-search-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))

        with trace_context_scope(
            TraceContext(trace_id, request_id=request_id)
        ), trace_event_scope(
            recorder,
            trace_id=trace_id,
            request_id=request_id,
        ):
            response = self.runtime.handle_image(
                session_id,
                self.source_image,
                request_id=request_id,
            )

        search_id = str(response.protocol["search_id"])
        routes = [
            event
            for event in event_store.events_for_trace(trace_id)
            if event.event_type == "route_decided" and event.stage == "bank_routing"
        ]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].search_id, search_id)
        self.assertEqual(routes[0].workflow_search_id, search_id)
        self.assertEqual(routes[0].request_id, request_id)

    def test_cancel_clears_persisted_session(self):
        session_id = "cancel-session"
        self.runtime.handle_image(session_id, self.source_image)

        response = self.runtime.handle_text(session_id, "取消")

        self.assertEqual(response.intent, "cancel")
        self.assertIsNone(self.store.load(session_id))
        self.assertFalse(self.artifacts.session_dir(session_id).exists())
        self.assertEqual(self.logger.entries[-1].outcome, "cancelled")

    def test_cancel_freezes_cancelled_v1_before_clearing_the_store(self):
        session_id = "cancel-frozen-v1-session"
        self.runtime.handle_image(session_id, self.source_image)

        response = self.runtime.handle_text(
            session_id,
            "取消",
            task_state_capabilities=TaskStateEntryCapabilities(
                reset_session_available=True,
            ),
        )

        self.assertEqual(response.intent, "cancel")
        self.assertIsNone(self.store.load(session_id))
        self.assertEqual(response.response_snapshot["phase"], "CANCELLED")
        self.assertEqual(
            response.response_projection_snapshot["phase"],
            "CANCELLED",
        )
        self.assertTrue(response.response_media_snapshot_captured)
        task_state = response.response_task_state_snapshot
        self.assertIsNotNone(task_state)
        self.assertEqual(task_state.consistency.status, "OK")
        self.assertEqual(task_state.active_child_task.phase, "CANCELLED")
        self.assertEqual(task_state.active_child_task.allowed_actions, ())

    def test_cancel_without_an_active_task_freezes_canonical_empty_v1(self):
        cases = (
            ("missing", None),
            ("idle", AgentState(session_id="empty-cancel-idle")),
            (
                "cancelled_residual",
                AgentState(
                    session_id="empty-cancel-cancelled",
                    phase="CANCELLED",
                    current_search_id="search_empty_cancel_residual",
                    task_revision=1,
                ),
            ),
        )
        for label, persisted in cases:
            with self.subTest(case=label):
                session_id = (
                    persisted.session_id
                    if persisted is not None
                    else "empty-cancel-missing"
                )
                if persisted is not None:
                    self.store.save(persisted)

                response = self.runtime.handle_text(
                    session_id,
                    "取消",
                    task_state_capabilities=TaskStateEntryCapabilities(
                        reset_session_available=True,
                    ),
                )

                self.assertEqual(response.intent, "cancel")
                self.assertIsNone(self.store.load(session_id))
                self.assertFalse(response.response_snapshot["session_valid"])
                self.assertEqual(response.response_snapshot["phase"], "IDLE")
                self.assertEqual(
                    response.response_projection_snapshot,
                    response.response_snapshot,
                )
                self.assertEqual(
                    response.response_task_state_snapshot,
                    empty_task_state_snapshot(),
                )
                self.assertTrue(response.response_media_snapshot_captured)

    def test_parent_managed_cancel_preserves_history_media_until_full_clear(self):
        session_id = "parent-managed-cancel-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
            preserve_artifacts_on_cancel=True,
        )
        runtime.handle_image(session_id, self.source_image)
        media = runtime.persist_media(session_id, self.source_image)

        response = runtime.handle_text(session_id, "取消")

        self.assertEqual(response.intent, "cancel")
        self.assertIsNone(self.store.load(session_id))
        self.assertTrue(self.artifacts.session_dir(session_id).exists())
        self.assertIsNone(runtime.resolve_media(session_id, media.name))
        self.assertEqual(
            runtime.resolve_media(session_id, media.name, allow_preserved=True),
            media.resolve(),
        )
        runtime.clear(session_id)
        self.assertFalse(self.artifacts.session_dir(session_id).exists())

    def test_state_only_clear_keeps_history_media_until_full_clear(self):
        session_id = "history-media-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        self.runtime.handle_image(session_id, self.source_image)
        upload = self.runtime.current_image_path(session_id)
        media = self.runtime.persist_media(session_id, self.source_image)

        self.runtime.clear(session_id, preserve_artifacts=True)

        self.assertIsNone(self.store.load(session_id))
        self.assertIsNone(self.runtime.resolve_upload(session_id, upload.name))
        self.assertIsNone(self.runtime.resolve_media(session_id, media.name))
        self.assertEqual(
            self.runtime.resolve_media(session_id, media.name, allow_preserved=True),
            media.resolve(),
        )
        self.runtime.clear(session_id)
        self.assertFalse(self.artifacts.session_dir(session_id).exists())

    def test_pending_chapter_survives_restart_and_is_consumed_once(self):
        session_id = "v2-pending-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )

        pending = runtime.handle_text(session_id, "待会传的题按影响线")
        self.assertEqual(pending.state["pending_chapter"], "8影响线")

        restarted = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )
        searched = restarted.handle_image(session_id, self.source_image)

        self.assertEqual(searched.state["current_chapter"], "8影响线")
        self.assertEqual(searched.state["pending_chapter"], "")
        self.assertEqual(self.store.load(session_id).pending_chapter, "")

    def test_global_search_offer_survives_restart_and_is_consumed(self):
        session_id = "v2-global-offer-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        tools = FakeTools().toolbox()
        tools.analyze_multi_image = lambda *_args, **_kwargs: ToolResult(
            ok=True,
            data={
                "is_multi": False,
                "single_analysis": {
                    "loads": [{"type": "集中", "raw": "P"}],
                    "chapter_hint": "unknown",
                },
            },
        )
        make_agent = lambda state: TikuSearchAgent(
            state=state,
            tools=tools,
            use_llm_intent=False,
        )
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=make_agent,
        )

        offered = runtime.handle_image(session_id, self.source_image)
        self.assertTrue(offered.state["global_search_offered"])
        self.assertTrue(self.store.load(session_id).global_search_offered)

        restarted = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=make_agent,
        )
        searched = restarted.handle_text(session_id, "可以")

        self.assertEqual(searched.intent, "global_search")
        self.assertEqual(searched.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertFalse(searched.state["global_search_offered"])
        self.assertFalse(self.store.load(session_id).global_search_offered)

    def test_execution_gate_bounds_active_and_waiting_tasks(self):
        gate = _ExecutionGate(max_concurrent=1, max_queued=1, wait_seconds=1)
        active = threading.Event()
        release = threading.Event()
        queued = threading.Event()
        completed = []

        def first_task():
            with gate.enter():
                active.set()
                release.wait(1)

        def second_task():
            with gate.enter(lambda stage, _message: queued.set() if stage == "queued" else None):
                completed.append("second")

        first = threading.Thread(target=first_task)
        second = threading.Thread(target=second_task)
        first.start()
        self.assertTrue(active.wait(1))
        second.start()
        self.assertTrue(queued.wait(1))

        with self.assertRaises(AgentRuntimeBusyError):
            with gate.enter():
                pass

        release.set()
        first.join(1)
        second.join(1)
        self.assertEqual(completed, ["second"])

    def test_execution_gate_releases_waiting_slot_when_progress_callback_fails(self):
        gate = _ExecutionGate(max_concurrent=1, max_queued=1, wait_seconds=1)
        active = threading.Event()
        release = threading.Event()
        queued = threading.Event()
        completed = []

        def first_task():
            with gate.enter():
                active.set()
                release.wait(1)

        first = threading.Thread(target=first_task)
        first.start()
        self.assertTrue(active.wait(1))

        def fail_progress(_stage, _message):
            raise RuntimeError("progress failed")

        with self.assertRaisesRegex(RuntimeError, "progress failed"):
            with gate.enter(fail_progress):
                pass

        def second_task():
            with gate.enter(
                lambda stage, _message: queued.set() if stage == "queued" else None
            ):
                completed.append("second")

        second = threading.Thread(target=second_task)
        second.start()
        self.assertTrue(queued.wait(1))
        release.set()
        first.join(1)
        second.join(1)
        self.assertEqual(completed, ["second"])

    def test_execution_gate_is_fifo_and_new_arrivals_cannot_barge(self):
        gate = _ExecutionGate(max_concurrent=1, max_queued=3, wait_seconds=2)
        active = threading.Event()
        release = threading.Event()
        queued = {name: threading.Event() for name in ("second", "third", "barger")}
        order = []
        errors = []

        def first_task():
            with gate.enter():
                active.set()
                release.wait(2)

        def queued_task(name):
            try:
                with gate.enter(
                    lambda stage, _message: (
                        queued[name].set() if stage == "queued" else None
                    )
                ):
                    order.append(name)
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        first = threading.Thread(target=first_task)
        first.start()
        self.assertTrue(active.wait(1))
        waiters = []
        for name in ("second", "third", "barger"):
            thread = threading.Thread(target=queued_task, args=(name,))
            waiters.append(thread)
            thread.start()
            self.assertTrue(queued[name].wait(1))

        release.set()
        first.join(2)
        for thread in waiters:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, ["second", "third", "barger"])

    def test_execution_gate_prevents_arrival_from_barging_during_slot_handoff(self):
        class DelayedWakeCondition(threading.Condition):
            def __init__(self):
                super().__init__()
                self.waiter_released_lock = threading.Event()
                self.resume_waiter = threading.Event()
                self.delayed_once = False

            def wait(self, timeout=None):
                notified = super().wait(timeout)
                if (
                    threading.current_thread().name == "oldest-gate-waiter"
                    and notified
                    and not self.delayed_once
                ):
                    self.delayed_once = True
                    self.release()
                    try:
                        self.waiter_released_lock.set()
                        if not self.resume_waiter.wait(2):
                            raise TimeoutError("oldest waiter was not resumed")
                    finally:
                        self.acquire()
                return notified

        gate = _ExecutionGate(max_concurrent=1, max_queued=2, wait_seconds=2)
        controlled_condition = DelayedWakeCondition()
        gate._condition = controlled_condition
        active = threading.Event()
        release = threading.Event()
        oldest_queued = threading.Event()
        arrival_queued = threading.Event()
        order = []
        errors = []

        def holder_task():
            with gate.enter():
                active.set()
                release.wait(2)

        def oldest_task():
            try:
                with gate.enter(
                    lambda stage, _message: (
                        oldest_queued.set() if stage == "queued" else None
                    )
                ):
                    order.append("oldest")
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        def arrival_task():
            try:
                with gate.enter(
                    lambda stage, _message: (
                        arrival_queued.set() if stage == "queued" else None
                    )
                ):
                    order.append("arrival")
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        holder = threading.Thread(target=holder_task)
        oldest = threading.Thread(target=oldest_task, name="oldest-gate-waiter")
        holder.start()
        self.assertTrue(active.wait(1))
        oldest.start()
        self.assertTrue(oldest_queued.wait(1))
        release.set()
        self.assertTrue(controlled_condition.waiter_released_lock.wait(1))

        arrival = threading.Thread(target=arrival_task)
        arrival.start()
        self.assertTrue(arrival_queued.wait(1))
        self.assertEqual(order, [])
        controlled_condition.resume_waiter.set()

        for thread in (holder, oldest, arrival):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, ["oldest", "arrival"])

    def test_execution_gate_cancelled_waiter_withdraws_before_execution(self):
        gate = _ExecutionGate(max_concurrent=1, max_queued=1, wait_seconds=2)
        active = threading.Event()
        release = threading.Event()
        queued = threading.Event()
        cancelled = threading.Event()
        withdrawn = threading.Event()
        business_calls = []
        errors = []

        def first_task():
            with gate.enter():
                active.set()
                release.wait(2)

        def cancelled_task():
            try:
                with gate.enter(
                    lambda stage, _message: queued.set() if stage == "queued" else None,
                    cancelled=cancelled.is_set,
                ):
                    business_calls.append("cancelled request ran")
            except _ExecutionCancelled:
                withdrawn.set()
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        first = threading.Thread(target=first_task)
        waiter = threading.Thread(target=cancelled_task)
        first.start()
        self.assertTrue(active.wait(1))
        waiter.start()
        self.assertTrue(queued.wait(1))
        cancelled.set()
        self.assertTrue(withdrawn.wait(1))
        self.assertEqual(business_calls, [])

        replacement_ran = threading.Event()

        def replacement_task():
            with gate.enter():
                replacement_ran.set()

        replacement = threading.Thread(target=replacement_task)
        replacement.start()
        release.set()
        first.join(2)
        waiter.join(2)
        replacement.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(replacement_ran.is_set())

    def test_execution_gate_cancelled_by_dequeued_callback_releases_permit_and_session(self):
        gate = _ExecutionGate(max_concurrent=1, max_queued=1, wait_seconds=2)
        active = threading.Event()
        release = threading.Event()
        queued = threading.Event()
        cancelled = threading.Event()
        withdrawn = threading.Event()
        business_calls = []
        errors = []
        session_key = "dequeue-cancel-session"

        def first_task():
            with gate.enter(session_key="active-dequeue-session"):
                active.set()
                release.wait(2)

        def progress(stage, _message):
            if stage == "queued":
                queued.set()
            elif stage == "dequeued":
                cancelled.set()

        def cancelled_task():
            try:
                with gate.enter(
                    progress,
                    session_key=session_key,
                    cancelled=cancelled.is_set,
                ):
                    business_calls.append("cancelled request ran")
            except _ExecutionCancelled:
                withdrawn.set()
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        first = threading.Thread(target=first_task)
        waiter = threading.Thread(target=cancelled_task)
        first.start()
        self.assertTrue(active.wait(1))
        waiter.start()
        self.assertTrue(queued.wait(1))
        release.set()
        self.assertTrue(withdrawn.wait(1))
        first.join(2)
        waiter.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(business_calls, [])

        replacement_ran = threading.Event()
        with gate.enter(session_key=session_key):
            replacement_ran.set()
        self.assertTrue(replacement_ran.is_set())

    def test_same_session_waiter_does_not_consume_global_runtime_permit(self):
        first_session = "same-session-permit-a"
        first_started = threading.Event()
        release_first = threading.Event()
        release_other = threading.Event()
        same_queued = threading.Event()
        same_second_started = threading.Event()
        other_started = threading.Event()
        calls_lock = threading.Lock()
        session_calls = {}
        errors = []

        class BlockingAgent:
            def __init__(self, state):
                self.state = state
                self.progress_reporter = None

            def handle_text(self, _text):
                with calls_lock:
                    call_number = session_calls.get(self.state.session_id, 0) + 1
                    session_calls[self.state.session_id] = call_number
                if self.state.session_id == first_session:
                    if call_number == 1:
                        first_started.set()
                        if not release_first.wait(3):
                            raise TimeoutError("first same-session call was not released")
                    else:
                        same_second_started.set()
                else:
                    other_started.set()
                    if not release_other.wait(3):
                        raise TimeoutError("other-session call was not released")
                return AgentResponse(text="ok", intent="chat")

        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=BlockingAgent,
            max_concurrent_tasks=2,
            max_queued_tasks=1,
            queue_wait_seconds=2,
        )
        first_lock = runtime._session_locks[
            hash(first_session) % len(runtime._session_locks)
        ]
        other_session = ""
        for index in range(1000):
            candidate = f"other-session-permit-{index}"
            candidate_lock = runtime._session_locks[
                hash(candidate) % len(runtime._session_locks)
            ]
            if candidate_lock is not first_lock:
                other_session = candidate
                break
        self.assertTrue(other_session)

        def run(session_id, *, progress=None):
            try:
                runtime.handle_text(session_id, "你好", progress=progress)
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        first = threading.Thread(target=run, args=(first_session,))
        same_second = threading.Thread(
            target=run,
            args=(first_session,),
            kwargs={
                "progress": lambda stage, _message: (
                    same_queued.set() if stage == "queued" else None
                )
            },
        )
        other = threading.Thread(target=run, args=(other_session,))
        first.start()
        self.assertTrue(first_started.wait(1))
        same_second.start()
        self.assertTrue(same_queued.wait(1))
        other.start()
        self.assertTrue(other_started.wait(1))
        self.assertFalse(same_second_started.is_set())
        with self.assertRaises(AgentRuntimeBusyError) as raised:
            runtime.handle_text("overflow-session-permit", "你好")
        self.assertEqual(raised.exception.code, "QUEUE_FULL")

        release_first.set()
        self.assertTrue(same_second_started.wait(1))
        release_other.set()
        for thread in (first, same_second, other):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(same_second_started.is_set())

    def test_distinct_sessions_sharing_lock_stripe_do_not_consume_extra_permit(self):
        first_started = threading.Event()
        collision_started = threading.Event()
        other_started = threading.Event()
        release_first = threading.Event()
        release_other = threading.Event()
        collision_queued = threading.Event()
        errors = []

        class BlockingAgent:
            def __init__(self, state):
                self.state = state
                self.progress_reporter = None

            def handle_text(self, _text):
                if self.state.session_id == first_session:
                    first_started.set()
                    if not release_first.wait(3):
                        raise TimeoutError("first stripe call was not released")
                elif self.state.session_id == collision_session:
                    collision_started.set()
                elif self.state.session_id == other_session:
                    other_started.set()
                    if not release_other.wait(3):
                        raise TimeoutError("other stripe call was not released")
                return AgentResponse(text="ok", intent="chat")

        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=BlockingAgent,
            max_concurrent_tasks=2,
            max_queued_tasks=1,
            queue_wait_seconds=2,
        )
        sessions_by_lock = {}
        first_session = ""
        collision_session = ""
        shared_lock = None
        for index in range(len(runtime._session_locks) + 1):
            candidate = f"stripe-collision-session-{index}"
            candidate_lock = runtime._session_locks[
                hash(candidate) % len(runtime._session_locks)
            ]
            previous = sessions_by_lock.get(id(candidate_lock))
            if previous is not None:
                first_session = previous
                collision_session = candidate
                shared_lock = candidate_lock
                break
            sessions_by_lock[id(candidate_lock)] = candidate
        self.assertTrue(first_session)
        self.assertTrue(collision_session)
        self.assertIsNotNone(shared_lock)

        other_session = ""
        for index in range(1000):
            candidate = f"stripe-distinct-session-{index}"
            candidate_lock = runtime._session_locks[
                hash(candidate) % len(runtime._session_locks)
            ]
            if candidate_lock is not shared_lock:
                other_session = candidate
                break
        self.assertTrue(other_session)

        def run(session_id, *, progress=None):
            try:
                runtime.handle_text(session_id, "你好", progress=progress)
            except Exception as exc:  # pragma: no cover - assertion reports details.
                errors.append(exc)

        first = threading.Thread(target=run, args=(first_session,))
        collision = threading.Thread(
            target=run,
            args=(collision_session,),
            kwargs={
                "progress": lambda stage, _message: (
                    collision_queued.set() if stage == "queued" else None
                )
            },
        )
        other = threading.Thread(target=run, args=(other_session,))
        first.start()
        self.assertTrue(first_started.wait(1))
        collision.start()
        self.assertTrue(collision_queued.wait(1))
        other.start()
        self.assertTrue(other_started.wait(1))
        self.assertFalse(collision_started.is_set())

        release_first.set()
        self.assertTrue(collision_started.wait(1))
        release_other.set()
        for thread in (first, collision, other):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_daily_budget_blocks_before_running_agent(self):
        class SpentLedger:
            def estimated_cost_micros_since(self, _started_at: str) -> int:
                return 1_000_000

        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            cost_ledger=SpentLedger(),
            daily_budget_cny=1,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )

        with self.assertRaises(AgentBudgetExceededError):
            runtime.handle_text("budget-session", "你好")
        self.assertEqual(len(self.logger.entries), 1)
        blocked = self.logger.entries[0]
        self.assertEqual(blocked.status, "NEEDS_INPUT")
        self.assertEqual(blocked.layer, "quota")
        self.assertEqual(blocked.code, "GLOBAL_DAILY_QUOTA_EXCEEDED")
        self.assertTrue(blocked.request_id.startswith("req_"))

    def test_per_identity_daily_budget_only_blocks_the_spent_invitation(self):
        class IdentityLedger:
            def estimated_cost_micros_since(
                self, _started_at: str, *, identity_key: str | None = None
            ) -> int:
                return 3_000_000 if identity_key == "invite-spent" else 0

        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            cost_ledger=IdentityLedger(),
            per_identity_daily_budget_cny=3,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )

        with self.assertRaisesRegex(AgentBudgetExceededError, "该邀请码"):
            runtime.handle_text(
                "spent-session", "你好", identity_key="invite-spent"
            )
        response = runtime.handle_text(
            "fresh-session", "你好", identity_key="invite-fresh"
        )
        self.assertIsNotNone(response)

    def test_dynamic_budget_policy_is_reloaded_for_every_request(self):
        class SpentLedger:
            def estimated_cost_micros_since(
                self, _started_at: str, *, identity_key: str | None = None
            ) -> int:
                return 1_000_000

        class MutablePolicy:
            global_micros = 1_000_000
            identity_micros = 2_000_000

            def budget_limits_for(self, _identity_key: str):
                return SimpleNamespace(
                    global_daily_micros=self.global_micros,
                    identity_daily_micros=self.identity_micros,
                )

        policy = MutablePolicy()
        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            cost_ledger=SpentLedger(),
            budget_policy=policy,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )

        with self.assertRaisesRegex(AgentBudgetExceededError, "今日服务"):
            runtime.handle_text("dynamic-budget", "你好", identity_key="invite-a")
        policy.global_micros = 2_000_000
        self.assertIsNotNone(
            runtime.handle_text("dynamic-budget", "你好", identity_key="invite-a")
        )
        policy.identity_micros = 1_000_000
        with self.assertRaisesRegex(AgentBudgetExceededError, "该邀请码"):
            runtime.handle_text("dynamic-budget-2", "你好", identity_key="invite-a")

    def test_current_image_lookup_purges_expired_session_files(self):
        database_path = RUNTIME_DIR / f"session_expiry_test_{uuid4().hex}.db"
        self.addCleanup(lambda: database_path.unlink(missing_ok=True))
        current = [datetime(2026, 8, 11, tzinfo=UTC)]
        store = SQLiteSessionStore(
            database_path,
            ttl=timedelta(seconds=1),
            now=lambda: current[0],
        )
        session_id = "expired-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )
        runtime.handle_image(session_id, self.source_image)
        self.assertTrue(self.artifacts.session_dir(session_id).exists())

        current[0] += timedelta(seconds=2)

        self.assertIsNone(runtime.current_image_path(session_id))
        self.assertFalse(self.artifacts.session_dir(session_id).exists())

    def test_response_snapshot_is_frozen_while_same_session_lock_is_held(self):
        session_id = "response-snapshot-lock-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))

        class LockObservingRuntime(AgentSessionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.snapshot_lock_states = []

            def session_snapshot(self, observed_session_id):
                clean = self._clean_session_id(observed_session_id)
                lock = self._session_locks[hash(clean) % len(self._session_locks)]
                self.snapshot_lock_states.append(lock.locked())
                return super().session_snapshot(clean)

        runtime = LockObservingRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )

        searched = runtime.handle_image(session_id, self.source_image)
        searched_snapshot = dict(searched.response_snapshot)

        self.assertEqual(runtime.snapshot_lock_states, [True])
        self.assertTrue(searched.response_media_snapshot_captured)
        self.assertEqual(searched_snapshot["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(searched_snapshot["chapter"], "4力法")
        self.assertEqual(searched_snapshot["candidate_count"], 1)
        self.assertEqual(searched_snapshot["search_id"], searched.protocol["search_id"])

        answered = runtime.handle_text(session_id, "就这个")

        self.assertEqual(runtime.snapshot_lock_states, [True, True])
        self.assertEqual(answered.response_snapshot["phase"], "ANSWERED")
        live_snapshot = runtime.session_snapshot(session_id)
        self.assertEqual(runtime.snapshot_lock_states, [True, True, False])
        self.assertEqual(live_snapshot["phase"], "ANSWERED")
        self.assertEqual(searched.response_snapshot, searched_snapshot)
        self.assertEqual(searched.response_snapshot["phase"], "WAIT_CANDIDATE_CHOICE")

    def test_typed_response_snapshot_is_frozen_under_the_existing_a2_lock(self):
        session_id = "typed-response-snapshot-lock-session"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))

        class TypedCaptureRuntime(AgentSessionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.capture_lock_states = []
                self.response_frozen_flags = []

            def session_snapshot(self, _session_id):
                raise AssertionError("JSON response capture must not live-read session_snapshot")

            def _response_snapshot_v1_locked(
                self,
                observed_session_id,
                *,
                capabilities,
                response_frozen=False,
            ):
                clean = self._clean_session_id(observed_session_id)
                lock = self._session_locks[hash(clean) % len(self._session_locks)]
                self.capture_lock_states.append(lock.locked())
                self.response_frozen_flags.append(response_frozen)
                return super()._response_snapshot_v1_locked(
                    clean,
                    capabilities=capabilities,
                    response_frozen=response_frozen,
                )

        runtime = TypedCaptureRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=FakeTools().toolbox(),
                use_llm_intent=False,
            ),
        )
        capabilities = TaskStateEntryCapabilities(
            trusted_image_event=True,
            reset_session_available=True,
        )

        response = runtime.handle_image(
            session_id,
            self.source_image,
            task_state_capabilities=capabilities,
        )

        self.assertEqual(runtime.capture_lock_states, [True])
        self.assertEqual(runtime.response_frozen_flags, [True])
        self.assertIsNotNone(response.response_task_state_snapshot)
        frozen = response.response_task_state_snapshot.to_dict()
        self.assertEqual(response.response_snapshot["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(
            frozen["active_child_task"]["phase"],
            "WAIT_CANDIDATE_CHOICE",
        )
        self.assertTrue(response.response_media_snapshot_captured)

        live = self.store.load(session_id)
        self.assertIsNotNone(live)
        live.phase = "ANSWERED"
        self.store.save(live)
        self.assertEqual(
            response.response_task_state_snapshot.to_dict()["active_child_task"]["phase"],
            "WAIT_CANDIDATE_CHOICE",
        )

    def test_a2_business_error_carries_mutated_frozen_state_after_one_load(self):
        session_id = "typed-business-error-single-read"
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))

        class CountingStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0

            def load(self, observed_session_id):
                self.load_count += 1
                return super().load(observed_session_id)

        class MutatingFailingAgent:
            def __init__(self, state):
                self.state = state
                self.progress_reporter = None

            def handle_text(self, _text):
                self.state.set_chapter("4力法")
                raise RuntimeError("private provider failure")

        store = CountingStore(self.database_path)
        persisted_image = self.artifacts.persist_image(session_id, self.source_image)
        persisted = AgentState(session_id=session_id)
        persisted.start_search(
            str(persisted_image),
            search_id="search_single_read_error_01",
        )
        persisted.set_analysis(loads=[{"type": "集中", "raw": "P"}])
        store.save(persisted)
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=MutatingFailingAgent,
        )

        with self.assertRaisesRegex(RuntimeError, "private provider failure") as raised:
            runtime.handle_text(
                session_id,
                "第四章",
                task_state_capabilities=TaskStateEntryCapabilities(
                    reset_session_available=True,
                ),
            )

        self.assertEqual(store.load_count, 1)
        error = raised.exception
        self.assertEqual(error.response_snapshot["phase"], "READY_TO_ROUTE")
        self.assertEqual(error.response_snapshot["chapter"], "4力法")
        frozen = error.response_task_state_snapshot
        self.assertIsNotNone(frozen)
        self.assertEqual(frozen.consistency.status, "OK")
        self.assertEqual(frozen.active_child_task.phase, "READY_TO_ROUTE")
        self.assertEqual(frozen.active_child_task.chapter, "4力法")
        self.assertEqual(
            frozen.active_child_task.task_id,
            "search_single_read_error_01",
        )
        # The failure did not checkpoint the mutation; the response snapshot is
        # therefore provably carried from memory rather than reread from SQLite.
        live = SQLiteSessionStore.load(store, session_id)
        self.assertEqual(live.phase, "WAIT_CHAPTER")
        self.assertEqual(live.chapter, "")

    def test_image_error_routes_forward_capabilities_and_avoid_a_second_load(self):
        class CountingStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0

            def load(self, observed_session_id):
                self.load_count += 1
                return super().load(observed_session_id)

        class FailingImageAgent:
            def __init__(self, state):
                self.state = state
                self.progress_reporter = None

            def handle_image(self, image_path, *, search_id, **_kwargs):
                self.state.start_search(str(image_path), search_id=search_id)
                self.state.set_analysis(loads=[{"type": "集中", "raw": "P"}])
                raise RuntimeError("private image provider failure")

        decision = SimpleNamespace(handoff=SimpleNamespace(route="A2"))
        cases = (
            ("direct", {}),
            (
                "authoritative",
                {
                    "image_triage_authority": SimpleNamespace(
                        decide=lambda _path: decision,
                    )
                },
            ),
            ("screened", {"external_load_screen": lambda _path: "yes"}),
        )
        for label, route_kwargs in cases:
            with self.subTest(route=label):
                session_id = f"typed-image-error-{label}"
                self.addCleanup(lambda value=session_id: self.artifacts.clear_session(value))
                store = CountingStore(self.database_path)
                runtime = AgentSessionRuntime(
                    store,
                    artifacts=self.artifacts,
                    task_logger=self.logger,
                    agent_factory=FailingImageAgent,
                    **route_kwargs,
                )
                try:
                    with self.assertRaisesRegex(RuntimeError, "private image") as raised:
                        runtime.handle_image(
                            session_id,
                            self.source_image,
                            task_state_capabilities=TaskStateEntryCapabilities(
                                trusted_image_event=True,
                                reset_session_available=True,
                            ),
                        )
                finally:
                    if runtime._image_executor is not None:
                        runtime._image_executor.shutdown(wait=True)
                    if runtime._observer_executor is not None:
                        runtime._observer_executor.shutdown(wait=True)

                self.assertEqual(store.load_count, 1)
                error = raised.exception
                self.assertEqual(error.response_snapshot["phase"], "WAIT_CHAPTER")
                frozen = error.response_task_state_snapshot
                self.assertIsNotNone(frozen)
                self.assertEqual(frozen.consistency.status, "OK")
                self.assertEqual(frozen.active_child_task.phase, "WAIT_CHAPTER")
                self.assertTrue(frozen.active_child_task.task_id.startswith("search_"))

    def test_a2_unreadable_initial_load_carries_inconsistent_v1_without_reread(self):
        class UnreadableStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0

            def load(self, _session_id):
                self.load_count += 1
                raise RuntimeError("private malformed state details")

        store = UnreadableStore(self.database_path)
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=lambda _state: (_ for _ in ()).throw(
                AssertionError("unreadable state must not construct an agent")
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "private malformed") as raised:
            runtime.handle_text(
                "typed-unreadable-error-single-read",
                "继续",
                task_state_capabilities=TaskStateEntryCapabilities(
                    reset_session_available=True,
                ),
            )

        self.assertEqual(store.load_count, 1)
        error = raised.exception
        self.assertEqual(error.response_snapshot, {"session_valid": False})
        frozen = error.response_task_state_snapshot
        self.assertIsNotNone(frozen)
        self.assertEqual(frozen.consistency.status, "INCONSISTENT")
        self.assertEqual(frozen.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertEqual(frozen.active_child_task.status, "INCONSISTENT")
        self.assertNotEqual(frozen, empty_task_state_snapshot())

    def test_session_response_purge_failure_carries_unreadable_v1_without_load(self):
        failure = RuntimeError("private purge database failure")

        class PurgeFailingStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.purge_count = 0
                self.load_count = 0

            def purge_expired(self):
                self.purge_count += 1
                raise failure

            def load(self, observed_session_id):
                del observed_session_id
                self.load_count += 1
                raise AssertionError("failed purge must not be repaired by a live load")

        store = PurgeFailingStore(self.database_path)
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
        )

        with self.assertRaises(RuntimeError) as raised:
            runtime.session_response_snapshot_v1(
                "purge-failure-session",
                capabilities=TaskStateEntryCapabilities(
                    reset_session_available=True,
                ),
                response_frozen=True,
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(store.purge_count, 1)
        self.assertEqual(store.load_count, 0)
        self.assertEqual(failure.response_snapshot, {"session_valid": False})
        frozen = failure.response_task_state_snapshot
        self.assertIs(type(frozen), TaskStateSnapshotV1)
        self.assertEqual(frozen.consistency.status, "INCONSISTENT")
        self.assertEqual(frozen.consistency.codes, ("CHILD_STATE_UNREADABLE",))
        self.assertEqual(frozen.active_child_task.status, "INCONSISTENT")
        self.assertTrue(getattr(failure, "_response_snapshot_attempted", False))

    def test_a2_queue_rejections_do_not_read_or_carry_typed_state(self):
        class CountingStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0

            def load(self, observed_session_id):
                self.load_count += 1
                return super().load(observed_session_id)

        cases = (
            ("full", 0, 1.0, "QUEUE_FULL"),
            ("timeout", 1, 0.01, "QUEUE_TIMEOUT"),
        )
        for label, max_queued, wait_seconds, expected_code in cases:
            with self.subTest(case=label):
                store = CountingStore(self.database_path)
                runtime = AgentSessionRuntime(
                    store,
                    artifacts=self.artifacts,
                    task_logger=self.logger,
                    max_concurrent_tasks=1,
                    max_queued_tasks=max_queued,
                    queue_wait_seconds=wait_seconds,
                )
                held = runtime._execution_gate.enter(session_key=object())
                held.__enter__()
                try:
                    with self.assertRaises(AgentRuntimeBusyError) as raised:
                        runtime.handle_text(
                            f"typed-queue-{label}",
                            "继续",
                            task_state_capabilities=TaskStateEntryCapabilities(
                                reset_session_available=True,
                            ),
                        )
                finally:
                    held.__exit__(None, None, None)

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(store.load_count, 0)
                self.assertIsNone(raised.exception.response_task_state_snapshot)
                self.assertFalse(raised.exception.response_snapshot["session_valid"])
                self.assertEqual(raised.exception.response_snapshot["phase"], "IDLE")

    def test_clear_store_failure_captures_post_attempt_state_once_under_lock(self):
        session_id = "clear-store-failure-post-attempt"
        failure = RuntimeError("private store clear failure")

        class FailingClearStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0
                self.load_lock_states = []
                self.lock_probe = lambda: False

            def load(self, observed_session_id):
                self.load_count += 1
                self.load_lock_states.append(self.lock_probe())
                return super().load(observed_session_id)

            def clear(self, _session_id):
                raise failure

        store = FailingClearStore(self.database_path)
        self.addCleanup(lambda: SQLiteSessionStore.clear(store, session_id))
        self.addCleanup(lambda: self.artifacts.clear_session(session_id))
        persisted_image = self.artifacts.persist_image(session_id, self.source_image)
        state = AgentState(session_id=session_id)
        state.start_search(
            str(persisted_image),
            search_id="search_clear_store_failure_01",
        )
        state.set_analysis(loads=[{"type": "集中", "raw": "P"}])
        store.save(state)
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
        )
        store.lock_probe = lambda: runtime._lock(session_id).locked()

        with self.assertRaises(RuntimeError) as raised:
            runtime.clear(
                session_id,
                task_state_capabilities=TaskStateEntryCapabilities(
                    reset_session_available=True,
                ),
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(store.load_count, 1)
        self.assertEqual(store.load_lock_states, [True])
        self.assertEqual(failure.response_snapshot["phase"], "WAIT_CHAPTER")
        frozen = failure.response_task_state_snapshot
        self.assertIsNotNone(frozen)
        self.assertEqual(frozen.consistency.status, "OK")
        self.assertEqual(frozen.active_child_task.phase, "WAIT_CHAPTER")
        self.assertEqual(
            frozen.active_child_task.task_id,
            "search_clear_store_failure_01",
        )

    def test_deferred_clear_failure_leaves_the_combined_capture_to_a3(self):
        failure = RuntimeError("private nested clear failure")

        class FailingClearStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0

            def load(self, observed_session_id):
                self.load_count += 1
                return super().load(observed_session_id)

            def clear(self, _session_id):
                raise failure

        store = FailingClearStore(self.database_path)
        runtime = AgentSessionRuntime(
            store,
            artifacts=self.artifacts,
            task_logger=self.logger,
        )

        with runtime._defer_error_response_snapshot_capture():
            with self.assertRaises(RuntimeError) as raised:
                runtime.clear(
                    "deferred-clear-failure",
                    task_state_capabilities=TaskStateEntryCapabilities(
                        reset_session_available=True,
                    ),
                )

        self.assertIs(raised.exception, failure)
        self.assertEqual(store.load_count, 0)
        self.assertFalse(hasattr(failure, "response_snapshot"))
        self.assertFalse(hasattr(failure, "response_task_state_snapshot"))

    def test_clear_artifact_failure_captures_canonical_empty_once_under_lock(self):
        session_id = "clear-artifact-failure-post-attempt"
        failure = RuntimeError("private artifact cleanup failure")

        class CountingStore(SQLiteSessionStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_count = 0
                self.load_lock_states = []
                self.lock_probe = lambda: False

            def load(self, observed_session_id):
                self.load_count += 1
                self.load_lock_states.append(self.lock_probe())
                return super().load(observed_session_id)

        class FailingArtifacts(SessionArtifacts):
            def clear_session(self, _session_id):
                raise failure

        store = CountingStore(self.database_path)
        artifact_root = RUNTIME_DIR / f"clear_failure_artifacts_{uuid4().hex}"
        artifacts = FailingArtifacts(artifact_root)
        self.addCleanup(
            lambda: SessionArtifacts.clear_session(artifacts, session_id)
        )
        persisted_image = artifacts.persist_image(session_id, self.source_image)
        state = AgentState(session_id=session_id)
        state.start_search(
            str(persisted_image),
            search_id="search_clear_artifact_failure_01",
        )
        state.set_analysis(loads=[{"type": "集中", "raw": "P"}])
        store.save(state)
        runtime = AgentSessionRuntime(
            store,
            artifacts=artifacts,
            task_logger=self.logger,
        )
        store.lock_probe = lambda: runtime._lock(session_id).locked()

        with self.assertRaises(RuntimeError) as raised:
            runtime.clear(
                session_id,
                task_state_capabilities=TaskStateEntryCapabilities(
                    reset_session_available=True,
                ),
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(store.load_count, 1)
        self.assertEqual(store.load_lock_states, [True])
        self.assertEqual(
            failure.response_snapshot,
            runtime._legacy_session_snapshot_from_state(None),
        )
        self.assertEqual(
            failure.response_task_state_snapshot,
            empty_task_state_snapshot(),
        )
        self.assertIsNone(SQLiteSessionStore.load(store, session_id))
        self.assertTrue(artifacts.session_dir(session_id).exists())

    def test_clear_waits_for_same_session_task_then_removes_its_state(self):
        started = threading.Event()
        release = threading.Event()
        cleared = threading.Event()

        class BlockingAgent:
            def __init__(self, state: AgentState):
                self.state = state
                self.progress_reporter = None

            def handle_text(self, _text: str) -> AgentResponse:
                started.set()
                release.wait(1)
                return AgentResponse(text="done", intent="clarification")

        runtime = AgentSessionRuntime(
            self.store,
            artifacts=self.artifacts,
            task_logger=self.logger,
            agent_factory=BlockingAgent,
        )
        worker = threading.Thread(target=lambda: runtime.handle_text("locked-session", "run"))
        worker.start()
        self.assertTrue(started.wait(1))

        def clear_session():
            runtime.clear("locked-session")
            cleared.set()

        clearer = threading.Thread(target=clear_session)
        clearer.start()
        self.assertFalse(cleared.wait(0.05))
        release.set()
        worker.join(1)
        clearer.join(1)
        self.assertTrue(cleared.is_set())
        self.assertIsNone(self.store.load("locked-session"))


if __name__ == "__main__":
    unittest.main()

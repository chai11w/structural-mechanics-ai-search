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
    _ExecutionGate,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import TaskLogEntry, TaskLogger
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

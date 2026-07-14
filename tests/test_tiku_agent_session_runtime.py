import unittest
from pathlib import Path
from uuid import uuid4

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import TaskLogEntry, TaskLogger
from tiku_agent.tools import ToolResult


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

    def test_cancel_clears_persisted_session(self):
        session_id = "cancel-session"
        self.runtime.handle_image(session_id, self.source_image)

        response = self.runtime.handle_text(session_id, "取消")

        self.assertEqual(response.intent, "cancel")
        self.assertIsNone(self.store.load(session_id))
        self.assertFalse(self.artifacts.session_dir(session_id).exists())
        self.assertEqual(self.logger.entries[-1].outcome, "cancelled")

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


if __name__ == "__main__":
    unittest.main()

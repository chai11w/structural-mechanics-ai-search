import unittest
from pathlib import Path
from uuid import uuid4

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
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
            rerank_candidates=lambda *_args, **_kwargs: ToolResult(
                ok=True,
                data={"reranked": False, "visible_candidates": [{"rank": 1, "path": "bank/q1.jpg", "name": "q1.jpg", "score": 0.9}]},
            ),
            answer_candidate=lambda *_args, **_kwargs: ToolResult(ok=True, data={"copied_paths": ["answers/q1.jpg"]}),
        )

    @staticmethod
    def _unreachable():
        raise AssertionError("single scope analysis should avoid duplicate image analysis")


class AgentSessionRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.database_path = RUNTIME_DIR / f"session_runtime_test_{uuid4().hex}.db"
        self.addCleanup(lambda: self.database_path.unlink(missing_ok=True))
        tools = FakeTools().toolbox()
        self.store = SQLiteSessionStore(self.database_path)
        self.runtime = AgentSessionRuntime(
            self.store,
            agent_factory=lambda state: TikuSearchAgent(state=state, tools=tools, use_llm_intent=False),
        )

    def test_restart_can_resume_unique_candidate_with_natural_confirmation(self):
        session_id = "resume-session"
        first = self.runtime.handle_image(session_id, "question.jpg")
        self.assertEqual(first.state["phase"], "WAIT_CANDIDATE_CHOICE")

        restarted_runtime = AgentSessionRuntime(
            self.store,
            agent_factory=lambda state: TikuSearchAgent(state=state, tools=FakeTools().toolbox(), use_llm_intent=False),
        )
        answer = restarted_runtime.handle_text(session_id, "就这个")

        self.assertEqual(answer.state["phase"], "ANSWERED")
        self.assertEqual(answer.images, ["answers/q1.jpg"])
        self.assertEqual(self.store.load(session_id).last_answer_paths, ["answers/q1.jpg"])

    def test_cancel_clears_persisted_session(self):
        session_id = "cancel-session"
        self.runtime.handle_image(session_id, "question.jpg")

        response = self.runtime.handle_text(session_id, "取消")

        self.assertEqual(response.intent, "cancel")
        self.assertIsNone(self.store.load(session_id))


if __name__ == "__main__":
    unittest.main()

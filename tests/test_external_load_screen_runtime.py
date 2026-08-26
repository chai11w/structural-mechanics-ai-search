from __future__ import annotations

from pathlib import Path
import shutil
import threading
import time
import unittest
from uuid import uuid4

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.external_load_screen import NO_EXTERNAL_LOAD_MESSAGE
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import TaskLogEntry, TaskLogger
from tiku_agent.tools import ToolResult
from tiku_shared.trace_context import TraceContext, trace_context_scope


ROOT = Path(__file__).resolve().parents[1]


class RecordingTaskLogger(TaskLogger):
    def __init__(self) -> None:
        self.entries: list[TaskLogEntry] = []
        self.event = threading.Event()

    def write(self, entry: TaskLogEntry) -> None:
        self.entries.append(entry)
        self.event.set()


def delayed_screen(delay: float, verdict: str | BaseException):
    def screen(_image_path: str | Path) -> str:
        time.sleep(delay)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    return screen


class ExternalLoadScreenRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / f".tmp_external_load_runtime_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.source = self.root / "source.jpg"
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(b"fake image")

    def build_runtime(
        self,
        *,
        screen,
        chapter: str = "",
        analysis_delay: float = 0.0,
        timeout: float = 0.2,
    ) -> tuple[AgentSessionRuntime, RecordingTaskLogger]:
        logger = RecordingTaskLogger()

        def analyze_multi(*_args, **_kwargs):
            time.sleep(analysis_delay)
            return ToolResult.success(
                code="SINGLE",
                data={
                    "is_multi": False,
                    "single_analysis": {
                        "loads": [{"type": "集中", "raw": "P"}],
                        "chapter_hint": chapter or "unknown",
                    },
                },
            )

        tools = AgentToolbox(
            analyze_multi_image=analyze_multi,
            analyze_image=lambda *_args, **_kwargs: ToolResult.success(
                code="ANALYZED",
                data={
                    "image_path": str(self.source),
                    "loads": [{"type": "集中", "raw": "P"}],
                    "chapter": chapter,
                },
            ),
            route_bank=lambda *_args, **_kwargs: ToolResult.success(
                code="ROUTED", data={"route": "main"}
            ),
            classify_structure=lambda *_args, **_kwargs: ToolResult.success(
                code="STRUCTURE", data={"structure_type": ""}
            ),
            coarse_search=lambda *_args, **_kwargs: ToolResult.success(
                code="COARSE",
                data={
                    "candidates": [
                        {"rank": 1, "path": "bank/q1.jpg", "name": "q1.jpg"}
                    ]
                },
            ),
            rerank_candidates=lambda *_args, **_kwargs: ToolResult.success(
                code="RERANKED",
                data={
                    "visible_candidates": [
                        {"rank": 1, "path": "bank/q1.jpg", "name": "q1.jpg"}
                    ]
                },
            ),
        )
        artifacts = SessionArtifacts(self.root / "sessions")
        runtime = AgentSessionRuntime(
            SQLiteSessionStore(self.root / "session.db"),
            artifacts=artifacts,
            task_logger=logger,
            agent_factory=lambda state: TikuSearchAgent(
                state=state, tools=tools, use_llm_intent=False
            ),
            external_load_screen=screen,
            external_load_timeout_seconds=timeout,
        )
        return runtime, logger

    def test_no_first_returns_fallback_and_late_search_cannot_overwrite_state(self):
        runtime, logger = self.build_runtime(
            screen=delayed_screen(0.01, "no"),
            chapter="4力法",
            analysis_delay=0.08,
        )

        response = runtime.handle_image("no-first", self.source)

        self.assertEqual(response.intent, "external_load_screen")
        self.assertEqual(response.text, NO_EXTERNAL_LOAD_MESSAGE)
        self.assertEqual(
            response.text,
            "未识别到图片中的外荷载，暂时无法检索。请重新上传外荷载清晰可见的题图。",
        )
        self.assertEqual(runtime.store.load("no-first").candidate_count, 0)
        self.assertTrue(logger.event.wait(1))
        self.assertEqual(runtime.store.load("no-first").candidate_count, 0)

    def test_candidates_first_return_immediately_and_late_no_is_ignored(self):
        release_screen = threading.Event()

        def blocked_no(_image_path: str | Path) -> str:
            release_screen.wait(1)
            return "no"

        runtime, logger = self.build_runtime(
            screen=blocked_no, chapter="4力法"
        )

        response_box = []
        finished = threading.Event()
        trace_id = "trace_11111111111111111111111111111111"
        request_id = "req_11111111111111111111111111111111"

        def run_search() -> None:
            with trace_context_scope(TraceContext(trace_id, request_id=request_id)):
                response_box.append(
                    runtime.handle_image(
                        "candidate-first",
                        self.source,
                        request_id=request_id,
                    )
                )
            finished.set()

        worker = threading.Thread(target=run_search)
        worker.start()
        try:
            self.assertTrue(finished.wait(0.5))
            self.assertFalse(release_screen.is_set())
            response = response_box[0]
            self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")
            self.assertEqual(runtime.store.load("candidate-first").candidate_count, 1)
        finally:
            release_screen.set()
            worker.join(1)

        self.assertTrue(logger.event.wait(1))
        self.assertEqual(logger.entries[-1].trace_id, trace_id)
        self.assertEqual(runtime.store.load("candidate-first").candidate_count, 1)

    def test_chapter_prompt_waits_for_no_and_is_replaced(self):
        runtime, _ = self.build_runtime(screen=delayed_screen(0.04, "no"))

        response = runtime.handle_image("chapter-no", self.source)

        self.assertEqual(response.intent, "external_load_screen")
        self.assertEqual(response.text, NO_EXTERNAL_LOAD_MESSAGE)

    def test_chapter_prompt_waits_for_yes_then_uses_original_result(self):
        runtime, _ = self.build_runtime(screen=delayed_screen(0.04, "yes"))

        response = runtime.handle_image("chapter-yes", self.source)

        self.assertEqual(response.intent, "search_image")
        self.assertEqual(response.state["phase"], "WAIT_CHAPTER")

    def test_screen_error_falls_back_to_original_result(self):
        runtime, _ = self.build_runtime(
            screen=delayed_screen(0.02, RuntimeError("provider failed"))
        )

        response = runtime.handle_image("screen-error", self.source)

        self.assertEqual(response.state["phase"], "WAIT_CHAPTER")

    def test_screen_timeout_revokes_late_no_authority(self):
        runtime, logger = self.build_runtime(
            screen=delayed_screen(0.08, "no"), timeout=0.02
        )

        started = time.perf_counter()
        response = runtime.handle_image("screen-timeout", self.source)

        self.assertLess(time.perf_counter() - started, 0.06)
        self.assertEqual(response.state["phase"], "WAIT_CHAPTER")
        self.assertTrue(logger.event.wait(1))
        self.assertEqual(runtime.store.load("screen-timeout").phase, "WAIT_CHAPTER")

    def test_clear_waits_for_discarded_background_search(self):
        runtime, logger = self.build_runtime(
            screen=delayed_screen(0.01, "no"),
            chapter="4力法",
            analysis_delay=0.08,
        )
        runtime.handle_image("clear-after-no", self.source)
        cleared = threading.Event()

        def clear_session() -> None:
            runtime.clear("clear-after-no")
            cleared.set()

        worker = threading.Thread(target=clear_session)
        worker.start()
        self.assertFalse(cleared.wait(0.02))
        self.assertTrue(logger.event.wait(1))
        worker.join(1)

        self.assertTrue(cleared.is_set())
        self.assertIsNone(runtime.store.load("clear-after-no"))
        self.assertFalse(runtime.artifacts.session_dir("clear-after-no").exists())


if __name__ == "__main__":
    unittest.main()

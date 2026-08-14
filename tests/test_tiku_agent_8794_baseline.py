from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.run_tiku_agent_8794 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE as CANDIDATE_SESSION_COOKIE,
    build_argument_parser,
    build_app as build_8794_app,
    build_runtime,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE as MAINLINE_SESSION_COOKIE, create_app
from tiku_agent.state import AgentState


class RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.snapshot = {
            "session_valid": False,
            "phase": "IDLE",
            "has_active_image": False,
            "task_revision": 0,
            "candidate_generation": "",
            "candidate_count": 0,
        }

    def handle_text(self, session_id: str, text: str, *, progress=None) -> AgentResponse:
        self.calls.append(("text", session_id, text))
        self.snapshot.update({"session_valid": True, "phase": "WAIT_CHAPTER"})
        return AgentResponse(text="请告诉我题目章节。", intent="provide_chapter")

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        self.calls.append(("session", session_id, ""))
        return dict(self.snapshot)

    def current_image_path(self, session_id: str):
        return None

    def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id, ""))

    def resolve_upload(self, session_id: str, filename: str):
        return None

    def resolve_media(self, session_id: str, filename: str):
        return None


class Candidate8794BaselineTest(unittest.TestCase):
    def test_launcher_enables_safe_answers_by_default_with_explicit_rollback(self):
        parser = build_argument_parser()

        self.assertTrue(parser.parse_args([]).enable_safe_answer_v0)
        self.assertTrue(
            parser.parse_args(["--enable-safe-answer-v0"]).enable_safe_answer_v0
        )
        self.assertFalse(
            parser.parse_args(["--disable-safe-answer-v0"]).enable_safe_answer_v0
        )
        self.assertTrue(parser.parse_args([]).enable_dimension_filter)
        self.assertFalse(
            parser.parse_args(["--disable-dimension-filter"]).enable_dimension_filter
        )
        self.assertTrue(parser.parse_args([]).enable_external_load_screen)
        self.assertFalse(
            parser.parse_args(
                ["--disable-external-load-screen"]
            ).enable_external_load_screen
        )
        self.assertEqual(parser.parse_args([]).external_load_timeout_seconds, 15.0)

    def test_runtime_uses_only_the_candidate_root(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))

        runtime = build_runtime(root, enable_external_load_screen=False)

        self.assertEqual(runtime.store.database_path.resolve(), (root / "session.db").resolve())
        self.assertEqual(runtime.artifacts.root, (root / "sessions").resolve())
        self.assertEqual(runtime.task_logger.path.resolve(), (root / "task_logs.jsonl").resolve())
        self.assertEqual(DEFAULT_PORT, 8794)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_candidate_8794")
        self.assertNotEqual(CANDIDATE_SESSION_COOKIE, MAINLINE_SESSION_COOKIE)
        default_agent = runtime._make_agent(AgentState(session_id="default-off"))
        self.assertFalse(default_agent.enable_safe_answer_v0)
        self.assertIsNone(default_agent.safe_answer_generator_v0)

    def test_enabled_runtime_injects_generator_only_for_safe_conversation(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        requests = []

        def model_client(request):
            requests.append(request)
            return "我是力答，专注结构力学题库搜索，通过题图检索相似候选题。"

        runtime = build_runtime(
            root,
            enable_safe_answer_v0=True,
            safe_answer_model_client=model_client,
            enable_external_load_screen=False,
        )

        safe_response = runtime.handle_text("safe-session", "你是谁")
        business_response = runtime.handle_text("business-session", "帮我搜个题")

        self.assertEqual(safe_response.intent, "safe_answer")
        self.assertEqual(safe_response.reply_source, "model")
        self.assertEqual(len(requests), 1)
        self.assertNotEqual(business_response.intent, "safe_answer")
        self.assertEqual(runtime.session_snapshot("safe-session")["phase"], "IDLE")

    def test_dimension_filter_can_be_enabled_without_sharing_runtime_state(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))

        runtime = build_runtime(
            root,
            enable_dimension_filter=True,
            enable_external_load_screen=False,
        )
        agent = runtime._make_agent(AgentState(session_id="dimension-on"))

        self.assertTrue(agent.config.dimension_filter_enabled)
        self.assertEqual(agent.config.runtime_dir.resolve(), root.resolve())
        self.assertEqual(
            agent.config.session_dir.resolve(),
            runtime.artifacts.session_dir("dimension-on").resolve(),
        )

    def test_external_load_screen_is_isolated_to_8794_and_can_be_injected(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        screen = lambda _path: "yes"

        runtime = build_runtime(root, external_load_screen=screen)

        self.assertIs(runtime.external_load_screen, screen)
        self.assertEqual(runtime.external_load_timeout_seconds, 15.0)

    def test_candidate_http_behavior_matches_mainline_with_separate_cookie(self):
        mainline_runtime = RecordingRuntime()
        candidate_runtime = RecordingRuntime()
        runtime_dir = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(runtime_dir, ignore_errors=True))
        mainline = TestClient(create_app(runtime=mainline_runtime))
        candidate = TestClient(build_8794_app(runtime_dir, runtime=candidate_runtime))
        mainline.cookies.set(MAINLINE_SESSION_COOKIE, "parity-session", domain="testserver.local", path="/")
        candidate.cookies.set(CANDIDATE_SESSION_COOKIE, "parity-session", domain="testserver.local", path="/")

        self.assertEqual(mainline.get("/health").json(), candidate.get("/health").json())
        self.assertEqual(mainline.get("/").text, candidate.get("/").text)
        self.assertEqual(mainline.get("/api/session").json(), candidate.get("/api/session").json())
        mainline_message = mainline.post("/api/message", json={"text": "4"}).json()
        candidate_message = candidate.post("/api/message", json={"text": "4"}).json()
        self.assertTrue(mainline_message.pop("request_id").startswith("req_"))
        self.assertTrue(candidate_message.pop("request_id").startswith("req_"))
        self.assertEqual(mainline_message, candidate_message)
        self.assertEqual(mainline_runtime.calls, candidate_runtime.calls)
        mainline_cookie_names = {cookie.name for cookie in mainline.cookies.jar}
        candidate_cookie_names = {cookie.name for cookie in candidate.cookies.jar}
        self.assertIn(MAINLINE_SESSION_COOKIE, mainline_cookie_names)
        self.assertNotIn(CANDIDATE_SESSION_COOKIE, mainline_cookie_names)
        self.assertIn(CANDIDATE_SESSION_COOKIE, candidate_cookie_names)
        self.assertNotIn(MAINLINE_SESSION_COOKIE, candidate_cookie_names)


if __name__ == "__main__":
    unittest.main()

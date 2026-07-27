from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.run_tiku_agent_8794 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE as CANDIDATE_SESSION_COOKIE,
    build_app as build_8794_app,
    build_runtime,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE as MAINLINE_SESSION_COOKIE, create_app


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
    def test_runtime_uses_only_the_candidate_root(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8794_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))

        runtime = build_runtime(root)

        self.assertEqual(runtime.store.database_path.resolve(), (root / "session.db").resolve())
        self.assertEqual(runtime.artifacts.root, (root / "sessions").resolve())
        self.assertEqual(runtime.task_logger.path.resolve(), (root / "task_logs.jsonl").resolve())
        self.assertEqual(DEFAULT_PORT, 8794)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_candidate_8794")
        self.assertNotEqual(CANDIDATE_SESSION_COOKIE, MAINLINE_SESSION_COOKIE)

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
        self.assertEqual(
            mainline.post("/api/message", json={"text": "4"}).json(),
            candidate.post("/api/message", json={"text": "4"}).json(),
        )
        self.assertEqual(mainline_runtime.calls, candidate_runtime.calls)
        mainline_cookie_names = {cookie.name for cookie in mainline.cookies.jar}
        candidate_cookie_names = {cookie.name for cookie in candidate.cookies.jar}
        self.assertIn(MAINLINE_SESSION_COOKIE, mainline_cookie_names)
        self.assertNotIn(CANDIDATE_SESSION_COOKIE, mainline_cookie_names)
        self.assertIn(CANDIDATE_SESSION_COOKIE, candidate_cookie_names)
        self.assertNotIn(MAINLINE_SESSION_COOKIE, candidate_cookie_names)


if __name__ == "__main__":
    unittest.main()

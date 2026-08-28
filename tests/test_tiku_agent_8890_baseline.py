import io
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from scripts.run_tiku_agent_8890 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE as VALIDATION_SESSION_COOKIE,
    build_app as build_8890_app,
    build_argument_parser,
    build_runtime,
)
from scripts.run_tiku_agent_demo import build_app as build_8790_app
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE as MAINLINE_SESSION_COOKIE
from tiku_agent.image_triage_shadow import ImageTriageShadowRuntime
from tiku_agent.session_runtime import SessionResponseSnapshotV1
from tiku_agent.state import AgentState
from tiku_agent.task_state_contract import empty_task_state_snapshot
from tiku_shared.response_store import is_valid_response_id


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
        self.image_capabilities = []

    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        progress=None,
        task_state_capabilities=None,
    ) -> AgentResponse:
        self.calls.append(("text", session_id, text))
        self.snapshot.update({"session_valid": True, "phase": "WAIT_CHAPTER"})
        response = AgentResponse(text="请告诉我题目章节。", intent="provide_chapter")
        response.response_snapshot = dict(self.snapshot)
        response.response_projection_snapshot = dict(self.snapshot)
        if task_state_capabilities is not None:
            response.response_task_state_snapshot = empty_task_state_snapshot()
        response.response_media_snapshot_captured = True
        return response

    def handle_image(
        self,
        session_id: str,
        image_path: Path,
        *,
        progress=None,
        task_state_capabilities=None,
        **_kwargs,
    ) -> AgentResponse:
        del progress
        self.calls.append(("image", session_id, Path(image_path).name))
        self.image_capabilities.append(task_state_capabilities)
        self.snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CHAPTER",
            "has_active_image": True,
            "task_revision": 1,
        })
        response = AgentResponse(text="请告诉我题目章节。", intent="search_image")
        response.response_snapshot = dict(self.snapshot)
        response.response_projection_snapshot = dict(self.snapshot)
        if task_state_capabilities is not None:
            response.response_task_state_snapshot = empty_task_state_snapshot()
        response.response_media_snapshot_captured = True
        return response

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        self.calls.append(("session", session_id, ""))
        return dict(self.snapshot)

    def current_image_path(self, session_id: str):
        return None

    def session_response_snapshot_v1(self, session_id: str, *, capabilities=None):
        del capabilities
        self.calls.append(("session", session_id, ""))
        return SessionResponseSnapshotV1(
            uploaded_image_path=None,
            legacy_session=dict(self.snapshot),
            task_state=empty_task_state_snapshot(),
        )

    def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id, ""))

    def resolve_upload(self, session_id: str, filename: str):
        return None

    def resolve_media(self, session_id: str, filename: str):
        return None


class Baseline8890Test(unittest.TestCase):
    def make_root(self, label: str) -> Path:
        root = Path(__file__).resolve().parents[1] / (
            f".tmp_test_8890_{label}_{uuid4().hex}"
        )
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_launcher_defaults_and_explicit_rollbacks(self):
        parser = build_argument_parser()
        defaults = parser.parse_args([])

        self.assertEqual(defaults.host, "127.0.0.1")
        self.assertEqual(defaults.port, 8890)
        self.assertEqual(DEFAULT_PORT, 8890)
        self.assertEqual(
            DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_validation_8890"
        )
        self.assertTrue(defaults.enable_safe_answer_v0)
        self.assertTrue(defaults.enable_dimension_filter)
        self.assertTrue(defaults.enable_external_load_screen)
        self.assertTrue(defaults.enable_image_triage_shadow)
        self.assertEqual(defaults.external_load_timeout_seconds, 15.0)
        self.assertEqual(defaults.image_triage_timeout_seconds, 120.0)
        self.assertFalse(
            parser.parse_args(["--disable-safe-answer-v0"]).enable_safe_answer_v0
        )
        self.assertFalse(
            parser.parse_args(["--disable-dimension-filter"]).enable_dimension_filter
        )
        self.assertFalse(
            parser.parse_args(
                ["--disable-external-load-screen"]
            ).enable_external_load_screen
        )
        self.assertFalse(
            parser.parse_args(
                ["--disable-image-triage-shadow"]
            ).enable_image_triage_shadow
        )

    def test_runtime_artifacts_are_all_under_8890_root(self):
        root = self.make_root("runtime")
        runtime = build_runtime(root, enable_external_load_screen=False)

        self.assertEqual(
            runtime.store.database_path.resolve(), (root / "session.db").resolve()
        )
        self.assertEqual(runtime.artifacts.root, (root / "sessions").resolve())
        self.assertEqual(
            runtime.task_logger.path.resolve(), (root / "task_logs.jsonl").resolve()
        )
        self.assertEqual(
            runtime.cost_ledger.path.resolve(),
            (root / "model_costs.sqlite3").resolve(),
        )
        self.assertEqual(
            runtime.triage_shadow.logger.path.resolve(),
            (root / "triage_shadow.jsonl").resolve(),
        )
        self.assertNotEqual(VALIDATION_SESSION_COOKIE, MAINLINE_SESSION_COOKIE)
        agent = runtime._make_agent(AgentState(session_id="8890-isolated"))
        self.assertEqual(agent.config.runtime_dir.resolve(), root.resolve())
        self.assertEqual(
            agent.config.session_dir.resolve(),
            runtime.artifacts.session_dir("8890-isolated").resolve(),
        )

    def test_app_binds_incoming_feedback_and_cookie_to_8890(self):
        root = self.make_root("app")
        runtime = RecordingRuntime()

        with patch("scripts.run_tiku_agent_8890.create_app") as create_app:
            build_8890_app(root, runtime=runtime)

        kwargs = create_app.call_args.kwargs
        self.assertIs(kwargs["runtime"], runtime)
        self.assertEqual(kwargs["incoming_dir"], root.resolve() / "incoming")
        self.assertEqual(kwargs["session_cookie"], VALIDATION_SESSION_COOKIE)
        self.assertEqual(
            kwargs["feedback_store"].path.resolve(),
            (root / "feedback.sqlite3").resolve(),
        )
        self.assertEqual(
            kwargs["feedback_store"].cases_root,
            (root / "feedback_cases").resolve(),
        )

    def test_fixed_line_mock_http_behavior_is_equivalent(self):
        mainline_runtime = RecordingRuntime()
        validation_runtime = RecordingRuntime()
        mainline_root = self.make_root("mainline")
        validation_root = self.make_root("validation")
        mainline = TestClient(
            build_8790_app(mainline_root, runtime=mainline_runtime)
        )
        validation = TestClient(
            build_8890_app(validation_root, runtime=validation_runtime)
        )
        mainline.cookies.set(
            MAINLINE_SESSION_COOKIE,
            "parity-session",
            domain="testserver.local",
            path="/",
        )
        validation.cookies.set(
            VALIDATION_SESSION_COOKIE,
            "parity-session",
            domain="testserver.local",
            path="/",
        )

        mainline_health = mainline.get("/health").json()
        validation_health = validation.get("/health").json()
        self.assertEqual(mainline_health["status"], validation_health["status"])
        self.assertEqual(mainline_health["trace_events"]["status"], "ok")
        self.assertEqual(validation_health["trace_events"]["status"], "disabled")
        self.assertEqual(mainline.get("/").text, validation.get("/").text)
        self.assertEqual(
            mainline.get("/api/session").json(),
            validation.get("/api/session").json(),
        )
        mainline_message = mainline.post("/api/message", json={"text": "4"}).json()
        validation_message = validation.post(
            "/api/message", json={"text": "4"}
        ).json()
        expected_task_state = empty_task_state_snapshot().to_dict()
        self.assertEqual(mainline_message["task_state"], expected_task_state)
        self.assertEqual(validation_message["task_state"], expected_task_state)
        self.assertTrue(mainline_message.pop("request_id").startswith("req_"))
        self.assertTrue(validation_message.pop("request_id").startswith("req_"))
        mainline_response_id = mainline_message.pop("response_id")
        validation_response_id = validation_message.pop("response_id")
        self.assertTrue(is_valid_response_id(mainline_response_id))
        self.assertTrue(is_valid_response_id(validation_response_id))
        self.assertNotEqual(mainline_response_id, validation_response_id)
        self.assertEqual(mainline_message, validation_message)
        self.assertEqual(mainline_runtime.calls, validation_runtime.calls)

        mainline_cookies = {cookie.name for cookie in mainline.cookies.jar}
        validation_cookies = {cookie.name for cookie in validation.cookies.jar}
        self.assertIn(MAINLINE_SESSION_COOKIE, mainline_cookies)
        self.assertNotIn(VALIDATION_SESSION_COOKIE, mainline_cookies)
        self.assertIn(VALIDATION_SESSION_COOKIE, validation_cookies)
        self.assertNotIn(MAINLINE_SESSION_COOKIE, validation_cookies)

    def test_image_shadow_wrapper_preserves_json_task_state_capabilities(self):
        class RecordingShadow:
            def __init__(self) -> None:
                self.calls = []

            def submit(self, image_path, *, request_id=""):
                self.calls.append((Path(image_path).name, request_id))
                return True

        delegate = RecordingRuntime()
        shadow = RecordingShadow()
        runtime = ImageTriageShadowRuntime(delegate, shadow)
        root = self.make_root("shadow-json")
        client = TestClient(build_8890_app(root, runtime=runtime))
        image = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(image, format="JPEG")

        response = client.post(
            "/api/image",
            content=image.getvalue(),
            headers={"x-filename": "question.jpg"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["task_state"],
            empty_task_state_snapshot().to_dict(),
        )
        self.assertEqual(len(shadow.calls), 1)
        self.assertEqual(len(delegate.image_capabilities), 1)
        capabilities = delegate.image_capabilities[0]
        self.assertTrue(capabilities.trusted_image_event)
        self.assertTrue(capabilities.reset_session_available)


if __name__ == "__main__":
    unittest.main()

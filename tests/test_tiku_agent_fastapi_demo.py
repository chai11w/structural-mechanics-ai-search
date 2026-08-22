import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import MAX_FEEDBACK_BYTES, MAX_IMAGE_BYTES, SESSION_COOKIE, _SCRIPT, _STYLE, _agent_payload, _write_incoming_image, create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore, scope_feedback_conversation
from tiku_agent.invite_access import InviteAccess, build_invitation_config
from tiku_agent.session_runtime import AgentBudgetExceededError, AgentRuntimeBusyError
from tiku_agent.user_output_integration import build_a2_output_draft, build_a3_output_draft
from tiku_shared.request_protocol import RequestProtocol


class FakeRuntime:
    def __init__(self, image_path: Path):
        self.image_path = image_path
        self.calls = []
        self.upload_session = ""
        self.media_session = ""
        self.last_identity = ""
        self.snapshot = {
            "session_valid": False,
            "phase": "IDLE",
            "has_active_image": False,
            "task_revision": 0,
            "candidate_generation": "",
            "candidate_count": 0,
        }

    def handle_text(self, session_id: str, text: str, *, identity_key="", progress=None, request_id="") -> AgentResponse:
        self.last_identity = identity_key
        self.calls.append(("text", session_id, text))
        if progress is not None:
            progress("searching", "正在按「4力法」搜索题目…")
        self.snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "candidate_generation": "fake-generation",
            "candidate_count": 1,
        })
        protocol = RequestProtocol.from_code(
            "COARSE_CANDIDATES_FOUND",
            request_id=request_id or "req_fake_text_stream",
        )
        return AgentResponse(
            text="LEGACY_STREAM_TEXT",
            images=[str(self.image_path)],
            intent="select_candidate",
            protocol=protocol.to_dict(),
            output=build_a2_output_draft(
                "search_image",
                {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 1},
                protocol,
            ),
        )

    def handle_image(self, session_id: str, image_path: Path, *, identity_key="", progress=None, request_id="") -> AgentResponse:
        self.last_identity = identity_key
        self.calls.append(("image", session_id, image_path.is_file()))
        self.upload_session = session_id
        self.snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CHAPTER",
            "has_active_image": True,
            "task_revision": self.snapshot["task_revision"] + 1,
            "candidate_generation": "",
            "candidate_count": 0,
        })
        if progress is not None:
            progress("searching", "正在按「4力法」搜索题目…")
        protocol = RequestProtocol.from_code(
            "CHAPTER_REQUIRED",
            request_id=request_id or "req_fake_image_stream",
        )
        return AgentResponse(
            text="LEGACY_STREAM_IMAGE_TEXT",
            intent="search_image",
            protocol=protocol.to_dict(),
            output=build_a2_output_draft(
                "search_image",
                {"phase": "WAIT_CHAPTER"},
                protocol,
            ),
        )

    def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id))
        self.snapshot.update({"session_valid": False, "phase": "IDLE", "has_active_image": False})

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        return dict(self.snapshot)

    def current_image_path(self, session_id: str) -> Path | None:
        return self.image_path if session_id == self.upload_session else None

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        if session_id != self.upload_session:
            return None
        return self.image_path if filename == self.image_path.name and self.image_path.is_file() else None

    def persist_media(self, session_id: str, source: Path) -> Path | None:
        self.media_session = session_id
        return source if source.is_file() else None

    def resolve_media(self, session_id: str, filename: str) -> Path | None:
        if session_id != self.media_session:
            return None
        return self.image_path if filename == self.image_path.name and self.image_path.is_file() else None

    def current_auto_crop_overlay_path(self, session_id: str) -> Path | None:
        return self.image_path if session_id == self.upload_session else None


class SelectiveMediaRuntime(FakeRuntime):
    def __init__(self, image_path: Path, *, failed_names: set[str]):
        super().__init__(image_path)
        self.failed_names = set(failed_names)

    def persist_media(self, session_id: str, source: Path) -> Path | None:
        if source.name in self.failed_names:
            return None
        return super().persist_media(session_id, source)


class FastApiDemoTest(unittest.TestCase):
    def test_unstructured_agent_response_fails_closed_and_keeps_request_ids(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"unstructured_output_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "private_candidate.jpg"
        Image.new("RGB", (8, 8), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        protocol = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED",
            request_id="req_unstructured_output",
            search_id="search_unstructured_output",
        )

        payload = _agent_payload(
            AgentResponse(
                text="Traceback PRIVATE_LEGACY_TEXT",
                images=[str(image_path)],
                intent="a3_session_reset",
                protocol=protocol.to_dict(),
                author_contact={"label": "private", "value": "secret"},
            ),
            runtime,
            "session_unstructured_output",
        )

        self.assertEqual(payload["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(payload["request_id"], "req_unstructured_output")
        self.assertEqual(payload["search_id"], "search_unstructured_output")
        self.assertEqual(payload["images"], [])
        self.assertEqual(payload["intent"], "")
        self.assertEqual(payload["author_contact"], {})
        self.assertNotIn("PRIVATE", json.dumps(payload, ensure_ascii=False))

    def test_structured_candidate_media_is_atomic_and_never_uses_legacy_text(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"structured_candidates_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        first = test_dir / "candidate_1.jpg"
        second = test_dir / "candidate_2.jpg"
        Image.new("RGB", (8, 8), "white").save(first)
        Image.new("RGB", (8, 8), "white").save(second)
        runtime = SelectiveMediaRuntime(first, failed_names={second.name})
        protocol = RequestProtocol.from_code(
            "COARSE_CANDIDATES_FOUND",
            request_id="req_candidate_atomic",
            search_id="search_candidate_atomic",
        )
        draft = build_a2_output_draft(
            "search_image",
            {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 2},
            protocol,
        )

        payload = _agent_payload(
            AgentResponse(
                text="LEGACY_TEXT_MUST_NOT_ESCAPE",
                images=[str(first), str(second)],
                intent="search_image",
                protocol=protocol.to_dict(),
                output=draft,
            ),
            runtime,
            "session_structured_candidates",
        )

        self.assertEqual(payload["code"], "MEDIA_NOT_FOUND")
        self.assertEqual(payload["message_key"], "system.media.not_found")
        self.assertEqual(payload["images"], [])
        self.assertNotIn("LEGACY_TEXT_MUST_NOT_ESCAPE", payload["text"])
        self.assertEqual(payload["failure"]["recovery_action"], "retry_request")

    def test_structured_answer_keeps_internal_expected_count_when_path_list_is_short(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"structured_answer_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        first = test_dir / "answer_1.jpg"
        second = test_dir / "answer_2.jpg"
        Image.new("RGB", (8, 8), "white").save(first)
        Image.new("RGB", (8, 8), "white").save(second)
        runtime = SelectiveMediaRuntime(first, failed_names=set())
        protocol = RequestProtocol.from_code(
            "ANSWER_FILES_FOUND",
            request_id="req_answer_partial",
            search_id="search_answer_partial",
        )
        draft = build_a2_output_draft(
            "select_candidate",
            {
                "phase": "ANSWERED",
                "delivered_image_count": 2,
                "selected_question": 1,
            },
            protocol,
        )

        payload = _agent_payload(
            AgentResponse(
                text="LEGACY_ANSWER_MUST_NOT_ESCAPE",
                images=[str(first)],
                intent="select_candidate",
                protocol=protocol.to_dict(),
                output=draft,
            ),
            runtime,
            "session_structured_answer",
        )

        self.assertEqual(payload["code"], "MEDIA_PERSIST_FAILED")
        self.assertEqual(payload["status"], "PARTIAL")
        self.assertEqual(payload["images"], [f"/api/media/{first.name}"])
        self.assertIn("共 1 张", payload["text"])
        self.assertIn("部分结果图片未能交付", payload["text"])
        self.assertNotIn("LEGACY_ANSWER_MUST_NOT_ESCAPE", payload["text"])
        self.assertIsNone(payload["failure"])

    def test_structured_answer_with_no_delivered_media_fails_closed(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"structured_answer_none_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        answer = test_dir / "answer.jpg"
        Image.new("RGB", (8, 8), "white").save(answer)
        runtime = SelectiveMediaRuntime(answer, failed_names={answer.name})
        protocol = RequestProtocol.from_code(
            "ANSWER_FILES_FOUND",
            request_id="req_answer_missing",
            search_id="search_answer_missing",
        )
        draft = build_a2_output_draft(
            "select_candidate",
            {"phase": "ANSWERED", "delivered_image_count": 1},
            protocol,
        )

        payload = _agent_payload(
            AgentResponse(
                text="LEGACY_ANSWER_MUST_NOT_ESCAPE",
                images=[str(answer)],
                intent="select_candidate",
                protocol=protocol.to_dict(),
                output=draft,
            ),
            runtime,
            "session_structured_answer_none",
        )

        self.assertEqual(payload["code"], "MEDIA_NOT_FOUND")
        self.assertEqual(payload["images"], [])
        self.assertNotIn("LEGACY_ANSWER_MUST_NOT_ESCAPE", payload["text"])

    def test_malformed_structured_protocol_returns_safe_service_message(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"structured_bad_protocol_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "candidate.jpg"
        Image.new("RGB", (8, 8), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        protocol = RequestProtocol.from_code(
            "COARSE_CANDIDATES_FOUND",
            request_id="req_bad_protocol",
            search_id="search_bad_protocol",
        )
        draft = build_a2_output_draft(
            "search_image",
            {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 1},
            protocol,
        )
        malformed = protocol.to_dict()
        malformed["unexpected"] = "field"

        payload = _agent_payload(
            AgentResponse(
                text="RAW_PROTOCOL_ERROR_MUST_NOT_ESCAPE",
                images=[str(image_path)],
                intent="search_image",
                protocol=malformed,
                output=draft,
            ),
            runtime,
            "session_structured_bad_protocol",
        )

        self.assertEqual(payload["code"], "SERVICE_UNAVAILABLE")
        self.assertEqual(payload["message_key"], "system.service.unavailable")
        self.assertEqual(payload["images"], [])
        self.assertEqual(payload["intent"], "")
        self.assertNotIn("RAW_PROTOCOL_ERROR_MUST_NOT_ESCAPE", payload["text"])

    def test_feedback_submission_captures_visible_conversation_and_session_media(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"feedback_case_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "image.jpg"
        Image.new("RGB", (8, 8), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        store = SQLiteFeedbackStore(test_dir / "feedback.sqlite3")
        client = TestClient(create_app(runtime=runtime, feedback_store=store))

        upload_bytes = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(upload_bytes, format="PNG")
        uploaded = client.post(
            "/api/image",
            files={"file": ("question.png", upload_bytes.getvalue(), "image/png")},
        )
        self.assertEqual(uploaded.status_code, 200)
        uploaded_url = uploaded.json()["uploaded_image"]
        response = client.post("/api/feedback", json={
            "message_id": "message_case_123",
            "rating": "negative",
            "tags": ["not_found"],
            "detail": "没有合适候选",
            "feedback_scope": "page",
            "search_duration_ms": 1450,
            "conversation": [
                {
                    "me": True,
                    "message": "我发了一张题图。",
                    "images": [uploaded_url],
                    "createdAt": 1000,
                },
                {
                    "me": False,
                    "message": "我正在帮你找。",
                    "messageId": "message_case_123",
                    "createdAt": 2000,
                },
            ],
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["feedback"]["feedback_scope"], "question")
        saved = store.list_feedback()[0]
        self.assertEqual(saved.feedback_scope, "question")
        self.assertEqual(saved.search_key, f"{saved.session_key}:1")
        self.assertEqual(saved.search_duration_ms, 1450)
        self.assertRegex(saved.feedback_number, r"^FB-\d{8}-[0-9A-F]{10}$")
        self.assertEqual(len(saved.conversation), 2)
        media_name = saved.conversation[0]["images"][0]
        self.assertTrue(store.resolve_case_media(saved.feedback_id, media_name).is_file())
        rejected_empty = client.post("/api/feedback", json={
            "message_id": "message_case_123",
            "rating": "positive",
            "tags": ["found_answer"],
            "detail": "",
            "conversation": [],
        })
        self.assertEqual(rejected_empty.status_code, 400)
        self.assertTrue(store.resolve_case_media(saved.feedback_id, media_name).is_file())

    def test_feedback_scopes_latest_upload_and_captures_prepared_page_overlay(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"feedback_scope_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "page.jpg"
        Image.new("RGB", (12, 8), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        store = SQLiteFeedbackStore(test_dir / "feedback.sqlite3")
        client = TestClient(create_app(runtime=runtime, feedback_store=store))

        upload_bytes = io.BytesIO()
        Image.new("RGB", (12, 8), "white").save(upload_bytes, format="PNG")
        uploaded = client.post(
            "/api/image",
            files={"file": ("page.png", upload_bytes.getvalue(), "image/png")},
        )
        self.assertEqual(uploaded.status_code, 200)
        uploaded_url = uploaded.json()["uploaded_image"]
        session_id = client.cookies.get(SESSION_COOKIE)
        runtime.snapshot.update({
            "task_revision": 99,
            "candidate_count": 99,
            "a3": {"task_revision": 2, "auto_crop_overlay_available": True},
        })
        protocol = RequestProtocol.from_code(
            "QUESTION_UNITS_PREPARED",
            request_id="req_feedback_overlay",
        )
        prepared = _agent_payload(
            AgentResponse(
                text="已准备 9 道题。",
                intent="a3_units_prepared",
                protocol=protocol.to_dict(),
                output=build_a3_output_draft(
                    "a3_units_prepared",
                    {
                        "phase": "WAIT_UNIT_SELECTION",
                        "question_count": 9,
                        "ready_count": 9,
                        "manual_count": 0,
                    },
                    protocol,
                ),
            ),
            runtime,
            session_id,
        )
        self.assertEqual(prepared["images"], [])
        self.assertEqual(prepared["feedback_images"][0]["kind"], "a3_overlay")

        target_id = "message_page_two"
        response = client.post("/api/feedback", json={
            "message_id": target_id,
            "rating": "positive",
            "tags": ["found_answer"],
            "detail": "框选清楚",
            "feedback_scope": "question",
            "conversation": [
                {"me": True, "message": "上一页", "images": [uploaded_url], "taskRevision": 1},
                {"me": False, "message": "上一页结果", "messageId": "message_page_one", "taskRevision": 1},
                {"me": True, "message": "我发了一张题图。", "images": [uploaded_url], "taskRevision": 2},
                {
                    "me": False,
                    "message": "已准备 9 道题：9 道可以直接检索。请选择一道继续。",
                    "messageId": target_id,
                    "taskRevision": 2,
                    "candidateCount": 9,
                    "intent": "a3_units_prepared",
                    "a3Overlay": prepared["feedback_images"][0]["url"],
                },
            ],
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["feedback"]["feedback_scope"], "page")
        saved = store.list_feedback()[0]
        self.assertEqual(saved.feedback_scope, "page")
        self.assertEqual(saved.task_revision, 2)
        self.assertEqual(saved.candidate_count, 9)
        self.assertEqual(len(saved.conversation), 2)
        self.assertEqual(saved.conversation[0]["message"], "我发了一张题图。")
        overlay_name = saved.conversation[1]["a3_overlay"]
        self.assertTrue(store.resolve_case_media(saved.feedback_id, overlay_name).is_file())
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE message_feedback SET feedback_scope = '', schema_version = 6 "
                "WHERE feedback_id = ?",
                (saved.feedback_id,),
            )
        migrated = store.get_feedback(saved.feedback_id)
        self.assertEqual(migrated.feedback_scope, "page")
        with sqlite3.connect(store.path) as connection:
            schema_version = connection.execute(
                "SELECT schema_version FROM message_feedback WHERE feedback_id = ?",
                (saved.feedback_id,),
            ).fetchone()[0]
        self.assertEqual(schema_version, 7)

    def test_feedback_scope_does_not_cross_task_revision_without_current_upload(self):
        target_id = "message_current_revision"
        scoped = scope_feedback_conversation(
            [
                {
                    "me": True,
                    "message": "上一页",
                    "images": ["/api/upload/old.jpg"],
                    "taskRevision": 1,
                },
                {
                    "me": False,
                    "message": "上一页结果",
                    "messageId": "message_old_revision",
                    "taskRevision": 1,
                },
                {
                    "me": True,
                    "message": "当前问题",
                    "images": [],
                    "taskRevision": 2,
                },
                {
                    "me": False,
                    "message": "当前回复",
                    "messageId": target_id,
                    "taskRevision": 2,
                },
            ],
            target_id,
        )

        self.assertEqual(
            [message["message"] for message in scoped],
            ["当前问题", "当前回复"],
        )
        self.assertEqual(scope_feedback_conversation(scoped, "missing_target"), [])

    def test_message_feedback_is_private_bounded_and_upserted(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"feedback_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "image.jpg"
        Image.new("RGB", (4, 4), "white").save(image_path)
        store = SQLiteFeedbackStore(test_dir / "feedback.sqlite3")
        client = TestClient(create_app(runtime=FakeRuntime(image_path), feedback_store=store))
        client.get("/")

        first = client.post("/api/feedback", json={
            "message_id": "message_12345678",
            "rating": "positive",
            "tags": ["found_answer", "fast"],
            "detail": "很快找到了",
        })
        self.assertEqual(first.status_code, 200)
        saved = store.list_feedback()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].identity_key, "local")
        self.assertEqual(saved[0].rating, "positive")
        self.assertNotEqual(saved[0].session_key, client.cookies.get(SESSION_COOKIE))

        updated = client.post("/api/feedback", json={
            "message_id": "message_12345678",
            "rating": "negative",
            "tags": ["ranking_issue"],
            "detail": "正确题在后面",
        })
        self.assertEqual(updated.status_code, 200)
        saved = store.list_feedback()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].rating, "negative")
        self.assertEqual(saved[0].tags, ("ranking_issue",))

        outsider = TestClient(create_app(runtime=FakeRuntime(image_path), feedback_store=store))
        outsider.get("/")
        not_removed = outsider.delete("/api/feedback/message_12345678")
        self.assertEqual(not_removed.status_code, 200)
        self.assertFalse(not_removed.json()["removed"])
        self.assertEqual(len(store.list_feedback()), 1)

        removed = client.delete("/api/feedback/message_12345678")
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(store.list_feedback(), [])
        self.assertFalse(client.delete("/api/feedback/message_12345678").json()["removed"])
        self.assertEqual(client.delete("/api/feedback/bad").status_code, 400)
        self.assertEqual(
            client.post("/api/feedback", json={
                "message_id": "message_abcdefgh",
                "rating": "positive",
                "tags": ["wrong_answer"],
                "detail": "",
            }).status_code,
            400,
        )
        self.assertEqual(
            client.post("/api/feedback", content=b"x" * (MAX_FEEDBACK_BYTES + 1)).status_code,
            413,
        )

    def test_invitation_gate_authenticates_and_passes_stable_identity(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        test_dir = runtime_dir / f"invite_gate_{uuid4().hex}"
        test_dir.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(test_dir, ignore_errors=True))
        image_path = test_dir / "image.jpg"
        Image.new("RGB", (4, 4), "white").save(image_path)
        config, codes = build_invitation_config(2)
        config_path = test_dir / "invites.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        runtime = FakeRuntime(image_path)
        feedback_store = SQLiteFeedbackStore(test_dir / "feedback.sqlite3")
        access = InviteAccess(config_path)
        app = create_app(
            runtime=runtime,
            invite_access=access,
            feedback_store=feedback_store,
        )
        client = TestClient(app)

        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/api/session").status_code, 401)
        redirect = client.get("/", follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers["location"], "/invite")
        self.assertEqual(
            client.post("/api/invite/login", data={"code": "wrong"}).status_code,
            401,
        )
        self.assertEqual(
            client.post("/api/invite/login", content=b"x" * 4097).status_code,
            413,
        )

        login = client.post(
            "/api/invite/login", data={"code": codes[0][1]}, follow_redirects=False
        )
        self.assertEqual(login.status_code, 303)
        self.assertEqual(client.get("/").status_code, 200)
        response = client.post("/api/message", json={"text": "你好"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(runtime.last_identity, codes[0][0])
        feedback = client.post("/api/feedback", json={
            "message_id": "invite_message_01",
            "rating": "positive",
            "tags": ["clear_reply"],
            "detail": "",
        })
        self.assertEqual(feedback.status_code, 200)
        self.assertEqual(feedback_store.list_feedback()[0].identity_key, codes[0][0])

        expired = TestClient(app)
        expired.cookies.set(access.cookie_name, "invalid-signed-cookie")
        expired_redirect = expired.get("/", follow_redirects=False)
        self.assertEqual(expired_redirect.status_code, 303)
        self.assertEqual(expired_redirect.headers["location"], "/invite?reason=session_expired")
        expired_page = expired.get(expired_redirect.headers["location"])
        self.assertIn("登录状态已失效，请重新输入邀请码。", expired_page.text)
        self.assertNotIn("点赞", expired_page.text)
        self.assertNotIn("点踩", expired_page.text)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax validation")
    def test_javascript_has_valid_syntax(self):
        result = subprocess.run(
            [shutil.which("node"), "--check", "-"],
            input=_SCRIPT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_assets_cover_interview_demo_interactions(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_asset_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        client = TestClient(create_app(runtime=FakeRuntime(image_path)))

        page = client.get("/")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(client.get("/openapi.json").status_code, 404)
        self.assertEqual(client.get("/assets/demo.css").text.replace("\r\n", "\n"), _STYLE)
        self.assertEqual(client.get("/assets/demo.js").text.replace("\r\n", "\n"), _SCRIPT)
        for expected in (
            'href="/assets/demo.css?v=20260822-feedback-v1"', 'src="/assets/demo.js?v=20260822-output-layer-v1"',
            'id="session-drawer"',
            'id="menu-button"', 'id="lightbox"', 'role="log" aria-live="polite"',
            'role="status" aria-live="polite"', 'role="button" tabindex="0" aria-label="上传题图"',
            'id="drop-overlay"', 'type="submit" aria-label="发送消息" disabled', '松开即可上传题图',
            '题图会用于云端模型识别', '请勿上传个人敏感信息',
            'id="feedback-backdrop"', 'id="feedback-tags"', 'id="feedback-detail"',
            'id="feedback-cancel"', '取消反馈',
        ):
            self.assertIn(expected, page.text)
        for expected in (
            "URL.createObjectURL(selected)", "URL.revokeObjectURL", "function validateImage",
            "function uploadImage", "document.addEventListener('dragenter'", "document.addEventListener('drop'",
            "new AbortController()", "activeController.abort('new-chat')", "function resetConversation",
            "function openDrawer", "function openLightbox", "className = 'select-candidate'",
            "action_context: actionContext", "function invalidateCandidateActions()",
            "['WAIT_CANDIDATE_CHOICE', 'ANSWERED'].includes(sessionContext.phase)",
            "event.key === 'Enter'", "!event.shiftKey", "!event.isComposing", "event.keyCode !== 229",
            "HISTORY_TTL_MS = 2 * 60 * 60 * 1000", "HISTORY_LIMIT = 50", "repairUploadedImageHistory()",
            "lastActivityAt: historyLastActivityAt", "saveHistory({ refreshActivity: true })",
            "function scheduleHistoryExpiry()", "function expireHistoryIfNeeded()",
            "if (!snapshot.session_valid)", "window.addEventListener('focus', expireHistoryIfNeeded)",
            "document.addEventListener('visibilitychange'",
            "data.message_key === 'page.session.reset' && data.status === 'SUCCESS'",
            "data.uploaded_image", "Number.isFinite(activityAt)", "无法连接服务",
            "IMAGE_TARGET_BYTES = 1024 * 1024", "IMAGE_MAX_DIMENSION = 2560", "IMAGE_FALLBACK_DIMENSION = 2048",
            "canvas.toBlob(resolve, 'image/jpeg', quality)", "formData.append('file', prepared.blob, prepared.filename)",
            "const filename = `cropped_${Date.now()}.jpg`", "function retryUpload", "pendingUpload = prepared",
            "const uploadRow = addLocalUploadPreview(sourcePreview)", "setUploadRowStatus(uploadRow, '我发了一张题图。')",
            "message: '正在识别题目'", "setStatus('working', '正在识别题目…')",
            "requestStream('/api/message/stream'", "requestStream('/api/image/stream'",
            "function updatePendingMessage", "setStatus('working', event.text)",
            "updatePendingMessage(pending, event.text);",
            "function refocusComposerOnDesktop()", "window.matchMedia('(hover: hover) and (pointer: fine)')",
            "textInput.focus({ preventScroll: true })",
            "function syncVisualViewport()", "window.visualViewport?.addEventListener('resize', syncVisualViewport",
            "window.visualViewport?.addEventListener('scroll', syncVisualViewport", "syncVisualViewport();",
            "function createMessageActions", "function openFeedback", "request('/api/feedback'",
            "if (target < 0) return [];", "normalizeFeedbackImages(item.feedbackImages)",
            "feedbackImages: normalizeFeedbackImages(data.feedback_images)",
            "function cancelFeedback", "method: 'DELETE'", "syncFeedbackButtons(context.article, '')",
            "['found_answer', '找到了正确答案']", "['not_found', '没找到正确题']",
            "const feedbackEligible = !item.me && item.variant !== 'pending'",
            "function createRecoveryActions", "登录状态已失效，请重新登录。",
            "这次请求没有处理成功，请检查后重试。",
            "if (now - activityAt >= HISTORY_TTL_MS)", "showSessionExpiredNotice();",
            "function flushStartupNotices", "pendingSessionExpiredNotice = true",
            "variant: 'error', recoveryActions:",
            "function showFailureNotice", "function resolveFailureNotice",
            "retry_connection: '重新连接'", "function retryConnection()",
            "暂时无法连接服务。当前对话仍保留在本机",
            "浏览器无法保存临时对话", "浏览器中的临时对话无法读取",
            "图片已失效，请重新上传", "反馈提交失败，可重新提交",
            "retry_request: '重试上一条'", "retry_search: '重试搜索'",
            "function normalizeRetryAction", "function retryTextAction",
            "protocol.status === 'PARTIAL' ? 'partial' : ''",
            "function setResponseStatus(data)", "headers.set('x-request-id', requestId)",
            "isPersistentImage(data.submitted_crop)", "我提交了裁剪后的题图。",
            "search_id: context.item.searchId || sessionContext.search_id || ''",
        ):
            self.assertIn(expected, _SCRIPT)
        self.assertNotIn(
            "pending.querySelector('.message-content')?.replaceChildren(document.createTextNode(event.message));",
            _SCRIPT,
        )
        self.assertIn("const publicMessage = canonicalPublicMessage(data)", _SCRIPT)
        self.assertIn("canonicalPublicMessage(event?.data, { progress: true })", _SCRIPT)
        self.assertNotIn("A temporary network failure should not discard", _SCRIPT)
        self.assertNotIn("new File(", _SCRIPT)
        self.assertNotIn("sendTextValue(String(index + 1)", _SCRIPT)
        self.assertNotIn("题图处理中", _SCRIPT)
        self.assertNotIn("题图正在上传", _SCRIPT)
        self.assertNotIn("正在上传并识别题干", _SCRIPT)
        restore_body = _SCRIPT[_SCRIPT.index("function restoreHistory()"):_SCRIPT.index("async function repairUploadedImageHistory()")]
        self.assertIn("historyLastActivityAt = activityAt", restore_body)
        self.assertNotIn("refreshActivity: true", restore_body)
        self.assertLess(
            _SCRIPT.index("const uploadRow = addLocalUploadPreview(sourcePreview)"),
            _SCRIPT.index("await normalizeImage(selected, sourcePreview)"),
        )
        self.assertLess(
            _SCRIPT.index("message: '我发了一张题图。'"),
            _SCRIPT.index("await requestStream('/api/image/stream'"),
        )
        self.assertIn("overflow-y: auto", _STYLE)
        self.assertIn("top: var(--app-top, 0px)", _STYLE)
        self.assertIn("height: var(--app-height, 100dvh)", _STYLE)
        self.assertIn("prefers-reduced-motion: reduce", _STYLE)
        self.assertNotIn("window.scrollTo", _SCRIPT)

    def test_public_http_redirects_and_https_cookie_is_secure(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_secure_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        client = TestClient(create_app(runtime=FakeRuntime(image_path)))

        redirect = client.get("/", headers={"x-forwarded-proto": "http"}, follow_redirects=False)
        self.assertEqual(redirect.status_code, 308)
        self.assertTrue(redirect.headers["location"].startswith("https://"))

        response = client.post(
            "/api/message",
            json={"text": "就这个"},
            headers={"x-forwarded-proto": "https"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertEqual(response.headers["strict-transport-security"], "max-age=31536000")

    def test_first_page_assigns_session_before_upload_and_reopen_restores_image(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_reopen_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        app = create_app(runtime=runtime)
        first_visit = TestClient(app)

        page = first_visit.get("/")
        self.assertEqual(page.status_code, 200)
        session_id = first_visit.cookies.get(SESSION_COOKIE)
        self.assertTrue(session_id)

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        uploaded = first_visit.post(
            "/api/image",
            content=buffer.getvalue(),
            headers={"x-filename": "question.jpg"},
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(runtime.upload_session, session_id)
        uploaded_url = uploaded.json()["uploaded_image"]

        reopened = TestClient(app)
        reopened.cookies.set(SESSION_COOKIE, session_id)
        self.assertEqual(reopened.get("/api/session").json()["uploaded_image"], uploaded_url)
        self.assertEqual(reopened.get(uploaded_url).status_code, 200)

        no_page_visit = TestClient(app)
        session_response = no_page_visit.get("/api/session")
        self.assertEqual(session_response.status_code, 200)
        self.assertIn(SESSION_COOKIE, session_response.cookies)

    def test_multipart_cropped_jpeg_and_png_metadata_mismatch_are_accepted(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_multipart_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        client.get("/")

        jpeg = io.BytesIO()
        Image.new("RGB", (5, 5), "white").save(jpeg, format="JPEG")
        cropped = client.post(
            "/api/image",
            files={"file": ("cropped_1700000000000.jpg", jpeg.getvalue(), "image/jpeg")},
        )
        self.assertEqual(cropped.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "image")

        png = io.BytesIO()
        Image.new("RGB", (5, 5), "white").save(png, format="PNG")
        mismatched = _write_incoming_image(png.getvalue(), "crop_without_name.jpg", "image/jpeg")
        self.addCleanup(lambda: mismatched.unlink(missing_ok=True))
        self.assertEqual(mismatched.suffix, ".png")
        with Image.open(mismatched) as detected:
            self.assertEqual(detected.format, "PNG")

    def test_image_upload_rejects_missing_invalid_and_oversized_content(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_reject_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        client = TestClient(create_app(runtime=FakeRuntime(image_path)))

        missing = client.post("/api/image", files={"other": ("crop.jpg", b"data", "image/jpeg")})
        self.assertEqual(missing.status_code, 400)
        invalid = client.post("/api/image", files={"file": ("crop.jpg", b"not an image", "image/jpeg")})
        self.assertEqual(invalid.status_code, 400)
        oversized = client.post(
            "/api/image",
            files={"file": ("crop.jpg", b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg")},
        )
        self.assertEqual(oversized.status_code, 413)

    def test_health_text_cookie_image_upload_and_session_bound_media(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        media_path = runtime_dir / f"demo_test_result_{uuid4().hex}.jpg"
        self.addCleanup(lambda: media_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(media_path)
        runtime = FakeRuntime(media_path)
        app = create_app(runtime=runtime)
        client = TestClient(app)

        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        self.assertEqual(client.post("/api/message", content=b"not-json").status_code, 400)
        self.assertEqual(client.post("/api/message", json=[]).status_code, 400)
        text_response = client.post("/api/message", json={"text": "就这个"})
        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(
            text_response.json()["text"],
            "找到了 1 道较相似的候选题，请选择候选编号。",
        )
        self.assertEqual(text_response.json()["message_key"], "search.candidates.ready")
        self.assertEqual(text_response.json()["code"], "COARSE_CANDIDATES_FOUND")
        self.assertEqual(text_response.json()["author_contact"], {})
        self.assertIsNone(text_response.json()["failure"])
        self.assertIn(SESSION_COOKIE, text_response.cookies)
        follow_up = client.post("/api/message", json={"text": "再说一次"})
        self.assertIn(SESSION_COOKIE, follow_up.cookies)
        media_url = text_response.json()["images"][0]
        self.assertEqual(client.get(media_url).status_code, 200)
        self.assertEqual(client.get(media_url).headers["cache-control"], "private, no-store")
        other_client = TestClient(app)
        other_client.cookies.set(SESSION_COOKIE, "different-session")
        self.assertEqual(other_client.get(media_url).status_code, 404)

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        image_response = client.post("/api/image", content=buffer.getvalue(), headers={"x-filename": "question.jpg"})
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "image")
        uploaded_image_url = image_response.json()["uploaded_image"]
        self.assertTrue(uploaded_image_url.startswith("/api/upload/"))
        self.assertEqual(client.get("/api/session").json()["uploaded_image"], uploaded_image_url)
        self.assertEqual(client.get(uploaded_image_url).status_code, 200)
        self.assertEqual(client.get(uploaded_image_url).headers["cache-control"], "private, no-store")
        other_upload_client = TestClient(app)
        other_upload_client.cookies.set(SESSION_COOKIE, "different-session")
        self.assertEqual(other_upload_client.get(uploaded_image_url).status_code, 404)

        reset_response = client.post("/api/reset")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "clear")
        self.assertEqual(client.get(media_url).status_code, 404)

    def test_business_error_payload_has_public_recovery_contract(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_error_contract_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)

        class ErrorRuntime(FakeRuntime):
            def __init__(self, image_path: Path, *, has_active_image: bool):
                super().__init__(image_path)
                self.has_active_image = has_active_image

            def handle_text(
                self,
                session_id: str,
                text: str,
                *,
                identity_key="",
                progress=None,
                request_id="",
            ) -> AgentResponse:
                del text, identity_key, progress
                self.snapshot.update({
                    "session_valid": True,
                    "phase": "ERROR",
                    "has_active_image": self.has_active_image,
                })
                protocol = RequestProtocol.from_code(
                    "AGENT_FAILED",
                    request_id=request_id or "req_fake_business_error",
                )
                return AgentResponse(
                    text="这次没查成功。题图已保留，你可以直接回复“重试”。",
                    intent="unsupported",
                    protocol=protocol.to_dict(),
                    output=build_a2_output_draft(
                        "unsupported",
                        self.snapshot,
                        protocol,
                    ),
                )

        for has_active_image, expected_code, expected_action in (
            (True, "AGENT_FAILED", "retry_search"),
            (False, "SERVICE_UNAVAILABLE", "retry_request"),
        ):
            with self.subTest(has_active_image=has_active_image):
                response = TestClient(create_app(runtime=ErrorRuntime(
                    image_path, has_active_image=has_active_image,
                ))).post("/api/message", json={"text": "重试"})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], expected_code)
                self.assertEqual(
                    response.json()["failure"],
                    {"kind": "business_error", "recovery_action": expected_action},
                )

    def test_streaming_endpoints_emit_real_progress_before_result(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_stream_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        client = TestClient(create_app(runtime=FakeRuntime(image_path)))

        request_id = "req_0123456789abcdef0123456789abcdef"
        text_response = client.post(
            "/api/message/stream",
            json={"text": "按力法搜"},
            headers={"x-request-id": request_id},
        )
        text_events = [json.loads(line) for line in text_response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in text_events], ["progress", "result"])
        progress = text_events[0]["data"]
        self.assertEqual(progress["kind"], "progress")
        self.assertEqual(progress["message_key"], "progress.image.analysis")
        self.assertEqual(progress["stage"], "image_analysis")
        self.assertEqual(progress["sequence"], 1)
        self.assertEqual(progress["request_id"], request_id)
        self.assertNotIn("力法", progress["text"])
        self.assertEqual(text_events[-1]["data"]["request_id"], request_id)

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        image_response = client.post(
            "/api/image/stream",
            files={"file": ("crop.jpg", buffer.getvalue(), "image/jpeg")},
        )
        image_events = [json.loads(line) for line in image_response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in image_events], ["progress", "result"])
        self.assertEqual(image_events[0]["data"]["sequence"], 1)
        self.assertEqual(image_events[0]["data"]["kind"], "progress")
        self.assertTrue(image_events[-1]["data"]["uploaded_image"].startswith("/api/upload/"))

    def test_stream_progress_sequence_and_ids_are_monotonic_and_nonreflective(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_progress_contract_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        request_id = "req_abcdef0123456789abcdef0123456789"
        search_id = "search_abcdef0123456789abcdef01234567"

        class MultiProgressRuntime(FakeRuntime):
            def __init__(self, path):
                super().__init__(path)
                self.snapshot["search_id"] = search_id

            def handle_text(
                self,
                session_id,
                text,
                *,
                identity_key="",
                progress=None,
                request_id="",
            ):
                del session_id, text, identity_key
                progress("searching", "Traceback PRIVATE_PROGRESS_ONE")
                progress("unknown_internal_stage", "C:\\private\\PRIVATE_PROGRESS_TWO")
                self.snapshot.update({
                    "session_valid": True,
                    "phase": "WAIT_CANDIDATE_CHOICE",
                    "candidate_count": 1,
                })
                protocol = RequestProtocol.from_code(
                    "COARSE_CANDIDATES_FOUND",
                    request_id=request_id,
                    search_id=search_id,
                )
                return AgentResponse(
                    text="PRIVATE_FINAL_TEXT",
                    images=[str(self.image_path)],
                    intent="search_image",
                    protocol=protocol.to_dict(),
                    output=build_a2_output_draft(
                        "search_image",
                        {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 1},
                        protocol,
                    ),
                )

        response = TestClient(create_app(runtime=MultiProgressRuntime(image_path))).post(
            "/api/message/stream",
            json={"text": "搜索"},
            headers={"x-request-id": request_id},
        )
        events = [json.loads(line) for line in response.text.splitlines() if line]
        progress_events = [event["data"] for event in events if event["type"] == "progress"]
        self.assertEqual([item["sequence"] for item in progress_events], [1, 2])
        self.assertEqual({item["request_id"] for item in progress_events}, {request_id})
        self.assertEqual({item["search_id"] for item in progress_events}, {search_id})
        self.assertEqual(events[-1]["data"]["request_id"], request_id)
        self.assertEqual(events[-1]["data"]["search_id"], search_id)
        self.assertNotIn("PRIVATE_PROGRESS", response.text)
        self.assertNotIn("PRIVATE_FINAL_TEXT", response.text)

    def test_new_image_stream_establishes_search_id_after_empty_progress_id(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_new_search_stream_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        request_id = "req_1234567890abcdef1234567890abcdef"
        new_search_id = "search_1234567890abcdef1234567890abcd"

        class NewSearchRuntime(FakeRuntime):
            def __init__(self, path):
                super().__init__(path)
                self.snapshot["search_id"] = "search_previous_request_01"

            def handle_image(
                self,
                session_id,
                uploaded_path,
                *,
                identity_key="",
                progress=None,
                request_id="",
            ):
                del identity_key
                self.calls.append(("image", session_id, uploaded_path.is_file()))
                self.upload_session = session_id
                progress("searching", "PRIVATE_NEW_SEARCH_PROGRESS")
                self.snapshot.update({
                    "session_valid": True,
                    "phase": "WAIT_CHAPTER",
                    "has_active_image": True,
                    "search_id": new_search_id,
                })
                protocol = RequestProtocol.from_code(
                    "CHAPTER_REQUIRED",
                    request_id=request_id,
                    search_id=new_search_id,
                )
                return AgentResponse(
                    text="PRIVATE_NEW_SEARCH_FINAL",
                    intent="search_image",
                    protocol=protocol.to_dict(),
                    output=build_a2_output_draft(
                        "search_image",
                        {"phase": "WAIT_CHAPTER"},
                        protocol,
                    ),
                )

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        response = TestClient(create_app(runtime=NewSearchRuntime(image_path))).post(
            "/api/image/stream",
            files={"file": ("question.jpg", buffer.getvalue(), "image/jpeg")},
            headers={"x-request-id": request_id},
        )

        events = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in events], ["progress", "result"])
        self.assertEqual(events[0]["data"]["search_id"], "")
        self.assertEqual(events[-1]["data"]["search_id"], new_search_id)
        self.assertEqual({event["data"]["request_id"] for event in events}, {request_id})
        self.assertNotIn("PRIVATE_NEW_SEARCH", response.text)

    def test_public_session_snapshot_removes_internal_a3_state(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_public_session_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        runtime.snapshot.update({
            "session_valid": True,
            "phase": "WAIT_UNIT_SELECTION",
            "search_id": "search_public_session_01",
            "private_path": "C:\\private\\LEAK_SESSION",
            "a3": {
                "enabled": True,
                "phase": "WAIT_UNIT_SELECTION",
                "task_revision": 7,
                "auto_crop_enabled": True,
                "auto_prepare_all_enabled": True,
                "auto_prepare_all_units": False,
                "page_finished": False,
                "pending_intent_clarification": {"raw": "LEAK_PENDING"},
                "last_intent": {"reasoning": "LEAK_INTENT"},
                "completed_unit_ids": ["unit_2"],
                "searched_unit_ids": [],
                "requested_unit_ids": ["unit_1"],
                "auto_crop_page_status": "LEAK_AUTO_STATUS",
                "intent_v1_enabled": True,
                "auto_crop_overlay_available": True,
                "crop_review_required": True,
                "crop_review_feedback": "Traceback LEAK_CROP_FEEDBACK",
                "crop_draft": {
                    "available": True,
                    "bounds": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
                    "path": "C:\\private\\LEAK_CROP_PATH",
                },
                "units": [{
                    "unit_id": "unit_1",
                    "page_index": 1,
                    "display_label": "第1题",
                    "title_text": "题目C:\\private\\LEAK_TITLE",
                    "completed": False,
                    "searched": False,
                    "selected": True,
                    "requested": True,
                    "grounding_status": "auto_ready",
                    "validation_status": "manual_required",
                    "crop_available": True,
                    "auto_bounds": {"x": 0.1},
                    "reason_codes": ["LEAK_REASON"],
                }],
                "selected_unit": {
                    "unit_id": "unit_1",
                    "display_label": "第1题",
                    "context_text": "题干/home/private/LEAK_CONTEXT",
                },
            },
        })

        payload = TestClient(create_app(runtime=runtime)).get("/api/session").json()
        public = payload["session"]
        encoded = json.dumps(public, ensure_ascii=False)
        for forbidden in (
            "pending_intent_clarification", "last_intent", "reason_codes",
            "auto_bounds", "completed_unit_ids", "searched_unit_ids",
            "requested_unit_ids", "auto_crop_page_status", "intent_v1_enabled",
            "crop_review_feedback", "private_path", "LEAK_",
        ):
            self.assertNotIn(forbidden, encoded)
        a3 = public["a3"]
        self.assertEqual(a3["units"][0]["preparation_status"], "manual")
        self.assertEqual(a3["units"][0]["title_text"], "")
        self.assertEqual(a3["selected_unit"]["context_text"], "")
        self.assertEqual(
            a3["crop_draft"]["bounds"],
            {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        )

    def test_api_http_errors_use_canonical_nonreflective_output(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_http_output_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        response = TestClient(create_app(runtime=FakeRuntime(image_path))).post(
            "/api/message",
            content=b"PRIVATE_INVALID_JSON",
            headers={"content-type": "application/json"},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["message_key"], "search.clarification.required")
        self.assertEqual(payload["code"], "MESSAGE_INVALID")
        self.assertNotIn("detail", payload)
        self.assertNotIn("PRIVATE_INVALID_JSON", response.text)

    def test_old_candidate_button_is_rejected_without_running_agent(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_stale_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        client.get("/")

        runtime.snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CHAPTER",
            "has_active_image": True,
            "task_revision": 2,
            "candidate_generation": "",
            "candidate_count": 0,
        })
        response = client.post("/api/message/stream", json={
            "text": "选择候选 1",
            "action_context": {
                "type": "select_candidate",
                "rank": 1,
                "task_revision": 1,
                "candidate_generation": "old-generation",
            },
        })

        events = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in events], ["result"])
        self.assertEqual(
            events[0]["data"]["text"],
            "刚才的候选状态已经失效，请重新上传题图。",
        )
        self.assertEqual(events[0]["data"]["message_key"], "page.stale.candidate")
        self.assertEqual(events[0]["data"]["code"], "STALE_CANDIDATE")
        self.assertEqual(events[0]["data"]["intent"], "stale_candidate")
        self.assertEqual(runtime.calls, [])

    def test_busy_and_budget_guards_return_safe_public_errors(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_guard_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)

        class GuardedRuntime(FakeRuntime):
            error = AgentRuntimeBusyError("Traceback PRIVATE_QUEUE_DETAIL")

            def handle_text(self, session_id: str, text: str, *, progress=None) -> AgentResponse:
                raise self.error

        runtime = GuardedRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        busy = client.post("/api/message", json={"text": "你好"})
        self.assertEqual(busy.status_code, 429)
        self.assertEqual(busy.headers["retry-after"], "15")
        self.assertEqual(busy.headers["cache-control"], "no-store")
        self.assertEqual(busy.json()["message_key"], "system.queue.full")
        self.assertNotIn("PRIVATE_QUEUE_DETAIL", busy.text)

        stream = client.post("/api/message/stream", json={"text": "你好"})
        events = [json.loads(line) for line in stream.text.splitlines() if line]
        self.assertEqual(events[0]["type"], "error")
        event = events[0]["data"]
        self.assertEqual(event["message_key"], "system.queue.full")
        self.assertEqual(event["status"], "ERROR")
        self.assertEqual(event["layer"], "queue")
        self.assertEqual(event["code"], "QUEUE_FULL")
        self.assertTrue(event["retryable"])
        self.assertEqual(event["action"], "retry_request")
        self.assertTrue(event["request_id"].startswith("req_"))
        self.assertNotIn("PRIVATE_QUEUE_DETAIL", stream.text)

        runtime.error = AgentBudgetExceededError("今日服务额度已用完，请明天再试。")
        budget = client.post("/api/message", json={"text": "你好"})
        self.assertEqual(budget.status_code, 503)
        self.assertEqual(budget.headers["retry-after"], "3600")

    def test_lifespan_periodically_purges_expired_sessions(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_cleanup_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)

        class CleaningRuntime(FakeRuntime):
            def __init__(self, path: Path):
                super().__init__(path)
                self.purge_count = 0

            def purge_expired(self) -> None:
                self.purge_count += 1

        runtime = CleaningRuntime(image_path)
        with TestClient(create_app(runtime=runtime, cleanup_interval_seconds=0.01)):
            time.sleep(0.05)
        self.assertGreaterEqual(runtime.purge_count, 1)

    def test_answered_question_can_select_another_candidate_from_same_generation(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_reselect_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        client.get("/")

        runtime.snapshot.update({
            "session_valid": True,
            "phase": "ANSWERED",
            "has_active_image": True,
            "task_revision": 2,
            "candidate_generation": "2:1",
            "candidate_count": 3,
        })
        response = client.post("/api/message/stream", json={
            "text": "选择候选 3",
            "action_context": {
                "type": "select_candidate",
                "rank": 3,
                "task_revision": 2,
                "candidate_generation": "2:1",
            },
        })

        events = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["data"]["intent"], "select_candidate")
        self.assertEqual(runtime.calls[0][0], "text")
        self.assertEqual(runtime.calls[0][2], "选择候选 3")

    def test_answered_question_still_rejects_an_older_candidate_generation(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_reselect_stale_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)
        runtime = FakeRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        client.get("/")

        runtime.snapshot.update({
            "session_valid": True,
            "phase": "ANSWERED",
            "has_active_image": True,
            "task_revision": 2,
            "candidate_generation": "2:2",
            "candidate_count": 3,
        })
        response = client.post("/api/message/stream", json={
            "text": "选择候选 3",
            "action_context": {
                "type": "select_candidate",
                "rank": 3,
                "task_revision": 2,
                "candidate_generation": "2:1",
            },
        })

        events = [json.loads(line) for line in response.text.splitlines() if line]
        self.assertEqual(events[-1]["data"]["intent"], "stale_candidate")
        self.assertEqual(runtime.calls, [])


if __name__ == "__main__":
    unittest.main()

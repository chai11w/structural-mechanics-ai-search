import io
import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import MAX_FEEDBACK_BYTES, MAX_IMAGE_BYTES, SESSION_COOKIE, _SCRIPT, _STYLE, _write_incoming_image, create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess, build_invitation_config
from tiku_agent.session_runtime import AgentBudgetExceededError, AgentRuntimeBusyError


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

    def handle_text(self, session_id: str, text: str, *, identity_key="", progress=None) -> AgentResponse:
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
        return AgentResponse(text="我明白了。", images=[str(self.image_path)], intent="select_candidate")

    def handle_image(self, session_id: str, image_path: Path, *, identity_key="", progress=None) -> AgentResponse:
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
        return AgentResponse(text="我正在帮你找。", intent="search_image")

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


class FastApiDemoTest(unittest.TestCase):
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
        saved = store.list_feedback()[0]
        self.assertEqual(saved.search_key, f"{saved.session_key}:1")
        self.assertEqual(saved.search_duration_ms, 1450)
        self.assertRegex(saved.feedback_number, r"^FB-\d{8}-[0-9A-F]{10}$")
        self.assertEqual(len(saved.conversation), 2)
        media_name = saved.conversation[0]["images"][0]
        self.assertTrue(store.resolve_case_media(saved.feedback_id, media_name).is_file())

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
            'href="/assets/demo.css?v=20260820-a3-v1-v2"', 'src="/assets/demo.js?v=20260821-a3-progress-style-v1"',
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
            "if (!data.session?.session_valid)", "window.addEventListener('focus', expireHistoryIfNeeded)",
            "document.addEventListener('visibilitychange'",
            "data.intent === 'a3_session_reset'",
            "data.uploaded_image", "Number.isFinite(activityAt)", "无法连接服务",
            "IMAGE_TARGET_BYTES = 1024 * 1024", "IMAGE_MAX_DIMENSION = 2560", "IMAGE_FALLBACK_DIMENSION = 2048",
            "canvas.toBlob(resolve, 'image/jpeg', quality)", "formData.append('file', prepared.blob, prepared.filename)",
            "const filename = `cropped_${Date.now()}.jpg`", "function retryUpload", "pendingUpload = prepared",
            "const uploadRow = addLocalUploadPreview(sourcePreview)", "setUploadRowStatus(uploadRow, '我发了一张题图。')",
            "message: '正在识别题目'", "setStatus('working', '正在识别题目…')",
            "requestStream('/api/message/stream'", "requestStream('/api/image/stream'",
            "function updatePendingMessage", "setStatus('working', event.message)",
            "updatePendingMessage(pending, event.message);",
            "function refocusComposerOnDesktop()", "window.matchMedia('(hover: hover) and (pointer: fine)')",
            "textInput.focus({ preventScroll: true })",
            "function syncVisualViewport()", "window.visualViewport?.addEventListener('resize', syncVisualViewport",
            "window.visualViewport?.addEventListener('scroll', syncVisualViewport", "syncVisualViewport();",
            "function createMessageActions", "function openFeedback", "request('/api/feedback'",
            "function cancelFeedback", "method: 'DELETE'", "syncFeedbackButtons(context.article, '')",
            "['found_answer', '找到了正确答案']", "['not_found', '没找到正确题']",
            "const feedbackEligible = !item.me && item.variant !== 'pending'",
            "function createRecoveryActions", "登录状态已失效，请重新登录。",
            "这次请求没有处理成功，请直接重试；如果仍然失败，请点踩并补充说明。",
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
        self.assertLess(
            _SCRIPT.index("if (status === 503"),
            _SCRIPT.index("if (status >= 500)"),
        )
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
        self.assertEqual(text_response.json()["text"], "我明白了。")
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

            def handle_text(self, session_id: str, text: str, *, identity_key="", progress=None) -> AgentResponse:
                del text, identity_key, progress
                self.snapshot.update({
                    "session_valid": True,
                    "phase": "ERROR",
                    "has_active_image": self.has_active_image,
                })
                return AgentResponse(
                    text="这次没查成功。题图已保留，你可以直接回复“重试”。",
                    intent="unsupported",
                )

        for has_active_image, expected_action in ((True, "retry_search"), (False, "new_chat")):
            with self.subTest(has_active_image=has_active_image):
                response = TestClient(create_app(runtime=ErrorRuntime(
                    image_path, has_active_image=has_active_image,
                ))).post("/api/message", json={"text": "重试"})

                self.assertEqual(response.status_code, 200)
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

        text_response = client.post("/api/message/stream", json={"text": "按力法搜"})
        text_events = [json.loads(line) for line in text_response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in text_events], ["progress", "result"])
        self.assertEqual(text_events[0]["stage"], "searching")
        self.assertIn("力法", text_events[0]["message"])

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        image_response = client.post(
            "/api/image/stream",
            files={"file": ("crop.jpg", buffer.getvalue(), "image/jpeg")},
        )
        image_events = [json.loads(line) for line in image_response.text.splitlines() if line]
        self.assertEqual([event["type"] for event in image_events], ["progress", "result"])
        self.assertTrue(image_events[-1]["data"]["uploaded_image"].startswith("/api/upload/"))

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
        self.assertIn("上一道题", events[0]["data"]["text"])
        self.assertEqual(events[0]["data"]["intent"], "stale_candidate")
        self.assertEqual(runtime.calls, [])

    def test_busy_and_budget_guards_return_safe_public_errors(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        image_path = runtime_dir / f"demo_guard_{uuid4().hex}.jpg"
        self.addCleanup(lambda: image_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(image_path)

        class GuardedRuntime(FakeRuntime):
            error = AgentRuntimeBusyError("当前请求较多，请稍后再试。")

            def handle_text(self, session_id: str, text: str, *, progress=None) -> AgentResponse:
                raise self.error

        runtime = GuardedRuntime(image_path)
        client = TestClient(create_app(runtime=runtime))
        busy = client.post("/api/message", json={"text": "你好"})
        self.assertEqual(busy.status_code, 429)
        self.assertEqual(busy.headers["retry-after"], "15")
        self.assertEqual(busy.headers["cache-control"], "no-store")

        stream = client.post("/api/message/stream", json={"text": "你好"})
        events = [json.loads(line) for line in stream.text.splitlines() if line]
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["message"], "当前请求较多，请稍后再试。")
        self.assertEqual(events[0]["status"], "ERROR")
        self.assertEqual(events[0]["layer"], "queue")
        self.assertEqual(events[0]["code"], "QUEUE_FULL")
        self.assertTrue(events[0]["retryable"])
        self.assertEqual(events[0]["action"], "retry_request")
        self.assertTrue(events[0]["request_id"].startswith("req_"))

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

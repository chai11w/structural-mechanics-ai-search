import io
from pathlib import Path
import shutil
import subprocess
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE, _SCRIPT, _STYLE, _write_incoming_image, create_app


class FakeRuntime:
    def __init__(self, image_path: Path):
        self.image_path = image_path
        self.calls = []
        self.upload_session = ""
        self.media_session = ""

    def handle_text(self, session_id: str, text: str) -> AgentResponse:
        self.calls.append(("text", session_id, text))
        return AgentResponse(text="我明白了。", images=[str(self.image_path)], intent="select_candidate")

    def handle_image(self, session_id: str, image_path: Path) -> AgentResponse:
        self.calls.append(("image", session_id, image_path.is_file()))
        self.upload_session = session_id
        return AgentResponse(text="我正在帮你找。", intent="search_image")

    def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id))

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
            'href="/assets/demo.css"', 'src="/assets/demo.js"', 'id="session-drawer"',
            'id="menu-button"', 'id="lightbox"', 'role="log" aria-live="polite"',
            'role="status" aria-live="polite"', 'role="button" tabindex="0" aria-label="上传题图"',
            'id="drop-overlay"', 'type="submit" aria-label="发送消息" disabled', '松开即可上传题图',
            '题图会用于云端模型识别', '请勿上传个人敏感信息',
        ):
            self.assertIn(expected, page.text)
        for expected in (
            "URL.createObjectURL(selected)", "URL.revokeObjectURL", "function validateImage",
            "function uploadImage", "document.addEventListener('dragenter'", "document.addEventListener('drop'",
            "new AbortController()", "activeController.abort('new-chat')", "function resetConversation",
            "function openDrawer", "function openLightbox", "className = 'select-candidate'",
            "event.key === 'Enter'", "!event.shiftKey", "!event.isComposing", "event.keyCode !== 229",
            "HISTORY_TTL_MS = 2 * 60 * 60 * 1000", "HISTORY_LIMIT = 50", "repairUploadedImageHistory()",
            "data.uploaded_image", "Number.isFinite(savedAt)", "无法连接本地服务",
        ):
            self.assertIn(expected, _SCRIPT)
        self.assertIn("overflow-y: auto", _STYLE)
        self.assertIn("prefers-reduced-motion: reduce", _STYLE)
        self.assertNotIn("window.scrollTo", _SCRIPT)

    def test_mobile_crop_metadata_is_tolerated_but_content_is_verified(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (4, 4), (255, 0, 0, 128)).save(buffer, format="PNG")

        normalized = _write_incoming_image(buffer.getvalue(), "微信裁剪临时文件.tmp")
        self.addCleanup(lambda: normalized.unlink(missing_ok=True))
        self.assertEqual(normalized.suffix, ".jpg")
        with Image.open(normalized) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")

        heif_buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(heif_buffer, format="HEIF")
        normalized_heif = _write_incoming_image(heif_buffer.getvalue(), "crop_without_extension")
        self.addCleanup(lambda: normalized_heif.unlink(missing_ok=True))
        with Image.open(normalized_heif) as image:
            self.assertEqual(image.format, "JPEG")

        with self.assertRaises(Exception) as invalid:
            _write_incoming_image(b"not an image", "crop.jpg")
        self.assertEqual(getattr(invalid.exception, "status_code", None), 400)

        self.assertIn("normalizedType.startsWith('image/')", _SCRIPT)
        self.assertIn("normalizedType === 'application/octet-stream'", _SCRIPT)
        self.assertIn("'heic', 'heif', 'hif'", _SCRIPT)

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
        self.assertIn(SESSION_COOKIE, text_response.cookies)
        follow_up = client.post("/api/message", json={"text": "再说一次"})
        self.assertIn(SESSION_COOKIE, follow_up.cookies)
        media_url = text_response.json()["images"][0]
        self.assertEqual(client.get(media_url).status_code, 200)
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
        other_upload_client = TestClient(app)
        other_upload_client.cookies.set(SESSION_COOKIE, "different-session")
        self.assertEqual(other_upload_client.get(uploaded_image_url).status_code, 404)

        reset_response = client.post("/api/reset")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "clear")
        self.assertEqual(client.get(media_url).status_code, 404)


if __name__ == "__main__":
    unittest.main()

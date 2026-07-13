import io
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE, _PAGE, create_app


class FakeRuntime:
    def __init__(self, image_path: Path):
        self.image_path = image_path
        self.calls = []

    def handle_text(self, session_id: str, text: str) -> AgentResponse:
        self.calls.append(("text", session_id, text))
        return AgentResponse(text="我明白了。", images=[str(self.image_path)], intent="select_candidate")

    def handle_image(self, session_id: str, image_path: Path) -> AgentResponse:
        self.calls.append(("image", session_id, image_path.is_file()))
        return AgentResponse(text="我正在帮你找。", intent="search_image")

    def clear(self, session_id: str) -> None:
        self.calls.append(("clear", session_id))


class FastApiDemoTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for embedded JavaScript syntax validation")
    def test_embedded_javascript_has_valid_syntax(self):
        scripts = re.findall(r"<script>(.*?)</script>", _PAGE, re.DOTALL)
        self.assertTrue(scripts)
        result = subprocess.run(
            [shutil.which("node"), "--check", "-"],
            input=scripts[-1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_text_cookie_image_upload_and_media_token(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        media_path = runtime_dir / f"demo_test_result_{uuid4().hex}.jpg"
        self.addCleanup(lambda: media_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(media_path)
        runtime = FakeRuntime(media_path)
        app = create_app(runtime=runtime)
        client = TestClient(app)

        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        page = client.get("/")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("URL.createObjectURL(selected)", page.text)
        self.assertIn("已上传题图", page.text)
        self.assertIn("正在识别题图，请稍等", page.text)
        self.assertIn("finishPending", page.text)
        self.assertIn("TEXT_TIMEOUT_MS=60000", page.text)
        self.assertIn("IMAGE_TIMEOUT_MS=90000", page.text)
        self.assertIn("new AbortController()", page.text)
        self.assertIn("原图已保留", page.text)
        self.assertIn("safeHttpError", page.text)
        self.assertIn("content-type", page.text)
        self.assertIn("服务暂时异常", page.text)
        self.assertIn("服务返回格式异常", page.text)
        self.assertIn("HISTORY_KEY='tiku-agent-current-chat-v1'", page.text)
        self.assertIn("HISTORY_TTL_MS=2*60*60*1000", page.text)
        self.assertIn("HISTORY_LIMIT=50", page.text)
        self.assertIn("let chatHistory=[]", page.text)
        self.assertNotIn("let history=[]", page.text)
        self.assertIn("restoreHistory()", page.text)
        self.assertIn("clearHistory()", page.text)
        self.assertIn("!url.startsWith('blob:')", page.text)
        self.assertIn("图片已失效，请重新上传", page.text)
        self.assertIn("'题库候选题',false", page.text)
        self.assertIn('id="drop-overlay"', page.text)
        self.assertIn("MAX_IMAGE_BYTES=15*1024*1024", page.text)
        self.assertIn("function validateImage", page.text)
        self.assertIn("function uploadImage", page.text)
        self.assertIn("document.addEventListener('dragenter'", page.text)
        self.assertIn("document.addEventListener('drop'", page.text)
        self.assertIn("松开即可上传题图", page.text)
        self.assertIn("URL.revokeObjectURL", page.text)
        text_response = client.post("/api/message", json={"text": "就这个"})
        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(text_response.json()["text"], "我明白了。")
        self.assertIn(SESSION_COOKIE, text_response.cookies)
        follow_up = client.post("/api/message", json={"text": "再说一次"})
        self.assertIn(SESSION_COOKIE, follow_up.cookies)
        media_response = client.get(text_response.json()["images"][0])
        self.assertEqual(media_response.status_code, 200)
        other_client = TestClient(app)
        other_client.cookies.set(SESSION_COOKIE, "different-session")
        self.assertEqual(other_client.get(text_response.json()["images"][0]).status_code, 404)

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        image_response = client.post("/api/image", content=buffer.getvalue(), headers={"x-filename": "question.jpg"})
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "image")
        self.assertTrue(runtime.calls[-1][2])

        reset_response = client.post("/api/reset")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "clear")
        self.assertEqual(client.get(text_response.json()["images"][0]).status_code, 404)


if __name__ == "__main__":
    unittest.main()

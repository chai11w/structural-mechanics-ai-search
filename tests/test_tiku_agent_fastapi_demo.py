import io
from pathlib import Path
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE, create_app


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
    def test_health_text_cookie_image_upload_and_media_token(self):
        runtime_dir = Path(__file__).resolve().parents[1] / ".tmp_tiku_agent"
        media_path = runtime_dir / f"demo_test_result_{uuid4().hex}.jpg"
        self.addCleanup(lambda: media_path.unlink(missing_ok=True))
        Image.new("RGB", (4, 4), "white").save(media_path)
        runtime = FakeRuntime(media_path)
        client = TestClient(create_app(runtime=runtime))

        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        page = client.get("/")
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("URL.createObjectURL(selected)", page.text)
        self.assertIn("已上传题图", page.text)
        self.assertIn("正在识别题图，请稍等", page.text)
        self.assertIn("finishPending", page.text)
        text_response = client.post("/api/message", json={"text": "就这个"})
        self.assertEqual(text_response.status_code, 200)
        self.assertEqual(text_response.json()["text"], "我明白了。")
        self.assertIn(SESSION_COOKIE, text_response.cookies)
        follow_up = client.post("/api/message", json={"text": "再说一次"})
        self.assertIn(SESSION_COOKIE, follow_up.cookies)
        media_response = client.get(text_response.json()["images"][0])
        self.assertEqual(media_response.status_code, 200)

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
        image_response = client.post("/api/image", content=buffer.getvalue(), headers={"x-filename": "question.jpg"})
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "image")
        self.assertTrue(runtime.calls[-1][2])

        reset_response = client.post("/api/reset")
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(runtime.calls[-1][0], "clear")


if __name__ == "__main__":
    unittest.main()

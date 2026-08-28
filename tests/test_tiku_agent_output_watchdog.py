import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import _agent_payload, create_app
from tiku_agent.output_watchdog import OutputWatchdog, classify_output
from tiku_agent.task_state_contract import empty_task_state_snapshot


class _Runtime:
    def __init__(self, image: Path):
        self.image = image
        self.snapshot = {
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 1,
            "candidate_generation": "",
            "candidate_count": 0,
        }
        self.output_watchdog = None

    def handle_text(
        self,
        session_id,
        text,
        *,
        identity_key="",
        progress=None,
        task_state_capabilities=None,
    ):
        response = AgentResponse(text="用户可见回复。", intent="safe_answer")
        response.response_snapshot = dict(self.snapshot)
        response.response_projection_snapshot = dict(self.snapshot)
        if task_state_capabilities is not None:
            response.response_task_state_snapshot = empty_task_state_snapshot()
        response.response_media_snapshot_captured = True
        return response

    def session_snapshot(self, session_id):
        return dict(self.snapshot)

    def persist_media(self, session_id, source):
        return source if Path(source).is_file() else None

    def resolve_media(self, session_id, filename):
        return self.image if filename == self.image.name else None


class _CapturingWatchdog:
    def __init__(self):
        self.samples = []

    def observe(self, text, **metadata):
        self.samples.append({"text": text, **metadata})


class OutputWatchdogTest(unittest.TestCase):
    def test_classification_is_deterministic_and_does_not_rewrite_text(self):
        self.assertEqual(classify_output("已找到最相似候选。"), ("normal", ()))
        category, rules = classify_output("这句话太长了。" * 80)
        self.assertEqual(category, "awkward")
        self.assertIn("overlong", rules)

        category, rules = classify_output(
            'Traceback (most recent call last): File "C:\\private\\app.py", line 3\nRuntimeError: api_key=secret-value'
        )
        self.assertEqual(category, "dangerous")
        self.assertIn("traceback", rules)
        self.assertIn("windows_path", rules)
        self.assertIn("exception", rules)
        self.assertIn("credential", rules)

    def test_dangerous_sample_is_redacted_and_written_outside_request_path(self):
        with tempfile.TemporaryDirectory() as temp:
            watchdog = OutputWatchdog(Path(temp) / "watchdog")
            watchdog.observe(
                'Traceback: C:\\private\\secret.py RuntimeError: api_key=secret-value',
                intent="error",
                protocol_code="TOOL_FAILED",
            )
            self.assertTrue(watchdog.flush())
            record = json.loads(watchdog.path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["category"], "dangerous")
            self.assertEqual(record["preview"], "<dangerous output redacted>")
            encoded = json.dumps(record, ensure_ascii=False)
            for secret in ("Traceback", "secret.py", "secret-value", "C:\\private"):
                self.assertNotIn(secret, encoded)

    def test_full_queue_and_observer_failure_are_fail_open(self):
        with tempfile.TemporaryDirectory() as temp:
            watchdog = OutputWatchdog(Path(temp) / "watchdog", queue_size=1)
            for index in range(20):
                sample = watchdog.observe(f"样本 {index}")
                self.assertEqual(sample["category"], "normal")
            self.assertTrue(watchdog.flush())

        class Broken:
            def observe(self, *_args, **_kwargs):
                raise RuntimeError("observer unavailable")

        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.jpg"
            Image.new("RGB", (4, 4), "white").save(image)
            runtime = _Runtime(image)
            runtime.output_watchdog = Broken()
            payload = _agent_payload(
                AgentResponse(text="原样回复。", intent="safe_answer"),
                runtime,
                "session",
            )
            self.assertEqual(payload["text"], "原样回复。")

    def test_web_and_stream_observe_the_final_public_text(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.jpg"
            Image.new("RGB", (4, 4), "white").save(image)
            runtime = _Runtime(image)
            observer = _CapturingWatchdog()
            client = TestClient(create_app(runtime=runtime, output_watchdog=observer))

            json_response = client.post("/api/message", json={"text": "你好"})
            self.assertEqual(json_response.status_code, 200)
            self.assertIn("task_state", json_response.json())
            stream_response = client.post(
                "/api/message/stream", json={"text": "你好"}
            )
            self.assertEqual(stream_response.status_code, 200)
            stream_events = [
                json.loads(line) for line in stream_response.text.splitlines() if line
            ]
            self.assertNotIn("task_state", stream_events[-1]["data"])
            self.assertGreaterEqual(len(observer.samples), 2)
            self.assertTrue(all(item["text"] == "用户可见回复。" for item in observer.samples))
            self.assertTrue(all(item["endpoint"] == "web_a3" for item in observer.samples))

    def test_media_guard_observes_replaced_final_text(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "answer.jpg"
            Image.new("RGB", (4, 4), "white").save(image)
            runtime = _Runtime(image)
            watchdog = _CapturingWatchdog()
            runtime.output_watchdog = watchdog
            payload = _agent_payload(
                AgentResponse(
                    text="原始答案文案。",
                    images=[str(Path(temp) / "missing.jpg")],
                    state={"last_answer_paths": [str(Path(temp) / "missing.jpg")]},
                    intent="select_candidate",
                    media_kind="answer",
                ),
                runtime,
                "session",
            )
            self.assertEqual(payload["text"], "答案暂时无法发送，请回复“重试”。")
            self.assertEqual(watchdog.samples[-1]["text"], payload["text"])
            self.assertEqual(watchdog.samples[-1]["media_status"], "unavailable")

    def test_structured_http_error_is_observed_after_public_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "image.jpg"
            Image.new("RGB", (4, 4), "white").save(image)
            runtime = _Runtime(image)
            observer = _CapturingWatchdog()
            client = TestClient(create_app(runtime=runtime, output_watchdog=observer))
            response = client.post("/api/message", json={})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(observer.samples[-1]["text"], "请求内容无效，请重新提交。")
            self.assertEqual(observer.samples[-1]["endpoint"], "http_error")


if __name__ == "__main__":
    unittest.main()

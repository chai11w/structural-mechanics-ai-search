"""Real vision clients recover through fake HTTP and the durable boundary."""

from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts.classify_question_bank import qwen_extract_loads
from tests import test_execution_effects as fixtures
from tests.test_external_load_screen import FakeResponse
from tiku_agent.a3_models import CROP_COMPARE_CHECK_KEYS, QwenA3CropVerifier
from tiku_agent.agent import AgentResponse
from tiku_agent.glm_vision import call_glm_json


class ModelRecoveryClientTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(self.f.store.policy, model_transport_recovery=True,
                                      block_on_pending_costs=False)
        # Generated geometric fixture, never a user image or an external request.
        self.image = self.f.root / "synthetic-structure.png"
        image = Image.new("RGB", (96, 64), "white")
        drawing = ImageDraw.Draw(image)
        drawing.line((8, 48, 88, 48), fill="black", width=2)
        drawing.line((48, 12, 48, 42), fill="black", width=2)
        drawing.line((43, 36, 48, 42, 53, 36), fill="black", width=2)
        image.save(self.image)
        self.endpoint = "https://example.invalid/chat/completions"

    def exercise(self, callback, content, *, call_type, provider="dashscope", first_error=None):
        parsed_results = []

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                parsed_results.append(callback())
                return AgentResponse(text="parsed client result", state=self.state.to_dict(), intent="greeting")

        self.f.runtime.agent_factory = Agent
        envelope = {
            "id": "isolated-provider-success",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        }
        error = first_error or urllib.error.URLError(ssl.SSLEOFError(8, "controlled TLS EOF"))
        snapshots = []

        def urlopen(request, *, timeout):
            snapshots.append({
                "body": bytes(request.data),
                "authorization": request.get_header("Authorization"),
                "url": request.full_url,
                "method": request.get_method(),
                "timeout": timeout,
            })
            if len(snapshots) == 1:
                raise error
            return FakeResponse(envelope)

        request = self.f.request()
        with patch("urllib.request.urlopen", side_effect=urlopen) as send, patch("time.sleep") as sleep:
            response = self.f.runtime.handle_text("s", "run", operation_request=request)
            replay = self.f.runtime.handle_text("s", "run", operation_request=request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(parsed_results), 1)
        # The only backoff belongs to the outer recovery; the legacy Qwen
        # request_json_with_retry must not hide any additional HTTP attempts.
        self.assertEqual(sleep.call_count, 1)
        self.assertTrue(snapshots[0] == snapshots[1], "replacement changed the frozen HTTP request")
        self.assertTrue(bool(snapshots[0]["authorization"]), "synthetic credential header is missing")
        self.assertEqual(snapshots[0]["method"], "POST")
        self.assertEqual(snapshots[0]["timeout"], 7)

        effects = self.f.rows("execution_effects")
        links = self.f.rows("execution_model_recoveries")
        self.assertEqual(len(effects), 2)
        self.assertEqual(len(links), 1)
        by_id = {effect["call_id"]: effect for effect in effects}
        source, target = (by_id[links[0][name]] for name in ("source_call_id", "target_call_id"))
        self.assertEqual((source["status"], target["status"]), ("UNKNOWN", "CONFIRMED"))
        self.assertEqual((source["usage_known"], target["usage_known"]), (0, 1))
        self.assertNotEqual(source["run_id"], target["run_id"])
        self.assertTrue(all(effect["provider"] == provider and effect["call_type"] == call_type
                            for effect in effects))
        self.assertEqual([json.loads(effect["record"])["attempt_count"] for effect in effects], [1, 1])
        with closing(sqlite3.connect(self.f.ledger.path)) as connection:
            paid = connection.execute(
                "SELECT call_id,run_id,attempt_count,total_tokens FROM model_cost_calls").fetchall()
        self.assertEqual(paid, [(target["call_id"], target["run_id"], 1, 120)])
        outboxes = self.f.rows("execution_cost_outbox")
        self.assertEqual([(row["run_id"], row["status"]) for row in outboxes],
                         [(target["run_id"], "CONFIRMED")])
        self.assertEqual(self.f.ops.cost_exposure("local")["unresolved_calls"], 1)
        return parsed_results[0], json.loads(snapshots[0]["body"])

    def test_real_qwen_crop_verifier_recovers_without_nested_transport_retries(self):
        client = QwenA3CropVerifier(api_key="synthetic-test-only", endpoint=self.endpoint,
                                    timeout_seconds=7)
        content = {
            "schema_version": "a3-crop-compare-v2", "selected_unit_id": "g1-u1",
            "verdict": "verified", "checks": {key: True for key in CROP_COMPARE_CHECK_KEYS},
        }
        result, body = self.exercise(
            lambda: client.verify(self.image, self.image, {"unit_id": "g1-u1"}, {}),
            content, call_type="qwen_a3_crop_compare",
        )
        self.assertTrue(result.verified)
        self.assertEqual(result.selected_unit_id, "g1-u1")
        self.assertEqual(body["model"], "qwen3.7-plus")
        self.assertEqual(body["max_tokens"], 300)
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(sum(item["type"] == "image_url" for item in body["messages"][1]["content"]), 2)

    def test_real_qwen_load_extraction_recovers_and_parses_the_successful_response(self):
        content = {
            "loads": [{"type": "集中", "raw": "P"}], "chapter_hint": "4力法",
            "chapter_confidence": 0.99, "chapter_evidence": "题干明确要求力法",
            "visible_problem_text": "用力法求解图示结构。",
        }
        result, body = self.exercise(
            lambda: qwen_extract_loads(
                self.image, model="qwen3.7-plus", endpoint=self.endpoint,
                api_key="synthetic-test-only", timeout=7, context_text="用力法求解图示结构。"),
            content, call_type="qwen_image_classification",
        )
        self.assertEqual(result["loads"], [{"type": "集中", "raw": "P"}])
        self.assertEqual(result["chapter_hint"], "4力法")
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertEqual(body["max_tokens"], 1024)
        self.assertFalse(body["enable_thinking"])

    def test_real_glm_transport_preserves_http_503_for_one_durable_recovery(self):
        content = {"page_id": "synthetic", "units": []}
        result, body = self.exercise(
            lambda: call_glm_json(
                [self.image], prompt="Return fixture JSON", api_key="synthetic-test-only",
                endpoint=self.endpoint, timeout_seconds=7, call_type="glm_a3_page_auto_crop"),
            content, call_type="glm_a3_page_auto_crop", provider="zhipu",
            first_error=urllib.error.HTTPError(self.endpoint, 503, "controlled unavailable", {}, None),
        )
        self.assertEqual(result.payload, content)
        self.assertEqual(result.total_tokens, 120)
        self.assertEqual(body["thinking"], {"type": "disabled"})


if __name__ == "__main__":
    unittest.main()

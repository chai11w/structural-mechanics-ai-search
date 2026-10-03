"""Real structure classification uses bounded recovery before its filter fallback."""

from contextlib import closing, contextmanager
from dataclasses import replace
import json
import os
import sqlite3
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from PIL import Image, ImageDraw

from multi_agent_pipeline import QwenClassifier
from tests import test_execution_effects as fixtures
from tests.test_external_load_screen import FakeResponse
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_effects import ExecutionEffects
from tiku_agent.execution_receipts import recover_accounting
from tiku_agent.tools import classify_structure_tool


def transport_failure():
    return urllib.error.URLError(ssl.SSLEOFError(8, "controlled TLS EOF"))


class StructureModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(self.f.store.policy, model_transport_recovery=True,
                                      block_on_pending_costs=False)
        # The real image encoder receives only this generated test geometry.
        self.image = self.f.root / "synthetic-structure.png"
        image = Image.new("RGB", (96, 64), "white")
        ImageDraw.Draw(image).line((8, 48, 88, 48), fill="black", width=2)
        image.save(self.image)
        self.endpoint = "https://example.invalid/chat/completions"
        self.client = QwenClassifier(
            endpoint=self.endpoint, model="qwen3.7-plus", timeout=7,
            cache_path=self.f.root / "unused-cache.json", use_cache=False)
        self.snapshots = []
        self.results = []
        owner = self

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                owner.results.append(classify_structure_tool(owner.image, route="main"))
                return AgentResponse(text="structure result", state=self.state.to_dict(), intent="greeting")

        self.f.runtime.agent_factory = Agent

    def envelope(self, content=None):
        if content is None:
            content = json.dumps({"structure_type": "梁", "confidence": 0.97,
                                  "reason": "水平构件"}, ensure_ascii=False)
        return {
            "id": "synthetic-structure-success",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "choices": [{"message": {"content": content}}],
        }

    @contextmanager
    def http(self, *responses):
        pending = iter(responses)

        def send(request, *, timeout):
            self.snapshots.append((bytes(request.data), request.get_header("Authorization"),
                                   request.full_url, request.get_method(), timeout))
            response = next(pending)  # An unexpected third request fails the test.
            if isinstance(response, BaseException):
                raise response
            return FakeResponse(response)

        # Only the dependency factory is substituted; the tool, classifier,
        # image/payload encoder, timed boundary and response parser are real.
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "synthetic-test-only"}), \
                patch("tiku_agent.tools._make_qwen", return_value=self.client), \
                patch("urllib.request.urlopen", side_effect=send) as sender, \
                patch("time.sleep") as sleep:
            yield sender, sleep

    def run_request(self, request=None):
        return self.f.runtime.handle_text("s", "classify", operation_request=request or self.f.request())

    def pair(self, target_status):
        effects = self.f.rows("execution_effects")
        self.assertEqual(len(effects), 2)
        links = self.f.rows("execution_model_recoveries")
        self.assertEqual(len(links), 1)
        by_id = {effect["call_id"]: effect for effect in effects}
        source, target = (by_id[links[0][name]] for name in ("source_call_id", "target_call_id"))
        self.assertEqual((source["status"], target["status"]), ("UNKNOWN", target_status))
        self.assertEqual(source["usage_known"], 0)
        self.assertNotEqual(source["call_id"], target["call_id"])
        self.assertNotEqual(source["run_id"], target["run_id"])
        for effect in effects:
            self.assertEqual(effect["provider"], "dashscope")
            self.assertEqual(effect["model"], "qwen3.7-plus")
            self.assertEqual(effect["call_type"], "qwen_structure_type")
            self.assertEqual(json.loads(effect["record"])["attempt_count"], 1)
        # Compare without rendering request bytes or Authorization in failures.
        self.assertTrue(self.snapshots[0] == self.snapshots[1], "frozen request changed")
        return source, target

    def test_real_structure_chain_recovers_and_settles_only_the_new_known_fee(self):
        request = self.f.request()
        with self.http(transport_failure(), self.envelope()) as (send, sleep):
            response = self.run_request(request)
            replay = self.run_request(request)
            recover_accounting(self.f.ops, [self.f.ledger])
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(len(self.results), 1)
        result = self.results[0]
        self.assertEqual(result.code, "STRUCTURE_CLASSIFIED_FROM_IMAGE")
        self.assertEqual(result.data["structure_type"], "梁")
        self.assertTrue(result.data["filter_applicable"])
        source, target = self.pair("CONFIRMED")
        self.assertEqual(target["usage_known"], 1)
        body = json.loads(self.snapshots[0][0])
        self.assertEqual(body["max_tokens"], 256)
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(self.snapshots[0][2:], (self.endpoint, "POST", 7))
        self.assertTrue(bool(self.snapshots[0][1]))
        with closing(sqlite3.connect(self.f.ledger.path)) as connection:
            paid = connection.execute(
                "SELECT call_id,run_id,attempt_count,total_tokens FROM model_cost_calls").fetchall()
        self.assertEqual(paid, [(target["call_id"], target["run_id"], 1, 120)])
        self.assertEqual([(row["run_id"], row["status"]) for row in self.f.rows("execution_cost_outbox")],
                         [(target["run_id"], "CONFIRMED")])
        self.assertEqual(self.f.ops.cost_exposure("local")["unresolved_calls"], 1)
        result_record = json.loads(self.f.rows("execution_operations")[0]["result"])
        self.assertEqual(result_record.get("model_fallbacks", {}), {})
        self.assertEqual(source["status"], "UNKNOWN")

    def test_two_transient_failures_keep_both_unknowns_and_search_all_structure_types(self):
        request = self.f.request()
        with self.http(transport_failure(), transport_failure()) as (send, sleep):
            response = self.run_request(request)
            replay = self.run_request(request)
            recover_accounting(self.f.ops, [self.f.ledger])
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual((send.call_count, sleep.call_count), (2, 1))
        self.assertEqual(self.results[0].code, "STRUCTURE_CLASSIFICATION_FALLBACK")
        self.assertEqual(self.results[0].data["structure_type"], "")
        self.assertFalse(self.results[0].data["filter_applicable"])
        source, target = self.pair("UNKNOWN")
        self.assertEqual(target["usage_known"], 0)
        result = json.loads(self.f.rows("execution_operations")[0]["result"])
        self.assertEqual(result["model_fallbacks"], {
            source["call_id"]: "structure_filter_skip", target["call_id"]: "structure_filter_skip"})
        self.assertEqual(self.f.ops.cost_exposure("local")["unresolved_calls"], 2)
        self.assertEqual(self.f.rows("execution_cost_outbox"), [])

    def test_default_off_still_uses_one_call_and_existing_structure_fallback(self):
        self.f.store.policy = replace(self.f.store.policy, model_transport_recovery=False)
        with self.http(transport_failure()) as (send, sleep):
            response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual((send.call_count, sleep.call_count), (1, 0))
        self.assertEqual(self.results[0].code, "STRUCTURE_CLASSIFICATION_FALLBACK")
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_http_authentication_failure_is_not_recovered(self):
        error = urllib.error.HTTPError(self.endpoint, 401, "controlled unauthorized", {}, None)
        with self.http(error) as (send, sleep):
            response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual((send.call_count, sleep.call_count), (1, 0))
        self.assertEqual(self.results[0].code, "STRUCTURE_CLASSIFICATION_FALLBACK")
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_successful_http_with_invalid_classification_is_not_recovered(self):
        with self.http(self.envelope("not valid JSON")) as (send, sleep):
            response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual((send.call_count, sleep.call_count), (1, 0))
        self.assertEqual(self.results[0].code, "STRUCTURE_CLASSIFICATION_FALLBACK")
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])
        self.assertEqual(self.f.rows("execution_effects")[0]["status"], "CONFIRMED")

    def test_receipt_timeout_cannot_be_treated_as_a_transient_provider_failure(self):
        with self.http(self.envelope()) as (send, sleep), \
                patch.object(ExecutionEffects, "model_finished", side_effect=TimeoutError("receipt unavailable")):
            self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual((send.call_count, sleep.call_count), (1, 0))
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_pending_cost_budget_blocks_recovery_before_its_http_send(self):
        self.f.store.policy = replace(self.f.store.policy, block_on_pending_costs=True,
                                      max_global_unresolved_cost_calls=1)
        request = self.f.request()
        with self.http(transport_failure()) as (send, _sleep):
            self.run_request(request)
            replay = self.run_request(request)
            recover_accounting(self.f.ops, [self.f.ledger])
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(send.call_count, 1)
        effects = self.f.rows("execution_effects")
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["status"], "UNKNOWN")
        links = self.f.rows("execution_model_recoveries")
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]["target_call_id"])


if __name__ == "__main__":
    unittest.main()

"""Exercise the actual candidate scorer through the durable task boundary."""
from dataclasses import replace
import io
import json
import threading
import unittest
from unittest.mock import patch

from PIL import Image

import search
from tests import test_execution_effects as fixtures
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_effects import ExecutionEffects
from tiku_shared.execution_hooks import accept_rerank_fallback
from tiku_shared.model_costs import timed_model_call


class RerankFallbackTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(self.f.store.policy, block_on_pending_costs=False)
        self.image = self.f.root / "query.jpg"
        Image.new("RGB", (80, 60), "white").save(self.image)

    def install_rerank_agent(self, workers=3, failed=(1, 3)):
        owner, f = self, self.f
        self.calls, self.outputs = [], []
        lock = threading.Lock()
        initial_calls = threading.Barrier(workers)
        candidates = [{"rank": i, "name": f"candidate-{i}", "path": str(self.image),
                       "score": 1.0 - i / 100} for i in range(1, 10)]
        def send(request, timeout=None):
            with lock:
                self.calls.append(1)
                number = len(self.calls)
            if number <= workers:
                initial_calls.wait(timeout=5)
            if number in failed:
                raise TimeoutError("controlled rerank timeout")
            body = {"choices": [{"message": {"content": '{"score":0.9,"reason":"ok"}'}}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
            return io.BytesIO(json.dumps(body).encode())
        class Agent(f.runtime.agent_factory):
            def handle_text(self, text):
                result = search.rerank_candidates_concurrent(
                    owner.image, candidates, provider="qwen", model="qwen3.7-plus",
                    max_workers=workers, candidate_timeout_seconds=12,
                    retry_timeout_seconds=12, retry_max_candidates=9, retry_failed_candidates=True)
                owner.outputs.append(result)
                return AgentResponse(text=search.rerank_incomplete_note(result),
                                     state=self.state.to_dict(), intent="search")
        f.runtime.agent_factory = Agent
        self.addCleanup(patch.stopall)
        patch.object(search, "DASHSCOPE_API_KEY", "isolated-test-key").start()
        patch("urllib.request.urlopen", side_effect=send).start()
        return candidates

    def assert_completed_fallback(self, workers):
        f = self.f
        expected = self.install_rerank_agent(workers)
        request = f.request()
        result = f.runtime.handle_text("s", "search", operation_request=request)
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertIn("粗筛", result.text)
        self.assertEqual([x["name"] for x in self.outputs[0]], [x["name"] for x in expected])
        self.assertTrue(all(x["rerank_status"] == "incomplete" for x in self.outputs[0]))
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertLessEqual(len(self.calls), 9)  # no retry of a failed scorer
        sent_count = len(self.calls)
        effects = f.rows("execution_effects")
        unknown = [x for x in effects if x["status"] == "UNKNOWN"]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(all(x["usage_known"] == 0 for x in unknown))
        receipt = json.loads(f.rows("execution_operations")[0]["result"])
        self.assertEqual(set(receipt["model_fallbacks"]), {x["call_id"] for x in unknown})
        replay = f.runtime.handle_text("s", "search", operation_request=request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(len(self.calls), sent_count)
        self.assertEqual(f.rows("execution_effects"), effects)

    def test_sequential_candidates_continue_after_optional_failure(self):
        self.assert_completed_fallback(1)

    def test_concurrent_candidates_return_coarse_result_after_partial_timeout(self):
        self.assert_completed_fallback(3)

    def test_every_candidate_can_timeout_without_hiding_coarse_results(self):
        f = self.f
        self.install_rerank_agent(3, tuple(range(1, 10)))
        result = f.runtime.handle_text("s", "search", operation_request=f.request())
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(len(self.outputs[0]), 9)
        self.assertTrue(all(x["status"] == "UNKNOWN" for x in f.rows("execution_effects")))
        self.assertLessEqual(len(self.calls), 9)

    def install_caught_failure_agent(self, call_type, acknowledge):
        f = self.f
        class Agent(f.runtime.agent_factory):
            def handle_text(self, text):
                try:
                    timed_model_call(lambda: (_ for _ in ()).throw(TimeoutError("test")),
                        provider="dashscope", model="qwen3.7-plus", call_type=call_type,
                        usage_getter=lambda value: value)
                except TimeoutError as error:
                    if acknowledge:
                        accept_rerank_fallback(error)
                return AgentResponse(text="fallback", state=self.state.to_dict(), intent="search")
        f.runtime.agent_factory = Agent

    def test_essential_image_recognition_cannot_be_accepted_as_rerank_fallback(self):
        f = self.f
        self.install_caught_failure_agent("qwen_image_classification", True)
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text("s", "search", operation_request=f.request()))
        self.assertEqual(f.rows("execution_operations")[0]["status"], "UNKNOWN")

    def test_unacknowledged_scorer_failure_still_cannot_finish(self):
        f = self.f
        self.install_caught_failure_agent("qwen_shape_rerank", False)
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text("s", "search", operation_request=f.request()))

    def test_crash_after_fallback_does_not_make_unknown_operation_replayable(self):
        f = self.f
        self.install_rerank_agent(3)
        request = f.request()
        with patch("tiku_agent.execution_runtime.encode_response", side_effect=RuntimeError("test crash")):
            with self.assertRaises(RuntimeError):
                f.runtime.handle_text("s", "search", operation_request=request)
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text("s", "search", operation_request=request))
        self.assertLessEqual(len(self.calls), 9)

    def test_live_sent_call_cannot_be_acknowledged_as_failed_scorer(self):
        f = self.f
        row = f.ops.register("s", "local", f.request(), "handle_text", {})
        writer = f.ops.claim(row["id"])
        f.ops.bind_cost_run(writer, "test-run")
        observer = ExecutionEffects(f.ops, writer)
        observer.prepare_model(call_id="test-call", run_id="test-run", provider="dashscope",
                               model="qwen3.7-plus", call_type="qwen_shape_rerank")
        observer.model_sent("test-call")
        f.assert_code("EXECUTION_UNKNOWN", lambda: observer.accept_model_fallback("test-call", "rerank_coarse"))
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.ops.finish(
            writer, {"schema": 1, "response": None}, model_fallbacks={"test-call": "rerank_coarse"}))

    def test_length_tie_workers_preserve_effect_context_and_existing_fallback(self):
        f, owner = self.f, self
        calls = []
        def pair(*args, **kwargs):
            def send():
                calls.append(1)
                raise TimeoutError("controlled length outage")
            return timed_model_call(send, provider="dashscope", model="fixture",
                                    call_type="qwen_length_tie_break", usage_getter=lambda value: value)
        class Agent(f.runtime.agent_factory):
            def handle_text(self, text):
                rows = [{"rank": i, "path": str(owner.image), "final_score": 1.0} for i in (1, 2)]
                result = search.apply_length_tie_break(None, owner.image, rows, provider="qwen")
                owner.assertEqual([x["rank"] for x in result], [1, 2])
                return AgentResponse(text="existing candidates", state=self.state.to_dict(), intent="search")
        f.runtime.agent_factory = Agent
        with patch.object(search, "score_candidate_pair", side_effect=pair):
            result = f.runtime.handle_text("s", "search", operation_request=f.request())
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertTrue(calls)
        effects = f.rows("execution_effects")
        self.assertEqual(len(effects), len(calls))
        self.assertTrue(all(x["status"] == "UNKNOWN" for x in effects))
        receipt = json.loads(f.rows("execution_operations")[0]["result"])
        self.assertEqual(set(receipt["model_fallbacks"].values()), {"length_existing_order"})


if __name__ == "__main__":
    unittest.main()

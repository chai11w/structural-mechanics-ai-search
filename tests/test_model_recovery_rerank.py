"""Real Qwen ranking clients recover once inside a durable search operation."""

from collections import Counter
from contextlib import closing
from dataclasses import replace
import io
import json
import sqlite3
import ssl
import threading
import unittest
import urllib.error
from unittest.mock import patch

from PIL import Image

import search
from tests import test_execution_effects as fixtures
from tiku_agent.agent import AgentResponse


class ModelRecoveryRerankTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(
            self.f.store.policy, model_transport_recovery=True,
            block_on_pending_costs=False,
        )
        self.query = self.f.root / "synthetic-query.png"
        Image.new("RGB", (32, 24), "white").save(self.query)
        self.candidates = []
        self.image_ranks = {}
        for rank, color in enumerate(("red", "green", "blue"), 1):
            path = self.f.root / f"synthetic-candidate-{rank}.png"
            Image.new("RGB", (32, 24), color).save(path)
            self.candidates.append({
                "rank": rank, "name": f"candidate-{rank}",
                "path": str(path), "score": 0.8,
            })
            self.image_ranks[search.encode_rerank_image_base64(path)] = rank
        self.calls = Counter()
        self.failures = {}
        self.scores = {}
        self.outputs = []
        self.lock = threading.Lock()
        self.first_attempt_barrier = None
        self.barrier_stage = "shape"
        self.timeout_error = False

    def http(self, request, timeout=None):
        # Only the transport is replaced: payload construction, response parsing,
        # timed_model_call, search sorting and durable bookkeeping are real.
        self.assertEqual(request.full_url, "https://example.invalid/rerank")
        self.assertEqual(timeout, 12)
        content = json.loads(request.data)["messages"][1]["content"]
        stage = "length" if content[0]["text"] == search.LENGTH_TIE_PROMPT else "shape"
        images = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
        key = stage, self.image_ranks[images[-1]]
        with self.lock:
            self.calls[key] += 1
            attempt = self.calls[key]
        if self.first_attempt_barrier is not None and stage == self.barrier_stage and attempt == 1:
            self.first_attempt_barrier.wait(timeout=10)
        if attempt <= self.failures.get(key, 0):
            if self.timeout_error:
                raise TimeoutError("controlled request timeout")
            raise urllib.error.URLError(ssl.SSLEOFError(8, "controlled TLS EOF"))
        envelope = {
            "id": f"fixture-{stage}-{key[1]}-{attempt}",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "choices": [{"message": {"content": json.dumps({
                "score": self.scores.get(key, 0.8), "reason": "fixture score",
            })}}],
        }
        return io.BytesIO(json.dumps(envelope).encode())

    def rerank(self, *, workers=3):
        return search.rerank_candidates_concurrent(
            self.query, self.candidates, provider="qwen", model="qwen3.7-plus",
            endpoint="https://example.invalid/rerank", max_workers=workers,
            candidate_timeout_seconds=12, retry_timeout_seconds=12,
            # The legacy second batch must stay disabled under the observer,
            # including after both independently recorded attempts fail.
            retry_max_candidates=99, retry_max_workers=3, retry_failed_candidates=True,
        )

    def exercise(self, callback):
        owner = self

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                owner.outputs.append(callback())
                return AgentResponse(text="candidate result", state=self.state.to_dict(), intent="search")

        self.f.runtime.agent_factory = Agent
        request = self.f.request()
        with (
            patch.object(search, "DASHSCOPE_API_KEY", "isolated-test-key"),
            patch("urllib.request.urlopen", side_effect=self.http),
            patch("time.sleep"),
        ):
            response = self.f.runtime.handle_text("s", "search", operation_request=request)
            self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
            effects = self.f.rows("execution_effects")
            links = self.f.rows("execution_model_recoveries")
            sent = self.calls.copy()
            replay = self.f.runtime.handle_text("s", "search", operation_request=request)
            self.assertTrue(replay.execution_receipt["replayed"])
            self.assertEqual(self.calls, sent)
            self.assertEqual(self.f.rows("execution_effects"), effects)
            self.assertEqual(self.f.rows("execution_model_recoveries"), links)
        self.assertEqual(len(self.outputs), 1)
        self.assertEqual(len(effects), sum(self.calls.values()))
        return self.outputs[0]

    def assert_recovery_evidence(self, count, target_status="CONFIRMED", *, fallback=None):
        effects = {row["call_id"]: row for row in self.f.rows("execution_effects")}
        links = self.f.rows("execution_model_recoveries")
        self.assertEqual(len(links), count)
        target_runs = set()
        with closing(sqlite3.connect(self.f.ledger.path)) as conn:
            has_calls = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_cost_calls'").fetchone()
            ledger = ({row[0]: row[1:] for row in conn.execute(
                "SELECT call_id,run_id,total_tokens,attempt_count FROM model_cost_calls")} if has_calls else {})
        result = json.loads(self.f.rows("execution_operations")[0]["result"])
        fallbacks = result.get("model_fallbacks", {})
        for link in links:
            source = effects[link["source_call_id"]]
            target = effects[link["target_call_id"]]
            self.assertNotEqual(source["call_id"], target["call_id"])
            self.assertNotEqual(source["run_id"], target["run_id"])
            self.assertNotIn(target["run_id"], target_runs)
            target_runs.add(target["run_id"])
            self.assertEqual((source["status"], source["usage_known"]), ("UNKNOWN", 0))
            self.assertEqual(target["status"], target_status)
            for field in ("operation_id", "attempt_id", "provider", "model", "call_type"):
                self.assertEqual(source[field], target[field])
            self.assertEqual(source["provider"], "dashscope")
            for effect in (source, target):
                record = json.loads(effect["record"])
                self.assertEqual(record["call_id"], effect["call_id"])
                self.assertEqual(record["attempt_count"], 1)
            if target_status == "CONFIRMED":
                self.assertEqual(target["usage_known"], 1)
                # Known replacement usage must settle even though the original
                # parent collector still contains an UNKNOWN call.
                self.assertEqual(ledger[target["call_id"]], (target["run_id"], 120, 1))
                self.assertNotIn(source["call_id"], fallbacks)
            else:
                self.assertEqual(target["usage_known"], 0)
                self.assertNotIn(target["call_id"], ledger)
            if fallback is not None:
                self.assertEqual(fallbacks[source["call_id"]], fallback)
                self.assertEqual(fallbacks[target["call_id"]], fallback)

    def test_shape_recovery_keeps_successful_scores_without_legacy_third_send(self):
        self.failures = {("shape", 1): 1}
        self.scores = {("shape", 1): 0.98, ("shape", 2): 0.4, ("shape", 3): 0.7}
        result = self.exercise(lambda: self.rerank(workers=1))
        self.assertEqual([item["name"] for item in result], ["candidate-1", "candidate-3", "candidate-2"])
        self.assertTrue(search.rerank_results_complete(result))
        self.assertEqual(self.calls, Counter({("shape", 1): 2, ("shape", 2): 1, ("shape", 3): 1}))
        by_name = {item["name"]: item for item in result}
        self.assertAlmostEqual(by_name["candidate-2"]["rerank_score"], 0.4)
        self.assertAlmostEqual(by_name["candidate-3"]["rerank_score"], 0.7)
        self.assert_recovery_evidence(1)

    def test_three_parallel_shape_failures_recover_without_blocking_each_other(self):
        self.failures = {("shape", rank): 1 for rank in (1, 2, 3)}
        self.scores = {("shape", rank): rank / 4 for rank in (1, 2, 3)}
        self.first_attempt_barrier = threading.Barrier(3)
        result = self.exercise(self.rerank)
        self.assertTrue(search.rerank_results_complete(result))
        self.assertEqual([item["name"] for item in result], ["candidate-3", "candidate-2", "candidate-1"])
        self.assertEqual(self.calls, Counter({("shape", rank): 2 for rank in (1, 2, 3)}))
        self.assert_recovery_evidence(3)

    def test_two_shape_failures_return_original_coarse_order_with_both_unknowns(self):
        self.failures = {("shape", 1): 2}
        result = self.exercise(lambda: self.rerank(workers=1))
        self.assertEqual([item["name"] for item in result], [item["name"] for item in self.candidates])
        self.assertTrue(all(item["rerank_status"] == "incomplete" for item in result))
        self.assertTrue(all("final_score" not in item for item in result))
        self.assertEqual(self.calls, Counter({("shape", 1): 2, ("shape", 2): 1, ("shape", 3): 1}))
        self.assert_recovery_evidence(1, "UNKNOWN", fallback="rerank_coarse")

    def test_length_recovery_uses_successful_shape_results_and_correct_tie_order(self):
        for candidate in self.candidates:
            candidate["score"] = 1.0
        self.scores = {("shape", rank): 1.0 for rank in (1, 2, 3)}
        self.scores.update({("length", 1): 0.2, ("length", 2): 0.9, ("length", 3): 0.6})
        self.failures = {("length", 1): 1}
        result = self.exercise(self.rerank)
        self.assertTrue(search.rerank_results_complete(result))
        self.assertEqual([item["name"] for item in result], ["candidate-2", "candidate-3", "candidate-1"])
        self.assertTrue(all(item["rerank_score"] == 1.0 for item in result))
        expected = Counter({("shape", rank): 1 for rank in (1, 2, 3)})
        expected.update({("length", 1): 2, ("length", 2): 1, ("length", 3): 1})
        self.assertEqual(self.calls, expected)
        self.assert_recovery_evidence(1)

    def test_three_parallel_length_failures_recover_without_rescoring_shape(self):
        for candidate in self.candidates:
            candidate["score"] = 1.0
        self.scores = {("shape", rank): 1.0 for rank in (1, 2, 3)}
        self.scores.update({("length", rank): rank / 4 for rank in (1, 2, 3)})
        self.failures = {("length", rank): 1 for rank in (1, 2, 3)}
        self.first_attempt_barrier = threading.Barrier(3)
        self.barrier_stage = "length"
        result = self.exercise(self.rerank)
        self.assertEqual([item["name"] for item in result], ["candidate-3", "candidate-2", "candidate-1"])
        self.assertTrue(all(item["rerank_score"] == 1.0 for item in result))
        expected = Counter({("shape", rank): 1 for rank in (1, 2, 3)})
        expected.update({("length", rank): 2 for rank in (1, 2, 3)})
        self.assertEqual(self.calls, expected)
        self.assert_recovery_evidence(3)

    def test_two_length_failures_keep_existing_length_fallback_without_third_send(self):
        self.failures = {("length", 1): 2}
        self.timeout_error = True
        self.scores = {("length", 2): 0.4, ("length", 3): 0.6}
        rows = [dict(candidate, final_score=1.0, rerank_score=1.0, rerank_status="completed")
                for candidate in self.candidates]
        result = self.exercise(lambda: search.apply_length_tie_break(
            None, self.query, rows, timeout_seconds=12, model="qwen3.7-plus",
            provider="qwen", endpoint="https://example.invalid/rerank",
        ))
        self.assertEqual([item["name"] for item in result], [item["name"] for item in self.candidates])
        self.assertTrue(all(item["rerank_score"] == 1.0 for item in result))
        self.assertEqual(result[0]["length_score"], 0.0)
        self.assertEqual(result[0]["final_score"], search.LENGTH_TIE_FINAL_FLOOR)
        self.assertEqual((result[1]["length_score"], result[2]["length_score"]), (0.4, 0.6))
        self.assertEqual(self.calls, Counter({("length", 1): 2, ("length", 2): 1, ("length", 3): 1}))
        self.assert_recovery_evidence(1, "UNKNOWN", fallback="length_existing_order")


if __name__ == "__main__":
    unittest.main()

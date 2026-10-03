"""A failed replacement cannot cancel another live thread's eligible work."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import unittest

from tests import test_execution_effects as fixtures
from tiku_agent.agent import AgentResponse
from tiku_shared.execution_hooks import accept_model_fallback
from tiku_shared.model_costs import timed_model_call
from tiku_shared.trace_context import submit_with_trace_context


class ExecutionModelRecoveryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionEffectsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.store.policy = replace(
            self.fixture.store.policy,
            model_transport_recovery=True,
            block_on_pending_costs=False,
        )
        self.sends = Counter()
        self.send_lock = threading.Lock()

    def run_parallel(self, *, sibling_recovery, fallback):
        owner = self
        initial_calls = threading.Barrier(2)
        failed_replacement_returned = threading.Event()
        sibling_done = threading.Event()

        def invoke(provider, call_type):
            return timed_model_call(
                provider, provider="dashscope", model="qwen3.7-plus", call_type=call_type,
                usage_getter=lambda response: response["usage"],
                provider_request_id_getter=lambda response: response["id"],
            )

        def provider(label):
            with owner.send_lock:
                owner.sends[label] += 1
                attempt = owner.sends[label]
            if label == "failed" or sibling_recovery and attempt == 1:
                if sibling_recovery and attempt == 1:
                    initial_calls.wait(timeout=10)
                if label == "sibling":
                    owner.assertTrue(failed_replacement_returned.wait(timeout=10))
                raise TimeoutError("synthetic transport failure")
            return {"usage": {"input_tokens": 10, "output_tokens": 2}, "id": label}

        def failed_thread():
            try:
                invoke(lambda: provider("failed"), "qwen_a3_crop_compare")
            except TimeoutError as error:
                # Both failed receipts are already durable, but the business
                # fallback has not yet run. Hold this exact scheduling window.
                failed_replacement_returned.set()
                owner.assertTrue(sibling_done.wait(timeout=10))
                if fallback:
                    accept_model_fallback(error, "crop_manual")
            else:
                owner.fail("the replacement was expected to fail")

        def sibling_thread():
            try:
                if not sibling_recovery:
                    owner.assertTrue(failed_replacement_returned.wait(timeout=10))
                call_type = "qwen_a3_crop_compare" if sibling_recovery else "external_load_screen"
                return invoke(lambda: provider("sibling"), call_type)
            finally:
                sibling_done.set()

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [submit_with_trace_context(executor, function)
                               for function in (failed_thread, sibling_thread)]
                    for future in futures:
                        future.result(timeout=20)
                return AgentResponse(text="synthetic result", state=self.state.to_dict(), intent="greeting")

        self.fixture.runtime.agent_factory = Agent
        return self.fixture.runtime.handle_text(
            "s", "synthetic", operation_request=self.fixture.request(),
        )

    def assert_failed_pair_preserved(self, *, has_fallback):
        effects = {row["call_id"]: row for row in self.fixture.rows("execution_effects")}
        failed_links = [row for row in self.fixture.rows("execution_model_recoveries")
                        if row["target_call_id"] is not None
                        and effects[row["target_call_id"]]["status"] == "UNKNOWN"]
        self.assertEqual(len(failed_links), 1)
        pair = failed_links[0]
        for call_id in (pair["source_call_id"], pair["target_call_id"]):
            self.assertEqual(effects[call_id]["status"], "UNKNOWN")
            self.assertEqual(effects[call_id]["usage_known"], 0)
        operation = self.fixture.rows("execution_operations")[0]
        if has_fallback:
            result = json.loads(operation["result"])
            self.assertEqual(result["model_fallbacks"], {
                pair["source_call_id"]: "crop_manual",
                pair["target_call_id"]: "crop_manual",
            })
        else:
            self.assertEqual(operation["status"], "UNKNOWN")
            self.assertIsNone(operation["result"])

    def test_delayed_crop_fallback_does_not_block_sibling_replacement(self):
        response = self.run_parallel(sibling_recovery=True, fallback=True)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(self.sends, {"failed": 2, "sibling": 2})
        links = self.fixture.rows("execution_model_recoveries")
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link["target_call_id"] is not None for link in links))
        self.assert_failed_pair_preserved(has_fallback=True)

    def test_delayed_crop_fallback_does_not_block_normal_external_load_screen(self):
        response = self.run_parallel(sibling_recovery=False, fallback=True)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(self.sends, {"failed": 2, "sibling": 1})
        self.assertEqual(len(self.fixture.rows("execution_model_recoveries")), 1)
        screen = [row for row in self.fixture.rows("execution_effects")
                  if row["call_type"] == "external_load_screen"]
        self.assertEqual(len(screen), 1)
        self.assertEqual(screen[0]["status"], "CONFIRMED")
        self.assert_failed_pair_preserved(has_fallback=True)

    def test_unhandled_failed_pair_still_blocks_finish_after_sibling_success(self):
        self.fixture.assert_code("EXECUTION_UNKNOWN", lambda: self.run_parallel(
            sibling_recovery=True, fallback=False,
        ))
        self.assertEqual(self.sends, {"failed": 2, "sibling": 2})
        effects = self.fixture.rows("execution_effects")
        self.assertEqual(Counter(row["status"] for row in effects), {"UNKNOWN": 3, "CONFIRMED": 1})
        self.assert_failed_pair_preserved(has_fallback=False)


if __name__ == "__main__":
    unittest.main()

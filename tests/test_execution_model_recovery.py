"""Bounded new model attempts preserve unknown sends and durable accounting."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import ssl
import threading
import unittest
import urllib.error
from unittest.mock import patch

from tests import test_execution_effects as fixtures
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_effects import ExecutionEffects
from tiku_agent.execution_receipts import recover_accounting
from tiku_agent.execution_runtime import execute_claimed
from tiku_agent.execution_store import ExecutionError
from tiku_agent.session_runtime import AgentBudgetExceededError, AgentProtocolError
from tiku_shared.execution_hooks import accept_model_fallback
from tiku_shared.model_costs import timed_model_call
from tiku_shared.trace_context import submit_with_trace_context


def transport_failure():
    return urllib.error.URLError(ssl.SSLEOFError(8, "controlled TLS EOF"))


class ExecutionModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(
            self.f.store.policy, model_transport_recovery=True,
            block_on_pending_costs=False,
        )
        self.sends = []

    def install(self, provider, *, call_type="qwen_image_classification", fallback=None,
                catch_failure=False):
        owner = self

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                try:
                    owner.invoke(provider, call_type=call_type)
                except Exception as error:
                    if fallback:
                        accept_model_fallback(error, fallback)
                    elif not catch_failure:
                        raise
                return AgentResponse(text="business result", state=self.state.to_dict(),
                                     intent="greeting")

        self.f.runtime.agent_factory = Agent

    def invoke(self, provider, *, call_type="qwen_image_classification"):
        return timed_model_call(
            provider, provider="dashscope", model="qwen3.7-plus", call_type=call_type,
            usage_getter=lambda result: result["usage"],
            provider_request_id_getter=lambda result: result["id"],
        )

    def fail_then_succeed(self):
        self.sends.append("send")
        if len(self.sends) == 1:
            raise transport_failure()
        return {"usage": {"input_tokens": 100, "output_tokens": 20}, "id": "replacement"}

    def always_fail(self):
        self.sends.append("send")
        raise transport_failure()

    def run_request(self, request=None):
        return self.f.runtime.handle_text("s", "hello", operation_request=request or self.f.request())

    def ledger_calls(self):
        with closing(sqlite3.connect(self.f.ledger.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM model_cost_calls")]

    def assert_pair(self, target_status):
        effects = self.f.rows("execution_effects")
        self.assertEqual(len(effects), 2)
        links = self.f.rows("execution_model_recoveries")
        self.assertEqual(len(links), 1)
        by_id = {effect["call_id"]: effect for effect in effects}
        source = by_id[links[0]["source_call_id"]]
        target = by_id[links[0]["target_call_id"]]
        self.assertNotEqual(source["call_id"], target["call_id"])
        self.assertNotEqual(source["run_id"], target["run_id"])
        self.assertEqual(source["status"], "UNKNOWN")
        self.assertEqual(source["usage_known"], 0)
        self.assertEqual(target["status"], target_status)
        for name in ("operation_id", "attempt_id", "provider", "model", "call_type"):
            self.assertEqual(source[name], target[name])
        return source, target

    def test_successful_replacement_has_its_own_fee_and_preserves_unknown_source(self):
        self.install(self.fail_then_succeed)
        request = self.f.request()
        response = self.run_request(request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        source, target = self.assert_pair("CONFIRMED")
        self.assertEqual(len(self.sends), 2)
        calls = self.ledger_calls()
        self.assertEqual([(call["call_id"], call["total_tokens"], call["attempt_count"])
                          for call in calls], [(target["call_id"], 120, 1)])
        self.assertGreater(calls[0]["estimated_cost_micros"], 0)
        exposure = self.f.ops.cost_exposure("local")
        self.assertEqual(exposure["unresolved_calls"], 1)
        self.assertGreaterEqual(exposure["reserved_micros"],
                                self.f.store.policy.unknown_cost_reserve_micros)
        replay = self.run_request(request)
        self.assertTrue(replay.execution_receipt["replayed"])
        recover_accounting(self.f.ops, [self.f.ledger])
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(len(self.ledger_calls()), 1)
        self.assertEqual(next(row for row in self.f.rows("execution_effects")
                              if row["call_id"] == source["call_id"])["status"], "UNKNOWN")

    def test_two_failures_without_business_fallback_cannot_finish_or_replay(self):
        self.install(self.always_fail, catch_failure=True)
        request = self.f.request()
        self.f.assert_code("EXECUTION_UNKNOWN", lambda: self.run_request(request))
        self.assert_pair("UNKNOWN")
        self.assertEqual(len(self.sends), 2)
        self.f.assert_code("EXECUTION_UNKNOWN", lambda: self.run_request(request))
        recover_accounting(self.f.ops, [self.f.ledger])
        self.assertEqual(len(self.sends), 2)
        operation = self.f.rows("execution_operations")[0]
        self.assertEqual(operation["status"], "UNKNOWN")
        self.assertIsNone(operation["result"])

    def test_two_crop_failures_require_and_preserve_both_business_fallbacks(self):
        self.install(self.always_fail, call_type="qwen_a3_crop_compare", fallback="crop_manual")
        response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        source, target = self.assert_pair("UNKNOWN")
        result = json.loads(self.f.rows("execution_operations")[0]["result"])
        self.assertEqual(result["model_fallbacks"],
                         {source["call_id"]: "crop_manual", target["call_id"]: "crop_manual"})
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.f.ops.cost_exposure("local")["unresolved_calls"], 2)

    def test_three_concurrent_initial_failures_do_not_block_each_others_replacement(self):
        owner = self
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        counts = Counter()

        def call(label):
            def provider():
                with lock:
                    counts[label] += 1
                    attempt = counts[label]
                    owner.sends.append(label)
                if attempt == 1:
                    barrier.wait(timeout=10)
                    raise transport_failure()
                return {"usage": {"input_tokens": 10, "output_tokens": 3}, "id": label}
            return owner.invoke(provider)

        class Agent:
            config = None

            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [submit_with_trace_context(executor, call, str(i)) for i in range(3)]
                    for future in futures:
                        future.result(timeout=20)
                return AgentResponse(text="all three finished", state=self.state.to_dict(), intent="greeting")

        self.f.runtime.agent_factory = Agent
        response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(counts, {"0": 2, "1": 2, "2": 2})
        self.assertEqual(Counter(row["status"] for row in self.f.rows("execution_effects")),
                         {"UNKNOWN": 3, "CONFIRMED": 3})
        self.assertEqual(len(self.f.rows("execution_model_recoveries")), 3)
        self.assertEqual(len(self.ledger_calls()), 3)

    def test_failed_receipt_confirmation_never_triggers_a_provider_replacement(self):
        def successful_provider():
            self.sends.append("send")
            return {"usage": {"input_tokens": 10, "output_tokens": 2}, "id": "success"}

        self.install(successful_provider)
        with patch.object(ExecutionEffects, "model_finished", side_effect=TimeoutError("receipt unavailable")):
            with self.assertRaises(TimeoutError):
                self.run_request()
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_provider_failure_with_failed_receipt_does_not_recover(self):
        self.install(self.always_fail)
        with patch.object(ExecutionEffects, "model_finished", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.run_request()
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_crash_before_replacement_send_is_never_resumed_by_accounting_or_replay(self):
        self.install(self.fail_then_succeed)
        request = self.f.request()
        original = ExecutionEffects.prepare_model
        prepared = []

        class SimulatedCrash(BaseException):
            pass

        def prepare(observer, **kwargs):
            original(observer, **kwargs)
            prepared.append(kwargs["call_id"])
            if len(prepared) == 2:
                raise SimulatedCrash()

        with patch.object(ExecutionEffects, "prepare_model", prepare):
            with self.assertRaises(SimulatedCrash):
                self.run_request(request)
        self.assertEqual(len(self.sends), 1)
        recover_accounting(self.f.ops, [self.f.ledger])
        self.f.assert_code("EXECUTION_UNKNOWN", lambda: self.run_request(request))
        self.assertEqual(len(self.sends), 1)
        self.assertIsNone(self.f.rows("execution_operations")[0]["result"])

    def test_unknown_cost_limit_blocks_replacement_before_a_second_send(self):
        self.f.store.policy = replace(self.f.store.policy, block_on_pending_costs=True,
                                      max_global_unresolved_cost_calls=1)
        self.install(self.fail_then_succeed)
        with self.assertRaises((ExecutionError, AgentProtocolError)) as caught:
            self.run_request()
        self.assertEqual(caught.exception.code, "EXECUTION_COST_PENDING")
        self.assertEqual(len(self.sends), 1)
        self.assertTrue(any(row["status"] == "UNKNOWN" for row in self.f.rows("execution_effects")))

    def test_dynamic_revocation_blocks_the_replacement_send(self):
        self.install(self.fail_then_succeed)
        request = self.f.request()
        operation = self.f.ops.register("s", "local", request, "handle_text", {"text": "hello"})
        writer = self.f.ops.claim(operation["id"])

        def admission():
            if self.sends:
                raise ExecutionError("EXECUTION_STALE")

        with self.assertRaises(ExecutionError) as caught:
            execute_claimed(self.f.runtime, "s", writer,
                            lambda: self.f.runtime.handle_text("s", "hello"),
                            admission_check=admission)
        self.assertEqual(caught.exception.code, "EXECUTION_STALE")
        self.assertEqual(len(self.sends), 1)

    def test_new_budget_failure_preserves_the_first_send_instead_of_resetting_operation(self):
        self.install(self.fail_then_succeed)
        request = self.f.request()
        operation = self.f.ops.register("s", "local", request, "handle_text", {"text": "hello"})
        writer = self.f.ops.claim(operation["id"])

        def admission():
            if self.sends:
                raise AgentBudgetExceededError("replacement exceeds the remaining budget")

        with self.assertRaises(AgentBudgetExceededError):
            execute_claimed(self.f.runtime, "s", writer,
                            lambda: self.f.runtime.handle_text("s", "hello"),
                            admission_check=admission)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_operations")[0]["status"], "UNKNOWN")
        self.assertEqual(self.f.rows("execution_effects")[0]["status"], "UNKNOWN")

    def test_revocation_between_replacement_prepare_and_send_is_checked_again(self):
        self.install(self.fail_then_succeed)
        request = self.f.request()
        operation = self.f.ops.register("s", "local", request, "handle_text", {"text": "hello"})
        writer = self.f.ops.claim(operation["id"])

        def admission():
            if any(row["target_call_id"] for row in self.f.rows("execution_model_recoveries")):
                raise ExecutionError("EXECUTION_STALE")

        with self.assertRaises(ExecutionError) as caught:
            execute_claimed(self.f.runtime, "s", writer,
                            lambda: self.f.runtime.handle_text("s", "hello"),
                            admission_check=admission)
        self.assertEqual(caught.exception.code, "EXECUTION_STALE")
        self.assertEqual(len(self.sends), 1)
        self.assert_pair("PREPARED")

    def test_disabled_policy_does_not_retry(self):
        self.f.store.policy = replace(self.f.store.policy, model_transport_recovery=False)
        self.install(self.always_fail, catch_failure=True)
        self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_noneligible_call_type_keeps_its_existing_single_attempt_policy(self):
        self.install(self.always_fail, call_type="qwen_safe_answer", catch_failure=True)
        self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_parse_error_with_timeout_cause_never_recovers(self):
        def malformed_response():
            self.sends.append("send")
            try:
                raise TimeoutError("unrelated cause")
            except TimeoutError as cause:
                raise ValueError("invalid provider JSON") from cause

        self.install(malformed_response, catch_failure=True)
        self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_usage_adapter_timeout_is_not_a_transport_recovery(self):
        class BrokenUsage(dict):
            def __getitem__(self, name):
                if name == "usage":
                    raise TimeoutError("local usage adapter failure")
                return super().__getitem__(name)

        def provider():
            self.sends.append("send")
            return BrokenUsage(id="provider-already-responded")

        self.install(provider, catch_failure=True)
        self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.f.rows("execution_model_recoveries"), [])

    def test_recovery_intent_alone_cannot_authorize_a_successful_business_result(self):
        self.install(self.always_fail, catch_failure=True)
        # Interrupt only the new attempt, after the first UNKNOWN and intent
        # have been recorded. Catching that local failure cannot prove recovery.
        original = ExecutionEffects.prepare_model

        def prepare(observer, **kwargs):
            if kwargs.get("recovery_of"):
                raise RuntimeError("replacement never prepared")
            return original(observer, **kwargs)

        with patch.object(ExecutionEffects, "prepare_model", prepare):
            self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 1)
        link = self.f.rows("execution_model_recoveries")[0]
        self.assertIsNone(link["target_call_id"])
        self.assertIsNone(self.f.rows("execution_operations")[0]["result"])

    def test_generic_recovery_fallback_label_cannot_hide_failed_required_models(self):
        self.install(self.always_fail, fallback="model_recovery")
        self.f.assert_code("EXECUTION_UNKNOWN", self.run_request)
        self.assertEqual(len(self.sends), 2)
        self.assert_pair("UNKNOWN")
        self.assertIsNone(self.f.rows("execution_operations")[0]["result"])

    def test_replacement_fee_outage_is_reconciled_without_repeating_either_send(self):
        self.install(self.fail_then_succeed)
        real_connect = sqlite3.connect

        def fail_ledger(path, *args, **kwargs):
            if str(path) == str(self.f.ledger.path):
                raise sqlite3.OperationalError("isolated replacement ledger outage")
            return real_connect(path, *args, **kwargs)

        with patch("tiku_shared.model_costs.sqlite3.connect", side_effect=fail_ledger):
            response = self.run_request()
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        source, target = self.assert_pair("CONFIRMED")
        pending = self.f.rows("execution_cost_outbox")
        self.assertEqual([(row["run_id"], row["status"]) for row in pending],
                         [(target["run_id"], "PENDING")])
        recover_accounting(self.f.ops, [self.f.ledger])
        recover_accounting(self.f.ops, [self.f.ledger])
        self.assertEqual(len(self.sends), 2)
        self.assertEqual([row["call_id"] for row in self.ledger_calls()], [target["call_id"]])
        self.assertEqual(self.f.ops.cost_exposure("local")["unresolved_calls"], 1)


if __name__ == "__main__":
    unittest.main()

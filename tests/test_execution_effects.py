from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_agent.agent import AgentResponse
from tiku_agent.execution_effects import ExecutionEffects, reconcile_cost, stage_unwritten_cost
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionError, ExecutionSessionStore, ExecutionStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentProtocolError, AgentSessionRuntime
from tiku_shared.model_costs import ModelCostCollector, SQLiteModelCostLedger, ModelCostConflict, timed_model_call


class ExecutionEffectsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ExecutionStore(self.root / "execution.db")
        self.ledger = SQLiteModelCostLedger(self.root / "costs.db")
        self.calls = []
        owner = self

        class Agent:
            config = None
            def __init__(self, state):
                self.state = state

            def handle_text(self, text):
                def provider():
                    owner.calls.append(text)
                    if text == "timeout":
                        raise TimeoutError("injected network failure")
                    return {"usage": {} if text == "missing_usage" else {"input_tokens": 100, "output_tokens": 20}, "id": "provider-result"}

                def invoke():
                    return timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                                            call_type="test_effect", usage_getter=lambda value: value["usage"],
                                            provider_request_id_getter=lambda value: value["id"])
                try:
                    invoke()
                    if text == "schema_retry":
                        invoke()  # confirmed output failed a local schema check
                except TimeoutError:
                    try:
                        invoke()  # recovery code cannot replay an unknown network effect
                    except ExecutionError:
                        pass
                return AgentResponse(text="saved result", state=self.state.to_dict(), intent="greeting")

        self.runtime = AgentSessionRuntime(ExecutionSessionStore(self.store),
            artifacts=SessionArtifacts(self.root / "media"), agent_factory=Agent, cost_ledger=self.ledger)
        attach_execution(self.runtime, self.store)
        self.ops = self.runtime.execution_operations

    def request(self, sid="s"):
        context = self.store.context(sid)
        return OperationRequest(uuid4().hex, context["epoch"], context["state_version"])

    def rows(self, table):
        with self.store.transaction() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def assert_code(self, code, callback):
        with self.assertRaises((ExecutionError, AgentProtocolError)) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_success_has_persistent_call_identity_and_exact_fee_receipt(self):
        request = self.request()
        self.runtime.handle_text("s", "success", operation_request=request)
        replay = self.runtime.handle_text("s", "success", operation_request=request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(self.calls, ["success"])
        effects = self.rows("execution_effects")
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertEqual(effect["status"], "CONFIRMED")
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT call_id,run_id,total_tokens FROM model_cost_calls").fetchone(),
                             (effect["call_id"], effect["run_id"], 120))
        self.assertEqual(self.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")

    def test_schema_correction_is_two_identified_and_charged_calls(self):
        self.runtime.handle_text("s", "schema_retry", operation_request=self.request())
        effects = self.rows("execution_effects")
        self.assertEqual(len({row["call_id"] for row in effects}), 2)
        self.assertEqual(len({row["attempt_id"] for row in effects}), 1)
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT call_count,total_tokens FROM model_cost_runs").fetchone(), (2, 240))

    def test_unknown_send_cannot_be_replayed_even_when_business_catches_error(self):
        request = self.request()
        self.assert_code("EXECUTION_UNKNOWN", lambda: self.runtime.handle_text("s", "timeout", operation_request=request))
        self.assert_code("EXECUTION_UNKNOWN", lambda: self.runtime.handle_text("s", "timeout", operation_request=request))
        self.assertEqual(self.calls, ["timeout"])
        self.assertEqual(self.rows("execution_effects")[0]["status"], "UNKNOWN")
        self.assert_code("EXECUTION_COST_PENDING", lambda: self.runtime.handle_text("other", "new", operation_request=self.request("other")))

    def test_before_send_failure_keeps_prepared_proof_and_no_provider_call(self):
        with patch.object(ExecutionEffects, "model_sent", side_effect=RuntimeError("before send")):
            with self.assertRaises(RuntimeError):
                self.runtime.handle_text("s", "success", operation_request=self.request())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows("execution_effects")[0]["status"], "PREPARED")

    def test_response_received_but_confirmation_failed_is_unknown_without_replay(self):
        request = self.request()
        with patch.object(ExecutionEffects, "model_finished", side_effect=RuntimeError("before confirmation")):
            with self.assertRaises(RuntimeError):
                self.runtime.handle_text("s", "success", operation_request=request)
        self.assert_code("EXECUTION_UNKNOWN", lambda: self.runtime.handle_text("s", "success", operation_request=request))
        self.assertEqual(self.calls, ["success"])
        self.assertEqual(self.rows("execution_effects")[0]["status"], "UNKNOWN")

    def test_fee_write_failure_preserves_business_and_reconcile_only_writes_fees(self):
        real_connect = sqlite3.connect
        def fail_ledger(path, *args, **kwargs):
            if Path(path) == self.ledger.path:
                raise sqlite3.OperationalError("injected fee storage outage")
            return real_connect(path, *args, **kwargs)
        request = self.request()
        with patch("tiku_shared.model_costs.sqlite3.connect", side_effect=fail_ledger):
            response = self.runtime.handle_text("s", "success", operation_request=request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        outbox = self.rows("execution_cost_outbox")[0]
        self.assertEqual(outbox["status"], "PENDING")
        self.assertEqual(self.ops.observe("s", "local", request)["accounting"], {"pending_runs": 1})
        self.assert_code("EXECUTION_COST_PENDING", lambda: self.runtime.handle_text("s", "new", operation_request=self.request()))
        reconcile_cost(self.ops, self.ledger, outbox["run_id"])
        reconcile_cost(self.ops, self.ledger, outbox["run_id"])
        self.assertEqual(self.calls, ["success"])
        self.assertEqual(self.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM model_cost_calls").fetchone()[0], 1)

    def test_fee_commit_ack_lost_reconciles_without_duplicate_or_overwrite(self):
        with patch.object(ExecutionEffects, "cost_confirmed", side_effect=RuntimeError("lost ACK")):
            self.runtime.handle_text("s", "success", operation_request=self.request())
        outbox = self.rows("execution_cost_outbox")[0]
        self.assertEqual(outbox["status"], "PENDING")
        reconcile_cost(self.ops, self.ledger, outbox["run_id"])
        payload = json.loads(outbox["payload"])
        from tiku_shared.model_costs import ModelCallRecord
        changed = ModelCostCollector(**payload["collector"])
        changed._records = [replace(ModelCallRecord(**payload["records"][0]), total_tokens=999)]
        with self.assertRaises(ModelCostConflict):
            self.ledger.write_run(changed, finished_at=payload["finished_at"], outcome=payload["outcome"], idempotent=True)
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT total_tokens FROM model_cost_calls").fetchone()[0], 120)
        self.assertEqual(self.calls, ["success"])

    def test_reconcile_rejects_wrong_ledger(self):
        self.runtime.handle_text("s", "success", operation_request=self.request())
        run_id = self.rows("execution_cost_outbox")[0]["run_id"]
        self.assert_code("EXECUTION_COST_TARGET_INVALID", lambda: reconcile_cost(self.ops, SQLiteModelCostLedger(self.root/"other.db"), run_id))

    def test_missing_provider_usage_remains_pending_instead_of_zero_cost(self):
        request = self.request()
        response = self.runtime.handle_text("s", "missing_usage", operation_request=request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(self.rows("execution_cost_outbox"), [])
        effect = self.rows("execution_effects")[0]
        self.assertEqual(effect["status"], "CONFIRMED")
        self.assertEqual(effect["usage_known"], 0)
        self.assert_code("EXECUTION_COST_PENDING", lambda: stage_unwritten_cost(self.ops, self.ledger, effect["run_id"]))
        self.assert_code("EXECUTION_COST_PENDING", lambda: self.runtime.handle_text("s", "new", operation_request=self.request()))

    def test_crash_before_outbox_can_stage_confirmed_fees_without_rerun(self):
        with patch.object(ExecutionEffects, "prepare_cost", side_effect=RuntimeError("crash before outbox")):
            self.runtime.handle_text("s", "success", operation_request=self.request())
        self.assertEqual(self.rows("execution_cost_outbox"), [])
        run_id = self.rows("execution_effects")[0]["run_id"]
        fingerprint = stage_unwritten_cost(self.ops, self.ledger, run_id)
        self.assertEqual(fingerprint, stage_unwritten_cost(self.ops, self.ledger, run_id))
        reconcile_cost(self.ops, self.ledger, run_id)
        self.assertEqual(self.calls, ["success"])
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT total_tokens,outcome FROM model_cost_runs").fetchone(), (120, "interrupted"))

    def test_independent_ledger_connections_reconcile_same_run_atomically(self):
        with patch.object(ExecutionEffects, "cost_confirmed", side_effect=RuntimeError("lost ACK")):
            self.runtime.handle_text("s", "success", operation_request=self.request())
        run_id = self.rows("execution_cost_outbox")[0]["run_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reconcile_cost, self.ops, SQLiteModelCostLedger(self.ledger.path), run_id) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
        with closing(sqlite3.connect(self.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM model_cost_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM model_cost_calls").fetchone()[0], 1)

    def test_qwen_transport_does_not_retry_unknown_request_when_enabled(self):
        import urllib.request
        from scripts.classify_question_bank import request_json_with_retry
        from tiku_shared.execution_hooks import execution_effect_scope
        request = urllib.request.Request("https://example.invalid/model")
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as send:
            with execution_effect_scope(object()), self.assertRaises(TimeoutError):
                request_json_with_retry(request, timeout=1, retry_delays=(0, 0))
            self.assertEqual(send.call_count, 1)
        with patch("urllib.request.urlopen", side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError):
                request_json_with_retry(request, timeout=1, retry_delays=(0,))
            self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()

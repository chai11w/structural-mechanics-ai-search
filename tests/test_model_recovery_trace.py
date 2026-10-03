"""Recovery accounting must use the registered trace outcome vocabulary."""

from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_execution_model_recovery as fixtures
from tiku_agent.execution_effects import ExecutionEffects
from tiku_shared.model_costs import ModelCostCollector, new_run_id
from tiku_shared.trace_context import TraceContext, trace_context_scope
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder, trace_event_scope


class ModelRecoveryTraceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionModelRecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = SQLiteTraceEventStore(self.f.f.root / "recovery-trace.sqlite3")
        self.recorder = TraceEventRecorder(self.store)
        self.addCleanup(self.recorder.close)
        self.trace = TraceContext.create(request_id="req_recovery_trace_regression")

    def recorded(self, callback):
        with trace_context_scope(self.trace), trace_event_scope(
            self.recorder, trace_id=self.trace.trace_id, request_id=self.trace.request_id,
        ):
            result = callback()
        self.assertTrue(self.recorder.flush())
        health = self.recorder.health()
        self.assertEqual(health["validation_rejections"], 0)
        self.assertEqual(health["dropped"], 0)
        return result, self.store.events_for_trace(self.trace.trace_id)

    def test_successful_recovery_writes_its_cost_event_without_degrading_trace_health(self):
        self.f.install(self.f.fail_then_succeed)
        response, events = self.recorded(self.f.run_request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        source, target = self.f.assert_pair("CONFIRMED")
        written = [event for event in events if event.event_type == "cost_run_written"]
        self.assertEqual([(event.run_id, event.outcome) for event in written],
                         [(target["run_id"], "success")])
        self.assertEqual(written[0].safe_attributes["call_count"], 1)
        self.assertEqual(written[0].safe_attributes["total_tokens"], 120)
        finished = [event for event in events if event.event_type == "model_call_finished"]
        self.assertEqual({event.call_id: event.outcome for event in finished},
                         {source["call_id"]: "error", target["call_id"]: "success"})
        with closing(sqlite3.connect(self.f.f.ledger.path)) as connection:
            rows = connection.execute("SELECT run_id,outcome FROM model_cost_runs").fetchall()
        self.assertEqual(rows, [(target["run_id"], "success")])
        self.assertEqual(len(self.f.sends), 2)

    def test_second_failure_uses_error_outcome_and_preserves_both_unknown_receipts(self):
        self.f.install(self.f.always_fail, call_type="qwen_a3_crop_compare", fallback="crop_manual")
        outcomes = []
        original = ExecutionEffects.write_recovery_cost

        def write_cost(observer, collector, *, finished_at, outcome):
            outcomes.append(outcome)
            return original(observer, collector, finished_at=finished_at, outcome=outcome)

        with patch.object(ExecutionEffects, "write_recovery_cost", write_cost):
            response, events = self.recorded(self.f.run_request)
        self.assertEqual(response.execution_receipt["status"], "SUCCEEDED")
        source, target = self.f.assert_pair("UNKNOWN")
        self.assertEqual(outcomes, ["error"])
        finished = [event for event in events if event.event_type == "model_call_finished"]
        self.assertEqual({event.call_id: event.outcome for event in finished},
                         {source["call_id"]: "error", target["call_id"]: "error"})
        # Unknown usage intentionally does not produce a settled ledger run or
        # cost_run_written event; it is not a validation rejection or a free call.
        self.assertFalse(any(event.event_type == "cost_run_written" for event in events))
        self.assertEqual(self.f.f.rows("execution_cost_outbox"), [])
        self.assertEqual(self.f.f.ops.cost_exposure("local")["unresolved_calls"], 2)
        self.assertEqual(len(self.f.sends), 2)

    def test_error_outcome_of_settled_no_send_run_is_valid_and_old_ledger_is_immutable(self):
        # A replacement denied before sending can have an empty, settled run.
        # Its "error" event must validate, while old ledger outcomes stay intact.
        ledger = self.f.f.ledger
        previous = ModelCostCollector(run_id=new_run_id(), task_kind="image")
        ledger.write_run(previous, finished_at="2026-10-03T00:00:00+00:00",
                         outcome="model_recovery_succeeded")
        current = ModelCostCollector(run_id=new_run_id(), trace_id=self.trace.trace_id, task_kind="image")
        _, events = self.recorded(lambda: ledger.write_run(
            current, finished_at="2026-10-03T00:01:00+00:00", outcome="error"))
        self.assertEqual([(event.event_type, event.run_id, event.outcome) for event in events],
                         [("cost_run_written", current.run_id, "error")])
        with closing(sqlite3.connect(ledger.path)) as connection:
            rows = dict(connection.execute("SELECT run_id,outcome FROM model_cost_runs"))
        self.assertEqual(rows[previous.run_id], "model_recovery_succeeded")
        self.assertEqual(rows[current.run_id], "error")


if __name__ == "__main__":
    unittest.main()

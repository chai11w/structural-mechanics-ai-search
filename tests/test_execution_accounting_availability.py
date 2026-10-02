"""Pending historical accounting must not disable normal search service."""
from dataclasses import replace
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.test_execution_effects import ExecutionEffectsTests
from tests.test_execution_dispatch import DispatchFixture
from tiku_agent.execution_operations import OperationStore
from tiku_agent.execution_retention import execution_policy
from tiku_agent.execution_store import ExecutionPolicy, ExecutionStore, digest


class AccountingAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.f = ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = ExecutionPolicy()

    def unknown(self, sid):
        f = self.f
        request = f.request(sid)
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text(
            sid, "timeout", operation_request=request))
        return request

    def test_old_policy_without_new_field_uses_nonblocking_default(self):
        f = self.f
        old = vars(ExecutionPolicy()).copy()
        del old["block_on_pending_costs"]
        with f.store.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO execution_meta VALUES ('execution_policy',?)",
                         (json.dumps(old),))
            self.assertFalse(execution_policy(conn).block_on_pending_costs)

    def test_more_than_ten_unknown_calls_keep_new_work_available_and_old_evidence(self):
        f = self.f
        requests = [self.unknown(f"old-{i}") for i in range(12)]
        prior = f.rows("execution_effects")
        self.assertEqual(f.ops.cost_exposure("local")["unresolved_calls"], 12)
        # Still work even beyond the previous global limit, without changing it.
        f.store.policy = replace(f.store.policy, max_global_unresolved_cost_calls=1)
        result = f.runtime.handle_text("new", "success", operation_request=f.request("new"))
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(f.rows("execution_effects")[:12], prior)
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text(
            "old-0", "timeout", operation_request=requests[0]))
        self.assertEqual(f.calls, ["timeout"] * 12 + ["success"])

    def test_restart_does_not_turn_pending_history_into_lockout(self):
        f = self.f
        self.unknown("old")
        restarted = OperationStore(ExecutionStore(f.store.path))
        restarted.ensure_cost_available(identity_digest=digest("local"))
        self.assertEqual(restarted.cost_exposure("local")["unresolved_calls"], 1)

    def test_unknown_contingency_does_not_exhaust_daily_budget(self):
        f = self.f
        self.unknown("old")
        f.runtime._per_identity_daily_budget_micros = 500_000
        f.runtime._daily_budget_micros = 500_000
        f.runtime.ensure_budget_available("local")
        self.assertEqual(f.ops.cost_exposure("local")["reserved_micros"], 1_000_000)

    def test_recorded_spending_still_enforces_both_daily_limits(self):
        f = self.f
        self.unknown("old")
        with patch.object(f.ledger, "estimated_cost_micros_since", return_value=500_000):
            f.runtime._per_identity_daily_budget_micros = 500_000
            f.assert_code("INVITE_DAILY_QUOTA_EXCEEDED", lambda: f.runtime.ensure_budget_available("local"))
            f.runtime._daily_budget_micros = 500_000
            f.assert_code("GLOBAL_DAILY_QUOTA_EXCEEDED", lambda: f.runtime.ensure_budget_available("local"))

    def test_lost_usage_allows_next_step_without_settling_missing_usage(self):
        f = self.f
        first = f.runtime.handle_text("same", "missing_usage", operation_request=f.request("same"))
        prior = f.rows("execution_effects")[0]
        second = f.runtime.handle_text("same", "success", operation_request=f.request("same"))
        self.assertEqual(first.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(second.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(f.rows("execution_effects")[0], prior)
        self.assertEqual(prior["usage_known"], 0)

    def test_ledger_write_outage_does_not_block_following_calls(self):
        f = self.f
        connect = sqlite3.connect
        def fail(path, *args, **kwargs):
            if str(path) == str(f.ledger.path):
                raise sqlite3.OperationalError("test outage")
            return connect(path, *args, **kwargs)
        with patch("tiku_shared.model_costs.sqlite3.connect", side_effect=fail):
            for sid in ("first", "second"):
                result = f.runtime.handle_text(sid, "success", operation_request=f.request(sid))
                self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual([r["status"] for r in f.rows("execution_cost_outbox")], ["PENDING"] * 2)
        self.assertEqual(f.calls, ["success", "success"])

    def test_conflicting_ledger_keeps_evidence_but_does_not_block_independent_task(self):
        f = self.f
        f.runtime.handle_text("old", "success", operation_request=f.request("old"))
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_cost_outbox SET status='CONFLICT'")
        previous = f.rows("execution_cost_outbox")[0]
        result = f.runtime.handle_text("new", "success", operation_request=f.request("new"))
        self.assertEqual(result.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(f.rows("execution_cost_outbox")[0], previous)

    def test_queued_and_new_tasks_execute_after_pending_limit_is_exceeded(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            f.authority.policy = replace(f.authority.policy, max_global_unresolved_cost_calls=1)
            _, old_req, old_grant = f.accept("timeout", sid="old")
            _, queued_req, queued_grant = f.accept(sid="queued")
            self.assertTrue(f.worker.run_once())
            self.assertTrue(f.worker.run_once())
            self.assertEqual(f.observe(queued_req, queued_grant, "queued")["status"], "SUCCEEDED")
            _, new_req, new_grant = f.accept(sid="new")
            self.assertTrue(f.worker.run_once())
            self.assertEqual(f.observe(new_req, new_grant, "new")["status"], "SUCCEEDED")
            self.assertEqual(f.observe(old_req, old_grant, "old")["status"], "UNKNOWN")
            self.assertEqual(f.calls, ["timeout", "hello", "hello"])


if __name__ == "__main__":
    unittest.main()

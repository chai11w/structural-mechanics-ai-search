"""The emergency exception admits new work without settling an unknown call."""
import unittest
from unittest.mock import patch

import test_execution_effects as fixtures


class CostAdmissionIncidentTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text(
            "old", "timeout", operation_request=f.request("old")))
        effect = f.rows("execution_effects")[0]
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_operations SET status='CANCELLED' WHERE id=?",
                         (effect["operation_id"],))
            conn.execute("UPDATE execution_effects SET record=NULL WHERE call_id=?", (effect["call_id"],))
        self.effect = f.rows("execution_effects")[0]
        incident = tuple(self.effect[k] for k in ("call_id", "run_id", "operation_id", "updated"))
        self.scope = patch("tiku_agent.execution_operations._COST_ADMISSION_INCIDENT", incident)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_new_work_runs_without_rewriting_or_replaying_old_evidence(self):
        f = self.f
        f.ops.ensure_cost_available()
        reply = f.runtime.handle_text("new", "success", operation_request=f.request("new"))
        self.assertEqual(reply.execution_receipt["status"], "SUCCEEDED")
        self.assertEqual(f.calls, ["timeout", "success"])
        self.assertEqual(f.rows("execution_effects")[0], self.effect)
        self.assertEqual(len(f.rows("execution_cost_outbox")), 1)

    def test_another_unknown_call_is_still_blocking(self):
        f = self.f
        f.assert_code("EXECUTION_UNKNOWN", lambda: f.runtime.handle_text(
            "new", "timeout", operation_request=f.request("new")))
        f.assert_code("EXECUTION_COST_PENDING", f.ops.ensure_cost_available)

    def test_changed_evidence_revokes_the_exception(self):
        f = self.f
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_effects SET updated=updated+1 WHERE call_id=?",
                         (self.effect["call_id"],))
        f.assert_code("EXECUTION_COST_PENDING", f.ops.ensure_cost_available)


if __name__ == "__main__":
    unittest.main()

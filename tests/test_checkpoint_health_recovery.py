"""Historical failure, current recovery, and submission-budget diagnostics."""
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_checkpoint_async as fixture
from tiku_agent.checkpoint_submission_budget import CheckpointSubmissionBudget
from tiku_agent.fastapi_demo import _checkpoint_evidence_health
from scripts.run_tiku_agent_8790 import _combined_checkpoint_health


class CheckpointHealthRecoveryTests(unittest.TestCase):
    setUp = fixture.AsyncA2Test.setUp
    submit_route = fixture.AsyncA2Test.submit_route
    assert_drained = fixture.AsyncA2Test.assert_drained

    def test_success_clears_current_admission_failure_but_keeps_history(self):
        self.submit_route(budget=CheckpointSubmissionBudget(0))
        failed = self.recorder.health()
        self.assertEqual(failed["status"], "degraded")
        self.assertTrue(failed["last_failure_at"])
        self.assertEqual(failed["submission_budget"]["stage"], "prepare")
        self.submit_route()
        self.assert_drained()
        recovered = self.recorder.health()
        self.assertEqual((recovered["status"], recovered["current_reasons"]), ("ok", []))
        for key in ("last_failure_code", "last_failure_at", "submission_budget"):
            self.assertEqual(recovered[key], failed[key])
        self.assertEqual(recovered["counters"]["rejected"], 1)
        self.assertEqual(recovered["counters"]["recoveries"], 1)

    def test_older_inflight_success_cannot_clear_a_newer_request_rejection(self):
        entered, release = Event(), Event()
        execute = self.recorder._execute
        def held(job):
            entered.set()
            self.assertTrue(release.wait(5))
            return execute(job)
        with patch.object(self.recorder, "_execute", held):
            try:
                self.submit_route()
                self.assertTrue(entered.wait(3))
                self.submit_route(budget=CheckpointSubmissionBudget(0))
            finally:
                release.set()
            self.assert_drained()
        self.assertEqual(self.recorder.health()["current_reasons"], ["capture_request_budget_exhausted"])
        self.submit_route()
        self.assert_drained()
        self.assertEqual(self.recorder.health()["status"], "ok")

    def test_slow_lease_exhausts_elapsed_budget_without_queueing_or_leaking_lease(self):
        ticks = [1.0]
        lease = self.engine.bank_catalog.lease
        def slow_lease():
            result = lease()
            ticks[0] += .101
            return result
        with patch("tests.test_checkpoint_async.perf_counter", lambda: ticks[0]), \
                patch("tiku_agent.checkpoint_async.perf_counter", lambda: ticks[0]), \
                patch.object(self.engine.bank_catalog, "lease", slow_lease):
            result = self.submit_route(budget=CheckpointSubmissionBudget(.1))
        self.assertEqual(result.reason_code, "CAPTURE_REQUEST_BUDGET_EXHAUSTED")
        health = self.recorder.health()
        self.assertEqual(health["counters"]["queued"], 0)
        self.assertEqual(health["submission_budget"]["stage"], "bank_lease")
        self.assertGreaterEqual(health["submission_budget"]["spent_ms"], 100)
        self.assertEqual(health["submission_budget"]["limit_ms"], 100)
        self.submit_route()
        self.assert_drained()
        self.assertEqual(self.recorder.health()["status"], "ok")

    def test_consumer_failure_history_survives_actual_recovery(self):
        with patch.object(self.recorder, "_execute", side_effect=OSError("private failure")):
            self.submit_route()
            self.assert_drained()
        failed = self.recorder.health()
        self.submit_route()
        self.assert_drained()
        recovered = self.recorder.health()
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["last_failure_code"], failed["last_failure_code"])
        self.assertEqual(recovered["last_failure_at"], failed["last_failure_at"])
        self.assertEqual(recovered["counters"]["recoveries"], 1)

    def test_public_projection_preserves_history_and_only_whitelisted_budget_fields(self):
        self.submit_route(budget=CheckpointSubmissionBudget(0))
        self.submit_route()
        self.assert_drained()
        store = SimpleNamespace(health=lambda: {"status": "ok", "accepting": True})
        runner = SimpleNamespace(health=lambda: {"status": "ok"})
        raw = _combined_checkpoint_health(store, runner, self.recorder)
        raw["submission_budget"]["private_path"] = "must not escape"
        public = _checkpoint_evidence_health(lambda: raw)
        self.assertEqual(public["status"], "ok")
        self.assertEqual(public["last_failure_at"], raw["last_failure_at"])
        self.assertEqual(set(public["submission_budget"]), {"stage", "spent_ms", "limit_ms"})
        for value in ({"stage": []}, {"stage": "private_path", "spent_ms": 1, "limit_ms": 1},
                      {"stage": "prepare", "spent_ms": True, "limit_ms": 1}):
            raw["submission_budget"] = value
            self.assertEqual(_checkpoint_evidence_health(lambda: raw)["submission_budget"], {})


if __name__ == "__main__":
    unittest.main()

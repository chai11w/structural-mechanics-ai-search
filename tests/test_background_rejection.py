"""Admission proof must distinguish rejection from a lost durable ACK."""
import gc
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_background_http import BackgroundHttpFixture
from tiku_agent.execution_store import ExecutionError
from tiku_agent.session_runtime import AgentBudgetExceededError


class BackgroundRejectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(gc.collect)
        self.h = BackgroundHttpFixture(directory.name)

    def test_real_queue_rejection_then_same_session_manual_retry(self):
        h = self.h
        body = {"kind": "handle_text", "parameters": {"text": "queue retry"}}
        with patch.object(h.f.dispatch, "claim_next", return_value=None), TestClient(h.app) as client:
            for _ in range(3):
                client.cookies.delete("tiku_phase6_session")
                context = h.login(client)
                self.assertEqual(client.post("/api/jobs", json=body, headers=h.headers(context)).status_code, 202)
            client.cookies.delete("tiku_phase6_session")
            context = h.login(client)
            headers = h.headers(context)
            rejected = client.post("/api/jobs", json=body, headers=headers)
            self.assertEqual((rejected.status_code, rejected.json().get("admission")), (429, "rejected"))
            envelope = json.loads(headers["X-Tiku-Operation"])
            missing = client.get("/api/jobs/lookup", params={"key": envelope["key"], "epoch": envelope["epoch"]})
            self.assertEqual(missing.status_code, 404)
            self.assertNotIn("admission", missing.json(), "absence alone cannot prove an earlier POST has finished")
            self.assertEqual(len(h.f.rows("execution_dispatch")), 3)
            self.assertEqual(h.f.calls, [])
            h.f.clock[0] += 56
            retry = client.post("/api/jobs", json=body, headers=h.headers(context))
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(len(h.f.rows("execution_dispatch")), 4)

    def test_input_and_quota_rejections_have_no_operation(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            invalid = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": ""}}, headers=h.headers(context))
            self.assertEqual(invalid.json().get("admission"), "rejected")
            oversized = client.post("/api/jobs/image", content=b"x", headers={**h.headers(context), "Content-Length": str(16 * 1024 * 1024)})
            self.assertEqual((oversized.status_code, oversized.json().get("admission")), (413, "rejected"))
            with patch.object(h.f.runtime, "ensure_budget_available", side_effect=AgentBudgetExceededError("private")):
                quota = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "quota"}}, headers=h.headers(context))
            self.assertEqual(quota.json().get("admission"), "rejected")
            self.assertEqual(h.f.rows("execution_dispatch"), [])
            self.assertEqual(h.f.calls, [])

    def test_postcommit_lost_ack_and_diagnostic_failure_are_never_rejections(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            for diagnostic in (False, True):
                with self.subTest(diagnostic=diagnostic):
                    headers = h.headers(context)
                    original = h.f.dispatch.accept
                    def lose_ack(*args, **kwargs):
                        original(*args, **kwargs)
                        raise RuntimeError("response lost after durable commit")
                    target = patch("tiku_agent.background_http.record_trace_event", side_effect=ExecutionError("EXECUTION_CAPACITY")) if diagnostic else patch.object(h.f.dispatch, "accept", side_effect=lose_ack)
                    with target:
                        response = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "committed"}}, headers=headers)
                    self.assertNotIn("admission", response.json())
                    envelope = json.loads(headers["X-Tiku-Operation"])
                    found = client.get("/api/jobs/lookup", params={"key": envelope["key"], "epoch": envelope["epoch"]})
                    self.assertEqual(found.status_code, 200, found.text)
                    self.assertEqual(h.wait_result(client, found.json()["job"]["operation_id"])["publication"]["status"], "READY")
                    context = client.post("/api/jobs/session", headers={"X-Tiku-Background": "1"}).json()["execution"]
            self.assertEqual(h.f.calls, ["committed", "committed"])

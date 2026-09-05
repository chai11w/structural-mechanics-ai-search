from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tiku_admin.app import create_admin_app
from tiku_admin.control_store import SQLiteControlStore
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_diagnostics import DiagnosticQueryService
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent


class AdminTraceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tiku-admin-trace-")
        self.root = Path(self.temporary.name)
        self.control = SQLiteControlStore(self.root / "control.sqlite3")
        self.control.initialize_admin("admin-password-123")
        self.feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        self.reporter = AdminReporter(
            control_store=self.control,
            cost_database=self.root / "model_costs.sqlite3",
            feedback_store=self.feedback,
        )
        trace_id = "trace_" + "a" * 32
        trace_store = SQLiteTraceEventStore(self.root / "trace_events.sqlite3")
        trace_store.write(
            TraceEvent.create(
                trace_id=trace_id,
                event_type="request_received",
                stage="http",
                outcome="started",
                request_id="request_" + "b" * 32,
                session_key="session-private",
                identity_key="invite-owner",
                safe_attributes={
                    "method": "GET",
                    "endpoint": "/api/image",
                    "response_mode": "json",
                },
            )
        )
        trace_store.close()
        self.trace_id = trace_id

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _client(self, *, diagnostics: bool = True) -> TestClient:
        return TestClient(
            create_admin_app(
                control_store=self.control,
                reporter=self.reporter,
                feedback_store=self.feedback,
                diagnostic_query=(
                    DiagnosticQueryService(self.root) if diagnostics else None
                ),
            )
        )

    def _login(self, client: TestClient) -> None:
        response = client.post(
            "/api/admin/login", json={"password": "admin-password-123"}
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_trace_endpoint_requires_admin_and_returns_privacy_bounded_package(self):
        client = self._client()
        self.assertEqual(
            client.get(f"/api/admin/operations/traces/{self.trace_id}").status_code,
            401,
        )

        self._login(client)
        response = client.get(
            f"/api/admin/operations/traces/{self.trace_id}", params={"limit": 1}
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["query"]["selector"], "trace_id")
        self.assertEqual(payload["summary"]["trace_count"], 1)
        self.assertEqual(payload["query"]["limit"], 1)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("F:\\", rendered)
        self.assertNotIn("session-private", rendered)
        self.assertNotIn("request_id", rendered)
        self.assertNotIn("safe_attributes", rendered)
        self.assertIn("trace_events.sqlite3", rendered)

    def test_trace_endpoint_validates_query_and_reports_missing_or_unavailable(self):
        client = self._client()
        self._login(client)
        for path, expected in (
            ("bad", 400),
            ("trace_" + "0" * 32, 404),
        ):
            self.assertEqual(
                client.get(f"/api/admin/operations/traces/{path}").status_code,
                expected,
            )
        self.assertEqual(
            client.get(
                f"/api/admin/operations/traces/{self.trace_id}",
                params={"limit": 101},
            ).status_code,
            400,
        )
        unavailable = self._client(diagnostics=False)
        self._login(unavailable)
        self.assertEqual(
            unavailable.get(
                f"/api/admin/operations/traces/{self.trace_id}"
            ).status_code,
            503,
        )


if __name__ == "__main__":
    unittest.main()

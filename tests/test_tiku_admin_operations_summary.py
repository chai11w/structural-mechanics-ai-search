from __future__ import annotations

import hashlib
import sqlite3
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


class AdminOperationsSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tiku-admin-summary-")
        self.root = Path(self.temporary.name)
        self.control = SQLiteControlStore(self.root / "control.sqlite3")
        self.control.initialize_admin("admin-password-123")
        self.feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        self.costs = self.root / "model_costs.sqlite3"
        self.traces = self.root / "trace_events.sqlite3"
        self._write_trace(
            "a",
            "REQUEST_SUCCEEDED",
            "tool",
            "2026-09-05T00:01:00+00:00",
            "resp_" + "1" * 32,
        )
        self._write_trace(
            "b",
            "NO_MATCH",
            "tool",
            "2026-09-05T00:02:00+00:00",
            "resp_" + "2" * 32,
        )
        self._write_trace(
            "c",
            "MEDIA_ANSWERS_PARTIAL",
            "media",
            "2026-09-05T00:03:00+00:00",
            "resp_" + "3" * 32,
        )
        with sqlite3.connect(self.costs) as connection:
            connection.execute(
                "CREATE TABLE model_cost_calls (provider TEXT, model TEXT, call_type TEXT, status TEXT, finished_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO model_cost_calls VALUES (?, ?, ?, ?, ?)",
                [
                    ("deepseek", "v4", "vision", "error", "2026-09-05T00:04:00+00:00"),
                    ("deepseek", "v4", "vision", "error", "2026-09-05T00:05:00+00:00"),
                    ("qwen", "vl", "text", "success", "2026-09-05T00:06:00+00:00"),
                ],
            )
        self.reporter = AdminReporter(
            control_store=self.control,
            cost_database=self.costs,
            trace_database=self.traces,
            feedback_store=self.feedback,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_trace(
        self, suffix: str, code: str, layer: str, occurred_at: str, response_id: str
    ) -> None:
        trace_id = "trace_" + suffix * 32
        store = SQLiteTraceEventStore(self.traces)
        store.write(
            TraceEvent.create(
                trace_id=trace_id,
                response_id=response_id,
                event_type="public_response_finalized",
                occurred_at=occurred_at,
                stage="http",
                outcome="success",
                protocol={"status": "SUCCESS", "layer": layer, "code": code, "retryable": False},
            )
        )
        store.close()

    def _client(self) -> TestClient:
        return TestClient(
            create_admin_app(
                control_store=self.control,
                reporter=self.reporter,
                feedback_store=self.feedback,
                diagnostic_query=DiagnosticQueryService(self.root),
            )
        )

    def test_summary_requires_admin_and_uses_public_aggregate_contract(self):
        client = self._client()
        params = {
            "since": "2026-09-05T00:00:00Z",
            "until": "2026-09-05T01:00:00Z",
        }
        self.assertEqual(client.get("/api/admin/operations/summary", params=params).status_code, 401)
        login = client.post("/api/admin/login", json={"password": "admin-password-123"})
        self.assertEqual(login.status_code, 200, login.text)
        before = {path: _digest(path) for path in (self.traces, self.costs)}
        response = client.get("/api/admin/operations/summary", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["search"]["total"], 2)
        self.assertEqual(payload["search"]["success"], 1)
        self.assertEqual(payload["search"]["by_code"], [
            {"name": "NO_MATCH", "count": 1},
            {"name": "REQUEST_SUCCEEDED", "count": 1},
        ])
        self.assertEqual(payload["model_errors"]["total"], 2)
        self.assertEqual(payload["model_errors"]["by_call"], [{
            "provider": "deepseek", "model": "v4", "call_type": "vision", "count": 2,
        }])
        self.assertEqual({path: _digest(path) for path in (self.traces, self.costs)}, before)
        rendered = response.text
        self.assertNotIn("request_id", rendered)
        self.assertNotIn("session_key", rendered)
        self.assertNotIn("safe_attributes", rendered)

    def test_summary_rejects_unbounded_window_and_limit(self):
        client = self._client()
        client.post("/api/admin/login", json={"password": "admin-password-123"})
        self.assertEqual(
            client.get(
                "/api/admin/operations/summary",
                params={"since": "2026-01-01T00:00:00Z", "until": "2026-01-09T00:00:00Z"},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.get("/api/admin/operations/summary", params={"limit": 0}).status_code,
            400,
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()

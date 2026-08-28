from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.session_runtime import AgentProtocolError
from tiku_agent.task_state_contract import empty_task_state_snapshot
from tiku_shared.response_store import SQLiteResponseStore
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder


class TerminalBindingRuntime:
    """Small runtime that exercises both normal and protocol-error exits."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, object]] = {}

    def _snapshot(self, session_id: str) -> dict[str, object]:
        return self._snapshots.setdefault(
            session_id,
            {
                "session_valid": True,
                "phase": "WAIT_CANDIDATE_CHOICE",
                "has_active_image": True,
                "task_revision": 1,
                "candidate_generation": "generation_terminal_binding",
                "candidate_count": 2,
                "chapter": "4力法",
                "search_id": "search_terminal_binding",
                "workflow_search_id": "search_terminal_workflow",
                "image_route": "A2",
                "a3": {"enabled": False},
            },
        )

    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        identity_key: str = "",
        progress=None,
        task_state_capabilities=None,
    ) -> AgentResponse:
        del identity_key
        if progress is not None:
            progress("searching", "正在检索题库…")
        snapshot = self._snapshot(session_id)
        if text in {"json-error", "stream-error"}:
            error = AgentProtocolError(
                "internal detail must stay private", code="AGENT_FAILED"
            )
            error.response_snapshot = dict(snapshot)
            raise error
        response = AgentResponse(text="已找到候选题。", intent="show_candidates")
        response.response_snapshot = dict(snapshot)
        response.response_projection_snapshot = dict(snapshot)
        if task_state_capabilities is not None:
            response.response_task_state_snapshot = empty_task_state_snapshot()
        response.response_media_snapshot_captured = True
        return response

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        return dict(self._snapshot(session_id))

    def current_image_path(self, session_id: str) -> Path | None:
        del session_id
        return None

    def persist_media(self, session_id: str, source: Path) -> Path | None:
        del session_id, source
        return None

    def resolve_media(self, session_id: str, filename: str) -> Path | None:
        del session_id, filename
        return None

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        del session_id, filename
        return None

    def clear(self, session_id: str) -> None:
        self._snapshots.pop(session_id, None)


class ResponseTerminalBindingTest(unittest.TestCase):
    def _make_app(self, root: Path):
        trace_store = SQLiteTraceEventStore(root / "trace_events.sqlite3")
        recorder = TraceEventRecorder(trace_store)
        app = create_app(
            runtime=TerminalBindingRuntime(),
            feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
            response_store=SQLiteResponseStore(root / "responses.sqlite3"),
            trace_event_recorder=recorder,
        )
        return app, recorder, trace_store, app.state.response_store

    @staticmethod
    def _stream_payload(response: object, event_type: str) -> dict[str, object]:
        events = [
            json.loads(line)
            for line in str(response.text).splitlines()
            if line.strip()
        ]
        matching = [event for event in events if event.get("type") == event_type]
        if len(matching) != 1:
            raise AssertionError(f"expected one {event_type} event, got {events!r}")
        return matching[0] if event_type == "error" else matching[0]["data"]

    @staticmethod
    def _terminal_for_request(
        recorder: TraceEventRecorder,
        trace_store: SQLiteTraceEventStore,
        request_id: str,
    ):
        recorder.flush()
        with closing(sqlite3.connect(trace_store.path)) as connection:
            trace_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT trace_id FROM trace_events WHERE request_id = ?",
                    (request_id,),
                ).fetchall()
            ]
        if len(trace_ids) != 1:
            raise AssertionError(
                f"expected one trace for {request_id!r}, got {trace_ids!r}"
            )
        events = trace_store.events_for_trace(trace_ids[0])
        terminals = [
            event
            for event in events
            if event.event_type in {"public_response_finalized", "request_failed"}
        ]
        if len(terminals) != 1:
            raise AssertionError(f"expected one terminal event, got {events!r}")
        return terminals[0]

    def _assert_ids_match(
        self,
        *,
        payload: dict[str, object],
        recorder: TraceEventRecorder,
        trace_store: SQLiteTraceEventStore,
        response_store: SQLiteResponseStore,
    ) -> None:
        response_id = str(payload.get("response_id") or "")
        self.assertRegex(response_id, r"^resp_[0-9a-f]{32}$")
        record = response_store.get(response_id)
        self.assertIsNotNone(record)
        terminal = self._terminal_for_request(
            recorder,
            trace_store,
            str(payload["request_id"]),
        )
        self.assertEqual(terminal.response_id, response_id)
        self.assertEqual(record.trace_id, terminal.trace_id)
        self.assertEqual(record.request_id, str(payload["request_id"]))

    def test_json_success_terminal_uses_authoritative_response_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, recorder, trace_store, response_store = self._make_app(root)
            with TestClient(app) as client:
                response = client.post("/api/message", json={"text": "json-success"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["task_state"],
                empty_task_state_snapshot().to_dict(),
            )
            self._assert_ids_match(
                payload=response.json(),
                recorder=recorder,
                trace_store=trace_store,
                response_store=response_store,
            )

    def test_stream_success_terminal_uses_authoritative_response_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, recorder, trace_store, response_store = self._make_app(root)
            with TestClient(app) as client:
                response = client.post(
                    "/api/message/stream", json={"text": "stream-success"}
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("task_state", self._stream_payload(response, "result"))
            self._assert_ids_match(
                payload=self._stream_payload(response, "result"),
                recorder=recorder,
                trace_store=trace_store,
                response_store=response_store,
            )

    def test_json_protocol_error_terminal_uses_authoritative_response_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, recorder, trace_store, response_store = self._make_app(root)
            with TestClient(app) as client:
                response = client.post("/api/message", json={"text": "json-error"})
            self.assertEqual(response.status_code, 500, response.text)
            self._assert_ids_match(
                payload=response.json(),
                recorder=recorder,
                trace_store=trace_store,
                response_store=response_store,
            )

    def test_stream_protocol_error_terminal_uses_authoritative_response_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, recorder, trace_store, response_store = self._make_app(root)
            with TestClient(app) as client:
                response = client.post(
                    "/api/message/stream", json={"text": "stream-error"}
                )
            self.assertEqual(response.status_code, 200, response.text)
            self._assert_ids_match(
                payload=self._stream_payload(response, "error"),
                recorder=recorder,
                trace_store=trace_store,
                response_store=response_store,
            )


if __name__ == "__main__":
    unittest.main()

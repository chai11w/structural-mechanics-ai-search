from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.task_log import JsonlTaskLogger, TaskLogEntry
from tiku_diagnostics import DiagnosticQueryService, QuerySpec
from tiku_shared.model_costs import (
    ModelCostCollector,
    SQLiteModelCostLedger,
    new_run_id,
)
from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent


class TikuDiagnosticEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name) / "runtime"
        self.root.mkdir()
        self.trace_id = f"trace_{uuid4().hex}"
        self.identity_key = "invite-evidence-01"
        self.session_key = "b" * 64
        self.request_id = f"req_{uuid4().hex}"
        self.search_id = f"search_{uuid4().hex}"
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.response_id = self._write_authoritative_chain()

    def _write_authoritative_chain(self) -> str:
        trace_store = SQLiteTraceEventStore(self.root / "trace_events.sqlite3")
        trace_store.write(
            TraceEvent.create(
                trace_id=self.trace_id,
                event_type="request_received",
                stage="http_request",
                outcome="started",
                occurred_at=self.now.isoformat(),
                request_id=self.request_id,
                identity_key=self.identity_key,
                session_key=self.session_key,
                workflow_search_id=self.search_id,
                search_id=self.search_id,
                safe_attributes={
                    "method": "POST",
                    "endpoint": "/api/message",
                    "response_mode": "json",
                },
            )
        )
        response = SQLiteResponseStore(self.root / "responses.sqlite3").finalize(
            ResponseProjection(
                trace_id=self.trace_id,
                identity_key=self.identity_key,
                session_key=self.session_key,
                request_id=self.request_id,
                workflow_search_id=self.search_id,
                search_id=self.search_id,
                status="SUCCESS",
                layer="tool",
                code="REQUEST_SUCCEEDED",
                phase="ANSWERED",
                task_revision=1,
                candidate_count=1,
                chapter="chapter-1",
                image_route="A2",
                intent="search_result",
                text_length=12,
                duration_ms=20,
            )
        )
        trace_store.write(
            TraceEvent.create(
                trace_id=self.trace_id,
                event_type="public_response_finalized",
                stage="http_request",
                outcome="answered",
                occurred_at=(self.now + timedelta(seconds=1)).isoformat(),
                request_id=self.request_id,
                response_id=response.response_id,
                identity_key=self.identity_key,
                session_key=self.session_key,
                workflow_search_id=self.search_id,
                search_id=self.search_id,
                protocol={
                    "status": "SUCCESS",
                    "layer": "tool",
                    "code": "REQUEST_SUCCEEDED",
                    "retryable": False,
                    "action": "",
                },
                safe_attributes={
                    "endpoint": "/api/message",
                    "response_mode": "json",
                    "intent": "search_result",
                    "media_status": "complete",
                    "image_count": 1,
                    "text_length": 12,
                    "http_status": 200,
                },
            )
        )
        SQLiteFeedbackStore(self.root / "feedback.sqlite3").upsert(
            message_id="message_evidence_01",
            rated_response_id=response.response_id,
            identity_key=self.identity_key,
            session_key=self.session_key,
            rating="positive",
            tags=("accurate_result",),
            detail="private feedback is not part of diagnostics",
            task_revision=1,
            phase="ANSWERED",
            candidate_count=1,
            search_key=self.search_id,
            request_id=self.request_id,
            search_id=self.search_id,
            status="SUCCESS",
            layer="tool",
            code="REQUEST_SUCCEEDED",
            chapter="chapter-1",
            image_route="A2",
            workflow_search_id=self.search_id,
            intent="search_result",
            conversation=[],
        )
        return response.response_id

    def _write_cost(self, *, trace_id: str) -> None:
        started = self.now.isoformat()
        finished = (self.now + timedelta(milliseconds=10)).isoformat()
        collector = ModelCostCollector(
            run_id=new_run_id(),
            trace_id=trace_id,
            session_key=self.session_key,
            identity_key=self.identity_key,
            search_key=self.search_id,
            task_kind="image",
            started_at=started,
        )
        collector.record(
            provider="qwen",
            model="qwen3.7-plus",
            call_type="rerank",
            status="success",
            started_at=started,
            finished_at=finished,
            latency_ms=10,
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
        SQLiteModelCostLedger(self.root / "model_costs.sqlite3").write_run(
            collector,
            finished_at=finished,
            outcome="success",
        )

    def _write_task(self, *, trace_id: str) -> None:
        JsonlTaskLogger(self.root / "a2" / "task_logs.jsonl").write(
            TaskLogEntry(
                task_id=self.request_id,
                session_key=self.session_key,
                kind="image",
                started_at=self.now.isoformat(),
                finished_at=(self.now + timedelta(milliseconds=20)).isoformat(),
                duration_ms=20,
                phase_before="IDLE",
                phase_after="ANSWERED",
                outcome="answered",
                question_count=1,
                candidate_count=1,
                chapter="chapter-1",
                route="main",
                request_id=self.request_id,
                search_id=self.search_id,
                identity_key=self.identity_key,
                status="SUCCESS",
                layer="tool",
                code="REQUEST_SUCCEEDED",
                trace_id=trace_id,
            )
        )

    def _write_page_error(self) -> None:
        path = self.root / "a3_sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE a3_page_errors (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_key TEXT NOT NULL,
                        search_id TEXT NOT NULL,
                        task_kind TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        error_type TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO a3_page_errors (
                        session_key, search_id, task_kind, phase,
                        error_type, error_code, error_message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.session_key,
                        self.search_id,
                        "a3_page_understanding",
                        "UNDERSTANDING_PAGE",
                        "ValueError",
                        "PAGE_SCHEMA_INVALID",
                        "private exception text",
                        self.now.isoformat(),
                    ),
                )

    def _secondary_items(self, package: dict[str, object]) -> list[dict[str, object]]:
        names = {
            "model_cost_runs",
            "model_cost_calls",
            "task_logs",
            "a3_page_errors",
        }
        return [
            item for item in package["evidence"] if item["source"] in names
        ]

    def test_authoritative_first_merges_exact_secondary_evidence(self):
        self._write_cost(trace_id=self.trace_id)
        self._write_task(trace_id=self.trace_id)
        self._write_page_error()
        before = {
            str(path.relative_to(self.root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id)
        )

        after = {
            str(path.relative_to(self.root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(package["summary"]["model_run_count"], 1)
        self.assertEqual(package["summary"]["model_call_count"], 1)
        self.assertEqual(package["summary"]["task_count"], 1)
        self.assertEqual(package["summary"]["page_error_count"], 1)
        by_source = {
            item["source"]: item for item in self._secondary_items(package)
        }
        self.assertEqual(by_source["model_cost_runs"]["association"], "trace_exact")
        self.assertEqual(by_source["model_cost_calls"]["association"], "trace_exact")
        self.assertEqual(by_source["task_logs"]["association"], "trace_exact")
        self.assertEqual(
            by_source["a3_page_errors"]["association"], "legacy_compatibility"
        )
        rendered = json.dumps(package)
        self.assertNotIn("private exception text", rendered)
        for private_field in (
            "task_id",
            "session_key",
            "request_id",
            "message_id",
            "safe_attributes",
            "conversation_json",
            "detail",
        ):
            self.assertNotIn(f'"{private_field}"', rendered)
        self.assertNotIn(self.request_id, rendered)
        source_names = {source["name"] for source in package["sources"]}
        self.assertTrue(
            {"model_costs", "task_logs", "a3_page_errors"}.issubset(source_names)
        )

    def test_authoritative_first_reports_broken_links_without_legacy_fallback(self):
        self._write_cost(trace_id="")
        self._write_task(trace_id="")

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id)
        )

        secondary = self._secondary_items(package)
        self.assertEqual(secondary, [])
        self.assertIn(
            "model_costs:authoritative_evidence_missing",
            package["summary"]["evidence_gaps"],
        )
        self.assertIn(
            "task_logs:authoritative_evidence_missing",
            package["summary"]["evidence_gaps"],
        )
        self.assertFalse(package["summary"]["complete"])

    def test_authoritative_probe_surfaces_unreadable_legacy_sources(self):
        self._write_cost(trace_id="")
        self._write_task(trace_id="")
        legacy_cost = self.root / "a2" / "model_costs.sqlite3"
        legacy_cost.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(legacy_cost)) as connection:
            with connection:
                connection.execute("CREATE TABLE model_cost_runs (run_id TEXT)")
        (self.root / "task_logs.jsonl").write_text("{malformed\n", encoding="utf-8")

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id)
        )

        self.assertIn(
            "model_costs_legacy_a2:schema_mismatch",
            package["summary"]["evidence_gaps"],
        )
        self.assertIn(
            "task_logs_legacy_root:malformed_or_oversized_line",
            package["summary"]["evidence_gaps"],
        )
        states = {item["name"]: item["status"] for item in package["sources"]}
        self.assertEqual(states["model_costs_legacy_a2"], "schema_mismatch")
        self.assertEqual(states["task_logs_legacy_root"], "partial")
        self.assertFalse(package["summary"]["complete"])

    def test_live_jsonl_append_stops_at_opening_boundary(self):
        self._write_task(trace_id=self.trace_id)
        task_path = self.root / "a2" / "task_logs.jsonl"
        original_line = task_path.read_bytes()
        appended_value = json.loads(original_line)
        appended_value["task_id"] = "task_appended_after_open"
        appended_line = (json.dumps(appended_value) + "\n").encode("utf-8")
        original_open = Path.open

        class AppendAfterFirstRead:
            def __init__(self, handle):
                self.handle = handle
                self.appended = False

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def fileno(self):
                return self.handle.fileno()

            def readline(self, size=-1):
                value = self.handle.readline(size)
                if not self.appended:
                    self.appended = True
                    with original_open(task_path, "ab") as writer:
                        writer.write(appended_line)
                return value

        def controlled_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if Path(path) == task_path and args and args[0] == "rb":
                return AppendAfterFirstRead(handle)
            return handle

        with patch.object(Path, "open", controlled_open):
            package = DiagnosticQueryService(self.root).query(
                QuerySpec(response_id=self.response_id)
            )

        self.assertEqual(package["summary"]["task_count"], 1)
        self.assertIn(
            "task_logs:scan_limit_exceeded",
            package["summary"]["evidence_gaps"],
        )
        states = {item["name"]: item["status"] for item in package["sources"]}
        self.assertEqual(states["task_logs"], "partial")

    def test_oversized_jsonl_line_is_drained_with_bounded_reads(self):
        task_path = self.root / "a2" / "task_logs.jsonl"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_bytes(b"x" * (64 * 1024 + 1) + b"\n")
        self._write_task(trace_id=self.trace_id)

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id)
        )

        self.assertEqual(package["summary"]["task_count"], 1)
        self.assertIn(
            "task_logs:malformed_or_oversized_line",
            package["summary"]["evidence_gaps"],
        )
        states = {item["name"]: item["status"] for item in package["sources"]}
        self.assertEqual(states["task_logs"], "partial")

    def test_authoritative_only_never_reads_legacy_fallbacks(self):
        self._write_cost(trace_id="")
        self._write_task(trace_id="")
        self._write_page_error()

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="authoritative-only",
            )
        )

        self.assertEqual(self._secondary_items(package), [])
        self.assertEqual(package["summary"]["model_run_count"], 0)
        self.assertEqual(package["summary"]["task_count"], 0)
        self.assertEqual(package["summary"]["page_error_count"], 0)
        self.assertNotIn(
            "model_costs:authoritative_evidence_missing",
            package["summary"]["evidence_gaps"],
        )

    def test_legacy_only_labels_every_secondary_join_as_compatibility(self):
        self._write_cost(trace_id=self.trace_id)
        self._write_task(trace_id=self.trace_id)
        self._write_page_error()

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id, association_mode="legacy-only")
        )

        secondary = self._secondary_items(package)
        self.assertEqual(len(secondary), 4)
        self.assertTrue(
            all(item["association"] == "legacy_compatibility" for item in secondary)
        )
        self.assertGreaterEqual(package["summary"]["legacy_compatibility_count"], 4)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import UTC, datetime, timedelta
import gc
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from scripts.tiku_diagnostics import main
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_diagnostics import (
    DiagnosticQueryService,
    QuerySpec,
    compare_diagnostic_bundles,
)
from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent


class TikuDiagnosticLegacyFeedbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temporary.name) / "runtime"
        self.root.mkdir()
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.trace_id = f"trace_{uuid4().hex}"
        self.identity_key = "identity-stable-diagnostic-01"
        self.session_key = "c" * 64
        self.request_id = f"req_{uuid4().hex}"
        self.search_id = f"search_{uuid4().hex}"
        self.response_id, self.feedback_id = self._write_chain()

    def _write_chain(self) -> tuple[str, str]:
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
                chapter="2_static",
                image_route="A2",
                intent="search_result",
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
            )
        )
        feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3").upsert(
            message_id="message_legacy_compare_01",
            rated_response_id=response.response_id,
            identity_key=self.identity_key,
            session_key=self.session_key,
            rating="positive",
            tags=("accurate_result",),
            detail="private detail must not enter diagnostics",
            task_revision=1,
            phase="ANSWERED",
            candidate_count=1,
            search_key=self.search_id,
            request_id=self.request_id,
            search_id=self.search_id,
            status="SUCCESS",
            layer="tool",
            code="REQUEST_SUCCEEDED",
            chapter="2_static",
            image_route="A2",
            workflow_search_id=self.search_id,
            intent="search_result",
            conversation=[],
        )
        return response.response_id, feedback.feedback_id

    def test_legacy_view_uses_search_time_join_instead_of_response_binding(self):
        service = DiagnosticQueryService(self.root)
        authoritative = service.query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="authoritative-only",
            )
        )
        legacy = service.query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="legacy-only",
            )
        )

        authoritative_feedback = [
            item for item in authoritative["evidence"] if item["source"] == "feedback"
        ]
        legacy_feedback = [
            item for item in legacy["evidence"] if item["source"] == "feedback"
        ]
        self.assertEqual(len(authoritative_feedback), 1)
        self.assertEqual(len(legacy_feedback), 1)
        self.assertEqual(
            authoritative_feedback[0]["association"],
            "authoritative_response_id",
        )
        self.assertEqual(
            legacy_feedback[0]["association"], "legacy_compatibility"
        )
        self.assertEqual(legacy_feedback[0]["completeness"], "partial")
        self.assertEqual(
            compare_diagnostic_bundles(authoritative, legacy).classification,
            "match",
        )

    def test_legacy_feedback_join_allows_submission_after_five_minutes(self):
        delayed = (self.now + timedelta(minutes=10)).isoformat()
        with closing(
            sqlite3.connect(self.root / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET created_at = ?, updated_at = ? "
                    "WHERE feedback_id = ?",
                    (delayed, delayed, self.feedback_id),
                )

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="legacy-only",
            )
        )
        self.assertTrue(
            any(item["source"] == "feedback" for item in package["evidence"]),
            package,
        )

    def test_legacy_page_feedback_does_not_join_on_child_search_key(self):
        with closing(
            sqlite3.connect(self.root / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET feedback_scope = 'page', "
                    "workflow_search_id = ?, search_key = ?, search_id = ? "
                    "WHERE feedback_id = ?",
                    (
                        f"workflow_{uuid4().hex}",
                        self.search_id,
                        self.search_id,
                        self.feedback_id,
                    ),
                )

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="legacy-only",
            )
        )
        self.assertFalse(
            any(item["source"] == "feedback" for item in package["evidence"]),
            package,
        )

    def test_legacy_feedback_join_excludes_response_after_feedback_cutoff(self):
        submitted_before_response = (self.now - timedelta(minutes=1)).isoformat()
        with closing(
            sqlite3.connect(self.root / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET created_at = ?, updated_at = ? "
                    "WHERE feedback_id = ?",
                    (
                        submitted_before_response,
                        submitted_before_response,
                        self.feedback_id,
                    ),
                )

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="legacy-only",
            )
        )
        self.assertFalse(
            any(item["source"] == "feedback" for item in package["evidence"]),
            package,
        )

    def test_legacy_feedback_join_honors_trusted_target_timestamp(self):
        target_at = self.now - timedelta(seconds=1)
        submitted_at = self.now + timedelta(minutes=10)
        conversation = json.dumps(
            [
                {
                    "message_id": "message_legacy_compare_01",
                    "created_at": int(target_at.timestamp() * 1000),
                }
            ]
        )
        with closing(
            sqlite3.connect(self.root / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET conversation_json = ?, created_at = ?, "
                    "updated_at = ? WHERE feedback_id = ?",
                    (
                        conversation,
                        submitted_at.isoformat(),
                        submitted_at.isoformat(),
                        self.feedback_id,
                    ),
                )

        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="legacy-only",
            )
        )
        self.assertFalse(
            any(item["source"] == "feedback" for item in package["evidence"]),
            package,
        )

    def test_old_v7_feedback_is_visible_only_in_legacy_identity_view(self):
        with closing(
            sqlite3.connect(self.root / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET rated_response_id = '', schema_version = 7 "
                    "WHERE feedback_id = ?",
                    (self.feedback_id,),
                )
        common = {
            "identity_key": self.identity_key,
            "since": (self.now - timedelta(days=1)).isoformat(),
            "until": (self.now + timedelta(days=1)).isoformat(),
        }
        service = DiagnosticQueryService(self.root)
        authoritative = service.query(
            QuerySpec(**common, association_mode="authoritative-only")
        )
        legacy = service.query(QuerySpec(**common, association_mode="legacy-only"))

        self.assertFalse(
            any(item["source"] == "feedback" for item in authoritative["evidence"])
        )
        legacy_feedback = [
            item for item in legacy["evidence"] if item["source"] == "feedback"
        ]
        self.assertEqual(len(legacy_feedback), 1)
        self.assertEqual(
            legacy_feedback[0]["association"], "legacy_compatibility"
        )
        self.assertTrue(legacy_feedback[0]["record"]["legacy_binding"])
        self.assertEqual(
            compare_diagnostic_bundles(authoritative, legacy).classification,
            "legacy_only",
        )

        direct_authoritative = service.query(
            QuerySpec(
                feedback_id=self.feedback_id,
                association_mode="authoritative-only",
            )
        )
        direct_legacy = service.query(
            QuerySpec(
                feedback_id=self.feedback_id,
                association_mode="legacy-only",
            )
        )
        self.assertEqual(
            compare_diagnostic_bundles(
                direct_authoritative, direct_legacy
            ).classification,
            "evidence_missing",
        )

    def test_global_limit_reserves_the_direct_record_and_each_primary_source(self):
        trace_store = SQLiteTraceEventStore(self.root / "trace_events.sqlite3")
        for offset in range(8):
            trace_store.write(
                TraceEvent.create(
                    trace_id=self.trace_id,
                    event_type="stage_started",
                    stage=f"stage_{offset}",
                    outcome="started",
                    occurred_at=(self.now + timedelta(milliseconds=offset)).isoformat(),
                    request_id=self.request_id,
                    identity_key=self.identity_key,
                    session_key=self.session_key,
                )
            )
        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                response_id=self.response_id,
                association_mode="authoritative-only",
                limit=3,
            )
        )

        self.assertEqual(
            {item["source"] for item in package["evidence"]},
            {"trace_events", "responses", "feedback"},
        )
        self.assertTrue(
            any(item["association"] == "direct_selector" for item in package["evidence"])
        )
        self.assertFalse(
            any(
                str(gap).startswith("terminal_missing:")
                for gap in package["summary"]["evidence_gaps"]
            ),
            package,
        )
        self.assertFalse(package["summary"]["complete"])
        self.assertIn(
            "diagnostic_output:result_truncated",
            package["summary"]["evidence_gaps"],
        )

    def test_later_terminal_query_preserves_join_key_truncation_reason(self):
        service = DiagnosticQueryService(self.root)
        states = {}
        response_ids = {
            self.response_id,
            *(f"resp_{offset:032x}" for offset in range(256)),
        }
        service._traces_by_feedback(set(), response_ids, 256, states)
        service._terminal_traces_by_ids({self.trace_id}, 256, states)

        self.assertEqual(states["trace_events"].status, "partial")
        self.assertEqual(states["trace_events"].reason, "join_keys_truncated")

    def test_cli_compare_returns_only_the_safe_comparison_package(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "--response-id",
                    self.response_id,
                    "--compare-legacy",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(status, 0)
        package = json.loads(stdout.getvalue())
        self.assertEqual(package["comparison"]["classification"], "match")
        rendered = json.dumps(package, ensure_ascii=False)
        self.assertNotIn("private detail must not enter diagnostics", rendered)
        self.assertNotIn("session_key", rendered)
        self.assertNotIn("request_id", rendered)

    def test_feedback_selector_can_compare_the_direct_row_on_both_views(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "--feedback-id",
                    self.feedback_id,
                    "--compare-legacy",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(status, 0)
        package = json.loads(stdout.getvalue())
        self.assertEqual(package["comparison"]["classification"], "match")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import UTC, datetime
import gc
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from contextlib import closing, redirect_stdout

from scripts.tiku_diagnostics import main
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_diagnostics import (
    DiagnosticQueryError,
    DiagnosticQueryService,
    QuerySpec,
)
from tiku_diagnostics import sqlite_reader as sqlite_reader_module
from tiku_diagnostics.sqlite_reader import readonly_connection
from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent


class TikuDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name) / "runtime"
        self.root.mkdir()
        self.trace_id = f"trace_{uuid4().hex}"
        self.identity_key = "invite-privacy-safe-01"
        self.session_key = "a" * 64
        self.request_id = f"req_{uuid4().hex}"
        self._write_fixture()

    def _write_fixture(self) -> None:
        trace_store = SQLiteTraceEventStore(self.root / "trace_events.sqlite3")
        trace_store.write(
            TraceEvent.create(
                trace_id=self.trace_id,
                event_type="request_received",
                stage="http_request",
                outcome="started",
                request_id=self.request_id,
                identity_key=self.identity_key,
                session_key=self.session_key,
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
                status="NEEDS_INPUT",
                layer="upload",
                code="UPLOAD_REQUIRED",
                action="retry_upload",
                intent="clarification",
            )
        )
        self.response_id = response.response_id
        trace_store.write(
            TraceEvent.create(
                trace_id=self.trace_id,
                event_type="public_response_finalized",
                stage="http_request",
                outcome="needs_input",
                request_id=self.request_id,
                response_id=self.response_id,
                identity_key=self.identity_key,
                session_key=self.session_key,
                protocol={
                    "status": "NEEDS_INPUT",
                    "layer": "upload",
                    "code": "UPLOAD_REQUIRED",
                    "retryable": False,
                    "action": "retry_upload",
                },
                safe_attributes={
                    "endpoint": "/api/message",
                    "response_mode": "json",
                    "intent": "clarification",
                    "media_status": "unavailable",
                    "image_count": 0,
                    "text_length": 8,
                    "http_status": 200,
                },
            )
        )
        feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3").upsert(
            message_id="message_diag_01",
            rated_response_id=self.response_id,
            identity_key=self.identity_key,
            session_key=self.session_key,
            rating="positive",
            tags=("clear_reply",),
            detail="must never appear",
            task_revision=0,
            phase="IDLE",
            candidate_count=0,
            request_id=self.request_id,
            status="NEEDS_INPUT",
            layer="upload",
            code="UPLOAD_REQUIRED",
            intent="clarification",
            conversation=[],
        )
        self.feedback_id = feedback.feedback_id

    def test_response_selector_builds_authoritative_chain_without_private_text(self):
        before = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.glob("*.sqlite3")
        }
        package = DiagnosticQueryService(self.root).query(
            QuerySpec(response_id=self.response_id)
        )
        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.glob("*.sqlite3")
        }

        self.assertEqual(before, after)
        self.assertTrue(package["summary"]["complete"], package)
        self.assertEqual(package["summary"]["response_count"], 1)
        self.assertEqual(package["summary"]["feedback_count"], 1)
        rendered = str(package)
        self.assertNotIn("must never appear", rendered)
        self.assertNotIn("conversation", rendered)
        self.assertNotIn("admin_note", rendered)
        self.assertIn("authoritative_response_id", rendered)
        encoded = json.dumps(package, ensure_ascii=False)
        for private_field in (
            "session_key",
            "request_id",
            "requestId",
            "provider_request_id",
            "providerRequestId",
            "message_id",
            "safe_attributes",
            "conversation_json",
            "detail",
        ):
            self.assertNotIn(f'"{private_field}"', encoded)
        self.assertNotIn(self.session_key, encoded)
        self.assertNotIn(self.request_id, encoded)
        self.assertNotIn("message_diag_01", encoded)

    def test_identity_selector_requires_a_bounded_aware_window(self):
        with self.assertRaisesRegex(DiagnosticQueryError, "require since and until"):
            QuerySpec(identity_key=self.identity_key)
        with self.assertRaisesRegex(DiagnosticQueryError, "cannot exceed"):
            QuerySpec(
                identity_key=self.identity_key,
                since="2026-01-01T00:00:00+00:00",
                until="2026-03-01T00:00:00+00:00",
            )
        now = datetime.now(UTC)
        package = DiagnosticQueryService(self.root).query(
            QuerySpec(
                identity_key=self.identity_key,
                since=now.replace(hour=0, minute=0, second=0).isoformat(),
                until=now.replace(hour=23, minute=59, second=59).isoformat(),
            )
        )
        self.assertEqual(package["summary"]["trace_count"], 1)

    def test_invitation_code_and_over_limit_are_rejected(self):
        with self.assertRaisesRegex(DiagnosticQueryError, "invitation codes"):
            QuerySpec(
                identity_key="TIKU-not-an-identity",
                since="2026-08-01T00:00:00+00:00",
                until="2026-08-02T00:00:00+00:00",
            )
        with self.assertRaisesRegex(DiagnosticQueryError, "limit"):
            QuerySpec(trace_id=self.trace_id, limit=101)

    def test_missing_sources_are_reported_without_creating_files(self):
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        package = DiagnosticQueryService(empty).query(QuerySpec(trace_id=self.trace_id))
        self.assertFalse(package["summary"]["complete"])
        self.assertEqual(list(empty.iterdir()), [])
        self.assertIn("trace_events:missing", package["summary"]["evidence_gaps"])

    def test_readonly_connection_reads_live_wal_without_touching_source(self):
        path = self.root / "live_wal.sqlite3"
        with closing(sqlite3.connect(path)) as writer:
            with writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE live_rows (value INTEGER NOT NULL)")
                writer.execute("INSERT INTO live_rows (value) VALUES (1)")

            before = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in self.root.glob(f"{path.name}*")
                if item.is_file()
            }
            with readonly_connection(path) as reader:
                count = reader.execute("SELECT COUNT(*) FROM live_rows").fetchone()[0]
            after = {
                item.name: (item.read_bytes(), item.stat().st_mtime_ns)
                for item in self.root.glob(f"{path.name}*")
                if item.is_file()
            }

            self.assertEqual(count, 1)
            self.assertEqual(before, after)

    def test_readonly_connection_retries_when_wal_is_reused_between_rounds(self):
        path = self.root / "reused_wal.sqlite3"
        with closing(sqlite3.connect(path)) as writer:
            with writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE live_value (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                writer.execute(
                    "INSERT INTO live_value (id, value) VALUES (1, ?)", ("a" * 2048,)
                )
            with closing(writer.execute("PRAGMA wal_checkpoint(RESTART)")) as cursor:
                self.assertEqual(cursor.fetchone()[0], 0)
            with writer:
                writer.execute(
                    "UPDATE live_value SET value = ? WHERE id = 1", ("b" * 2048,)
                )

            original_round = sqlite_reader_module._copy_snapshot_round
            round_count = 0

            def copy_round(source: Path, destination: Path):
                nonlocal round_count
                result = original_round(source, destination)
                round_count += 1
                if round_count == 1:
                    with closing(
                        writer.execute("PRAGMA wal_checkpoint(RESTART)")
                    ) as cursor:
                        self.assertEqual(cursor.fetchone()[0], 0)
                    with writer:
                        writer.execute(
                            "UPDATE live_value SET value = ? WHERE id = 1",
                            ("c" * 2048,),
                        )
                return result

            with patch.object(
                sqlite_reader_module, "_copy_snapshot_round", side_effect=copy_round
            ):
                with readonly_connection(path) as reader:
                    value = reader.execute(
                        "SELECT value FROM live_value WHERE id = 1"
                    ).fetchone()[0]

            self.assertEqual(value, "c" * 2048)
            self.assertGreaterEqual(round_count, 4)

    def test_readonly_connection_handles_restart_same_size_wal_stress(self):
        path = self.root / "restart_stress.sqlite3"
        wal_path = Path(f"{path}-wal")
        with closing(sqlite3.connect(path)) as writer:
            with writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE live_value (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                writer.execute(
                    "INSERT INTO live_value (id, value) VALUES (1, ?)", ("0" * 2048,)
                )
            with closing(writer.execute("PRAGMA wal_checkpoint(RESTART)")) as cursor:
                self.assertEqual(cursor.fetchone()[0], 0)

            wal_sizes: set[int] = set()
            wal_hashes: set[str] = set()
            for revision in range(12):
                value = f"{revision:04d}" + ("x" * 2044)
                with writer:
                    writer.execute(
                        "UPDATE live_value SET value = ? WHERE id = 1", (value,)
                    )
                wal_bytes = wal_path.read_bytes()
                wal_sizes.add(len(wal_bytes))
                wal_hashes.add(sha256(wal_bytes).hexdigest())

                with readonly_connection(path) as reader:
                    observed = reader.execute(
                        "SELECT value FROM live_value WHERE id = 1"
                    ).fetchone()[0]
                self.assertEqual(observed, value)

                with closing(
                    writer.execute("PRAGMA wal_checkpoint(RESTART)")
                ) as cursor:
                    self.assertEqual(cursor.fetchone()[0], 0)

            self.assertEqual(len(wal_sizes), 1)
            self.assertGreater(len(wal_hashes), 1)

    def test_snapshot_validation_rejects_non_schema_btree_damage(self):
        path = self.root / "damaged_snapshot.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE payloads (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO payloads (value) VALUES (?)", ("still-readable-schema",)
                )
            root_page = connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = 'payloads'"
            ).fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]

        content = bytearray(path.read_bytes())
        content[(root_page - 1) * page_size] = 0
        path.write_bytes(content)

        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as reader:
            self.assertEqual(
                reader.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()[0], 1
            )
        with self.assertRaisesRegex(sqlite3.OperationalError, "quick_check"):
            sqlite_reader_module._validate_readable_snapshot(path)

    def test_cli_imports_ignore_an_earlier_scripts_search_path(self):
        repository = Path(__file__).resolve().parents[1]
        scripts = repository / "scripts"
        for module in ("scripts.tiku_diagnostics", "scripts.tiku_retention"):
            code = (
                "import sys; "
                f"sys.path.insert(0, {str(repository)!r}); "
                f"sys.path.insert(0, {str(scripts)!r}); "
                f"import {module}"
            )
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_cli_emits_json_and_has_no_invitation_code_argument(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = main(
                [
                    "--runtime-root",
                    str(self.root),
                    "--feedback-id",
                    self.feedback_id,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(status, 0)
        rendered = stdout.getvalue()
        self.assertIn('"selector":"feedback_id"', rendered)
        for private_field in (
            "session_key",
            "request_id",
            "message_id",
            "safe_attributes",
            "conversation_json",
            "detail",
        ):
            self.assertNotIn(f'"{private_field}"', rendered)


if __name__ == "__main__":
    unittest.main()

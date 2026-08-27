from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
import gc
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts import tiku_retention as retention_cli
from tiku_admin.app import create_admin_app
from tiku_admin.control_store import SQLiteControlStore
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_diagnostics.retention import (
    RetentionError,
    apply_retention_plan,
    build_retention_plan,
    load_retention_plan,
    retention_plan_report,
    retention_plan_hash,
    write_retention_plan,
)
from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore


class TikuRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.base = Path(self.temp.name)
        self.repository = self.base / "repository"
        self.runtime = self.repository / ".tmp_tiku_agent_v2_prod_8790"
        self.backup_root = self.base / "external-backups"
        self.runtime.mkdir(parents=True)
        self.as_of = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
        self._write_fixture()

    def _projection(self, trace_id: str) -> ResponseProjection:
        return ResponseProjection(
            trace_id=trace_id,
            identity_key="identity-stable-01",
            session_key="a" * 64,
            request_id=f"req_{uuid4().hex}",
            status="NEEDS_INPUT",
            layer="upload",
            code="UPLOAD_REQUIRED",
            action="retry_upload",
            intent="clarification",
        )

    def _write_fixture(self) -> None:
        clock = [self.as_of - timedelta(days=10)]
        response_store = SQLiteResponseStore(
            self.runtime / "responses.sqlite3",
            retention_days=1,
            clock=lambda: clock[0],
        )
        self.expired_response = response_store.finalize(
            self._projection(f"trace_{uuid4().hex}")
        )
        self.held_response = response_store.finalize(
            self._projection(f"trace_{uuid4().hex}")
        )
        clock[0] = self.as_of
        self.fresh_response = response_store.finalize(
            self._projection(f"trace_{uuid4().hex}")
        )

        feedback_store = SQLiteFeedbackStore(self.runtime / "feedback.sqlite3")
        source_media = self.runtime / "source.jpg"
        source_media.write_bytes(b"case-media-evidence")
        self.expired_feedback = feedback_store.upsert(
            message_id="message_expired_case",
            rated_response_id=self.expired_response.response_id,
            identity_key="identity-stable-01",
            session_key="a" * 64,
            rating="negative",
            tags=("wrong_answer",),
            detail="retained feedback detail",
            task_revision=1,
            phase="WAIT_CHAPTER",
            candidate_count=0,
            conversation=[
                {
                    "role": "user",
                    "message": "case text",
                    "images": ["/api/upload/source.jpg"],
                },
                {
                    "role": "assistant",
                    "message": "rated reply",
                    "messageId": "message_expired_case",
                },
            ],
            media_resolver=lambda _value: source_media,
        )
        self.active_feedback = feedback_store.upsert(
            message_id="message_active_case",
            rated_response_id=self.held_response.response_id,
            identity_key="identity-stable-01",
            session_key="a" * 64,
            rating="positive",
            tags=("clear_reply",),
            detail="active case",
            task_revision=2,
            phase="WAIT_CHAPTER",
            candidate_count=0,
            conversation=[],
        )
        expired_at = (self.as_of - timedelta(seconds=1)).isoformat()
        active_at = (self.as_of + timedelta(days=7)).isoformat()
        with closing(sqlite3.connect(feedback_store.path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET case_expires_at = ?, updated_at = ? "
                    "WHERE feedback_id = ?",
                    (expired_at, expired_at, self.expired_feedback.feedback_id),
                )
                connection.execute(
                    "UPDATE message_feedback SET case_expires_at = ?, updated_at = ? "
                    "WHERE feedback_id = ?",
                    (active_at, active_at, self.active_feedback.feedback_id),
                )

        with closing(
            sqlite3.connect(self.runtime / "a3_sessions.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE a3_page_errors ("
                    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO a3_page_errors(created_at) VALUES (?)",
                    ((self.as_of - timedelta(days=31)).isoformat(),),
                )
                connection.execute(
                    "INSERT INTO a3_page_errors(created_at) VALUES (?)",
                    ((self.as_of - timedelta(days=1)).isoformat(),),
                )

        with closing(
            sqlite3.connect(self.runtime / "trace_events.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE trace_events ("
                    "event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO trace_events VALUES (?, ?)",
                    (f"evt_{uuid4().hex}", self.as_of.isoformat()),
                )

        (self.runtime / "a2").mkdir()
        (self.runtime / "a2" / "task_logs.jsonl").write_text(
            '{"task_id":"task-1"}\n', encoding="utf-8"
        )
        (self.runtime / "output_watchdog").mkdir()
        (self.runtime / "output_watchdog" / "output_watchdog.jsonl").write_text(
            '{"category":"normal"}\n', encoding="utf-8"
        )
        (self.runtime / "tiku_8790.out.log").write_text("service output\n", encoding="utf-8")
        (self.runtime / "tiku_8790.err.log").write_text("", encoding="utf-8")
        (self.runtime / "watchdog_8790.status").write_text("healthy\n", encoding="utf-8")
        (self.runtime / "model_costs.sqlite3").write_bytes(b"fee-ledger-must-stay")
        (self.runtime / "control.sqlite3").write_bytes(b"control-must-stay")

    def _plan(self) -> dict[str, object]:
        return build_retention_plan(
            self.runtime,
            runtime_name="8790",
            repository_root=self.repository,
            as_of=self.as_of,
            now=self.as_of,
        )

    @staticmethod
    def _action(plan: dict[str, object], source: str) -> dict[str, object]:
        return next(
            item for item in plan["actions"] if item["source"] == source  # type: ignore[index]
        )

    def _source_snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.runtime).as_posix(): _sha256(path)
            for path in self.runtime.rglob("*")
            if path.is_file()
        }

    def test_plan_is_read_only_and_reports_actions_policy_gaps_and_exclusions(self) -> None:
        before = self._source_snapshot()

        plan = self._plan()

        self.assertEqual(before, self._source_snapshot())
        self.assertEqual(plan["plan_hash"], retention_plan_hash(plan))
        responses = self._action(plan, "responses")
        feedback = self._action(plan, "feedback_cases")
        errors = self._action(plan, "a3_page_errors")
        self.assertEqual(responses["candidate_count"], 1)
        self.assertEqual(responses["held_count"], 1)
        self.assertEqual(feedback["candidate_count"], 1)
        self.assertGreater(feedback["estimated_logical_bytes"], 0)
        self.assertEqual(errors["candidate_count"], 1)
        self.assertEqual(plan["summary"]["candidate_count"], 3)  # type: ignore[index]
        self.assertTrue(
            all(item["policy"] == "policy_missing" for item in plan["report_only"])  # type: ignore[index]
        )
        report_sources = {item["source"] for item in plan["report_only"]}  # type: ignore[index]
        self.assertTrue(
            {"trace_events", "task_logs", "output_watchdog", "service_logs"}.issubset(
                report_sources
            )
        )
        exclusion_sources = {item["source"] for item in plan["exclusions"]}  # type: ignore[index]
        self.assertTrue(
            {"model_costs", "feedback_metadata", "control_admin_audit"}.issubset(
                exclusion_sources
            )
        )
        public = retention_plan_report(plan)
        rendered = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("candidates", rendered)
        self.assertNotIn("held_response_ids", rendered)
        self.assertNotIn(str(self.runtime), rendered)
        self.assertNotIn(self.expired_response.response_id, rendered)

    def test_future_prediction_is_report_only_and_not_persistable(self) -> None:
        future = self.as_of + timedelta(days=1)
        before = self._source_snapshot()

        with self.assertRaisesRegex(RetentionError, "future as_of.*report-only"):
            build_retention_plan(
                self.runtime,
                runtime_name="8790",
                repository_root=self.repository,
                as_of=future,
                now=self.as_of,
            )

        report = build_retention_plan(
            self.runtime,
            runtime_name="8790",
            repository_root=self.repository,
            as_of=future,
            now=self.as_of,
            future_report_only=True,
        )
        self.assertEqual(report["mode"], "report_only")
        self.assertTrue(
            all(item["policy"] == "report_only" for item in report["actions"])  # type: ignore[index]
        )
        self.assertTrue(
            all("candidates" not in item for item in report["actions"])  # type: ignore[index]
        )
        self.assertTrue(
            all("held_response_ids" not in item for item in report["actions"])  # type: ignore[index]
        )
        target = self.base / "future-report.json"
        with self.assertRaisesRegex(RetentionError, "not an applyable plan"):
            write_retention_plan(target, report, now=self.as_of)

        self.assertFalse(target.exists())
        self.assertEqual(before, self._source_snapshot())

    def test_cli_future_prediction_is_report_only_and_plan_out_is_rejected(self) -> None:
        future = self.as_of + timedelta(days=1)
        before = self._source_snapshot()
        stdout = io.StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stdout(stdout),
        ):
            status = retention_cli.main(
                [
                    "--runtime",
                    "8790",
                    "--as-of",
                    future.isoformat(),
                    "--format",
                    "json",
                ],
                now=self.as_of,
            )
        self.assertEqual(status, 0)
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["mode"], "report_only")
        self.assertTrue(
            all(item["policy"] == "report_only" for item in rendered["actions"])
        )

        plan_path = self.base / "future-plan.json"
        stderr = io.StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stderr(stderr),
        ):
            status = retention_cli.main(
                [
                    "--runtime",
                    "8790",
                    "--as-of",
                    future.isoformat(),
                    "--plan-out",
                    str(plan_path),
                ],
                now=self.as_of,
            )
        self.assertEqual(status, 2)
        self.assertIn("future as_of", stderr.getvalue())
        self.assertFalse(plan_path.exists())
        self.assertEqual(before, self._source_snapshot())

    def test_future_saved_plan_is_rejected_on_load_and_apply(self) -> None:
        plan = self._plan()
        plan["as_of"] = (self.as_of + timedelta(days=1)).isoformat()
        plan["plan_hash"] = retention_plan_hash(plan)
        plan_path = self.base / "forged-future-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        before = self._source_snapshot()

        with self.assertRaisesRegex(RetentionError, "future as_of"):
            load_retention_plan(plan_path, now=self.as_of)
        with self.assertRaisesRegex(RetentionError, "future as_of"):
            apply_retention_plan(
                plan,
                expected_plan_hash=str(plan["plan_hash"]),
                repository_root=self.repository,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime,),
                runtime_stopped_confirmed=True,
                now=self.as_of,
            )

        self.assertFalse(self.backup_root.exists())
        self.assertEqual(before, self._source_snapshot())

    def test_apply_requires_explicit_runtime_stop_confirmation(self) -> None:
        plan = self._plan()
        with self.assertRaisesRegex(RetentionError, "stop confirmation"):
            apply_retention_plan(
                plan,
                expected_plan_hash=str(plan["plan_hash"]),
                repository_root=self.repository,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime,),
                now=self.as_of,
            )
        self.assertFalse(self.backup_root.exists())

    def test_apply_rejects_wrong_hash_internal_backup_and_forged_path(self) -> None:
        plan = self._plan()
        before = self._source_snapshot()
        with self.assertRaisesRegex(RetentionError, "hash"):
            apply_retention_plan(
                plan,
                expected_plan_hash="0" * 64,
                repository_root=self.repository,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime,),
                runtime_stopped_confirmed=True,
                now=self.as_of,
            )
        with self.assertRaisesRegex(RetentionError, "outside"):
            apply_retention_plan(
                plan,
                expected_plan_hash=str(plan["plan_hash"]),
                repository_root=self.repository,
                backup_root=self.repository / "backups",
                allowed_runtime_roots=(self.runtime,),
                runtime_stopped_confirmed=True,
                now=self.as_of,
            )

        forged = json.loads(json.dumps(plan))
        self._action(forged, "responses")["file"] = "../responses.sqlite3"
        forged["plan_hash"] = retention_plan_hash(forged)
        with self.assertRaisesRegex(RetentionError, "approved policy"):
            apply_retention_plan(
                forged,
                expected_plan_hash=str(forged["plan_hash"]),
                repository_root=self.repository,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime,),
                runtime_stopped_confirmed=True,
                now=self.as_of,
            )
        self.assertEqual(before, self._source_snapshot())
        self.assertFalse(self.backup_root.exists())

    def test_apply_rejects_duplicate_candidate_keys_before_backup_or_cleanup(self) -> None:
        def assert_duplicate(source: str, key: str, expected: str) -> None:
            plan = json.loads(json.dumps(self._plan()))
            action = self._action(plan, source)
            candidate = json.loads(json.dumps(action["candidates"][0]))
            action["candidates"].append(candidate)
            action["candidate_count"] = len(action["candidates"])
            self.assertEqual(candidate[key], action["candidates"][0][key])
            plan["plan_hash"] = retention_plan_hash(plan)
            before = self._source_snapshot()

            with self.assertRaisesRegex(RetentionError, expected):
                apply_retention_plan(
                    plan,
                    expected_plan_hash=str(plan["plan_hash"]),
                    repository_root=self.repository,
                    backup_root=self.backup_root,
                    allowed_runtime_roots=(self.runtime,),
                    runtime_stopped_confirmed=True,
                    now=self.as_of,
                )

            self.assertEqual(before, self._source_snapshot())
            self.assertFalse(self.backup_root.exists())

        feedback = self._action(self._plan(), "feedback_cases")
        self.assertEqual(len(feedback["candidates"]), 1)
        self.assertTrue(feedback["candidates"][0]["media_files"])
        media_paths = list(
            (self.runtime / "feedback_cases" / self.expired_feedback.feedback_id).rglob("*")
        )
        self.assertTrue(media_paths)
        assert_duplicate("feedback_cases", "feedback_id", "duplicate feedback_id")
        self.assertTrue(all(path.exists() for path in media_paths))

        assert_duplicate("responses", "response_id", "duplicate response_id")
        assert_duplicate("a3_page_errors", "event_id", "duplicate event_id")

    def test_apply_backs_up_then_removes_only_approved_expired_evidence(self) -> None:
        plan = self._plan()
        immutable_paths = [
            self.runtime / "model_costs.sqlite3",
            self.runtime / "control.sqlite3",
            self.runtime / "trace_events.sqlite3",
            self.runtime / "a2" / "task_logs.jsonl",
            self.runtime / "output_watchdog" / "output_watchdog.jsonl",
            self.runtime / "tiku_8790.out.log",
        ]
        immutable_before = {path: _sha256(path) for path in immutable_paths}

        result = apply_retention_plan(
            plan,
            expected_plan_hash=str(plan["plan_hash"]),
            repository_root=self.repository,
            backup_root=self.backup_root,
            allowed_runtime_roots=(self.runtime,),
            runtime_stopped_confirmed=True,
            now=self.as_of,
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["changed"], {  # type: ignore[index]
            "responses": 1,
            "feedback_cases": 1,
            "a3_page_errors": 1,
        })
        with closing(
            sqlite3.connect(self.runtime / "responses.sqlite3")
        ) as connection:
            remaining = {
                row[0] for row in connection.execute("SELECT response_id FROM public_responses")
            }
        self.assertNotIn(self.expired_response.response_id, remaining)
        self.assertIn(self.held_response.response_id, remaining)
        self.assertIn(self.fresh_response.response_id, remaining)

        feedback_store = SQLiteFeedbackStore(self.runtime / "feedback.sqlite3")
        expired = feedback_store.get_feedback(self.expired_feedback.feedback_id)
        active = feedback_store.get_feedback(self.active_feedback.feedback_id)
        self.assertIsNotNone(expired)
        self.assertEqual(expired.conversation, ())  # type: ignore[union-attr]
        self.assertEqual(expired.rating, "negative")  # type: ignore[union-attr]
        self.assertEqual(
            expired.rated_response_id, self.expired_response.response_id  # type: ignore[union-attr]
        )
        self.assertTrue(expired.case_purged_at)  # type: ignore[union-attr]
        self.assertFalse(
            (feedback_store.cases_root / self.expired_feedback.feedback_id).exists()
        )
        self.assertIsNotNone(active)
        self.assertFalse(active.case_purged_at)  # type: ignore[union-attr]

        with closing(
            sqlite3.connect(self.runtime / "a3_sessions.sqlite3")
        ) as connection:
            error_count = connection.execute("SELECT COUNT(*) FROM a3_page_errors").fetchone()[0]
        self.assertEqual(error_count, 1)
        self.assertEqual(immutable_before, {path: _sha256(path) for path in immutable_paths})

        backup_dir = Path(str(result["backup_dir"]))
        self.assertFalse(backup_dir.is_relative_to(self.repository))
        for name in ("responses.sqlite3", "feedback.sqlite3", "a3_sessions.sqlite3"):
            backup = backup_dir / "sqlite" / name
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue((backup_dir / "backup_manifest.json").is_file())
        self.assertTrue((backup_dir / "result.json").is_file())

        repeated = apply_retention_plan(
            plan,
            expected_plan_hash=str(plan["plan_hash"]),
            repository_root=self.repository,
            backup_root=self.backup_root,
            allowed_runtime_roots=(self.runtime,),
            runtime_stopped_confirmed=True,
            now=self.as_of,
        )
        self.assertEqual(repeated["status"], "already_applied")

    def test_plan_drift_aborts_before_backup_or_cleanup(self) -> None:
        plan = self._plan()
        with closing(
            sqlite3.connect(self.runtime / "feedback.sqlite3")
        ) as connection:
            with connection:
                connection.execute(
                    "UPDATE message_feedback SET updated_at = ? WHERE feedback_id = ?",
                    (self.as_of.isoformat(), self.expired_feedback.feedback_id),
                )
        with self.assertRaisesRegex(RetentionError, "drift"):
            apply_retention_plan(
                plan,
                expected_plan_hash=str(plan["plan_hash"]),
                repository_root=self.repository,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime,),
                runtime_stopped_confirmed=True,
                now=self.as_of,
            )
        self.assertFalse(self.backup_root.exists())
        self.assertIsNotNone(
            SQLiteResponseStore(self.runtime / "responses.sqlite3").get(
                self.expired_response.response_id
            )
        )

    def test_cli_defaults_to_plan_and_never_requires_an_apply_flag(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stdout(stdout),
        ):
            status = retention_cli.main(
                [
                    "--runtime",
                    "8790",
                    "--as-of",
                    self.as_of.isoformat(),
                    "--format",
                    "json",
                ],
                now=self.as_of,
            )
        self.assertEqual(status, 0)
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["mode"], "plan")
        self.assertIn("plan_hash", rendered)
        self.assertNotIn("runtime_root", rendered)
        self.assertNotIn("repository_root", rendered)
        self.assertTrue(
            all("candidates" not in action for action in rendered["actions"])
        )
        self.assertEqual(
            set(rendered["summary"]),
            {
                "candidate_count",
                "estimated_logical_bytes",
                "policy_missing_count",
                "sqlite_disk_note",
            },
        )

    def test_cli_apply_rejects_plan_with_mismatched_runtime_name(self) -> None:
        plan = build_retention_plan(
            self.runtime,
            runtime_name="8790",
            repository_root=self.repository,
            as_of=self.as_of,
            now=self.as_of,
        )
        plan["runtime_name"] = "8896"
        plan["plan_hash"] = retention_plan_hash(plan)
        plan_path = self.base / "retention-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        stderr = io.StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stderr(stderr),
        ):
            status = retention_cli.main(
                [
                    "--runtime",
                    "8790",
                    "--apply-plan",
                    str(plan_path),
                    "--confirm-plan-hash",
                    str(plan["plan_hash"]),
                    "--confirm-runtime-stopped",
                ],
                now=self.as_of,
            )
        self.assertEqual(status, 2)
        self.assertIn("does not belong to the selected runtime", stderr.getvalue())

    def test_cli_apply_rejects_plan_with_mismatched_runtime_root(self) -> None:
        plan = build_retention_plan(
            self.runtime,
            runtime_name="8790",
            repository_root=self.repository,
            as_of=self.as_of,
            now=self.as_of,
        )
        plan["runtime_root"] = str((self.repository / "other-runtime").resolve())
        plan["plan_hash"] = retention_plan_hash(plan)
        plan_path = self.base / "retention-plan-wrong-root.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        stderr = io.StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stderr(stderr),
        ):
            status = retention_cli.main(
                [
                    "--runtime",
                    "8790",
                    "--apply-plan",
                    str(plan_path),
                    "--confirm-plan-hash",
                    str(plan["plan_hash"]),
                    "--confirm-runtime-stopped",
                ],
                now=self.as_of,
            )
        self.assertEqual(status, 2)
        self.assertIn("does not belong to the selected runtime", stderr.getvalue())

    def test_admin_startup_no_longer_runs_physical_feedback_purge(self) -> None:
        control = SQLiteControlStore(self.repository / "admin" / "control.sqlite3")
        feedback = SQLiteFeedbackStore(self.runtime / "feedback.sqlite3")
        reporter = AdminReporter(
            control_store=control,
            cost_databases=(),
            feedback_store=feedback,
        )
        with patch.object(feedback, "purge_expired_cases") as purge:
            with TestClient(
                create_admin_app(
                    control_store=control,
                    reporter=reporter,
                    feedback_store=feedback,
                )
            ):
                pass
        purge.assert_not_called()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()

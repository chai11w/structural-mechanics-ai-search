from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import UTC, datetime
from io import BytesIO, StringIO
import json
from hashlib import sha256
import multiprocessing
from pathlib import Path
import shutil
import sqlite3
from threading import Event, Thread
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from scripts import tiku_checkpoint_retention as retention_cli
from tiku_agent.checkpoint_contract import (
    RETENTION_NORMAL,
    SCOPE_WORKFLOW,
    CheckpointOwnerV1,
    EvidenceCapacityPolicyV1,
)
from tiku_agent.checkpoint_store import EvidenceMaintenanceError, SQLiteCheckpointStore
from tiku_diagnostics import checkpoint_retention as retention
from tiku_shared.trace_context import TraceContext
from tiku_shared.trace_events import (
    SQLiteTraceEventStore,
    TraceCleanupSnapshot,
    TraceEvent,
)


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _hold_os_lock(runtime: str, ready, release) -> None:
    with retention._execution_lock(Path(runtime)):
        ready.set()
        release.wait(10)


class CheckpointRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        # The worktree can be read-only under the bundled runtime.  Keep
        # mutable SQLite/backup fixtures in the system temp directory.
        self._temp_dir = tempfile.TemporaryDirectory(prefix="checkpoint_retention_")
        self.base = Path(self._temp_dir.name)
        self.repository = self.base / "repository"
        self.runtime = self.repository / "runtime"
        self.backups = self.base / "backups"
        self.runtime.mkdir(parents=True)
        self.addCleanup(self._temp_dir.cleanup)
        self.capacity = EvidenceCapacityPolicyV1(
            max_checkpoint_rows=100,
            max_artifact_rows=100,
            max_audit_rows=100,
            max_trace_rows=100,
            max_artifact_bytes=10_000_000,
            min_free_bytes=1,
            max_artifacts_per_checkpoint=10,
        )

    def trace_store(self) -> SQLiteTraceEventStore:
        return SQLiteTraceEventStore(
            self.runtime / retention.TRACE_DATABASE,
            max_rows=self.capacity.max_trace_rows,
        )

    @staticmethod
    def event(trace_id: str, occurred_at: str) -> TraceEvent:
        return TraceEvent.create(
            trace_id=trace_id,
            event_type="stage_started",
            stage="candidate_rerank",
            outcome="started",
            occurred_at=occurred_at,
            call_id=uuid4().hex,
        )

    def build_plan(self) -> dict[str, object]:
        return retention.build_checkpoint_retention_plan(
            self.runtime,
            runtime_name=retention.RUNTIME_NAME_8790,
            repository_root=self.repository,
            capacity=self.capacity,
            backup_keep_runs=2,
            now=NOW,
        )

    def apply(self, plan: dict[str, object]) -> dict[str, object]:
        return retention.apply_checkpoint_retention_plan(
            plan,
            expected_plan_hash=str(plan["plan_hash"]),
            repository_root=self.repository,
            backup_root=self.backups,
            allowed_runtime_roots=(self.runtime,),
            capacity=self.capacity,
            now=NOW,
        )

    def checkpoint_store(self, now: datetime) -> SQLiteCheckpointStore:
        return SQLiteCheckpointStore(
            self.runtime / retention.CHECKPOINT_DATABASE,
            artifact_root=self.runtime / retention.CHECKPOINT_ARTIFACT_ROOT,
            capacity=self.capacity,
            trace_db_path=self.runtime / retention.TRACE_DATABASE,
            trace_row_counter=lambda _path: self.trace_store().capacity_snapshot()[
                "current_rows"
            ],
            clock=lambda: now,
        )

    @staticmethod
    def image_bytes() -> bytes:
        output = BytesIO()
        Image.new("RGB", (3, 3), "white").save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def owner() -> CheckpointOwnerV1:
        return CheckpointOwnerV1(
            scope=SCOPE_WORKFLOW,
            session_key="a" * 64,
            identity_key="invite_test",
            workflow_search_id="workflow_test",
            workflow_task_revision=1,
            task_revision=1,
        )

    def create_missing_artifact(self) -> str:
        content = self.image_bytes()
        descriptor = self.checkpoint_store(
            datetime(2026, 9, 1, tzinfo=UTC)
        ).put_artifact(
            self.owner(),
            content,
            retention_class=RETENTION_NORMAL,
        )
        digest = descriptor.sha256
        path = (
            self.runtime
            / retention.CHECKPOINT_ARTIFACT_ROOT
            / "blobs"
            / digest[:2]
            / f"{digest}.bin"
        )
        path.unlink()
        return descriptor.artifact_id

    def create_unindexed_file(self, content: bytes = b"orphan-bytes") -> Path:
        digest = sha256(content).hexdigest()
        path = (
            self.runtime
            / retention.CHECKPOINT_ARTIFACT_ROOT
            / "blobs"
            / digest[:2]
            / f"{digest}.bin"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_plan_is_read_only_and_uses_complete_30_day_trace_timelines(self) -> None:
        store = self.trace_store()
        expired = TraceContext.create()
        mixed = TraceContext.create()
        store.write(self.event(expired.trace_id, "2026-07-01T00:00:00Z"))
        store.write(self.event(expired.trace_id, "2026-08-06T12:00:00Z"))
        store.write(self.event(mixed.trace_id, "2026-07-01T00:00:00Z"))
        store.write(self.event(mixed.trace_id, "2026-08-07T00:00:00Z"))
        checkpoint_db = self.runtime / retention.CHECKPOINT_DATABASE
        artifact_root = self.runtime / retention.CHECKPOINT_ARTIFACT_ROOT

        plan = self.build_plan()

        self.assertFalse(checkpoint_db.exists())
        self.assertFalse(artifact_root.exists())
        snapshot = plan["trace_store"]["cleanup_snapshot"]  # type: ignore[index]
        self.assertEqual(snapshot["candidate_count"], 1)
        self.assertEqual(snapshot["event_count"], 2)
        self.assertEqual(snapshot["candidates"][0]["trace_id"], expired.trace_id)
        self.assertEqual(plan["policy"]["trace_capacity_eviction"], False)  # type: ignore[index]

    def test_apply_allows_new_fresh_trace_and_replay_rechecks_manifest(self) -> None:
        store = self.trace_store()
        expired = TraceContext.create()
        store.write(self.event(expired.trace_id, "2026-07-01T00:00:00Z"))
        plan = self.build_plan()
        fresh = TraceContext.create()
        store.write(self.event(fresh.trace_id, "2026-09-05T11:59:00Z"))

        result = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(store.events_for_trace(expired.trace_id), [])
        self.assertEqual(len(store.events_for_trace(fresh.trace_id)), 1)
        backup = Path(str(result["backup_dir"])) / "sqlite" / retention.TRACE_DATABASE
        backup.write_bytes(b"corrupt")
        with self.assertRaises(retention.CheckpointRetentionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, retention.RETENTION_BACKUP_FAILED)

    def test_replay_allows_unrelated_trace_retention_after_terminal_result(self) -> None:
        store = self.trace_store()
        expired = TraceContext.create()
        fresh = TraceContext.create()
        store.write(self.event(expired.trace_id, "2026-07-01T00:00:00Z"))
        store.write(self.event(fresh.trace_id, "2026-09-05T11:59:00Z"))
        plan = self.build_plan()

        result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["metrics_after"]["trace_rows"], 1)  # type: ignore[index]

        # A separate retention run may remove a fresh trace after this plan
        # has published its terminal metrics.
        fresh_snapshot = store.cleanup_candidates(cutoff=NOW.isoformat())
        self.assertEqual(fresh_snapshot.candidate_count, 1)
        store.apply_cleanup(fresh_snapshot)
        self.assertEqual(store.capacity_snapshot()["current_rows"], 0)

        replay = self.apply(plan)
        self.assertEqual(replay["status"], "already_applied")

    def test_trace_satisfaction_rejects_a_missing_store(self) -> None:
        store = self.trace_store()
        trace = TraceContext.create()
        store.write(self.event(trace.trace_id, "2026-07-01T00:00:00Z"))
        plan = self.build_plan()
        snapshot = TraceCleanupSnapshot.from_dict(
            plan["trace_store"]["cleanup_snapshot"]  # type: ignore[index]
        )
        (self.runtime / retention.TRACE_DATABASE).unlink()

        with self.assertRaises(retention.CheckpointRetentionError) as caught:
            retention._trace_candidates_already_satisfied(store, snapshot)
        self.assertEqual(caught.exception.code, retention.RETENTION_DRIFT_DETECTED)

    def test_missing_artifact_is_cleaned_without_requiring_missing_blob_backup(self) -> None:
        artifact_id = self.create_missing_artifact()
        plan = self.build_plan()
        candidates = plan["checkpoint_store"]["retention_plan"][  # type: ignore[index]
            "artifact_candidates"
        ]
        self.assertEqual(candidates[0]["reason"], "missing")

        result = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        with closing(sqlite3.connect(self.runtime / retention.CHECKPOINT_DATABASE)) as connection:
            status = connection.execute(
                "SELECT status FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()[0]
        self.assertEqual(status, "purged")
        manifest = json.loads(
            (Path(str(result["backup_dir"])) / "backup_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(any(item["kind"] == "artifact" for item in manifest["files"]))

    def test_physical_unlink_failure_restores_zero_ref_blob_for_same_plan_retry(self) -> None:
        store = self.checkpoint_store(datetime(2026, 9, 1, tzinfo=UTC))
        descriptor = store.put_artifact(
            self.owner(), self.image_bytes(), retention_class=RETENTION_NORMAL
        )
        blob_path = (
            self.runtime
            / retention.CHECKPOINT_ARTIFACT_ROOT
            / "blobs"
            / descriptor.sha256[:2]
            / f"{descriptor.sha256}.bin"
        )
        plan = self.build_plan()
        candidates = plan["checkpoint_store"]["retention_plan"]["artifact_candidates"]  # type: ignore[index]
        self.assertEqual(candidates[0]["reason"], "expired")

        original_unlink = Path.unlink
        failed = False

        def fail_once(path: Path, *args, **kwargs):
            nonlocal failed
            if path == blob_path and not failed:
                failed = True
                raise PermissionError("injected physical unlink failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", new=fail_once):
            with self.assertRaises(retention.CheckpointRetentionError) as caught:
                self.apply(plan)
        self.assertEqual(caught.exception.code, retention.RETENTION_DRIFT_DETECTED)
        self.assertTrue(failed)
        self.assertTrue(blob_path.is_file())

        with closing(sqlite3.connect(self.runtime / retention.CHECKPOINT_DATABASE)) as connection:
            artifact_status, ref_count = connection.execute(
                "SELECT a.status, b.ref_count FROM artifacts a "
                "JOIN artifact_blobs b ON b.sha256 = a.sha256 "
                "WHERE a.artifact_id = ?",
                (descriptor.artifact_id,),
            ).fetchone()
        self.assertEqual(artifact_status, "purged")
        self.assertEqual(ref_count, 0)
        failure = json.loads(
            next(self.backups.rglob("failure.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(failure["failure_code"], retention.RETENTION_DRIFT_DETECTED)

        result = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        self.assertFalse(blob_path.exists())
        with closing(sqlite3.connect(self.runtime / retention.CHECKPOINT_DATABASE)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM artifact_blobs WHERE sha256 = ?",
                    (descriptor.sha256,),
                ).fetchone()
            )

    def test_partial_backup_is_safely_completed_on_same_plan_retry(self) -> None:
        self.create_missing_artifact()
        trace = TraceContext.create()
        self.trace_store().write(self.event(trace.trace_id, "2026-07-01T00:00:00Z"))
        plan = self.build_plan()
        original = retention._backup_sqlite
        calls = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise retention.CheckpointRetentionError(
                    "injected backup failure", code=retention.RETENTION_BACKUP_FAILED
                )
            original(source, destination)

        with patch.object(retention, "_backup_sqlite", side_effect=fail_second):
            with self.assertRaises(retention.CheckpointRetentionError):
                self.apply(plan)

        target = next(self.backups.rglob("checkpoint_retention_*"))
        failure = json.loads((target / "failure.json").read_text(encoding="utf-8"))
        state = json.loads((target / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["status"], "failed_closed")
        self.assertEqual(failure["failure_code"], retention.RETENTION_BACKUP_FAILED)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["completed_phases"], {})

        result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        backup_dir = Path(str(result["backup_dir"]))
        self.assertTrue((backup_dir / "sqlite" / retention.CHECKPOINT_DATABASE).is_file())
        self.assertTrue((backup_dir / "sqlite" / retention.TRACE_DATABASE).is_file())
        self.assertFalse((backup_dir / "failure.json").exists())

    def test_absent_checkpoint_store_cleans_unindexed_file_without_creating_database(self) -> None:
        orphan = self.create_unindexed_file()
        plan = self.build_plan()
        checkpoint_plan = plan["checkpoint_store"]["retention_plan"]  # type: ignore[index]
        candidates = checkpoint_plan["orphan_candidates"]  # type: ignore[index]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reason"], "unindexed_file")
        self.assertFalse((self.runtime / retention.CHECKPOINT_DATABASE).exists())

        result = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        self.assertFalse(orphan.exists())
        self.assertFalse((self.runtime / retention.CHECKPOINT_DATABASE).exists())
        self.assertFalse((self.runtime / retention.TRACE_DATABASE).exists())

    def test_empty_absent_stores_apply_and_replay_without_creating_databases(self) -> None:
        plan = self.build_plan()

        result = self.apply(plan)
        replay = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(replay["status"], "already_applied")
        self.assertFalse((self.runtime / retention.CHECKPOINT_DATABASE).exists())
        self.assertFalse((self.runtime / retention.TRACE_DATABASE).exists())

    def test_absent_store_plan_is_not_satisfied_while_orphan_file_remains(self) -> None:
        orphan = self.create_unindexed_file(b"still-present")
        store = self.checkpoint_store(NOW)
        plan = store.plan_retention(as_of=NOW)

        self.assertFalse(store.retention_plan_satisfied(plan))
        orphan.unlink()
        self.assertTrue(store.retention_plan_satisfied(plan))

    def test_artifact_row_disappearance_does_not_hide_a_remaining_blob(self) -> None:
        writer = self.checkpoint_store(datetime(2026, 9, 1, tzinfo=UTC))
        descriptor = writer.put_artifact(
            self.owner(), self.image_bytes(), retention_class=RETENTION_NORMAL
        )
        store = self.checkpoint_store(NOW)
        plan = store.plan_retention(as_of=NOW)
        with closing(sqlite3.connect(self.runtime / retention.CHECKPOINT_DATABASE)) as connection:
            connection.execute(
                "DELETE FROM artifacts WHERE artifact_id = ?",
                (descriptor.artifact_id,),
            )
            connection.commit()

        self.assertFalse(store.retention_plan_satisfied(plan))
        with self.assertRaises(EvidenceMaintenanceError):
            store.apply_retention_plan(plan)

    def test_runtime_name_must_be_a_string(self) -> None:
        with self.assertRaises(retention.CheckpointRetentionError):
            retention.build_checkpoint_retention_plan(
                self.runtime,
                runtime_name=8790,  # type: ignore[arg-type]
                repository_root=self.repository,
                capacity=self.capacity,
                backup_keep_runs=2,
                now=NOW,
            )

    def test_store_success_then_trace_failure_retries_same_plan(self) -> None:
        artifact_id = self.create_missing_artifact()
        trace = TraceContext.create()
        self.trace_store().write(self.event(trace.trace_id, "2026-07-01T00:00:00Z"))
        plan = self.build_plan()
        original = SQLiteTraceEventStore.apply_cleanup
        attempts = 0

        def fail_once(store, snapshot):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected")
            return original(store, snapshot)

        with patch.object(SQLiteTraceEventStore, "apply_cleanup", new=fail_once):
            with self.assertRaises(retention.CheckpointRetentionError):
                self.apply(plan)
            result = self.apply(plan)

        self.assertEqual(result["status"], "applied")
        with closing(sqlite3.connect(self.runtime / retention.CHECKPOINT_DATABASE)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()[0],
                "purged",
            )
        self.assertEqual(self.trace_store().events_for_trace(trace.trace_id), [])

    def test_trace_success_then_progress_failure_recovers_same_store(self) -> None:
        trace = TraceContext.create()
        self.trace_store().write(self.event(trace.trace_id, "2026-07-01T00:00:00Z"))
        plan = self.build_plan()
        original = retention._atomic_write_json
        failed = False

        def fail_trace_progress(path: Path, value) -> None:
            nonlocal failed
            phases = value.get("completed_phases", {})
            if path.name == "state.json" and "trace_store" in phases and not failed:
                failed = True
                raise retention.CheckpointRetentionError(
                    "injected state failure", code=retention.RETENTION_IO_FAILED
                )
            original(path, value)

        with patch.object(retention, "_atomic_write_json", side_effect=fail_trace_progress):
            with self.assertRaises(retention.CheckpointRetentionError):
                self.apply(plan)

        result = self.apply(plan)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            result["completed_phases"]["trace_store"]["status"],  # type: ignore[index]
            "already_satisfied",
        )

    def test_both_public_apply_paths_share_process_lock(self) -> None:
        plan = self.build_plan()
        with retention._execution_lock(self.runtime):
            with self.assertRaises(retention.CheckpointRetentionError) as direct:
                self.apply(plan)
            with self.assertRaises(retention.CheckpointRetentionError) as periodic:
                retention.run_checkpoint_retention_once(
                    self.runtime,
                    runtime_name=retention.RUNTIME_NAME_8790,
                    repository_root=self.repository,
                    backup_root=self.backups,
                    allowed_runtime_roots=(self.runtime,),
                    capacity=self.capacity,
                    backup_keep_runs=2,
                    now=NOW,
                )
        self.assertEqual(direct.exception.code, retention.RETENTION_ALREADY_RUNNING)
        self.assertEqual(periodic.exception.code, retention.RETENTION_ALREADY_RUNNING)

    def test_os_lock_rejects_second_process(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_os_lock, args=(str(self.runtime), ready, release)
        )
        process.start()
        self.addCleanup(lambda: process.is_alive() and process.terminate())
        self.assertTrue(ready.wait(10))
        try:
            with self.assertRaises(retention.CheckpointRetentionError) as caught:
                with retention._execution_lock(self.runtime):
                    pass
            self.assertEqual(caught.exception.code, retention.RETENTION_ALREADY_RUNNING)
        finally:
            release.set()
            process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_lock_rejects_an_existing_nonregular_path(self) -> None:
        (self.runtime / ".checkpoint_retention.lock").mkdir()
        with self.assertRaises(retention.CheckpointRetentionError) as caught:
            with retention._execution_lock(self.runtime):
                pass
        self.assertEqual(caught.exception.code, retention.RETENTION_PATH_REJECTED)

    def test_runner_records_arbitrary_failure_in_safe_health(self) -> None:
        runner = retention.CheckpointRetentionRunner(
            runtime_root=self.runtime,
            runtime_name=retention.RUNTIME_NAME_8790,
            repository_root=self.repository,
            backup_root=self.backups,
            capacity=self.capacity,
            backup_keep_runs=2,
            clock=lambda: NOW,
        )
        with patch.object(
            retention, "run_checkpoint_retention_once", side_effect=RuntimeError("private")
        ):
            with self.assertRaises(retention.CheckpointRetentionError):
                runner.run_once()
        health = runner.health()
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["last_failure_code"], "retention_apply_failed")
        self.assertNotIn("private", json.dumps(health))
        self.assertEqual(health["pending"], 0)
        self.assertEqual(health["queue_capacity"], 0)

    def test_cli_defaults_to_plan_requires_all_capacity_and_only_allows_8790(self) -> None:
        parser = retention_cli.build_argument_parser()
        runtime_action = next(
            action for action in parser._actions if action.dest == "runtime"
        )
        self.assertEqual(tuple(runtime_action.choices), ("8790",))
        common = [
            "--runtime", "8790",
            "--backup-keep-runs", "2",
            "--max-checkpoint-rows", "100",
            "--max-artifact-rows", "100",
            "--max-audit-rows", "100",
            "--max-trace-rows", "100",
            "--max-artifact-bytes", "10000000",
            "--min-free-bytes", "1",
            "--max-artifacts-per-checkpoint", "10",
            "--format", "json",
        ]
        output = StringIO()
        with (
            patch.object(retention_cli, "BASE", self.repository),
            patch.dict(retention_cli.RUNTIME_ROOTS, {"8790": self.runtime}, clear=True),
            redirect_stdout(output),
        ):
            status = retention_cli.main(common, now=NOW)
        self.assertEqual(status, 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["mode"], "plan")
        self.assertNotIn("runtime_root", rendered)
        self.assertFalse((self.runtime / retention.CHECKPOINT_DATABASE).exists())
        self.assertFalse((self.runtime / ".checkpoint_retention.lock").exists())

        with self.assertRaises(SystemExit):
            parser.parse_args(common[:-4])


if __name__ == "__main__":
    unittest.main()

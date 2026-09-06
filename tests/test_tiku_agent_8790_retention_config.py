from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8790 import BASE, build_app, main
from tiku_admin.control_store import SQLiteControlStore
from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1


def _capacity() -> EvidenceCapacityPolicyV1:
    return EvidenceCapacityPolicyV1(
        max_checkpoint_rows=101,
        max_artifact_rows=102,
        max_audit_rows=103,
        max_trace_rows=104,
        max_artifact_bytes=10_000_000,
        min_free_bytes=1,
        max_artifacts_per_checkpoint=5,
    )


class TikuAgent8790RetentionConfigTest(unittest.TestCase):
    def test_control_store_supplies_feedback_retention_dynamically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control_store = SQLiteControlStore(root / "admin" / "control.sqlite3")
            settings = control_store.settings()
            control_store.update_settings(
                global_daily_budget_micros=int(
                    settings["global_daily_budget_micros"]
                ),
                default_invite_daily_budget_micros=int(
                    settings["default_invite_daily_budget_micros"]
                ),
                feedback_retention_days=45,
            )

            with (
                patch(
                    "scripts.run_tiku_agent_8790.build_a3_runtime",
                    return_value=object(),
                ),
                patch(
                    "scripts.run_tiku_agent_8790.create_app",
                    return_value=object(),
                ) as create_app,
            ):
                build_app(
                    root / "runtime",
                    control_db=control_store.path,
                    enable_a3_text_orientation=False,
                )

            provider = create_app.call_args.kwargs[
                "feedback_retention_days_provider"
            ]
            self.assertIsNotNone(provider)
            self.assertEqual(provider(), 45)

            control_store.update_settings(
                global_daily_budget_micros=int(
                    settings["global_daily_budget_micros"]
                ),
                default_invite_daily_budget_micros=int(
                    settings["default_invite_daily_budget_micros"]
                ),
                feedback_retention_days=60,
            )
            self.assertEqual(provider(), 60)

    def test_static_invite_mode_keeps_the_default_retention(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch(
                    "scripts.run_tiku_agent_8790.build_a3_runtime",
                    return_value=object(),
                ),
                patch(
                    "scripts.run_tiku_agent_8790.create_app",
                    return_value=object(),
                ) as create_app,
            ):
                build_app(root / "runtime", enable_a3_text_orientation=False)

            self.assertIsNone(
                create_app.call_args.kwargs["feedback_retention_days_provider"]
            )

    def test_explicit_configuration_builds_isolated_evidence_control_plane(self):
        with (
            tempfile.TemporaryDirectory(dir=BASE) as runtime_temp,
            tempfile.TemporaryDirectory() as backup_temp,
            patch(
                "scripts.run_tiku_agent_8790.build_a3_runtime",
                return_value=object(),
            ) as build_runtime,
        ):
            runtime = Path(runtime_temp)
            backup = Path(backup_temp)
            capacity = _capacity()
            app = build_app(
                runtime,
                enable_triage=False,
                evidence_capacity=capacity,
                checkpoint_retention_backup_root=backup,
                checkpoint_retention_interval_seconds=60,
                checkpoint_retention_backup_keep_runs=3,
            )
            self.addCleanup(app.state.trace_event_recorder.close)

            checkpoint_store = app.state.checkpoint_evidence_store
            runner = app.state.checkpoint_retention_controller
            self.assertIsNotNone(checkpoint_store)
            self.assertIsNotNone(runner)
            self.assertEqual(
                checkpoint_store.path,
                (runtime / "checkpoint_evidence.sqlite3").resolve(),
            )
            self.assertEqual(
                checkpoint_store.artifact_root,
                (runtime / "checkpoint_artifacts").resolve(),
            )
            self.assertEqual(
                app.state.trace_event_recorder.store.path.resolve(),
                (runtime / "trace_events.sqlite3").resolve(),
            )
            self.assertEqual(app.state.trace_event_recorder.store.max_rows, 104)
            self.assertEqual(runner.backup_root, backup.resolve())
            self.assertEqual(runner.capacity, capacity)
            self.assertNotIn("checkpoint_recorder", build_runtime.call_args.kwargs)
            self.assertNotIn("checkpoint_store", build_runtime.call_args.kwargs)
            self.assertNotIn("artifact_store", build_runtime.call_args.kwargs)

    def test_partial_or_unsafe_evidence_configuration_is_rejected(self):
        capacity = _capacity()
        with self.assertRaisesRegex(ValueError, "backup root is required"):
            build_app("unused", evidence_capacity=capacity)
        with self.assertRaisesRegex(ValueError, "require an evidence capacity"):
            build_app(
                "unused",
                checkpoint_retention_backup_root=Path(tempfile.gettempdir()),
            )
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            build_app(
                "unused",
                evidence_capacity=capacity,
                checkpoint_retention_backup_root=Path("relative"),
                checkpoint_retention_interval_seconds=60,
                checkpoint_retention_backup_keep_runs=3,
            )
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            build_app(
                "unused",
                evidence_capacity=capacity,
                checkpoint_retention_backup_root=BASE / "backup",
                checkpoint_retention_interval_seconds=60,
                checkpoint_retention_backup_keep_runs=3,
            )
        with tempfile.TemporaryDirectory() as external_runtime:
            with self.assertRaisesRegex(ValueError, "runtime root"):
                build_app(
                    external_runtime,
                    evidence_capacity=capacity,
                    checkpoint_retention_backup_root=Path(tempfile.gettempdir()),
                    checkpoint_retention_interval_seconds=60,
                    checkpoint_retention_backup_keep_runs=3,
                )
        for interval in (None, 0, float("nan")):
            with self.subTest(interval=interval), self.assertRaisesRegex(
                ValueError, "interval"
            ):
                build_app(
                    "unused",
                    evidence_capacity=capacity,
                    checkpoint_retention_backup_root=Path(tempfile.gettempdir()),
                    checkpoint_retention_interval_seconds=interval,
                    checkpoint_retention_backup_keep_runs=3,
                )
        for keep_runs in (None, 0):
            with self.subTest(keep_runs=keep_runs), self.assertRaisesRegex(
                ValueError, "keep runs"
            ):
                build_app(
                    "unused",
                    evidence_capacity=capacity,
                    checkpoint_retention_backup_root=Path(tempfile.gettempdir()),
                    checkpoint_retention_interval_seconds=60,
                    checkpoint_retention_backup_keep_runs=keep_runs,
                )

    def test_main_passes_all_required_evidence_settings_to_builder(self):
        arguments = [
            "run_tiku_agent_8790.py",
            "--max-checkpoint-rows", "101",
            "--max-artifact-rows", "102",
            "--max-audit-rows", "103",
            "--max-trace-rows", "104",
            "--max-artifact-bytes", "10000000",
            "--min-free-bytes", "1",
            "--max-artifacts-per-checkpoint", "5",
            "--checkpoint-retention-backup-root", str(Path(tempfile.gettempdir())),
            "--checkpoint-retention-interval-seconds", "60",
            "--checkpoint-retention-backup-keep-runs", "3",
        ]
        with patch("sys.argv", arguments), patch(
            "scripts.run_tiku_agent_8790.build_app",
            return_value="app",
        ) as builder, patch("scripts.run_tiku_agent_8790.uvicorn.run") as serve:
            self.assertEqual(main(), 0)

        kwargs = builder.call_args.kwargs
        self.assertEqual(kwargs["evidence_capacity"], _capacity())
        self.assertEqual(kwargs["checkpoint_retention_interval_seconds"], 60.0)
        self.assertEqual(kwargs["checkpoint_retention_backup_keep_runs"], 3)
        serve.assert_called_once_with("app", host="127.0.0.1", port=8790)


if __name__ == "__main__":
    unittest.main()

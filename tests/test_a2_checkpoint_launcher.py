from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tiku_agent_8790 as launcher
from tests.test_checkpoint_capture import _context
from tests.test_tiku_agent_8790_retention_config import _capacity


class A2CheckpointLauncherTest(unittest.TestCase):
    def test_capture_requires_capacity_before_constructing_runtime(self):
        with patch.object(launcher, "build_a3_runtime") as build:
            with self.assertRaises(ValueError):
                launcher.build_app(enable_a2_checkpoint_capture=True)
            build.assert_not_called()

    def test_capture_requires_matching_clean_release(self):
        for head, dirty in (("b" * 40, ""), ("a" * 40, " M module.py")):
            with patch.object(launcher.subprocess, "run", side_effect=[SimpleNamespace(stdout=head), SimpleNamespace(stdout=dirty)]):
                with self.assertRaises(ValueError):
                    launcher._capture_producer("a" * 40)
        with patch.object(launcher.subprocess, "run", side_effect=[SimpleNamespace(stdout="a" * 40), SimpleNamespace(stdout="")]):
            self.assertEqual(launcher._capture_producer("a" * 40).code_revision, "a" * 40)

    def test_enabled_capture_shares_the_existing_store_retention_and_health(self):
        with tempfile.TemporaryDirectory(prefix=".tmp_capture_test_", dir=launcher.BASE) as runtime, tempfile.TemporaryDirectory() as backup:
            app = SimpleNamespace(state=SimpleNamespace())
            with patch.object(launcher, "_capture_producer", return_value=_context().producer), patch.object(
                launcher, "build_a3_runtime", return_value=object()
            ) as build, patch.object(launcher, "create_app", return_value=app) as create:
                launcher.build_app(
                    runtime, evidence_capacity=_capacity(),
                    checkpoint_retention_backup_root=backup, checkpoint_retention_interval_seconds=60,
                    checkpoint_retention_backup_keep_runs=3, enable_a2_checkpoint_capture=True,
                    checkpoint_code_revision="4" * 40,
                )
            recorder = build.call_args.kwargs["checkpoint_recorder"]
            self.assertIs(recorder.store, app.state.checkpoint_evidence_store)
            self.assertTrue(recorder.gate.enabled)
            self.assertEqual(recorder.media_root, Path(runtime).resolve() / "a2")
            self.assertIsNotNone(create.call_args.kwargs["checkpoint_retention_runner"])
            recorder.input_unavailable()
            health = create.call_args.kwargs["checkpoint_evidence_health_provider"]()
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["counters"]["capture_rejected"], 1)


if __name__ == "__main__":
    unittest.main()

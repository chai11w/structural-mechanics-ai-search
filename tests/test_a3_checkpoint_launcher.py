from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tiku_agent_8790 as launcher
from tests.test_checkpoint_capture import _context
from tests.test_tiku_agent_8790_retention_config import _capacity


class A3CheckpointLauncherTest(unittest.TestCase):
    def test_a3_capture_requires_a2_and_capacity_before_runtime_creation(self):
        for kwargs in ({"enable_a3_checkpoint_capture": True},
                       {"enable_a3_checkpoint_capture": True, "enable_a2_checkpoint_capture": True}):
            with patch.object(launcher, "build_a3_runtime") as build:
                with self.assertRaises(ValueError):
                    launcher.build_app(**kwargs)
                build.assert_not_called()

    def test_parent_and_child_share_store_health_and_retention(self):
        with tempfile.TemporaryDirectory(prefix=".tmp_capture_test_", dir=launcher.BASE) as runtime, tempfile.TemporaryDirectory() as backup:
            app = SimpleNamespace(state=SimpleNamespace())
            with patch.object(launcher, "_capture_producer", return_value=_context().producer), patch.object(
                launcher, "build_a3_runtime", return_value=object()
            ) as build, patch.object(launcher, "create_app", return_value=app) as create:
                launcher.build_app(runtime, evidence_capacity=_capacity(),
                    checkpoint_retention_backup_root=backup, checkpoint_retention_interval_seconds=60,
                    checkpoint_retention_backup_keep_runs=3, enable_a2_checkpoint_capture=True,
                    enable_a3_checkpoint_capture=True, checkpoint_code_revision="4" * 40)
            recorder = build.call_args.kwargs["checkpoint_recorder"]
            self.assertIs(recorder, build.call_args.kwargs["a3_checkpoint_recorder"])
            self.assertIs(recorder, app.state.a3_checkpoint_recorder)
            self.assertIs(recorder.store, app.state.checkpoint_evidence_store)
            self.assertEqual(recorder._page_reader.media_root, Path(runtime).resolve() / "a3_sessions")
            self.assertIsNotNone(create.call_args.kwargs["checkpoint_retention_runner"])
            recorder.input_unavailable()
            self.assertEqual(create.call_args.kwargs["checkpoint_evidence_health_provider"]()["status"], "degraded")


if __name__ == "__main__":
    unittest.main()

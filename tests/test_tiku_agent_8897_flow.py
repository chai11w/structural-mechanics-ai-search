from pathlib import Path
import tempfile
import unittest

from scripts.run_tiku_agent_8896 import build_runtime as build_8896_runtime
from scripts.run_tiku_agent_8897 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_argument_parser,
    build_runtime,
)
from tiku_agent.a3_auto_crop import GlmA3AutoCropper


class TikuAgent8897FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.watchdog = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "tiku_agent_watchdog_8897.ps1"
        ).read_text(encoding="utf-8")

    def test_launcher_uses_isolated_port_state_and_cookie(self):
        defaults = build_argument_parser().parse_args([])

        self.assertEqual(DEFAULT_PORT, 8897)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_v1_8897")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8897_session")
        self.assertTrue(defaults.enable_triage)

    def test_8896_promotion_keeps_8897_runtime_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_8896 = build_8896_runtime(root / "8896", enable_triage=False)
            runtime_8897 = build_runtime(root / "8897", enable_triage=False)

            self.assertIsInstance(runtime_8896.auto_cropper, GlmA3AutoCropper)
            self.assertIsInstance(runtime_8897.auto_cropper, GlmA3AutoCropper)
            self.assertTrue(runtime_8896.auto_prepare_all_units)
            self.assertFalse(runtime_8897.auto_prepare_all_units)
            self.assertNotEqual(
                runtime_8896.store.database_path,
                runtime_8897.store.database_path,
            )
            self.assertNotEqual(
                runtime_8896.artifacts.root,
                runtime_8897.artifacts.root,
            )

    def test_watchdog_is_scoped_to_8897_state_and_launcher(self):
        self.assertIn("[int]$Port = 8897", self.watchdog)
        self.assertIn('.tmp_tiku_agent_a3_v1_8897', self.watchdog)
        self.assertIn('scripts\\run_tiku_agent_8897.py', self.watchdog)
        self.assertIn('watchdog_8897.status', self.watchdog)
        self.assertNotIn('run_tiku_agent_8896.py', self.watchdog)


if __name__ == "__main__":
    unittest.main()

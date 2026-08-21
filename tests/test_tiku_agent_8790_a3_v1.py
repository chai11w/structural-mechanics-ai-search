from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8790 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    build_app,
    build_argument_parser,
)
from tiku_admin.control_store import SQLiteControlStore


class TikuAgent8790A3V1Test(unittest.TestCase):
    def test_launcher_defaults_to_production_port_and_a3_v1(self):
        defaults = build_argument_parser().parse_args([])

        self.assertEqual(DEFAULT_PORT, 8790)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_prod_8790")
        self.assertTrue(defaults.enable_triage)
        self.assertTrue(defaults.enable_auto_crop)

    def test_control_database_protects_the_a3_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control_path = root / "control.sqlite3"
            SQLiteControlStore(control_path)

            app = build_app(
                root / "runtime",
                control_db=control_path,
                enable_triage=False,
                enable_auto_crop=False,
            )

            self.assertIsNotNone(app)

    def test_production_auto_validates_all_units_before_selection(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8790.build_a3_runtime"
        ) as build_runtime:
            build_runtime.return_value = object()

            app = build_app(Path(temp) / "runtime", enable_triage=False)

            self.assertIsNotNone(app)
            self.assertTrue(build_runtime.call_args.kwargs["auto_prepare_all_units"])


if __name__ == "__main__":
    unittest.main()

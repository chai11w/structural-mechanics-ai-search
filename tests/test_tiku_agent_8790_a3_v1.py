from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()

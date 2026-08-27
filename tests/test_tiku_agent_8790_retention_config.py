from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8790 import build_app
from tiku_admin.control_store import SQLiteControlStore


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
                build_app(root / "runtime", control_db=control_store.path)

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
                build_app(root / "runtime")

            self.assertIsNone(
                create_app.call_args.kwargs["feedback_retention_days_provider"]
            )


if __name__ == "__main__":
    unittest.main()

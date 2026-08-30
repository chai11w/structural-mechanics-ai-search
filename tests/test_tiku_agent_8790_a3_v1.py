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
        self.assertTrue(defaults.enable_output_watchdog)
        self.assertTrue(defaults.enable_a3_text_orientation)
        self.assertEqual(defaults.max_concurrent_tasks, 1)
        self.assertEqual(defaults.max_queued_tasks, 2)
        self.assertEqual(defaults.queue_wait_seconds, 55.0)
        self.assertFalse(
            build_argument_parser().parse_args(["--disable-output-watchdog"]).enable_output_watchdog
        )
        self.assertFalse(
            build_argument_parser()
            .parse_args(["--disable-a3-text-orientation"])
            .enable_a3_text_orientation
        )

        custom = build_argument_parser().parse_args([
            "--max-concurrent-tasks", "3",
            "--max-queued-tasks", "4",
            "--queue-wait-seconds", "66",
        ])
        self.assertEqual(custom.max_concurrent_tasks, 3)
        self.assertEqual(custom.max_queued_tasks, 4)
        self.assertEqual(custom.queue_wait_seconds, 66.0)

        self.assertEqual(
            build_argument_parser().parse_args(["--max-queued-tasks", "0"]).max_queued_tasks,
            0,
        )
        for arguments in (
            ["--max-concurrent-tasks", "0"],
            ["--max-queued-tasks", "-1"],
            ["--queue-wait-seconds", "0"],
            ["--queue-wait-seconds", "nan"],
            ["--queue-wait-seconds", "inf"],
        ):
            with self.assertRaises(SystemExit):
                build_argument_parser().parse_args(arguments)

    def test_production_builder_rejects_queue_settings_that_disable_protection(self):
        with self.assertRaisesRegex(ValueError, "max_concurrent_tasks"):
            build_app(Path("unused"), max_concurrent_tasks=0)
        with self.assertRaisesRegex(ValueError, "max_queued_tasks"):
            build_app(Path("unused"), max_queued_tasks=-1)
        with self.assertRaisesRegex(ValueError, "queue_wait_seconds"):
            build_app(Path("unused"), queue_wait_seconds=0)
        for invalid in (float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "queue_wait_seconds"):
                build_app(Path("unused"), queue_wait_seconds=invalid)

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
                enable_a3_text_orientation=False,
            )

            self.assertIsNotNone(app)

    def test_production_auto_validates_all_units_before_selection(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8790.build_a3_runtime"
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8790.build_a3_page_orienter",
            return_value="orienter",
        ):
            build_runtime.return_value = object()

            app = build_app(
                Path(temp) / "runtime",
                enable_triage=False,
                max_concurrent_tasks=3,
                max_queued_tasks=4,
                queue_wait_seconds=66,
            )

            self.assertIsNotNone(app)
            self.assertEqual(
                app.state.trace_event_recorder.store.path.resolve(),
                (Path(temp) / "runtime" / "trace_events.sqlite3").resolve(),
            )
            self.assertTrue(build_runtime.call_args.kwargs["auto_prepare_all_units"])
            self.assertTrue(build_runtime.call_args.kwargs["enable_a3_intent_v1"])
            self.assertTrue(
                build_runtime.call_args.kwargs["enable_a3_intent_model_fallback"]
            )
            self.assertTrue(
                build_runtime.call_args.kwargs["enable_author_contact_fallback"]
            )
            self.assertTrue(
                build_runtime.call_args.kwargs["enable_three_scope_cancel_clarification"]
            )
            self.assertTrue(
                build_runtime.call_args.kwargs["preserve_a2_artifacts_on_cancel"]
            )
            self.assertEqual(
                build_runtime.call_args.kwargs["a3_page_orienter"],
                "orienter",
            )
            self.assertFalse(
                build_runtime.call_args.kwargs["orient_before_routing"]
            )
            self.assertEqual(build_runtime.call_args.kwargs["max_concurrent_tasks"], 3)
            self.assertEqual(build_runtime.call_args.kwargs["max_queued_tasks"], 4)
            self.assertEqual(build_runtime.call_args.kwargs["queue_wait_seconds"], 66)

    def test_production_orientation_can_be_disabled_without_loading_ocr(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8790.build_a3_runtime",
            return_value=object(),
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8790.build_a3_page_orienter"
        ) as build_orienter:
            app = build_app(
                Path(temp) / "runtime",
                enable_triage=False,
                enable_a3_text_orientation=False,
            )

            self.assertIsNotNone(app)
            build_orienter.assert_not_called()
            self.assertIsNone(
                build_runtime.call_args.kwargs["a3_page_orienter"]
            )


if __name__ == "__main__":
    unittest.main()

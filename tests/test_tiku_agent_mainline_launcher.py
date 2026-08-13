from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from scripts.run_tiku_agent_demo import build_app, build_argument_parser, build_runtime
from tiku_agent.external_load_screen import ZhipuExternalLoadScreen
from tiku_admin.control_store import SQLiteControlStore


class MainlineLauncherTest(unittest.TestCase):
    def test_safe_answers_are_enabled_by_default_with_explicit_rollback(self):
        parser = build_argument_parser()

        self.assertTrue(parser.parse_args([]).enable_safe_answer_v0)
        self.assertFalse(
            parser.parse_args(["--disable-safe-answer-v0"]).enable_safe_answer_v0
        )

    def test_dimension_filter_is_enabled_by_default_with_explicit_rollback(self):
        parser = build_argument_parser()

        self.assertTrue(parser.parse_args([]).enable_dimension_filter)
        self.assertFalse(
            parser.parse_args(["--disable-dimension-filter"]).enable_dimension_filter
        )

    def test_external_load_screen_is_enabled_by_default_with_explicit_rollback(self):
        parser = build_argument_parser()

        defaults = parser.parse_args([])
        self.assertTrue(defaults.enable_external_load_screen)
        self.assertEqual(defaults.external_load_timeout_seconds, 15.0)
        self.assertFalse(
            parser.parse_args(
                ["--disable-external-load-screen"]
            ).enable_external_load_screen
        )

    def test_public_beta_guards_are_explicit_and_configurable(self):
        parser = build_argument_parser()
        defaults = parser.parse_args([])
        self.assertEqual(defaults.max_concurrent_tasks, 0)
        self.assertEqual(defaults.max_queued_tasks, 0)
        self.assertIsNone(defaults.daily_budget_cny)
        self.assertIsNone(defaults.per_invite_daily_budget_cny)
        self.assertIsNone(defaults.invite_config)
        self.assertIsNone(defaults.control_db)

        guarded = parser.parse_args([
            "--max-concurrent-tasks", "1",
            "--max-queued-tasks", "2",
            "--queue-wait-seconds", "55",
            "--daily-budget-cny", "5.0",
            "--per-invite-daily-budget-cny", "3.0",
            "--invite-config", "invites.json",
        ])
        self.assertEqual(guarded.max_concurrent_tasks, 1)
        self.assertEqual(guarded.max_queued_tasks, 2)
        self.assertEqual(guarded.queue_wait_seconds, 55)
        self.assertEqual(guarded.daily_budget_cny, 5.0)
        self.assertEqual(guarded.per_invite_daily_budget_cny, 3.0)
        self.assertEqual(guarded.invite_config, Path("invites.json"))

        dynamic = parser.parse_args(["--control-db", "admin/control.sqlite3"])
        self.assertEqual(dynamic.control_db, Path("admin/control.sqlite3"))

    def test_dynamic_control_database_must_exist_and_replaces_static_guards(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8790_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        control_path = root / "admin" / "control.sqlite3"

        with self.assertRaisesRegex(ValueError, "control database not found"):
            build_app(root / "runtime", control_db=control_path)

        SQLiteControlStore(control_path)
        with self.assertRaisesRegex(ValueError, "either control_db or invite_config"):
            build_app(
                root / "runtime",
                control_db=control_path,
                invite_config=root / "invites.json",
            )
        with self.assertRaisesRegex(ValueError, "omit static budget arguments"):
            build_app(root / "runtime", control_db=control_path, daily_budget_cny=30)

    def test_enabled_runtime_uses_model_only_for_safe_conversation(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8790_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        requests = []

        def model_client(request):
            requests.append(request)
            return "我是力答，专注结构力学题库搜索，通过题图检索相似候选题。"

        runtime = build_runtime(root, safe_answer_model_client=model_client)
        safe_response = runtime.handle_text("safe-session", "你是谁")
        business_response = runtime.handle_text("business-session", "帮我搜个题")

        self.assertEqual(safe_response.intent, "safe_answer")
        self.assertEqual(safe_response.reply_source, "model")
        self.assertEqual(len(requests), 1)
        self.assertNotEqual(business_response.intent, "safe_answer")
        self.assertEqual(runtime.session_snapshot("safe-session")["phase"], "IDLE")

    def test_mainline_enables_external_load_screen_with_injectable_rollback(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8790_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))

        runtime = build_runtime(root)
        disabled = build_runtime(
            root / "disabled", enable_external_load_screen=False
        )
        screen = lambda _path: "yes"
        injected = build_runtime(root / "injected", external_load_screen=screen)

        self.assertIsInstance(runtime.external_load_screen, ZhipuExternalLoadScreen)
        self.assertEqual(runtime.external_load_timeout_seconds, 15.0)
        self.assertIsNone(disabled.external_load_screen)
        self.assertIs(injected.external_load_screen, screen)


if __name__ == "__main__":
    unittest.main()

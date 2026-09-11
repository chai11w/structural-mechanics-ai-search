from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8790 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    _capacity_from_args,
    build_app,
    build_argument_parser,
)
from tiku_admin.control_store import SQLiteControlStore


EVIDENCE_CLI_ARGUMENTS = [
    "--max-checkpoint-rows", "1000",
    "--max-artifact-rows", "2000",
    "--max-audit-rows", "3000",
    "--max-trace-rows", "4000",
    "--max-artifact-bytes", "5000000",
    "--min-free-bytes", "6000000",
    "--max-artifacts-per-checkpoint", "7",
    "--checkpoint-retention-backup-root", r"F:\backups\tiku-evidence",
    "--checkpoint-retention-interval-seconds", "3600",
    "--checkpoint-retention-backup-keep-runs", "14",
]


class TikuAgent8790A3V1Test(unittest.TestCase):
    def test_launcher_defaults_to_production_port_and_a3_v1(self):
        with self.assertRaises(SystemExit):
            build_argument_parser().parse_args([])
        defaults = build_argument_parser().parse_args(EVIDENCE_CLI_ARGUMENTS)

        self.assertEqual(DEFAULT_PORT, 8790)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_prod_8790")
        self.assertTrue(defaults.enable_triage)
        self.assertTrue(defaults.enable_auto_crop)
        self.assertTrue(defaults.enable_output_watchdog)
        self.assertFalse(defaults.enable_a3_text_orientation)
        self.assertFalse(defaults.enable_durable_execution)
        self.assertTrue(build_argument_parser().parse_args(
            [*EVIDENCE_CLI_ARGUMENTS, "--enable-durable-execution"]).enable_durable_execution)
        self.assertEqual(defaults.max_concurrent_tasks, 1)
        self.assertEqual(defaults.max_queued_tasks, 2)
        self.assertEqual(defaults.queue_wait_seconds, 55.0)
        self.assertEqual(
            _capacity_from_args(defaults).to_dict(),
            {
                "max_checkpoint_rows": 1000,
                "max_artifact_rows": 2000,
                "max_audit_rows": 3000,
                "max_trace_rows": 4000,
                "max_artifact_bytes": 5000000,
                "min_free_bytes": 6000000,
                "max_artifacts_per_checkpoint": 7,
            },
        )
        self.assertFalse(
            build_argument_parser().parse_args(
                [*EVIDENCE_CLI_ARGUMENTS, "--disable-output-watchdog"]
            ).enable_output_watchdog
        )
        self.assertFalse(
            build_argument_parser()
            .parse_args([*EVIDENCE_CLI_ARGUMENTS, "--disable-a3-text-orientation"])
            .enable_a3_text_orientation
        )
        self.assertTrue(
            build_argument_parser()
            .parse_args([*EVIDENCE_CLI_ARGUMENTS, "--enable-a3-text-orientation"])
            .enable_a3_text_orientation
        )

        custom = build_argument_parser().parse_args([*EVIDENCE_CLI_ARGUMENTS,
            "--max-concurrent-tasks", "3",
            "--max-queued-tasks", "4",
            "--queue-wait-seconds", "66",
        ])
        self.assertEqual(custom.max_concurrent_tasks, 3)
        self.assertEqual(custom.max_queued_tasks, 4)
        self.assertEqual(custom.queue_wait_seconds, 66.0)

        self.assertEqual(
            build_argument_parser().parse_args(
                [*EVIDENCE_CLI_ARGUMENTS, "--max-queued-tasks", "0"]
            ).max_queued_tasks,
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
                build_argument_parser().parse_args(
                    [*EVIDENCE_CLI_ARGUMENTS, *arguments]
                )
        with self.assertRaises(SystemExit):
            build_argument_parser().parse_args(
                [
                    *EVIDENCE_CLI_ARGUMENTS[:-8],
                    "--max-artifacts-per-checkpoint", "51",
                    *EVIDENCE_CLI_ARGUMENTS[-6:],
                ]
            )

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

    def test_phase5_standard_production_graph_supports_reset_without_model_calls(self):
        import json
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.urlopen") as provider:
            app = build_app(temp, enable_durable_execution=True)
            with TestClient(app) as client:
                context = client.get("/api/session").json()["execution"]
                self.assertEqual(client.post("/api/reset", json={}).status_code, 409)
                operation = {"key":"production-reset-fixture", "epoch":context["epoch"],
                             "state_version":context["state_version"]}
                headers = {"X-Tiku-Operation":json.dumps(operation)}
                first = client.post("/api/reset", headers=headers, json={})
                second = client.post("/api/reset", headers=headers, json={})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["execution"], second.json()["execution"])
                self.assertNotEqual(context["epoch"], first.json()["execution"]["epoch"])
            provider.assert_not_called()
            with self.assertRaisesRegex(ValueError, "enable-durable-execution"):
                build_app(temp)

    def test_expired_conversation_reset_needs_one_context_read_first(self):
        """A reloaded page has no execution context yet; reset cannot lead."""
        import json
        import time
        from uuid import uuid4
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.urlopen") as provider:
            app = build_app(temp, enable_durable_execution=True)
            with TestClient(app) as client:
                # The reloaded page used to send exactly this and was rejected.
                blind = client.post(
                    "/api/reset",
                    json={},
                    headers={
                        "X-Session-Coordination-Version": "6",
                        "X-Session-Request-Fence": f"{int(time.time() * 1000)}:{uuid4().hex}",
                    },
                )
                self.assertEqual(blind.status_code, 409)
                self.assertEqual(blind.json()["code"], "EXECUTION_CONTEXT_REQUIRED")
                # One bounded authoritative read supplies the context, and the
                # reset that follows it succeeds on the first attempt.
                context = client.get("/api/session").json()["execution"]
                operation = {
                    "key": f"{int(time.time() * 1000)}:{uuid4().hex}",
                    "epoch": context["epoch"],
                    "state_version": context["state_version"],
                }
                reset = client.post(
                    "/api/reset",
                    json={},
                    headers={"X-Tiku-Operation": json.dumps(operation)},
                )
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(reset.json()["code"], "SESSION_RESET")
            provider.assert_not_called()

    def test_conversation_lifetime_has_one_source_and_reaches_the_browser(self):
        """Client countdown, sessions, fences and executions share one number."""
        import json
        import time
        from uuid import uuid4
        from fastapi.testclient import TestClient
        from tiku_agent import fastapi_demo
        from tiku_agent.conversation_ttl import CONVERSATION_TTL_SECONDS
        from tiku_agent.execution_store import ExecutionPolicy
        from tiku_agent.session_store import DEFAULT_SESSION_TTL

        self.assertEqual(DEFAULT_SESSION_TTL.total_seconds(), CONVERSATION_TTL_SECONDS)
        self.assertEqual(
            fastapi_demo._SESSION_COORDINATION_RETENTION_SECONDS,
            CONVERSATION_TTL_SECONDS,
        )
        self.assertEqual(ExecutionPolicy().session_ttl, CONVERSATION_TTL_SECONDS)

        with tempfile.TemporaryDirectory() as temp, patch("urllib.request.urlopen") as provider:
            app = build_app(temp, enable_durable_execution=True)
            with TestClient(app) as client:
                session = client.get("/api/session").json()
                self.assertEqual(
                    session["conversation_ttl_seconds"], CONVERSATION_TTL_SECONDS
                )
                operation = {
                    "key": f"{int(time.time() * 1000)}:{uuid4().hex}",
                    "epoch": session["execution"]["epoch"],
                    "state_version": session["execution"]["state_version"],
                }
                reset = client.post(
                    "/api/reset",
                    json={},
                    headers={"X-Tiku-Operation": json.dumps(operation)},
                )
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(
                    reset.json()["conversation_ttl_seconds"], CONVERSATION_TTL_SECONDS
                )
            provider.assert_not_called()

    def test_phase5_keeps_production_invite_authentication(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control.sqlite3"
            SQLiteControlStore(control)
            with TestClient(build_app(root / "runtime", control_db=control,
                                      enable_durable_execution=True)) as client:
                self.assertEqual(client.get("/api/session").status_code, 401)
                self.assertEqual(client.get("/api/execution").status_code, 401)

    def test_phase5_does_not_implicitly_import_legacy_sessions(self):
        from tiku_agent.session_store import SQLiteSessionStore
        from tiku_agent.state import AgentState
        from tiku_agent.execution_store import ExecutionError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = SQLiteSessionStore(root / "a2" / "session.db")
            legacy.save(AgentState(session_id="legacy"))
            with self.assertRaises(ExecutionError) as error:
                build_app(root, enable_durable_execution=True)
            self.assertEqual(error.exception.code, "EXECUTION_MIGRATION_REQUIRED")
            self.assertIsNotNone(legacy.load("legacy"))

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
                enable_a3_text_orientation=True,
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
            self.assertTrue(
                build_runtime.call_args.kwargs["orient_before_routing"]
            )
            self.assertEqual(build_runtime.call_args.kwargs["max_concurrent_tasks"], 3)
            self.assertEqual(build_runtime.call_args.kwargs["max_queued_tasks"], 4)
            self.assertEqual(build_runtime.call_args.kwargs["queue_wait_seconds"], 66)

    def test_production_orientation_is_disabled_by_default_without_loading_ocr(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8790.build_a3_runtime",
            return_value=object(),
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8790.build_a3_page_orienter"
        ) as build_orienter:
            app = build_app(
                Path(temp) / "runtime",
                enable_triage=False,
            )

            self.assertIsNotNone(app)
            build_orienter.assert_not_called()
            self.assertIsNone(
                build_runtime.call_args.kwargs["a3_page_orienter"]
            )

    def test_existing_trace_database_is_migrated_during_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            runtime.mkdir()
            (runtime / "trace_events.sqlite3").touch()
            with patch(
                "scripts.run_tiku_agent_8790.build_a3_runtime",
                return_value=object(),
            ), patch(
                "scripts.run_tiku_agent_8790.SQLiteTraceEventStore.ensure_store_identity"
            ) as ensure_identity:
                app = build_app(runtime, enable_triage=False)

            self.assertIsNotNone(app)
            ensure_identity.assert_called_once()


if __name__ == "__main__":
    unittest.main()

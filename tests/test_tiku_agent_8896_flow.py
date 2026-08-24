from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8896 import (
    A2_RERANK_POLICY,
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_argument_parser,
    build_runtime,
)
from scripts.run_tiku_agent_demo import build_runtime as build_a2_runtime
from tiku_agent.a3_auto_crop import GlmA3AutoCropper
from tiku_agent.external_load_screen import QwenExternalLoadScreen
from tiku_agent.state import AgentState


class TikuAgent8896FlowTest(unittest.TestCase):
    def test_launcher_enables_full_triage_by_default(self):
        defaults = build_argument_parser().parse_args([])

        self.assertEqual(DEFAULT_PORT, 8896)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_mvp_8896")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8896_session")
        self.assertTrue(build_argument_parser().parse_args([]).enable_output_watchdog)
        self.assertFalse(
            build_argument_parser().parse_args(["--disable-output-watchdog"]).enable_output_watchdog
        )
        self.assertTrue(defaults.enable_triage)
        self.assertTrue(defaults.enable_auto_crop)
        self.assertTrue(defaults.enable_a3_intent_v1)

    def test_auto_crop_is_promoted_by_default_with_manual_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            promoted = build_runtime(root / "promoted", enable_triage=False)
            rolled_back = build_runtime(
                root / "rolled-back",
                enable_triage=False,
                enable_auto_crop=False,
            )

            self.assertIsInstance(promoted.auto_cropper, GlmA3AutoCropper)
            self.assertTrue(promoted.auto_prepare_all_units)
            self.assertTrue(
                promoted.session_snapshot("empty")["a3"]["auto_prepare_all_enabled"]
            )
            self.assertFalse(
                rolled_back.session_snapshot("empty")["a3"]["auto_prepare_all_enabled"]
            )
            self.assertIsNone(rolled_back.auto_cropper)

    def test_a3_intent_v1_is_enabled_only_when_requested(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            enabled = build_runtime(root / "enabled", enable_triage=False)
            disabled = build_runtime(
                root / "disabled",
                enable_triage=False,
                enable_a3_intent_v1=False,
            )

            self.assertIsNotNone(enabled.intent_engine)
            self.assertIsNone(disabled.intent_engine)

    def test_a3_intent_can_run_rules_without_model_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = build_runtime(
                Path(temp),
                enable_triage=False,
                enable_a3_intent_model_fallback=False,
            )

            self.assertIsNotNone(runtime.intent_engine)
            self.assertIsNone(runtime.intent_engine.model_client)

    def test_a3_and_child_a2_share_the_production_cost_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = build_runtime(root, enable_triage=False)

            self.assertIs(runtime.cost_ledger, runtime.a2_runtime.cost_ledger)
            self.assertTrue(runtime.a2_runtime.preserve_artifacts_on_cancel)
            self.assertEqual(
                runtime.cost_ledger.path.resolve(),
                (root / "model_costs.sqlite3").resolve(),
            )

    def test_launcher_can_disable_or_inject_the_triage_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            disabled = build_runtime(root / "disabled", enable_triage=False)
            authority = object()
            injected = build_runtime(
                root / "injected",
                image_triage_authority=authority,
            )

            self.assertIsNone(disabled.image_triage_authority)
            self.assertIs(injected.image_triage_authority, authority)

    def test_only_8896_enables_chapter_scope_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_8896 = build_runtime(root / "8896", enable_triage=False)
            runtime_8790 = build_a2_runtime(
                root / "8790",
                enable_external_load_screen=False,
            )

            agent_8896 = runtime_8896.a2_runtime.agent_factory(AgentState())
            agent_8790 = runtime_8790.agent_factory(AgentState())

            self.assertTrue(agent_8896.enable_chapter_scope_fallback)
            self.assertFalse(agent_8790.enable_chapter_scope_fallback)
            self.assertIsInstance(runtime_8896.external_load_screen, QwenExternalLoadScreen)

    def test_only_8896_enables_author_contact_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_8896 = build_runtime(root / "8896", enable_triage=False)
            runtime_8790 = build_a2_runtime(
                root / "8790",
                enable_external_load_screen=False,
            )

            agent_8896 = runtime_8896.a2_runtime.agent_factory(AgentState())
            agent_8790 = runtime_8790.agent_factory(AgentState())

            self.assertTrue(agent_8896.enable_author_contact_fallback)
            self.assertFalse(agent_8790.enable_author_contact_fallback)

    def test_8896_enables_three_scope_cancel_clarification(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = build_runtime(Path(temp), enable_triage=False)

            self.assertTrue(runtime.enable_three_scope_cancel_clarification)

    def test_8896_passes_qwen_policy_with_tool_parameter_names(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_demo.rerank_candidates_tool"
        ) as rerank_tool:
            rerank_tool.return_value = object()
            runtime = build_runtime(Path(temp), enable_triage=False)
            agent = runtime.a2_runtime.agent_factory(AgentState())
            candidates = [{"rank": 1, "path": "candidate.jpg", "score": 1.0}]

            agent.tools.rerank_candidates(
                "query.jpg",
                candidates,
                route="symbolic",
            )

            rerank_tool.assert_called_once_with(
                "query.jpg",
                candidates,
                route="symbolic",
                **A2_RERANK_POLICY,
            )
            self.assertEqual(A2_RERANK_POLICY["rerank_provider"], "qwen")
            self.assertEqual(A2_RERANK_POLICY["rerank_model"], "qwen3.7-plus")
            self.assertEqual(A2_RERANK_POLICY["max_workers"], 10)
            self.assertTrue(A2_RERANK_POLICY["display_by_rerank_score"])
            self.assertEqual(A2_RERANK_POLICY["display_all_score"], 0.95)
            self.assertEqual(A2_RERANK_POLICY["display_fallback_top_n"], 3)


if __name__ == "__main__":
    unittest.main()

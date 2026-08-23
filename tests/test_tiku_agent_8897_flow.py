from pathlib import Path
import tempfile
import unittest

from scripts.run_tiku_agent_8896 import build_runtime as build_8896_runtime
from scripts.run_tiku_agent_8897 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    TRIAGE_PROMPT_PATH,
    TRIAGE_POLICIES,
    TRIAGE_POLICY_VERSION,
    build_argument_parser,
    build_runtime,
)
from tiku_agent.a3_auto_crop import GlmA3AutoCropper
from tiku_agent.image_triage import DEFAULT_PROMPT_PATH
from tiku_agent.image_triage_8897 import (
    finalize_route_8897,
    finalize_route_8897_v2,
    observation_from_model_text_8897,
    observation_from_model_text_8897_v2,
)


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
        self.assertEqual(defaults.triage_policy_version, "v3")
        self.assertEqual(TRIAGE_POLICY_VERSION, "v3")
        self.assertEqual(set(TRIAGE_POLICIES), {"v1", "v2", "v3"})

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

    def test_8897_uses_boundary_prompt_without_changing_8896_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_8896 = build_8896_runtime(root / "8896")
            runtime_8897 = build_runtime(root / "8897")

            self.assertEqual(
                runtime_8896.image_triage_authority.observer.prompt_path,
                DEFAULT_PROMPT_PATH,
            )
            self.assertEqual(
                runtime_8897.image_triage_authority.observer.prompt_path,
                TRIAGE_PROMPT_PATH,
            )

    def test_8897_boundary_policy_keeps_routes_narrow(self):
        def observe(summary: str):
            return observation_from_model_text_8897(summary)

        self.assertEqual(
            finalize_route_8897(
                observe(
                    "建议路线：A1\n题目数量：2\n原结构图数量：2\n"
                    "辅助图数量：0\n真实外荷载：没有\n图片完整性：完整\n"
                    "结构力学内容：有\n题图边界：不清楚"
                )
            ),
            "A3",
        )
        self.assertEqual(
            finalize_route_8897(
                observe(
                    "建议路线：A1\n题目数量：1\n原结构图数量：1\n"
                    "辅助图数量：0\n真实外荷载：没有\n图片完整性：残缺\n"
                    "结构力学内容：有\n题图边界：不清楚"
                )
            ),
            "A3",
        )
        self.assertEqual(
            finalize_route_8897(
                observe(
                    "建议路线：A2\n题目数量：1\n原结构图数量：1\n"
                    "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
                    "结构力学内容：有\n题图边界：清楚"
                )
            ),
            "A2",
        )

    def test_8897_separates_complete_unit_from_adjacent_fragment(self):
        truncated_only = observation_from_model_text_8897(
            "建议路线：A3\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：残缺\n"
            "结构力学内容：有\n题图边界：不清楚"
        )
        complete_with_fragment = observation_from_model_text_8897(
            "建议路线：A3\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：不清楚\n"
            "主体题完整，但底部有下一题残片。"
        )

        self.assertEqual(finalize_route_8897(truncated_only), "A3")
        self.assertEqual(finalize_route_8897(complete_with_fragment), "A3")

    def test_8897_uses_explanation_evidence_to_reject_contradictory_a2(self):
        observation = observation_from_model_text_8897(
            "建议路线：A2\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：清楚\n"
            "图片底部边缘有其他图形的顶部，明显属于下一题。"
        )

        self.assertFalse(observation.image_boundary_clear)
        self.assertEqual(finalize_route_8897(observation), "A3")

    def test_8897_does_not_treat_negated_contamination_as_evidence(self):
        observation = observation_from_model_text_8897(
            "建议路线：A2\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：清楚\n"
            "边缘没有混入相邻题目的题号或结构残片。"
        )

        self.assertTrue(observation.image_boundary_clear)
        self.assertEqual(finalize_route_8897(observation), "A2")

    def test_8897_v3_allows_primary_truncation_to_fall_back_to_a3(self):
        observation = observation_from_model_text_8897(
            "建议路线：A3\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：不清楚\n"
            "最右侧的下弦节点、支座和荷载箭头只露出一部分，延伸到了图片之外。"
        )

        self.assertTrue(observation.image_recoverable)
        self.assertEqual(finalize_route_8897(observation), "A3")

    def test_8897_v2_preserves_strict_primary_truncation_behavior(self):
        observation = observation_from_model_text_8897_v2(
            "建议路线：A3\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：不清楚\n"
            "最右侧的下弦节点、支座和荷载箭头只露出一部分，延伸到了图片之外。"
        )

        self.assertFalse(observation.image_recoverable)
        self.assertEqual(finalize_route_8897_v2(observation), "A1")

    def test_8897_keeps_complete_main_unit_when_only_next_question_is_truncated(self):
        observation = observation_from_model_text_8897(
            "建议路线：A3\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：不清楚\n"
            "底部露出下一题的支座和荷载箭头残片，但主体题完整。"
        )

        self.assertTrue(observation.image_recoverable)
        self.assertEqual(finalize_route_8897(observation), "A3")

    def test_8897_multi_unit_page_stays_a3_when_some_units_are_truncated(self):
        observation = observation_from_model_text_8897(
            "建议路线：A3\n题目数量：1\n原结构图数量：5\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：不清楚\n"
            "子图(b)的杆件被截断，但页面内仍有三个完整子图。"
        )

        self.assertEqual(finalize_route_8897(observation), "A3")

    def test_8897_does_not_treat_current_stem_crop_as_structure_truncation(self):
        observation = observation_from_model_text_8897(
            "建议路线：A2\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：清楚\n"
            "题干文字中的荷载作用点描述在右侧被截断，但结构图完整。"
        )

        self.assertTrue(observation.image_recoverable)
        self.assertEqual(finalize_route_8897(observation), "A2")

    def test_8897_does_not_treat_quoted_policy_as_observed_truncation(self):
        observation = observation_from_model_text_8897(
            "建议路线：A2\n题目数量：1\n原结构图数量：1\n"
            "辅助图数量：0\n真实外荷载：明确\n图片完整性：完整\n"
            "结构力学内容：有\n题图边界：清楚\n"
            "根据规则，若杆件、荷载箭头或支座延伸到图片外，则结构被截断。"
        )

        self.assertTrue(observation.image_recoverable)
        self.assertEqual(finalize_route_8897(observation), "A2")

    def test_watchdog_is_scoped_to_8897_state_and_launcher(self):
        self.assertIn("[int]$Port = 8897", self.watchdog)
        self.assertIn('.tmp_tiku_agent_a3_v1_8897', self.watchdog)
        self.assertIn('scripts\\run_tiku_agent_8897.py', self.watchdog)
        self.assertIn('watchdog_8897.status', self.watchdog)
        self.assertIn('[string]$TriagePolicyVersion = "v3"', self.watchdog)
        self.assertIn('"--triage-policy-version", "$TriagePolicyVersion"', self.watchdog)
        self.assertNotIn('run_tiku_agent_8896.py', self.watchdog)


if __name__ == "__main__":
    unittest.main()

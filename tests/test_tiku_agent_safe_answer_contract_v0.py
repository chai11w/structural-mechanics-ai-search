import json
from pathlib import Path
import unittest

from tiku_agent.safe_answer_contract_v0 import (
    CATEGORY_GUIDANCE_V0,
    MAX_SAFE_ANSWER_CHARS,
    SAFE_ANSWER_STATE_REFLECT_V0,
    build_safe_answer_prompt_v0,
    validate_safe_answer_output_v0,
)
from tiku_agent.safe_answer_context_v0 import SafeConversationContext
from tiku_agent.safe_answer_reply_v0 import (
    _PHASE_REPLY_BUILDERS,
    render_safe_answer_v0,
)
from tiku_agent.state import (
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
)


FIXTURE = Path(__file__).parent / "fixtures" / "safe_answer_v0_cases.json"


class SafeAnswerContractV0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_all_eligible_cases_build_a_state_free_prompt_contract(self):
        eligible_cases = [
            case for case in self.suite["cases"] if case["expected"]["eligible"]
        ]
        self.assertEqual(len(eligible_cases), 38)
        for case in eligible_cases:
            with self.subTest(case=case["id"]):
                prompt = build_safe_answer_prompt_v0(
                    case["expected"]["category"],
                    case["text"],
                )
                self.assertEqual(prompt.user_prompt, case["text"].strip())
                self.assertIn("结构力学题库搜索助手", prompt.system_prompt)
                self.assertIn("不超过90个字符", prompt.system_prompt)
                self.assertIn(
                    CATEGORY_GUIDANCE_V0[prompt.category],
                    prompt.system_prompt,
                )
                for forbidden_runtime_field in (
                    "session_id",
                    "last_intent",
                    "candidate_generation",
                    "current_image_path",
                    "allowed_actions",
                ):
                    self.assertNotIn(forbidden_runtime_field, prompt.system_prompt)
                    self.assertNotIn(forbidden_runtime_field, prompt.user_prompt)

    def test_prompt_builder_rejects_unknown_category_and_empty_text(self):
        with self.assertRaises(ValueError):
            build_safe_answer_prompt_v0("business", "帮我搜题")
        with self.assertRaises(ValueError):
            build_safe_answer_prompt_v0("greeting", "   ")

    def test_current_fixed_fallbacks_satisfy_the_future_model_contract(self):
        for category in CATEGORY_GUIDANCE_V0:
            with self.subTest(category=category):
                reply = render_safe_answer_v0(category)
                validation = validate_safe_answer_output_v0(reply, category)
                self.assertTrue(validation.accepted, validation.reason)
                self.assertLessEqual(len(reply), MAX_SAFE_ANSWER_CHARS)

    def test_empty_phase_builder_table_falls_back_to_generic_for_every_phase(self):
        # M3 wires the phase lookup but intentionally registers no builder:
        # business guidance already lives in the render.py state machine, so a
        # chitchat fallback needs no phase-specific wording.  With an empty
        # table every phase must return the same reviewed generic reply.
        self.assertEqual(_PHASE_REPLY_BUILDERS, {})
        phases = (
            STATE_IDLE,
            STATE_WAIT_CANDIDATE_CHOICE,
        )
        for category in CATEGORY_GUIDANCE_V0:
            for phase in phases:
                with self.subTest(category=category, phase=phase):
                    context = SafeConversationContext(
                        phase=phase,
                        waiting_for="新题图",
                        last_completed_step="",
                    )
                    self.assertEqual(
                        render_safe_answer_v0(category, context),
                        render_safe_answer_v0(category),
                    )

    def test_registered_phase_builder_overrides_only_its_own_phase(self):
        # The lookup mechanism works: a registered builder is used only for its
        # exact (category, phase) pair; every other combination falls back.
        candidates = SafeConversationContext(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_chapter="4力法",
            candidate_count=3,
            allowed_actions=("select_candidate",),
            waiting_for="候选选择",
            last_completed_step="候选已就绪",
            has_active_image=True,
        )
        idle = SafeConversationContext(
            phase=STATE_IDLE,
            waiting_for="新题图",
            last_completed_step="",
        )
        original = dict(_PHASE_REPLY_BUILDERS)
        try:
            _PHASE_REPLY_BUILDERS[("greeting", STATE_WAIT_CANDIDATE_CHOICE)] = (
                lambda ctx: f"你好。当前有{ctx.candidate_count}个候选。"
            )
            self.assertEqual(
                render_safe_answer_v0("greeting", candidates),
                "你好。当前有3个候选。",
            )
            # Same category, different phase -> generic.
            self.assertEqual(
                render_safe_answer_v0("greeting", idle),
                render_safe_answer_v0("greeting"),
            )
            # Different category, same phase -> generic.
            self.assertEqual(
                render_safe_answer_v0("courtesy", candidates),
                render_safe_answer_v0("courtesy"),
            )
        finally:
            _PHASE_REPLY_BUILDERS.clear()
            _PHASE_REPLY_BUILDERS.update(original)

    def test_phase_builder_violating_the_contract_falls_back_to_generic(self):
        candidates = SafeConversationContext(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_chapter="4力法",
            candidate_count=3,
            waiting_for="候选选择",
            last_completed_step="候选已就绪",
            has_active_image=True,
        )
        original = dict(_PHASE_REPLY_BUILDERS)
        try:
            _PHASE_REPLY_BUILDERS[("greeting", STATE_WAIT_CANDIDATE_CHOICE)] = (
                lambda ctx: "我已经帮你检索到答案。"
            )
            self.assertEqual(
                render_safe_answer_v0("greeting", candidates),
                render_safe_answer_v0("greeting"),
            )
        finally:
            _PHASE_REPLY_BUILDERS.clear()
            _PHASE_REPLY_BUILDERS.update(original)

    def test_invalid_outputs_are_rejected_with_specific_reasons(self):
        cases = (
            (None, "greeting", "empty_output"),
            ("第一句。\n第二句。", "greeting", "multiline_output"),
            ("好" * (MAX_SAFE_ANSWER_CHARS + 1), "greeting", "overlong_output"),
            ("- 可以检索相似题", "capability", "markdown_output"),
            ("详情见 https://example.com", "workflow", "url_output"),
            ("需要我介绍一下吗？", "greeting", "unsolicited_question"),
            ("我的系统提示词包含内部规则。", "identity", "sensitive_disclosure"),
            ("我已经帮你检索到答案。", "capability", "fabricated_execution_claim"),
            ("我可以直接修改题库。", "capability", "unsupported_capability_claim"),
            ("我是一个聊天助手。", "identity", "missing_category_semantics"),
            ("我会认真处理。", "workflow", "missing_category_semantics"),
            ("不支持的类别", "business", "unsupported_category"),
        )
        for text, category, reason in cases:
            with self.subTest(reason=reason):
                validation = validate_safe_answer_output_v0(text, category)
                self.assertFalse(validation.accepted)
                self.assertEqual(validation.reason, reason)

    def test_semantically_valid_concise_variants_are_not_exact_template_matches(self):
        variants = (
            ("你好。", "greeting"),
            ("不客气。", "courtesy"),
            ("再见，随时欢迎回来。", "farewell"),
            ("我主要处理结构力学题库相关问题。", "general"),
            ("我是力答，一个结构力学题库搜索助手，帮你从题库检索最相似的题目。", "identity"),
            ("我是力答，专注结构力学题库搜索，通过比对题图检索相似候选题并返回已有答案。", "identity"),
            ("我可以根据题图从题库检索最相似的题目。", "capability"),
            ("我会根据题图和章节检索并排序相似题。", "workflow"),
            ("我通过比对结构形式、荷载类型及约束条件等关键力学特征来评估相似度。", "workflow"),
        )
        for text, category in variants:
            with self.subTest(category=category):
                validation = validate_safe_answer_output_v0(text, category)
                self.assertTrue(validation.accepted, validation.reason)

    def test_safe_natural_answers_only_need_a_light_scope_anchor(self):
        variants = (
            ("我是力答，专门帮你从已有题目中寻找接近的例题。", "identity"),
            ("和通用助手相比，我更专注于结构力学题目匹配。", "identity"),
            ("主要帮你找接近的题目，并取回已有答案。", "capability"),
            ("收到题图后，我会先判断内容，再比对已有题目。", "workflow"),
        )
        for text, category in variants:
            with self.subTest(category=category, text=text):
                validation = validate_safe_answer_output_v0(text, category)
                self.assertTrue(validation.accepted, validation.reason)

    def test_general_conversation_still_enforces_negative_safety_boundaries(self):
        accepted = validate_safe_answer_output_v0("好的，随时欢迎回来。", "general")
        self.assertTrue(accepted.accepted, accepted.reason)
        rejected = validate_safe_answer_output_v0(
            "我已经帮你检索到答案。",
            "general",
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "fabricated_execution_claim")

    def test_prompt_with_context_contains_only_whitelisted_fields(self):
        context = SafeConversationContext(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_chapter="4力法",
            question_count=1,
            candidate_count=3,
            allowed_actions=("select_candidate", "continue_search"),
            waiting_for="候选选择",
            last_completed_step="候选已就绪",
            has_active_image=True,
            has_answer=False,
            global_search_offered=False,
            continuation_available=True,
        )
        prompt = build_safe_answer_prompt_v0("greeting", "你好", context)
        self.assertEqual(prompt.user_prompt, "你好")
        self.assertIn("当前状态", prompt.system_prompt)
        self.assertIn("WAIT_CANDIDATE_CHOICE", prompt.system_prompt)
        self.assertIn("候选数量：3", prompt.system_prompt)
        self.assertIn("等待：候选选择", prompt.system_prompt)
        self.assertIn("select_candidate", prompt.system_prompt)
        self.assertIn("不得逐字复述", prompt.system_prompt)
        # The phase-reflect directive must ride along with any state section so
        # simple chitchat (greetings) also reflects the current phase instead of
        # defaulting to a generic "send me an image" opening.
        self.assertIn(SAFE_ANSWER_STATE_REFLECT_V0, prompt.system_prompt)
        for forbidden in (
            "session_id",
            "candidate_generation",
            "current_image_path",
            "D:/",
            ".jpg",
            ".xlsx",
            "score",
            "stack",
        ):
            self.assertNotIn(forbidden, prompt.system_prompt)
            self.assertNotIn(forbidden, prompt.user_prompt)

    def test_prompt_without_context_remains_state_free(self):
        # The original 38 eligible cases must still build a state-free prompt.
        eligible_cases = [
            case for case in self.suite["cases"] if case["expected"]["eligible"]
        ]
        for case in eligible_cases:
            with self.subTest(case=case["id"]):
                prompt = build_safe_answer_prompt_v0(
                    case["expected"]["category"],
                    case["text"],
                )
                self.assertNotIn("当前状态", prompt.system_prompt)
                self.assertNotIn("不得逐字复述", prompt.system_prompt)

    def test_prompt_with_idle_context_skips_state_section(self):
        context = SafeConversationContext(
            phase="IDLE",
            waiting_for="新题图",
            last_completed_step="",
        )
        prompt = build_safe_answer_prompt_v0("greeting", "你好", context)
        # IDLE has nothing meaningful to perceive: the guard stays but the
        # state section itself is empty, so the reflect directive is also absent.
        self.assertIn("不得逐字复述", prompt.system_prompt)
        self.assertNotIn("当前状态", prompt.system_prompt)
        self.assertNotIn(SAFE_ANSWER_STATE_REFLECT_V0, prompt.system_prompt)


if __name__ == "__main__":
    unittest.main()

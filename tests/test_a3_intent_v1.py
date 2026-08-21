import json
import unittest

from tiku_agent.a3_intent_v1 import (
    A3_INTENT_SYSTEM_PROMPT_V1,
    A3ActionDecisionV1,
    A3IntentContextV1,
    A3IntentEngineV1,
    A3IntentUnitV1,
    authorize_a3_action_v1,
    build_a3_intent_input_v1,
)


def _context(
    *,
    phase: str = "WAIT_UNIT_SELECTION",
    completed: tuple[int, ...] = (),
    searched: tuple[int, ...] = (),
    selected: int | None = None,
    candidate_count: int = 0,
    pending: tuple[str, ...] = (),
) -> A3IntentContextV1:
    return A3IntentContextV1(
        phase=phase,
        units=tuple(
            A3IntentUnitV1(
                unit_id=f"unit-{index}",
                question_index=index,
                display_label=f"四-{index}",
                completed=index in completed,
                searched=index in searched,
                selected=index == selected,
            )
            for index in (1, 2, 3)
        ),
        child_phase="WAIT_CANDIDATE_CHOICE" if phase == "A2_ACTIVE" else "",
        candidate_count=candidate_count,
        pending_cancel_scopes=pending,
    )


class A3IntentV1Tests(unittest.TestCase):
    def test_generic_cancel_always_clarifies_without_choosing_scope(self):
        engine = A3IntentEngineV1()

        for text in ("结束", "取消", "算了", "不搜了", "重新开始"):
            with self.subTest(text=text):
                decision = engine.decide(text, _context(phase="CROP_REQUIRED", selected=2))
                self.assertEqual(decision.action, "clarification")
                self.assertEqual(decision.clarification_reason, "ambiguous_cancel_scope")

    def test_explicit_cancel_scopes_are_separate_actions(self):
        engine = A3IntentEngineV1()
        context = _context(phase="CROP_REQUIRED", selected=2)

        self.assertEqual(engine.decide("取消当前题", context).action, "cancel_current_unit")
        self.assertEqual(engine.decide("结束这张图", context).action, "finish_page")
        self.assertEqual(engine.decide("开始新对话", context).action, "reset_session")

        for text in ("这道题", "这张图"):
            with self.subTest(text=text):
                self.assertEqual(engine.decide(text, context).action, "clarification")

    def test_scope_clarification_accepts_only_current_option_order(self):
        context = _context(
            phase="CROP_REQUIRED",
            selected=2,
            pending=(
                "cancel_current_unit",
                "finish_page",
                "reset_session",
                "continue_current",
            ),
        )
        engine = A3IntentEngineV1()

        self.assertEqual(engine.decide("1", context).action, "cancel_current_unit")
        self.assertEqual(engine.decide("2", context).action, "finish_page")
        self.assertEqual(engine.decide("4", context).action, "continue_current")

    def test_stable_original_index_does_not_renumber_remaining_units(self):
        engine = A3IntentEngineV1()
        context = _context(completed=(1,))

        completed = engine.decide("第1题", context)
        second = engine.decide("第2题", context)

        self.assertEqual(completed.action, "clarification")
        self.assertEqual(completed.clarification_reason, "unit_completed")
        self.assertEqual(second.action, "select_unit")
        self.assertEqual(second.question_index, 2)

    def test_a2_bare_number_clarifies_but_explicit_image_number_selects(self):
        engine = A3IntentEngineV1()
        context = _context(phase="A2_ACTIVE", selected=1, candidate_count=3)

        bare = engine.decide("第2题", context)
        explicit = engine.decide("图片第2题", context)

        self.assertEqual(bare.action, "clarification")
        self.assertEqual(bare.clarification_reason, "ambiguous_number_namespace")
        self.assertEqual(explicit.action, "select_unit")
        self.assertEqual(explicit.question_index, 2)

    def test_negative_named_unit_never_becomes_selection(self):
        decision = A3IntentEngineV1().decide("四-1 不搜了", _context())

        self.assertEqual(decision.action, "clarification")
        self.assertEqual(decision.clarification_reason, "ambiguous_action")

    def test_model_cannot_invent_selection_or_cancel_scope(self):
        context = _context()

        invented_selection = A3IntentEngineV1(
            lambda _prompt: {
                "action": "select_unit",
                "question_index": 2,
                "clarification_reason": None,
                "confidence": 0.9,
                "reason": "猜测用户指第二题",
            }
        ).decide("就那个", context)
        invented_finish = A3IntentEngineV1(
            lambda _prompt: {
                "action": "finish_page",
                "question_index": None,
                "clarification_reason": None,
                "confidence": 0.9,
                "reason": "猜测用户想结束",
            }
        ).decide("我想停一下", context)

        self.assertEqual(invented_selection.action, "clarification")
        self.assertEqual(invented_selection.clarification_reason, "ambiguous_reference")
        self.assertEqual(invented_finish.action, "clarification")
        self.assertEqual(invented_finish.clarification_reason, "ambiguous_cancel_scope")

    def test_prompt_payload_is_bounded_and_excludes_internal_unit_ids(self):
        context = _context(phase="CROP_REQUIRED", selected=2)

        payload = json.loads(build_a3_intent_input_v1("取消", context))

        self.assertEqual(payload["user_text"], "取消")
        self.assertNotIn("unit_id", payload["conversation_context"]["units"][0])
        self.assertIn("question_index", payload["conversation_context"]["units"][0])
        self.assertIn("ambiguous_cancel_scope", A3_INTENT_SYSTEM_PROMPT_V1)

    def test_authorization_rejects_current_cancel_without_selected_unit(self):
        decision = A3ActionDecisionV1(action="cancel_current_unit", confidence=1.0)

        authorization = authorize_a3_action_v1(decision, _context())

        self.assertFalse(authorization.allowed)
        self.assertEqual(authorization.code, "current_unit_unavailable")


if __name__ == "__main__":
    unittest.main()

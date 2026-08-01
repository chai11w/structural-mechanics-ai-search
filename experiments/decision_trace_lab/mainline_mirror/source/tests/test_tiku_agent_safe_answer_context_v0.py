from dataclasses import fields
import re
import unittest

from tiku_agent.action_decision_v2 import TASK_ACTIONS
from tiku_agent.safe_answer_context_v0 import (
    SAFE_ACTION_LABELS,
    SafeConversationContext,
    build_safe_answer_context,
    build_safe_answer_validation_facts,
    render_state_section,
)
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_CANCELLED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    PHASE_PROCESSING,
    PHASE_READY_FOR_SEARCH,
    PHASE_READY_TO_ROUTE,
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    AgentState,
    KNOWN_PHASES,
)

# Banned execution-claim verbs used by the safe-answer output validator.
_BANNED_VERBS = re.compile(
    r"(?:搜索|搜题|检索|查找|找到|查到|读取|复制|修改|删除|入库|执行)"
)


def _candidate_state(**overrides) -> AgentState:
    state = AgentState()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class SafeAnswerContextV0Test(unittest.TestCase):
    def test_build_from_idle_state(self):
        ctx = build_safe_answer_context(AgentState())
        self.assertEqual(ctx.phase, STATE_IDLE)
        self.assertEqual(ctx.candidate_count, 0)
        self.assertEqual(ctx.allowed_actions, ())
        self.assertEqual(ctx.waiting_for, "新题图")
        self.assertEqual(ctx.last_completed_step, "")
        self.assertIsNone(ctx.chapter)

    def test_idle_renders_empty_state_section(self):
        ctx = build_safe_answer_context(AgentState())
        self.assertEqual(render_state_section(ctx), "")

    def test_allowed_actions_match_permission_matrix_for_wait_candidate(self):
        state = _candidate_state(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_chapter="4力法",
            current_image_path="D:/bank/4/q1.jpg",
            candidates=[{"rank": 1}, {"rank": 2}, {"rank": 3}],
            continuation_available=True,
        )
        ctx = build_safe_answer_context(state)
        self.assertIn("选择候选题", ctx.allowed_actions)
        self.assertIn("说明候选都不合适", ctx.allowed_actions)
        self.assertIn("查看下一批候选", ctx.allowed_actions)
        self.assertIn("重新查看候选列表", ctx.allowed_actions)
        self.assertIn("补充或更换章节", ctx.allowed_actions)
        self.assertNotIn("确认后查找全部章节", ctx.allowed_actions)
        self.assertEqual(ctx.candidate_count, 3)
        self.assertEqual(ctx.chapter, "4力法")

    def test_continue_search_only_when_continuation_available(self):
        base = dict(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="D:/bank/4/q1.jpg",
            candidates=[{"rank": 1}, {"rank": 2}],
        )
        with_cont = build_safe_answer_context(_candidate_state(**base, continuation_available=True))
        self.assertIn("查看下一批候选", with_cont.allowed_actions)
        without_cont = build_safe_answer_context(_candidate_state(**base, continuation_available=False))
        self.assertNotIn("查看下一批候选", without_cont.allowed_actions)

    def test_global_search_only_when_offered(self):
        base = dict(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="D:/bank/2/q1.jpg",
            questions=[{"index": 1}],
        )
        offered = build_safe_answer_context(_candidate_state(**base, global_search_offered=True))
        self.assertIn("确认后查找全部章节", offered.allowed_actions)
        not_offered = build_safe_answer_context(_candidate_state(**base, global_search_offered=False))
        self.assertNotIn("确认后查找全部章节", not_offered.allowed_actions)

    def test_retry_search_only_in_error_phase(self):
        base = dict(
            phase=PHASE_ERROR,
            current_image_path="D:/bank/4/q1.jpg",
            last_error="timeout",
        )
        ctx = build_safe_answer_context(_candidate_state(**base))
        self.assertIn("重试刚才的操作", ctx.allowed_actions)
        self.assertIn("了解失败情况", ctx.allowed_actions)

    def test_internal_phases_have_no_meaningful_actions(self):
        for phase in (PHASE_PROCESSING, PHASE_READY_TO_ROUTE, PHASE_READY_FOR_SEARCH):
            with self.subTest(phase=phase):
                ctx = build_safe_answer_context(_candidate_state(phase=phase))
                self.assertEqual(ctx.allowed_actions, ())

    def test_answered_phase_exposes_answer_and_actions(self):
        state = _candidate_state(
            phase=PHASE_ANSWERED,
            current_chapter="4力法",
            current_image_path="D:/bank/4/q1.jpg",
            candidates=[{"rank": 1}],
            last_answer_paths=["D:/answers/4/q1.png"],
        )
        ctx = build_safe_answer_context(state)
        facts = build_safe_answer_validation_facts(state)
        self.assertTrue(facts.has_answer)
        self.assertIn("重新查看刚才的答案", ctx.allowed_actions)
        self.assertIn("选择候选题", ctx.allowed_actions)
        self.assertIn("补充或更换章节", ctx.allowed_actions)

    def test_no_match_phase_offers_chapter_change(self):
        state = _candidate_state(
            phase=PHASE_NO_MATCH,
            current_image_path="D:/bank/6/q1.jpg",
        )
        ctx = build_safe_answer_context(state)
        self.assertIn("补充或更换章节", ctx.allowed_actions)
        self.assertEqual(ctx.waiting_for, "换章节或新题图")
        self.assertEqual(ctx.last_completed_step, "无匹配题目")

    def test_waiting_for_and_last_completed_step_cover_all_phases(self):
        from tiku_agent.safe_answer_context_v0 import (
            _WAITING_FOR,
            _LAST_COMPLETED_STEP,
        )

        self.assertEqual(set(_WAITING_FOR), KNOWN_PHASES)
        self.assertEqual(set(_LAST_COMPLETED_STEP), KNOWN_PHASES)
        for phase in KNOWN_PHASES:
            with self.subTest(phase=phase):
                waiting = _WAITING_FOR[phase]
                last_step = _LAST_COMPLETED_STEP[phase]
                self.assertIsInstance(waiting, str)
                self.assertIsInstance(last_step, str)
                self.assertIsNone(_BANNED_VERBS.search(waiting))
                self.assertIsNone(_BANNED_VERBS.search(last_step))

    def test_cancelled_phase_waiting_is_empty(self):
        ctx = build_safe_answer_context(_candidate_state(phase=PHASE_CANCELLED))
        self.assertEqual(ctx.waiting_for, "")
        self.assertEqual(ctx.allowed_actions, ())

    def test_context_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            SafeConversationContext(phase="UNKNOWN_PHASE")

    def test_context_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            SafeConversationContext(phase=STATE_IDLE, candidate_count=-1)

    def test_context_rejects_non_whitelisted_action(self):
        with self.assertRaises(ValueError):
            SafeConversationContext(phase=STATE_IDLE, allowed_actions=("delete",))

    def test_payload_keys_are_exactly_the_whitelist(self):
        ctx = build_safe_answer_context(AgentState())
        payload = ctx.to_prompt_payload()
        expected = {
            "phase",
            "chapter",
            "candidate_count",
            "allowed_actions",
            "waiting_for",
            "last_completed_step",
        }
        self.assertEqual(
            set(payload),
            expected,
        )
        self.assertEqual({item.name for item in fields(SafeConversationContext)}, expected)

    def test_validation_facts_are_code_only_and_not_in_prompt_payload(self):
        state = _candidate_state(
            phase=PHASE_ANSWERED,
            current_image_path="D:/bank/4/q1.jpg",
            last_answer_paths=["D:/answers/4/q1.png"],
        )
        context = build_safe_answer_context(state)
        facts = build_safe_answer_validation_facts(state)

        self.assertTrue(facts.has_active_image)
        self.assertTrue(facts.has_answer)
        self.assertNotIn("has_active_image", context.to_prompt_payload())
        self.assertNotIn("has_answer", context.to_prompt_payload())
        self.assertNotIn("question_count", context.to_prompt_payload())

    def test_context_never_exposes_sensitive_fields(self):
        state = _candidate_state(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            session_id="secret-session-id",
            current_image_path="D:/bank/4/private/q1.jpg",
            candidates=[{"rank": 1, "path": "D:/bank/4/private/q1.jpg", "score": 0.99}],
            last_answer_paths=["D:/answers/private/q1.png"],
            last_error="internal stack: /secret",
        )
        ctx = build_safe_answer_context(state)
        payload = ctx.to_prompt_payload()
        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertNotIn("secret-session-id", str(value))
                self.assertNotIn("private", str(value))
                self.assertNotIn("D:/", str(value))
                self.assertNotIn(".jpg", str(value))
                self.assertNotIn("stack", str(value))
                self.assertNotIn("score", str(value))

    def test_allowed_actions_are_reviewed_labels_not_internal_actions(self):
        ctx = build_safe_answer_context(
            _candidate_state(
                phase=STATE_WAIT_CANDIDATE_CHOICE,
                current_image_path="D:/bank/4/q1.jpg",
                candidates=[{"rank": 1}],
                continuation_available=True,
            )
        )
        self.assertTrue(set(ctx.allowed_actions) <= set(SAFE_ACTION_LABELS.values()))
        self.assertTrue(set(ctx.allowed_actions).isdisjoint(TASK_ACTIONS))
        self.assertEqual(
            set(SAFE_ACTION_LABELS),
            set(TASK_ACTIONS) - {"cancel", "search_image"},
        )

    def test_render_state_section_mentions_candidate_count(self):
        state = _candidate_state(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_chapter="4力法",
            current_image_path="D:/bank/4/q1.jpg",
            candidates=[{"rank": 1}, {"rank": 2}],
            continuation_available=True,
        )
        ctx = build_safe_answer_context(state)
        section = render_state_section(ctx)
        self.assertIn("当前状态", section)
        self.assertIn("WAIT_CANDIDATE_CHOICE", section)
        self.assertIn("候选数量：2", section)
        self.assertIn("等待：候选选择", section)
        self.assertIn("选择候选题", section)
        for internal_action in TASK_ACTIONS:
            self.assertNotIn(internal_action, section)


if __name__ == "__main__":
    unittest.main()

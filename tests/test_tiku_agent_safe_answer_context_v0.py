import re
import unittest

from tiku_agent.action_decision_v2 import TASK_ACTIONS
from tiku_agent.safe_answer_context_v0 import (
    SafeConversationContext,
    build_safe_answer_context,
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
        self.assertEqual(ctx.question_count, 0)
        self.assertEqual(ctx.candidate_count, 0)
        self.assertEqual(ctx.allowed_actions, ())
        self.assertEqual(ctx.waiting_for, "新题图")
        self.assertEqual(ctx.last_completed_step, "")
        self.assertFalse(ctx.has_active_image)
        self.assertFalse(ctx.has_answer)
        self.assertIsNone(ctx.current_chapter)

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
        self.assertIn("select_candidate", ctx.allowed_actions)
        self.assertIn("reject_candidates", ctx.allowed_actions)
        self.assertIn("continue_search", ctx.allowed_actions)
        self.assertIn("show_candidates", ctx.allowed_actions)
        self.assertIn("set_chapter", ctx.allowed_actions)
        self.assertNotIn("global_search", ctx.allowed_actions)
        self.assertNotIn("cancel", ctx.allowed_actions)
        self.assertNotIn("search_image", ctx.allowed_actions)
        self.assertEqual(ctx.candidate_count, 3)
        self.assertEqual(ctx.current_chapter, "4力法")

    def test_continue_search_only_when_continuation_available(self):
        base = dict(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="D:/bank/4/q1.jpg",
            candidates=[{"rank": 1}, {"rank": 2}],
        )
        with_cont = build_safe_answer_context(_candidate_state(**base, continuation_available=True))
        self.assertIn("continue_search", with_cont.allowed_actions)
        without_cont = build_safe_answer_context(_candidate_state(**base, continuation_available=False))
        self.assertNotIn("continue_search", without_cont.allowed_actions)

    def test_global_search_only_when_offered(self):
        base = dict(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="D:/bank/2/q1.jpg",
            questions=[{"index": 1}],
        )
        offered = build_safe_answer_context(_candidate_state(**base, global_search_offered=True))
        self.assertIn("global_search", offered.allowed_actions)
        not_offered = build_safe_answer_context(_candidate_state(**base, global_search_offered=False))
        self.assertNotIn("global_search", not_offered.allowed_actions)

    def test_retry_search_only_in_error_phase(self):
        base = dict(
            phase=PHASE_ERROR,
            current_image_path="D:/bank/4/q1.jpg",
            last_error="timeout",
        )
        ctx = build_safe_answer_context(_candidate_state(**base))
        self.assertIn("retry_search", ctx.allowed_actions)
        self.assertIn("explain_failure", ctx.allowed_actions)

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
        self.assertTrue(ctx.has_answer)
        self.assertIn("resend_answer", ctx.allowed_actions)
        self.assertIn("select_candidate", ctx.allowed_actions)
        self.assertIn("set_chapter", ctx.allowed_actions)

    def test_no_match_phase_offers_chapter_change(self):
        state = _candidate_state(
            phase=PHASE_NO_MATCH,
            current_image_path="D:/bank/6/q1.jpg",
        )
        ctx = build_safe_answer_context(state)
        self.assertIn("set_chapter", ctx.allowed_actions)
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
        self.assertEqual(
            set(payload),
            {
                "phase",
                "current_chapter",
                "question_count",
                "candidate_count",
                "allowed_actions",
                "waiting_for",
                "last_completed_step",
                "has_active_image",
                "has_answer",
                "global_search_offered",
                "continuation_available",
            },
        )

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

    def test_allowed_actions_are_all_task_actions(self):
        ctx = build_safe_answer_context(
            _candidate_state(
                phase=STATE_WAIT_CANDIDATE_CHOICE,
                current_image_path="D:/bank/4/q1.jpg",
                candidates=[{"rank": 1}],
                continuation_available=True,
            )
        )
        self.assertTrue(set(ctx.allowed_actions) <= TASK_ACTIONS)
        # cancel/search_image are intentionally excluded from the safe surface.
        self.assertNotIn("cancel", ctx.allowed_actions)
        self.assertNotIn("search_image", ctx.allowed_actions)

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
        self.assertIn("select_candidate", section)


if __name__ == "__main__":
    unittest.main()

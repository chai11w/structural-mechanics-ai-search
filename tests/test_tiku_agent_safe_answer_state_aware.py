"""Runtime acceptance tests for the state-aware safe-answer stage (M5).

These tests treat TikuSearchAgent as a black box: a state is built, a greeting
or business text is sent, and the assertions check what the safe-answer model
received, what the user is told, whether the AgentState changed, and whether any
business tool ran.  No real model, no real question bank, and no real tool is
used; a capturing SafeAnswerGeneratorV0 stands in for the model.
"""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.safe_answer_reply_v0 import MAX_SAFE_ANSWER_CHARS, render_safe_answer_v0
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


TOOL_NAMES = (
    "analyze_image",
    "analyze_multi_image",
    "prepare_question_units",
    "route_bank",
    "classify_structure",
    "coarse_search",
    "global_search",
    "rerank_candidates",
    "answer_candidate",
)

_SENSITIVE_MARKERS = (
    "session_id",
    "D:/",
    ".jpg",
    "score",
    "stack",
    "private",
)


def _toolbox_that_must_not_run() -> tuple[AgentToolbox, dict[str, Mock]]:
    mocks = {
        name: Mock(side_effect=AssertionError(f"safe-answer route called tool: {name}"))
        for name in TOOL_NAMES
    }
    return AgentToolbox(**mocks), mocks


def _state_for_phase(phase: str) -> AgentState:
    """Build a legal AgentState for every known phase with a distinct session."""
    common = dict(
        session_id=f"m5-{phase.lower()}",
        current_image_path="question.jpg",
        current_question_image_path="question.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
        current_chapter="4力法",
        questions=[{"index": 1}],
        candidates=[{"rank": 1, "path": "candidate.jpg"}, {"rank": 2}],
        selected_question=1,
        last_answer_paths=["answer.png"],
        last_error="internal stack: /secret",
        continuation_available=True,
        global_search_offered=True,
    )
    base = {key: value for key, value in common.items()}
    if phase == STATE_IDLE:
        base = {"session_id": "m5-idle"}
    elif phase == STATE_WAIT_QUESTION_CHOICE:
        base.pop("candidates", None)
    elif phase == PHASE_ANSWERED:
        pass
    elif phase == PHASE_NO_MATCH:
        base.pop("candidates", None)
        base.pop("last_answer_paths", None)
    elif phase == PHASE_CANCELLED:
        base.pop("candidates", None)
    elif phase == PHASE_PROCESSING:
        base.pop("candidates", None)
    return AgentState(phase=phase, **base)


def _capturing_agent(
    state: AgentState,
    *,
    reply: str = "你好。",
) -> tuple[TikuSearchAgent, list, dict[str, Mock]]:
    """Build an agent whose generator captures the prompt it receives."""
    toolbox, tool_mocks = _toolbox_that_must_not_run()
    seen: list = []

    def model_client(request):
        seen.append(request.prompt)
        return reply

    agent = TikuSearchAgent(
        state=state,
        tools=toolbox,
        use_llm_intent=False,
        enable_safe_answer_v0=True,
        safe_answer_generator_v0=SafeAnswerGeneratorV0(model_client),
    )
    return agent, seen, tool_mocks


def _assert_sensitive_fields_absent(self, *texts: str) -> None:
    for text in texts:
        for marker in _SENSITIVE_MARKERS:
            with self.subTest(text=text[:20], marker=marker):
                self.assertNotIn(marker, text)


class SafeAnswerStateAwareAcceptanceTest(unittest.TestCase):
    def test_every_phase_greeting_reaches_model_with_phase_aware_prompt(self):
        for phase in KNOWN_PHASES:
            with self.subTest(phase=phase):
                state = _state_for_phase(phase)
                before = deepcopy(state.to_dict())
                agent, seen, tool_mocks = _capturing_agent(state)
                response = agent.handle_text("你好")

                self.assertEqual(response.intent, "safe_answer")
                self.assertEqual(response.reply_source, "model")
                self.assertEqual(len(seen), 1)
                if phase == STATE_IDLE:
                    # IDLE has no meaningful state section, so the prompt stays
                    # guard-only and must not claim a phase.
                    self.assertNotIn("当前状态", seen[0].system_prompt)
                    self.assertNotIn("阶段：", seen[0].system_prompt)
                else:
                    self.assertIn("当前状态", seen[0].system_prompt)
                    self.assertIn(f"阶段：{phase}", seen[0].system_prompt)
                self.assertEqual(agent.state.to_dict(), before)
                self.assertEqual(response.state, before)
                for tool_mock in tool_mocks.values():
                    tool_mock.assert_not_called()

    def test_phase_aware_prompt_never_leaks_sensitive_fields(self):
        for phase in KNOWN_PHASES:
            with self.subTest(phase=phase):
                state = _state_for_phase(phase)
                agent, seen, _ = _capturing_agent(state)
                agent.handle_text("你好")
                _assert_sensitive_fields_absent(
                    self,
                    seen[0].system_prompt,
                    seen[0].user_prompt,
                )

    def test_code_only_validation_facts_never_enter_model_prompt(self):
        state = _state_for_phase(PHASE_ANSWERED)
        agent, seen, _ = _capturing_agent(state)
        agent.handle_text("你好")

        for hidden_field in (
            "question_count",
            "题目数量",
            "has_active_image",
            "has_answer",
            "global_search_offered",
            "continuation_available",
        ):
            self.assertNotIn(hidden_field, seen[0].system_prompt)

    def test_candidate_phase_greeting_mentions_candidate_count(self):
        state = _state_for_phase(STATE_WAIT_CANDIDATE_CHOICE)
        before = deepcopy(state.to_dict())
        agent, seen, tool_mocks = _capturing_agent(state)
        response = agent.handle_text("你好")

        self.assertIn("候选数量：2", seen[0].system_prompt)
        self.assertIn("等待：候选选择", seen[0].system_prompt)
        self.assertEqual(response.text, "你好。")
        self.assertEqual(agent.state.to_dict(), before)
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()

    def test_wait_chapter_courtesy_is_phase_aware_with_global_search_offer(self):
        state = _state_for_phase(STATE_WAIT_CHAPTER)
        before = deepcopy(state.to_dict())
        agent, seen, tool_mocks = _capturing_agent(state, reply="不客气。")
        response = agent.handle_text("谢谢")

        self.assertEqual(response.intent, "safe_answer")
        self.assertEqual(response.reply_source, "model")
        self.assertIn("阶段：WAIT_CHAPTER", seen[0].system_prompt)
        self.assertIn("等待：章节", seen[0].system_prompt)
        self.assertEqual(response.text, "不客气。")
        self.assertEqual(agent.state.to_dict(), before)
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()

    def test_answered_phase_greeting_acknowledges_answer_without_paths(self):
        state = _state_for_phase(PHASE_ANSWERED)
        before = deepcopy(state.to_dict())
        agent, seen, tool_mocks = _capturing_agent(state)
        response = agent.handle_text("你好")

        self.assertIn("阶段：ANSWERED", seen[0].system_prompt)
        self.assertIn("候选数量：2", seen[0].system_prompt)
        self.assertEqual(response.text, "你好。")
        self.assertEqual(agent.state.to_dict(), before)
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()
        # The prompt must never expose answer file paths.
        self.assertNotIn("answer.png", seen[0].system_prompt)
        self.assertNotIn("answer.png", seen[0].user_prompt)

    def test_business_text_never_enters_safe_answer_in_candidate_phase(self):
        business_texts = (
            "选1",
            "继续搜",
            "换第五章",
            "全局搜索",
            "取消",
            "重发答案",
            "第2题",
            "把答案发给我",
        )
        for text in business_texts:
            with self.subTest(text=text):
                state = _state_for_phase(STATE_WAIT_CANDIDATE_CHOICE)
                generator = Mock()
                agent = TikuSearchAgent(
                    state=state,
                    use_llm_intent=False,
                    enable_safe_answer_v0=True,
                    safe_answer_generator_v0=generator,
                )
                response = agent.handle_text(text)
                self.assertNotEqual(response.intent, "safe_answer")
                generator.generate.assert_not_called()

    def test_model_failures_fall_back_to_generic_reply_in_phase(self):
        failures = (
            TimeoutError("slow"),
            RuntimeError("provider details"),
        )
        for error in failures:
            with self.subTest(error=type(error).__name__):
                state = _state_for_phase(STATE_WAIT_CANDIDATE_CHOICE)
                before = deepcopy(state.to_dict())
                agent = TikuSearchAgent(
                    state=state,
                    use_llm_intent=False,
                    enable_safe_answer_v0=True,
                    safe_answer_generator_v0=SafeAnswerGeneratorV0(
                        Mock(side_effect=error)
                    ),
                )
                response = agent.handle_text("你好")
                self.assertEqual(response.intent, "safe_answer")
                self.assertEqual(response.reply_source, "fixed_fallback")
                self.assertEqual(response.text, render_safe_answer_v0("greeting"))
                self.assertLessEqual(len(response.text), MAX_SAFE_ANSWER_CHARS)
                self.assertNotIn("\n", response.text)
                self.assertEqual(agent.state.to_dict(), before)
                self.assertEqual(response.state, before)

    def test_model_output_violations_fall_back_to_generic_in_phase(self):
        invalid_outputs = (
            "我已经帮你检索到答案。",
            "需要我介绍一下吗？",
            "第一句。\n第二句。",
            "详情见 https://example.com",
        )
        for output in invalid_outputs:
            with self.subTest(output=output[:20]):
                state = _state_for_phase(STATE_WAIT_CHAPTER)
                before = deepcopy(state.to_dict())
                agent, _, _ = _capturing_agent(state, reply=output)
                response = agent.handle_text("你好")
                self.assertEqual(response.reply_source, "fixed_fallback")
                self.assertEqual(response.text, render_safe_answer_v0("greeting"))
                self.assertEqual(agent.state.to_dict(), before)

    def test_allowed_actions_in_prompt_match_permission_matrix(self):
        cases = (
            (
                STATE_WAIT_CANDIDATE_CHOICE,
                ("选择候选题", "说明候选都不合适"),
            ),
            (
                PHASE_ANSWERED,
                ("选择候选题", "补充或更换章节", "重新查看刚才的答案"),
            ),
            (PHASE_NO_MATCH, ("补充或更换章节",)),
        )
        for phase, expected_actions in cases:
            with self.subTest(phase=phase):
                state = _state_for_phase(phase)
                agent, seen, _ = _capturing_agent(state)
                agent.handle_text("你好")
                for action in expected_actions:
                    self.assertIn(action, seen[0].system_prompt)

    def test_cross_restart_keeps_safe_answer_state_aware(self):
        original = _state_for_phase(STATE_WAIT_CANDIDATE_CHOICE)
        agent_before, seen_before, _ = _capturing_agent(original)
        agent_before.handle_text("你好")

        # Simulate a restart: rebuild the AgentState from the serialized dict.
        restored = AgentState.from_dict(original.to_dict())
        agent_after, seen_after, _ = _capturing_agent(restored)
        agent_after.handle_text("你好")

        # The freshly reconstructed agent must still see the same phase/counts.
        self.assertIn("WAIT_CANDIDATE_CHOICE", seen_before[0].system_prompt)
        self.assertIn("候选数量：2", seen_before[0].system_prompt)
        self.assertIn("WAIT_CANDIDATE_CHOICE", seen_after[0].system_prompt)
        self.assertIn("候选数量：2", seen_after[0].system_prompt)

    def test_cross_user_sessions_are_isolated_in_safe_answer_context(self):
        user_a = AgentState(
            session_id="user-a",
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            candidates=[{"rank": 1}, {"rank": 2}, {"rank": 3}],
            continuation_available=True,
        )
        user_b = AgentState(
            session_id="user-b",
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            global_search_offered=True,
        )
        agent_a, seen_a, _ = _capturing_agent(user_a)
        agent_b, seen_b, _ = _capturing_agent(user_b)

        agent_a.handle_text("你好")
        agent_b.handle_text("你好")

        self.assertIn("WAIT_CANDIDATE_CHOICE", seen_a[0].system_prompt)
        self.assertIn("候选数量：3", seen_a[0].system_prompt)
        self.assertIn("WAIT_CHAPTER", seen_b[0].system_prompt)
        self.assertNotIn("user-a", seen_a[0].system_prompt)
        self.assertNotIn("user-b", seen_a[0].system_prompt)
        self.assertNotIn("user-a", seen_b[0].system_prompt)
        self.assertNotIn("user-b", seen_b[0].system_prompt)


if __name__ == "__main__":
    unittest.main()

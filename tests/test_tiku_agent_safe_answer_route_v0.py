from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.safe_answer_reply_v0 import (
    MAX_SAFE_ANSWER_CHARS,
    render_grounded_safe_answer_v0,
)
from tiku_agent.state import (
    STATE_WAIT_CANDIDATE_CHOICE,
    AgentState,
    PHASE_ANSWERED,
)


FIXTURE = Path(__file__).parent / "fixtures" / "safe_answer_v0_cases.json"
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


def _toolbox_that_must_not_run() -> tuple[AgentToolbox, dict[str, Mock]]:
    mocks = {
        name: Mock(side_effect=AssertionError(f"safe-answer route called tool: {name}"))
        for name in TOOL_NAMES
    }
    return AgentToolbox(**mocks), mocks


def _representative_state() -> AgentState:
    return AgentState(
        session_id="safe-answer-contract",
        phase=PHASE_ANSWERED,
        current_image_path="question.jpg",
        current_question_image_path="question.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
        current_chapter="4力法",
        current_route="main",
        candidates=[{"rank": 1, "path": "candidate.jpg"}],
        selected_rank=1,
        last_answer_paths=["answer.jpg"],
        last_intent={"action": "select_candidate", "candidate_rank": 1},
        task_revision=3,
        candidate_revision=2,
        candidate_generation="generation-2",
    )


class SafeAnswerRouteV0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_every_eligible_case_uses_zero_tool_zero_model_state_preserving_route(self):
        eligible_cases = [
            case for case in self.suite["cases"] if case["expected"]["eligible"]
        ]
        self.assertEqual(len(eligible_cases), 38)

        for case in eligible_cases:
            with self.subTest(case=case["id"]):
                state = _representative_state()
                before = deepcopy(state.to_dict())
                toolbox, tool_mocks = _toolbox_that_must_not_run()
                model = Mock(side_effect=AssertionError("safe-answer route called the model"))
                agent = TikuSearchAgent(
                    state=state,
                    tools=toolbox,
                    use_llm_intent=True,
                    llm_client=model,
                    enable_safe_answer_v0=True,
                )

                response = agent.handle_text(case["text"])

                self.assertEqual(response.intent, "safe_answer")
                self.assertEqual(response.reply_source, "fixed_fallback")
                self.assertTrue(response.text)
                self.assertLessEqual(len(response.text), MAX_SAFE_ANSWER_CHARS)
                self.assertNotIn("\n", response.text)
                self.assertEqual(agent.state.to_dict(), before)
                self.assertEqual(response.state, before)
                model.assert_not_called()
                for tool_mock in tool_mocks.values():
                    tool_mock.assert_not_called()

    def test_noneligible_cases_are_behavior_identical_to_the_existing_path(self):
        denied_cases = [
            case for case in self.suite["cases"] if not case["expected"]["eligible"]
        ]
        self.assertEqual(len(denied_cases), 22)

        for case in denied_cases:
            with self.subTest(case=case["id"]):
                baseline_state = AgentState(session_id="same-session")
                candidate_state = AgentState(session_id="same-session")
                baseline_tools, baseline_mocks = _toolbox_that_must_not_run()
                candidate_tools, candidate_mocks = _toolbox_that_must_not_run()
                generator = Mock()
                baseline = TikuSearchAgent(
                    state=baseline_state,
                    tools=baseline_tools,
                    use_llm_intent=False,
                )
                candidate = TikuSearchAgent(
                    state=candidate_state,
                    tools=candidate_tools,
                    use_llm_intent=False,
                    enable_safe_answer_v0=True,
                    safe_answer_generator_v0=generator,
                )

                baseline_response = baseline.handle_text(case["text"])
                candidate_response = candidate.handle_text(case["text"])

                self.assertEqual(candidate_response, baseline_response)
                self.assertEqual(candidate.state.to_dict(), baseline.state.to_dict())
                for name in TOOL_NAMES:
                    self.assertEqual(
                        candidate_mocks[name].call_args_list,
                        baseline_mocks[name].call_args_list,
                    )
                generator.generate.assert_not_called()

    def test_reply_contract_is_concise_and_category_specific(self):
        expected_phrases = {
            "你好": "你好",
            "谢谢": "不客气",
            "你是谁": "力答",
            "你能做什么": "检索最相似的题目",
            "你是怎么工作的": "识别题图",
        }
        for text, phrase in expected_phrases.items():
            with self.subTest(text=text):
                agent = TikuSearchAgent(
                    use_llm_intent=False,
                    enable_safe_answer_v0=True,
                )
                response = agent.handle_text(text)
                self.assertIn(phrase, response.text)
                self.assertEqual(response.intent, "safe_answer")

    def test_supported_chapter_fact_is_exact_zero_model_zero_tool_and_state_preserving(self):
        expected = (
            "结构力学题库支持静定结构、静定结构位移、力法、位移法、力矩分配；"
            "矩阵位移和影响线仅支持含具体外荷载的题目。"
        )
        questions = (
            "你可以回答哪些章节的问题",
            "你支持哪些章节？",
            "你能回答哪几章",
            "题库覆盖的章节有哪些",
            "题库里收录哪几个章节的题目",
        )

        for text in questions:
            with self.subTest(text=text):
                state = _representative_state()
                before = deepcopy(state.to_dict())
                toolbox, tool_mocks = _toolbox_that_must_not_run()
                intent_model = Mock(
                    side_effect=AssertionError("grounded fact called intent model")
                )
                answer_generator = Mock()
                answer_generator.generate.side_effect = AssertionError(
                    "grounded fact called answer model"
                )
                agent = TikuSearchAgent(
                    state=state,
                    tools=toolbox,
                    use_llm_intent=True,
                    llm_client=intent_model,
                    enable_safe_answer_v0=True,
                    safe_answer_generator_v0=answer_generator,
                )

                response = agent.handle_text(text)

                self.assertEqual(response.text, expected)
                for chapter in CHAPTERS:
                    self.assertIn(chapter.lstrip("0123456789"), response.text)
                self.assertNotRegex(response.text, r"第[2-8]章")
                self.assertEqual(response.intent, "safe_answer")
                self.assertEqual(response.reply_source, "grounded_fact")
                self.assertLessEqual(len(response.text), MAX_SAFE_ANSWER_CHARS)
                self.assertEqual(agent.state.to_dict(), before)
                self.assertEqual(response.state, before)
                intent_model.assert_not_called()
                answer_generator.generate.assert_not_called()
                for tool_mock in tool_mocks.values():
                    tool_mock.assert_not_called()

    def test_supported_chapter_fact_does_not_intercept_business_or_mixed_text(self):
        for text in (
            "按第4章搜题",
            "帮我搜第4章的题",
            "你支持哪些章节，顺便帮我搜题",
            "第4章",
        ):
            with self.subTest(text=text):
                self.assertIsNone(render_grounded_safe_answer_v0(text))

    def test_default_disabled_keeps_the_previous_safe_text_path(self):
        implicit = TikuSearchAgent(
            state=AgentState(session_id="same-session"),
            use_llm_intent=False,
        )
        explicit = TikuSearchAgent(
            state=AgentState(session_id="same-session"),
            use_llm_intent=False,
            enable_safe_answer_v0=False,
        )

        self.assertEqual(implicit.handle_text("你是谁"), explicit.handle_text("你是谁"))
        self.assertNotEqual(implicit.handle_text("你好").intent, "safe_answer")

    def test_injected_generator_answer_and_fallback_preserve_state_and_call_no_tools(self):
        cases = (
            (
                lambda _request: "我是力答，专注结构力学题库搜索，通过题图检索相似候选题。",
                "model",
                "",
            ),
            (
                lambda _request: "我已经帮你检索到答案。",
                "fixed_fallback",
                "output_fabricated_execution_claim",
            ),
            (
                lambda _request: (_ for _ in ()).throw(TimeoutError("slow")),
                "fixed_fallback",
                "model_timeout",
            ),
        )
        for model_client, source, reason in cases:
            with self.subTest(source=source, reason=reason):
                state = _representative_state()
                before = deepcopy(state.to_dict())
                toolbox, tool_mocks = _toolbox_that_must_not_run()
                agent = TikuSearchAgent(
                    state=state,
                    tools=toolbox,
                    use_llm_intent=False,
                    enable_safe_answer_v0=True,
                    safe_answer_generator_v0=SafeAnswerGeneratorV0(model_client),
                )

                response = agent.handle_text("你是谁")

                self.assertEqual(response.intent, "safe_answer")
                self.assertEqual(response.reply_source, source)
                self.assertEqual(response.fallback_reason, reason)
                self.assertEqual(agent.state.to_dict(), before)
                self.assertEqual(response.state, before)
                for tool_mock in tool_mocks.values():
                    tool_mock.assert_not_called()

    def test_unexpected_generator_error_uses_fixed_reply_without_leaking_details(self):
        generator = Mock()
        generator.generate.side_effect = RuntimeError("private provider details")
        agent = TikuSearchAgent(
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=generator,
        )

        response = agent.handle_text("你好")

        self.assertEqual(response.text, "你好。")
        self.assertEqual(response.reply_source, "fixed_fallback")
        self.assertEqual(response.fallback_reason, "generator_error")
        self.assertNotIn("private provider details", repr(response))

    def test_noneligible_text_does_not_call_injected_generator(self):
        generator = Mock()
        agent = TikuSearchAgent(
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=generator,
        )

        response = agent.handle_text("你好，帮我搜个题")

        self.assertNotEqual(response.intent, "safe_answer")
        generator.generate.assert_not_called()

    def test_farewell_is_model_answered_without_cancelling_active_state(self):
        state = _representative_state()
        before = deepcopy(state.to_dict())
        toolbox, tool_mocks = _toolbox_that_must_not_run()
        agent = TikuSearchAgent(
            state=state,
            tools=toolbox,
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=SafeAnswerGeneratorV0(
                lambda _request: "再见，随时欢迎回来。"
            ),
        )

        response = agent.handle_text("算了我要走了")

        self.assertEqual(response.intent, "safe_answer")
        self.assertEqual(response.reply_source, "model")
        self.assertEqual(agent.state.to_dict(), before)
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()

    def test_general_text_uses_intent_as_a_business_safety_net(self):
        generator = Mock()
        agent = TikuSearchAgent(
            state=_representative_state(),
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=generator,
        )

        response = agent.handle_text("算了")

        self.assertNotEqual(response.intent, "safe_answer")
        generator.generate.assert_not_called()

    def test_general_nonbusiness_clarification_is_still_model_answered(self):
        generator = SafeAnswerGeneratorV0(
            lambda _request: "我在，可以聊聊结构力学题库相关的问题。"
        )
        agent = TikuSearchAgent(
            use_llm_intent=True,
            llm_client=lambda _prompt: {
                "action": "clarification",
                "clarification_reason": "ambiguous_action",
            },
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=generator,
        )

        response = agent.handle_text("随便说点什么")

        self.assertEqual(response.intent, "safe_answer")
        self.assertEqual(response.reply_source, "model")

    def test_candidate_phase_greeting_is_phase_aware_zero_tool_zero_state(self):
        state = AgentState(
            session_id="candidate-greeting",
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="question.jpg",
            current_question_image_path="question.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            candidates=[{"rank": 1}, {"rank": 2}, {"rank": 3}],
            continuation_available=True,
        )
        before = deepcopy(state.to_dict())
        toolbox, tool_mocks = _toolbox_that_must_not_run()
        seen = []
        generator = SafeAnswerGeneratorV0(
            lambda request: (
                seen.append(request.prompt),
                "你好，候选已经准备好了。",
            )[1]
        )
        agent = TikuSearchAgent(
            state=state,
            tools=toolbox,
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=generator,
        )

        response = agent.handle_text("你好")

        self.assertEqual(response.intent, "safe_answer")
        self.assertEqual(response.reply_source, "model")
        self.assertIn("WAIT_CANDIDATE_CHOICE", seen[0].system_prompt)
        self.assertIn("候选数量：3", seen[0].system_prompt)
        self.assertEqual(agent.state.to_dict(), before)
        self.assertEqual(response.state, before)
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()

    def test_context_derivation_failure_degrades_to_state_free_fallback(self):
        state = AgentState(
            session_id="broken-state",
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="question.jpg",
        )
        toolbox, tool_mocks = _toolbox_that_must_not_run()
        agent = TikuSearchAgent(
            state=state,
            tools=toolbox,
            use_llm_intent=False,
            enable_safe_answer_v0=True,
        )

        with patch(
            "tiku_agent.agent.build_safe_answer_context",
            side_effect=RuntimeError("state summary must not break safe answer"),
        ):
            response = agent.handle_text("你好")

        self.assertEqual(response.intent, "safe_answer")
        self.assertEqual(response.reply_source, "fixed_fallback")
        self.assertEqual(response.text, "你好。")
        for tool_mock in tool_mocks.values():
            tool_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

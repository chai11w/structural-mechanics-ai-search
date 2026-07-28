from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.safe_answer_reply_v0 import MAX_SAFE_ANSWER_CHARS
from tiku_agent.state import AgentState, PHASE_ANSWERED


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

    def test_reply_contract_is_concise_and_category_specific(self):
        expected_phrases = {
            "你好": "题图",
            "谢谢": "不客气",
            "你是谁": "结构力学题库助手",
            "你能做什么": "检索相似题",
            "你是怎么工作的": "荷载与结构特征",
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


if __name__ == "__main__":
    unittest.main()

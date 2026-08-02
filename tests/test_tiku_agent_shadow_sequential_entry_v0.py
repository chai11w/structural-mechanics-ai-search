"""Runtime-entry tests for the two observer-only sequential scenarios."""

import json
from pathlib import Path
import unittest

from scripts.evaluate_shadow_plan_entry_qwen_v0 import (
    EvaluationTools,
    MemoryShadowLogger,
    RecordingPlanner,
    ReplayIntentClient,
    build_phase_state,
)
from tiku_agent.agent import TikuSearchAgent
from tiku_agent.shadow_plan_v0 import ShadowPlan, ShadowPlannerResult
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0
from tiku_agent.tools import AgentToolConfig


FIXTURES = Path(__file__).parent / "fixtures"
FIRST_WAVE_IDS = {
    "seq_show_then_select",
    "seq_report_then_show",
    "holdout_show_select_1",
    "holdout_show_select_2",
    "holdout_show_select_3",
    "holdout_report_show_1",
    "holdout_report_show_2",
    "holdout_report_show_3",
    "confirm_show_select_1",
    "confirm_show_select_2",
    "confirm_report_show_1",
    "confirm_report_show_2",
}


class _UnplannablePlanner(ShadowPlannerV0):
    def __init__(self) -> None:
        pass

    def plan(self, user_text, context_payload):
        return ShadowPlannerResult(
            rewritten_text=user_text,
            keywords=(),
            reason="entry test",
            plan=ShadowPlan(
                goal="observe only",
                steps=(),
                source="unplannable",
            ),
        )


def _load_cases() -> list[dict]:
    names = (
        "shadow_admission_v0_cases.json",
        "shadow_sequential_holdout_v0_cases.json",
        "shadow_sequential_confirmation_v0_cases.json",
    )
    cases = []
    for name in names:
        cases.extend(json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"])
    return cases


def _intent_client(_prompt: str) -> dict:
    # Explicit fixture requests are handled by fixed intent rules.  This keeps
    # a deterministic fallback for the few non-first-wave long-tail cases.
    return {
        "action": "clarification",
        "clarification_reason": "missing_candidate_rank",
        "confidence": 0.4,
        "reason": "entry test fallback",
    }


def _agent(state, tools, *, planner=None, logger=None) -> TikuSearchAgent:
    return TikuSearchAgent(
        state=state,
        tools=tools.toolbox(),
        config=AgentToolConfig(top_k=3, rerank_top=3),
        use_llm_intent=True,
        llm_client=ReplayIntentClient(_intent_client),
        enable_safe_answer_v0=True,
        shadow_planner=planner,
        shadow_logger=logger,
    )


class SequentialShadowEntryTest(unittest.TestCase):
    def test_only_first_wave_sequential_cases_gain_runtime_admission(self):
        admitted_ids = set()
        cases = [
            case
            for case in _load_cases()
            if case["group"] in {"sequential", "atomic"}
        ]
        for case in cases:
            baseline_state = build_phase_state(case["phase"], case_id=f"base-{case['id']}")
            observed_state = build_phase_state(case["phase"], case_id=f"observed-{case['id']}")
            baseline_tools = EvaluationTools()
            observed_tools = EvaluationTools()
            logger = MemoryShadowLogger()
            planner = RecordingPlanner(_UnplannablePlanner())
            baseline = _agent(baseline_state, baseline_tools)
            observed = _agent(observed_state, observed_tools, planner=planner, logger=logger)

            baseline_response = baseline.handle_text(case["text"])
            observed_response = observed.handle_text(case["text"])
            if planner.calls:
                admitted_ids.add(case["id"])

            self.assertEqual(baseline_response.text, observed_response.text, case["id"])
            self.assertEqual(baseline_response.images, observed_response.images, case["id"])
            self.assertEqual(baseline_response.intent, observed_response.intent, case["id"])
            self.assertEqual(baseline_tools.calls, observed_tools.calls, case["id"])
            self.assertEqual(_business_state(baseline.state), _business_state(observed.state), case["id"])

        self.assertEqual(admitted_ids, FIRST_WAVE_IDS)

    def test_approved_request_records_scenario_without_executing_plan(self):
        state = build_phase_state("WAIT_CANDIDATE_CHOICE", case_id="approved")
        tools = EvaluationTools()
        logger = MemoryShadowLogger()
        planner = RecordingPlanner(_UnplannablePlanner())
        agent = _agent(state, tools, planner=planner, logger=logger)

        response = agent.handle_text("先把候选列表再发一下，然后选择候选2")

        self.assertEqual(planner.calls, 1)
        self.assertEqual(len(logger.entries), 1)
        self.assertEqual(logger.entries[0].trigger_reason, "sequential:show_then_select")
        self.assertEqual(tools.calls, [])
        self.assertEqual(response.intent, "show_candidates")

    def test_atomic_request_keeps_fixed_path_without_planner(self):
        state = build_phase_state("WAIT_CANDIDATE_CHOICE", case_id="atomic")
        tools = EvaluationTools()
        logger = MemoryShadowLogger()
        planner = RecordingPlanner(_UnplannablePlanner())
        agent = _agent(state, tools, planner=planner, logger=logger)

        response = agent.handle_text("选择候选2")

        self.assertEqual(planner.calls, 0)
        self.assertEqual(len(logger.entries), 0)
        self.assertEqual(response.intent, "select_candidate")
        self.assertEqual(tools.calls, ["answer_candidate"])


def _business_state(state) -> dict:
    payload = state.to_dict()
    payload.pop("session_id", None)
    return payload


if __name__ == "__main__":
    unittest.main()

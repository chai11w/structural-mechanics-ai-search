"""Evaluate Stage 5 through the real Agent text entry and planner gate.

The older ``evaluate_shadow_plan_qwen_v0.py`` is intentionally a planner
contract stress test: it calls the planner for every phase/utterance cell.  This
script answers the product question that stress test cannot: after safe-answer
and fixed-business routing, which real inputs actually reach shadow planning,
what kind of plan is recorded, and does enabling the observer change anything
the user or existing business flow can observe?

Every case runs a paired comparison from the same AgentState:

* baseline Agent: shadow planning disabled;
* observed Agent: shadow planning enabled;
* both Agents share a replay cache for the intent-model response;
* deterministic evaluation tools prevent question-bank or filesystem writes.

The gold fixture defines allowed routes plus actions that must never be inferred
from the utterance.  ``unplannable`` is a separate outcome, never counted as an
actionable accepted plan.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from tiku_agent.agent import AgentResponse, AgentToolbox, TikuSearchAgent
from tiku_agent.intent_v2 import call_qwen_decision_v2
from tiku_agent.shadow_plan_log import ShadowPlanLogEntry, ShadowPlanLogger
from tiku_agent.shadow_plan_v0 import ShadowPlannerResult
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0, call_qwen_planner_v0
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    AgentState,
)
from tiku_agent.tools import AgentToolConfig, ToolResult


FIXTURE = BASE / "tests" / "fixtures" / "shadow_plan_entry_v0_cases.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_shadow_plan_entry_eval_8794"

ROUTE_SAFE_ANSWER = "safe_answer"
ROUTE_FIXED_BUSINESS = "fixed_business"
ROUTE_FIXED_CLARIFICATION = "fixed_clarification"
ROUTE_SHADOW_ACTIONABLE = "shadow_actionable"
ROUTE_NEEDS_CONFIRMATION = "needs_confirmation"
ROUTE_UNPLANNABLE = "unplannable"
ROUTE_PERMISSION_REJECTED = "permission_rejected"
ROUTE_PLANNER_UNAVAILABLE = "planner_unavailable"

KNOWN_ROUTES = frozenset({
    ROUTE_SAFE_ANSWER,
    ROUTE_FIXED_BUSINESS,
    ROUTE_FIXED_CLARIFICATION,
    ROUTE_SHADOW_ACTIONABLE,
    ROUTE_NEEDS_CONFIRMATION,
    ROUTE_UNPLANNABLE,
    ROUTE_PERMISSION_REJECTED,
    ROUTE_PLANNER_UNAVAILABLE,
})


class ReplayIntentClient:
    """Call the real intent model once per distinct prompt, then replay it."""

    def __init__(self, delegate: Callable[[str], dict[str, Any]]) -> None:
        self.delegate = delegate
        self.cache: dict[str, dict[str, Any]] = {}
        self.delegate_calls = 0

    def __call__(self, prompt: str) -> dict[str, Any]:
        if prompt not in self.cache:
            self.delegate_calls += 1
            self.cache[prompt] = dict(self.delegate(prompt))
        return dict(self.cache[prompt])


class RecordingPlanner:
    """Record whether the production Agent gate actually invoked the planner."""

    def __init__(self, delegate: ShadowPlannerV0) -> None:
        self.delegate = delegate
        self.calls = 0
        self.results: list[ShadowPlannerResult | None] = []

    def plan(self, user_text: str, context_payload: dict[str, Any]) -> ShadowPlannerResult | None:
        self.calls += 1
        result = self.delegate.plan(user_text, context_payload)
        self.results.append(result)
        return result


class MemoryShadowLogger(ShadowPlanLogger):
    """Capture shadow records without touching the runtime JSONL file."""

    def __init__(self) -> None:
        self.entries: list[ShadowPlanLogEntry] = []

    def write(self, entry: ShadowPlanLogEntry) -> None:
        self.entries.append(entry)


class EvaluationTools:
    """Deterministic read-only tool doubles used by entry evaluation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def toolbox(self) -> AgentToolbox:
        return AgentToolbox(
            analyze_image=self.analyze_image,
            analyze_multi_image=self.analyze_multi_image,
            prepare_question_units=self.prepare_question_units,
            route_bank=self.route_bank,
            classify_structure=self.classify_structure,
            coarse_search=self.coarse_search,
            global_search=self.global_search,
            rerank_candidates=self.rerank_candidates,
            answer_candidate=self.answer_candidate,
        )

    def _mark(self, name: str) -> None:
        self.calls.append(name)

    def analyze_image(self, image_path, *, chapter="auto", config=None, include_layout=False):
        self._mark("analyze_image")
        return ToolResult.success(
            tool="analyze_image",
            code="EVAL_IMAGE_ANALYZED",
            data={
                "image_path": str(image_path),
                "chapter": "4力法" if chapter == "auto" else chapter,
                "loads": [{"type": "集中", "raw": "P"}],
            },
        )

    def analyze_multi_image(self, image_path, *, config=None):
        self._mark("analyze_multi_image")
        return ToolResult.success(
            tool="analyze_multi_image",
            code="EVAL_SINGLE_IMAGE",
            data={
                "is_multi": False,
                "questions": [],
                "single_analysis": {
                    "loads": [{"type": "集中", "raw": "P"}],
                    "chapter_hint": "4力法",
                },
            },
        )

    def prepare_question_units(self, image_path, questions, *, config=None):
        self._mark("prepare_question_units")
        return ToolResult.success(
            tool="prepare_question_units",
            code="EVAL_QUESTION_UNITS_READY",
            data={"questions": list(questions), "diagram_crops": {}},
        )

    def route_bank(self, loads):
        self._mark("route_bank")
        return ToolResult.success(
            tool="route_bank",
            code="EVAL_ROUTE_MAIN",
            data={"route": "main", "category": "main_numeric", "reason": "evaluation"},
        )

    def classify_structure(self, image_path, *, route, classified=None, config=None):
        self._mark("classify_structure")
        return ToolResult.success(
            tool="classify_structure",
            code="EVAL_STRUCTURE_CLASSIFIED",
            data={"structure_type": "", "source": "not_applicable"},
        )

    def coarse_search(
        self,
        loads,
        *,
        chapter,
        route,
        structure_type="",
        top_k=None,
        exclude_candidate_keys=None,
    ):
        self._mark("coarse_search")
        excluded = set(exclude_candidate_keys or [])
        candidates = [
            {
                "rank": rank,
                "path": f"{chapter}/candidate-{index}.jpg",
                "name": f"candidate-{index}.jpg",
                "score": 0.95 - index / 100,
                "candidate_key": f"{chapter}|main|candidate-{index}.jpg",
            }
            for rank, index in enumerate(range(1, 5), 1)
            if f"{chapter}|main|candidate-{index}.jpg" not in excluded
        ][:2]
        return ToolResult.success(
            tool="coarse_search",
            code="EVAL_CANDIDATES_FOUND",
            data={"candidates": candidates, "has_more": True},
        )

    def global_search(self, loads, query_image_path, *, route, structure_type="", config=None):
        self._mark("global_search")
        return ToolResult.success(
            tool="global_search",
            code="EVAL_GLOBAL_CANDIDATES_FOUND",
            data={"candidates": [_candidate(1), _candidate(2)]},
            next_state=STATE_WAIT_CANDIDATE_CHOICE,
        )

    def rerank_candidates(self, query_image_path, candidates, *, route, rerank_top=3, force_rerank=False):
        self._mark("rerank_candidates")
        visible = [dict(candidate, final_score=candidate.get("score", 0.9)) for candidate in candidates]
        return ToolResult.success(
            tool="rerank_candidates",
            code="EVAL_RERANK_COMPLETED",
            data={"reranked": True, "visible_candidates": visible, "rerank_note": ""},
        )

    def answer_candidate(self, candidates, *, rank, copy_to_output=True, config=None):
        self._mark("answer_candidate")
        return ToolResult.success(
            tool="answer_candidate",
            code="EVAL_ANSWER_FOUND",
            data={
                "rank": rank,
                "candidate": dict(candidates[rank - 1]),
                "answer_paths": [f"answer-{rank}.jpg"],
                "copied_paths": [f"answer-{rank}.jpg"],
            },
            next_state=PHASE_ANSWERED,
        )


def _candidate(rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "path": f"4力法/candidate-{rank}.jpg",
        "name": f"candidate-{rank}.jpg",
        "score": 0.95 - rank / 100,
        "candidate_key": f"4力法|main|candidate-{rank}.jpg",
    }


def build_phase_state(phase: str, *, case_id: str = "entry-eval") -> AgentState:
    common: dict[str, Any] = {
        "session_id": f"shadow-entry-{case_id}",
        "current_image_path": "question.jpg",
        "current_question_image_path": "question.jpg",
        "current_loads": [{"type": "集中", "raw": "P"}],
        "task_revision": 1,
    }
    if phase == STATE_IDLE:
        return AgentState(session_id=f"shadow-entry-{case_id}")
    if phase == STATE_WAIT_CHAPTER:
        common.update(global_search_offered=True)
    elif phase == STATE_WAIT_QUESTION_CHOICE:
        common.update(
            questions=[{"index": 1, "image_path": "q1.jpg"}, {"index": 2, "image_path": "q2.jpg"}],
            selected_question=1,
        )
    elif phase == STATE_WAIT_CANDIDATE_CHOICE:
        common.update(
            current_chapter="4力法",
            current_route="main",
            candidates=[_candidate(1), _candidate(2), _candidate(3)],
            candidate_generation="entry-eval-generation",
            continuation_available=True,
        )
    elif phase == PHASE_ANSWERED:
        common.update(
            current_chapter="4力法",
            current_route="main",
            candidates=[_candidate(1), _candidate(2)],
            candidate_generation="entry-eval-generation",
            selected_rank=1,
            last_answer_paths=["answer-1.jpg"],
        )
    elif phase == PHASE_NO_MATCH:
        common.update(current_chapter="4力法", last_error="未找到可靠相似题")
    elif phase == PHASE_ERROR:
        common.update(last_error="工具执行失败")
    return AgentState(phase=phase, **common)


def load_gold_cases(fixture: Path = FIXTURE) -> list[dict[str, Any]]:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    cases = list(payload.get("cases") or [])
    if not cases:
        raise ValueError("shadow entry gold fixture must contain cases")
    for case in cases:
        allowed_routes = set(case.get("allowed_routes") or [])
        if not allowed_routes or not allowed_routes <= KNOWN_ROUTES:
            raise ValueError(f"invalid allowed_routes for {case.get('id')}")
        if case.get("planner") not in {"never", "optional", "required"}:
            raise ValueError(f"invalid planner expectation for {case.get('id')}")
    return cases


def evaluate_cases(
    cases: list[dict[str, Any]],
    *,
    runs: int,
    intent_model_client: Callable[[str], dict[str, Any]],
    planner_factory: Callable[[], ShadowPlannerV0],
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if runs <= 0:
        raise ValueError("runs must be positive")
    records: list[dict[str, Any]] = []
    total = len(cases) * runs
    completed = 0
    for run in range(1, runs + 1):
        for case in cases:
            record = evaluate_case(
                case,
                run=run,
                intent_model_client=intent_model_client,
                planner=planner_factory(),
            )
            records.append(record)
            completed += 1
            if progress is not None:
                progress(completed, total, record)
    return records


def evaluate_case(
    case: dict[str, Any],
    *,
    run: int,
    intent_model_client: Callable[[str], dict[str, Any]],
    planner: ShadowPlannerV0,
) -> dict[str, Any]:
    initial = build_phase_state(str(case["phase"]), case_id=str(case["id"]))
    replay_intent = ReplayIntentClient(intent_model_client)
    baseline_tools = EvaluationTools()
    observed_tools = EvaluationTools()
    baseline_agent = _build_agent(
        AgentState.from_dict(initial.to_dict()),
        baseline_tools,
        replay_intent,
    )
    recording_planner = RecordingPlanner(planner)
    logger = MemoryShadowLogger()
    observed_agent = _build_agent(
        AgentState.from_dict(initial.to_dict()),
        observed_tools,
        replay_intent,
        planner=recording_planner,
        logger=logger,
    )

    baseline_response = baseline_agent.handle_text(str(case["text"]))
    observed_response = observed_agent.handle_text(str(case["text"]))
    entry = logger.entries[-1].to_dict() if logger.entries else None
    route = classify_route(observed_response, entry)
    planner_actions = tuple(
        str(step.get("action") or "")
        for step in ((entry or {}).get("plan") or {}).get("steps", [])
        if step.get("action")
    )
    # Gold restrictions apply to both sides of the admission gate: an unsafe
    # semantic action is still a failure when the fixed intent path chose it,
    # and a shadow proposal is still worth surfacing even when permission review
    # later rejected it.
    evaluated_actions = tuple(sorted(set(planner_actions) | {observed_response.intent}))
    forbidden = tuple(
        sorted(set(evaluated_actions) & set(case.get("forbidden_actions") or []))
    )
    observable_equal = (
        _response_signature(baseline_response) == _response_signature(observed_response)
        and baseline_agent.state.to_dict() == observed_agent.state.to_dict()
        and baseline_tools.calls == observed_tools.calls
    )
    planner_expectation = str(case["planner"])
    planner_gate_ok = (
        (planner_expectation == "never" and recording_planner.calls == 0)
        or (planner_expectation == "required" and recording_planner.calls == 1)
        or planner_expectation == "optional"
    )
    route_ok = route in set(case["allowed_routes"])
    passed = route_ok and not forbidden and observable_equal and planner_gate_ok
    return {
        "case_id": case["id"],
        "run": run,
        "phase": case["phase"],
        "text": case["text"],
        "route": route,
        "allowed_routes": list(case["allowed_routes"]),
        "route_ok": route_ok,
        "planner_expectation": planner_expectation,
        "planner_calls": recording_planner.calls,
        "planner_gate_ok": planner_gate_ok,
        "intent_model_calls": replay_intent.delegate_calls,
        "planner_actions": list(planner_actions),
        "evaluated_actions": list(evaluated_actions),
        "forbidden_actions": list(case.get("forbidden_actions") or []),
        "forbidden_actions_seen": list(forbidden),
        "observable_equal": observable_equal,
        "baseline": _response_signature(baseline_response),
        "observed": _response_signature(observed_response),
        "baseline_tools": list(baseline_tools.calls),
        "observed_tools": list(observed_tools.calls),
        "shadow_entry": entry,
        "passed": passed,
    }


def _build_agent(
    state: AgentState,
    tools: EvaluationTools,
    intent_client: ReplayIntentClient,
    *,
    planner: RecordingPlanner | None = None,
    logger: MemoryShadowLogger | None = None,
) -> TikuSearchAgent:
    return TikuSearchAgent(
        state=state,
        tools=tools.toolbox(),
        config=AgentToolConfig(top_k=3, rerank_top=3),
        use_llm_intent=True,
        llm_client=intent_client,
        enable_safe_answer_v0=True,
        shadow_planner=planner,
        shadow_logger=logger,
    )


def classify_route(response: AgentResponse, entry: dict[str, Any] | None) -> str:
    if entry is not None:
        if entry.get("planner_unavailable"):
            return ROUTE_PLANNER_UNAVAILABLE
        review = dict(entry.get("review") or {})
        code = str(review.get("code") or "")
        outcome = str(review.get("outcome") or "")
        if code == "unplannable":
            return ROUTE_UNPLANNABLE
        if code == "needs_confirmation" or outcome == "needs_confirmation":
            return ROUTE_NEEDS_CONFIRMATION
        if outcome == "reject":
            return ROUTE_PERMISSION_REJECTED
        steps = list((entry.get("plan") or {}).get("steps") or [])
        if outcome == "allow" and steps:
            return ROUTE_SHADOW_ACTIONABLE
        return ROUTE_PLANNER_UNAVAILABLE
    if response.intent == "safe_answer":
        return ROUTE_SAFE_ANSWER
    if response.intent == "clarification":
        return ROUTE_FIXED_CLARIFICATION
    return ROUTE_FIXED_BUSINESS


def _response_signature(response: AgentResponse) -> dict[str, Any]:
    return {
        "text": response.text,
        "images": list(response.images),
        "intent": response.intent,
        "reply_source": response.reply_source,
        "fallback_reason": response.fallback_reason,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    passed = sum(bool(record["passed"]) for record in records)
    route_counts = Counter(str(record["route"]) for record in records)
    by_run: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for record in records:
        key = str(record["run"])
        by_run[key]["total"] += 1
        by_run[key]["passed"] += int(bool(record["passed"]))
    return {
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "routes": dict(route_counts),
        "actionable_plans": route_counts[ROUTE_SHADOW_ACTIONABLE],
        "needs_confirmation": route_counts[ROUTE_NEEDS_CONFIRMATION],
        "unplannable": route_counts[ROUTE_UNPLANNABLE],
        "permission_rejected": route_counts[ROUTE_PERMISSION_REJECTED],
        "planner_unavailable": route_counts[ROUTE_PLANNER_UNAVAILABLE],
        "fixed_path_planner_violations": sum(
            record["planner_expectation"] == "never" and record["planner_calls"] != 0
            for record in records
        ),
        "forbidden_action_violations": sum(
            bool(record["forbidden_actions_seen"]) for record in records
        ),
        "observable_differences": sum(not bool(record["observable_equal"]) for record in records),
        "failed_case_runs": [
            {"case_id": record["case_id"], "run": record["run"]}
            for record in records
            if not record["passed"]
        ],
        "by_run": dict(by_run),
    }


def write_results(output_dir: Path, records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "records.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate shadow planning through Agent.handle_text")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")

    cases = load_gold_cases(args.fixture)
    intent_client = lambda prompt: call_qwen_decision_v2(
        prompt, model=args.model, endpoint=args.endpoint
    )
    planner_factory = lambda: ShadowPlannerV0(
        lambda prompt: call_qwen_planner_v0(
            prompt, model=args.model, endpoint=args.endpoint
        )
    )
    records = evaluate_cases(
        cases,
        runs=args.runs,
        intent_model_client=intent_client,
        planner_factory=planner_factory,
        progress=lambda completed, total, record: print(
            f"[{completed}/{total}] run={record['run']} case={record['case_id']} "
            f"route={record['route']} passed={record['passed']}",
            flush=True,
        ),
    )
    summary = summarize(records)
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    write_results(output_dir, records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir.resolve()}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

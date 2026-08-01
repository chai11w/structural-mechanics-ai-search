"""Evaluate which explicit complex requests should reach shadow planning.

This is an admission *diagnostic*, not a runtime router.  Every case first goes
through ``TikuSearchAgent.handle_text`` with shadow planning off/on so the real
entry decision and observable parity are measured.  A separate diagnostic
Planner call then shows whether planning would add the steps that the atomic
fixed route cannot represent.  Direct diagnostic results are never reported as
real admission and never execute tools or mutate business state.
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
from scripts.evaluate_shadow_plan_entry_qwen_v0 import (
    ROUTE_FIXED_BUSINESS,
    ROUTE_FIXED_CLARIFICATION,
    ROUTE_NEEDS_CONFIRMATION,
    ROUTE_PERMISSION_REJECTED,
    ROUTE_PLANNER_UNAVAILABLE,
    ROUTE_SAFE_ANSWER,
    ROUTE_SHADOW_ACTIONABLE,
    ROUTE_UNPLANNABLE,
    build_phase_state,
    evaluate_case,
)
from tiku_agent.intent_runtime_v2 import build_runtime_context_v2
from tiku_agent.intent_v2 import call_qwen_decision_v2
from tiku_agent.shadow_plan_v0 import (
    PermissionReview,
    ShadowPlan,
    ShadowPlannerResult,
    build_permission_review_facts,
    review_shadow_plan,
)
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0, call_qwen_planner_v0
from tiku_agent.shadow_semantic_gate_v0 import (
    SemanticAuthorizationResult,
    review_shadow_plan_semantics,
)


FIXTURE = BASE / "tests" / "fixtures" / "shadow_admission_v0_cases.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_shadow_admission_eval_8794"
DEFAULT_PROFILE = "representative_v0"

EXPECTED_NEVER = "never"
EXPECTED_OBSERVE = "should_observe"
EXPECTED_OPTIONAL = "optional"
EXPECTED_ENTRY_VALUES = {EXPECTED_NEVER, EXPECTED_OBSERVE, EXPECTED_OPTIONAL}
GROUPS = {"atomic", "sequential", "conditional", "clarify_or_unsupported"}
ALL_ENTRY_ROUTES = [
    ROUTE_SAFE_ANSWER,
    ROUTE_FIXED_BUSINESS,
    ROUTE_FIXED_CLARIFICATION,
    ROUTE_SHADOW_ACTIONABLE,
    ROUTE_NEEDS_CONFIRMATION,
    ROUTE_UNPLANNABLE,
    ROUTE_PERMISSION_REJECTED,
    ROUTE_PLANNER_UNAVAILABLE,
]


def load_admission_cases(path: Path = FIXTURE) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("shadow admission fixture must contain cases")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("every admission case must be an object")
        case = dict(raw)
        case_id = str(case.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate case id: {case_id!r}")
        seen.add(case_id)
        if str(case.get("group")) not in GROUPS:
            raise ValueError(f"unknown group for {case_id}")
        if str(case.get("expected_entry")) not in EXPECTED_ENTRY_VALUES:
            raise ValueError(f"unknown expected_entry for {case_id}")
        if not str(case.get("text") or "").strip() or not str(case.get("phase") or "").strip():
            raise ValueError(f"missing phase/text for {case_id}")
        for field in ("required_steps", "forbidden_actions"):
            if not isinstance(case.get(field), list):
                raise ValueError(f"{field} must be a list for {case_id}")
            case[field] = [str(item) for item in case[field] if str(item)]
        normalized.append(case)
    return normalized


def load_evaluation_profile(
    path: Path = FIXTURE,
    profile_name: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    profiles = payload.get("evaluation_profiles") if isinstance(payload, dict) else None
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f"unknown evaluation profile: {profile_name}")
    weights = profile.get("group_weights")
    if not isinstance(weights, dict) or set(weights) != GROUPS:
        raise ValueError(f"profile {profile_name} must weight every admission group")
    normalized_weights = {str(group): float(weight) for group, weight in weights.items()}
    if any(weight < 0 for weight in normalized_weights.values()):
        raise ValueError(f"profile {profile_name} has a negative group weight")
    if abs(sum(normalized_weights.values()) - 1.0) > 1e-9:
        raise ValueError(f"profile {profile_name} group weights must sum to 1")
    hard_gates = profile.get("hard_gates")
    if not isinstance(hard_gates, list) or not hard_gates:
        raise ValueError(f"profile {profile_name} must define hard_gates")
    return {
        "name": profile_name,
        "status": str(profile.get("status") or "unknown"),
        "description": str(profile.get("description") or ""),
        "group_weights": normalized_weights,
        "hard_gates": [str(item) for item in hard_gates],
    }


def evaluate_admission_cases(
    cases: list[dict[str, Any]],
    *,
    runs: int,
    intent_model_client: Callable[[str], dict[str, Any]],
    planner_factory: Callable[[], ShadowPlannerV0],
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    total = len(cases) * runs
    for run in range(1, runs + 1):
        for case in cases:
            record = evaluate_admission_case(
                case,
                run=run,
                intent_model_client=intent_model_client,
                entry_planner=planner_factory(),
                diagnostic_planner=planner_factory(),
            )
            records.append(record)
            if progress is not None:
                progress(len(records), total, record)
    return records


def evaluate_admission_case(
    case: dict[str, Any],
    *,
    run: int,
    intent_model_client: Callable[[str], dict[str, Any]],
    entry_planner: ShadowPlannerV0,
    diagnostic_planner: ShadowPlannerV0,
) -> dict[str, Any]:
    entry_case = {
        "id": case["id"],
        "phase": case["phase"],
        "text": case["text"],
        "planner": EXPECTED_OPTIONAL,
        "allowed_routes": ALL_ENTRY_ROUTES,
        "forbidden_actions": [],
    }
    entry = evaluate_case(
        entry_case,
        run=run,
        intent_model_client=intent_model_client,
        planner=entry_planner,
    )
    diagnostic = diagnose_candidate_plan(case, diagnostic_planner)
    admitted = int(entry["planner_calls"]) == 1
    expected_entry = str(case["expected_entry"])
    entry_contract_ok = (
        (expected_entry == EXPECTED_NEVER and not admitted)
        or (expected_entry == EXPECTED_OBSERVE and admitted)
        or expected_entry == EXPECTED_OPTIONAL
    )
    return {
        "case_id": case["id"],
        "run": run,
        "group": case["group"],
        "phase": case["phase"],
        "text": case["text"],
        "expected_entry": expected_entry,
        "current_admitted": admitted,
        "entry_contract_ok": entry_contract_ok,
        "current_route": entry["route"],
        "current_response_intent": entry["observed"]["intent"],
        "current_response": entry["observed"],
        "current_tools": entry["observed_tools"],
        "current_tool_call_count": len(entry["observed_tools"]),
        "current_planner_actions": entry["planner_actions"],
        "observable_equal": entry["observable_equal"],
        "required_steps": list(case["required_steps"]),
        "forbidden_actions": list(case["forbidden_actions"]),
        "diagnostic": diagnostic,
    }


def diagnose_candidate_plan(
    case: dict[str, Any],
    planner: ShadowPlannerV0,
) -> dict[str, Any]:
    state = build_phase_state(str(case["phase"]), case_id=f"admission-{case['id']}")
    context = build_runtime_context_v2(state)
    result = planner.plan(str(case["text"]), context.to_prompt_payload())
    if result is None:
        return {
            "route": ROUTE_PLANNER_UNAVAILABLE,
            "actions": [],
            "required_steps_covered": False,
            "forbidden_actions_seen": [],
            "effective_forbidden_actions": [],
            "useful": False,
            "result": None,
            "permission_review": None,
            "semantic_review": None,
        }

    facts = build_permission_review_facts(state)
    permission = review_shadow_plan(result.plan, facts)
    semantic = review_shadow_plan_semantics(str(case["text"]), result, facts)
    actions = [step.action for step in result.plan.steps]
    required = list(case["required_steps"])
    forbidden = sorted(set(actions) & set(case["forbidden_actions"]))
    required_covered = _is_subsequence(required, actions)
    route = _diagnostic_route(result, permission, semantic)
    effective_forbidden = forbidden if route == ROUTE_SHADOW_ACTIONABLE else []
    useful = bool(required) and required_covered and not forbidden and bool(actions)
    conditional_guard_required = str(case["group"]) == "conditional" and bool(actions)
    conditional_semantic_allow_risk = (
        conditional_guard_required
        and permission.allowed
        and not semantic.requires_confirmation
    )
    future_admission_ready = (
        str(case["group"]) == "sequential"
        and useful
        and route == ROUTE_SHADOW_ACTIONABLE
    )
    return {
        "route": route,
        "actions": actions,
        "required_steps_covered": required_covered,
        "forbidden_actions_seen": forbidden,
        "effective_forbidden_actions": effective_forbidden,
        "useful": useful,
        "goal_covered": useful,
        "conditional_guard_required": conditional_guard_required,
        "conditional_semantic_allow_risk": conditional_semantic_allow_risk,
        "future_admission_ready": future_admission_ready,
        "result": _result_to_dict(result),
        "permission_review": _review_to_dict(permission),
        "semantic_review": _semantic_to_dict(semantic),
    }


def _diagnostic_route(
    result: ShadowPlannerResult,
    permission: PermissionReview,
    semantic: SemanticAuthorizationResult,
) -> str:
    if not result.plan.steps:
        return ROUTE_UNPLANNABLE
    if not permission.allowed:
        return ROUTE_PERMISSION_REJECTED
    if semantic.requires_confirmation:
        return ROUTE_NEEDS_CONFIRMATION
    return ROUTE_SHADOW_ACTIONABLE


def _is_subsequence(required: list[str], actual: list[str]) -> bool:
    if not required:
        return True
    cursor = iter(actual)
    return all(any(item == expected for item in cursor) for expected in required)


def summarize_admission(
    records: list[dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_counts = Counter(str(record["expected_entry"]) for record in records)
    group_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "admitted": 0, "diagnostic_useful": 0}
    )
    for record in records:
        group = group_counts[str(record["group"])]
        group["total"] += 1
        group["admitted"] += int(bool(record["current_admitted"]))
        group["diagnostic_useful"] += int(bool(record["diagnostic"]["useful"]))

    should_records = [
        record for record in records if record["expected_entry"] == EXPECTED_OBSERVE
    ]
    never_records = [
        record for record in records if record["expected_entry"] == EXPECTED_NEVER
    ]
    diagnostic_routes = Counter(str(record["diagnostic"]["route"]) for record in records)
    fixed_intents = Counter(str(record["current_response_intent"]) for record in records)
    summary = {
        "total": len(records),
        "targets": dict(target_counts),
        "groups": dict(group_counts),
        "current_should_observe_admitted": sum(
            bool(record["current_admitted"]) for record in should_records
        ),
        "current_should_observe_total": len(should_records),
        "current_admission_recall": round(
            sum(bool(record["current_admitted"]) for record in should_records) / len(should_records),
            4,
        ) if should_records else 0.0,
        "current_atomic_false_admissions": sum(
            bool(record["current_admitted"]) for record in never_records
        ),
        "current_conditional_tool_calls": sum(
            int(record.get("current_tool_call_count", len(record.get("current_tools", []))))
            for record in records
            if record["group"] == "conditional"
        ),
        "current_conditional_tool_turns": sum(
            bool(record.get("current_tool_call_count", len(record.get("current_tools", []))))
            for record in records
            if record["group"] == "conditional"
        ),
        "current_entry_contract_failures": sum(
            not bool(record["entry_contract_ok"]) for record in records
        ),
        "observable_differences": sum(
            not bool(record["observable_equal"]) for record in records
        ),
        "diagnostic_routes": dict(diagnostic_routes),
        "diagnostic_required_steps_covered": sum(
            bool(record["diagnostic"]["required_steps_covered"])
            for record in should_records
        ),
        "diagnostic_useful_plans": sum(
            bool(record["diagnostic"]["useful"]) for record in should_records
        ),
        "diagnostic_future_admission_ready": sum(
            bool(record["diagnostic"].get("future_admission_ready")) for record in should_records
        ),
        "conditional_semantic_allow_risks": sum(
            bool(record["diagnostic"].get("conditional_semantic_allow_risk"))
            for record in records
        ),
        "diagnostic_raw_forbidden_proposals": sum(
            bool(record["diagnostic"]["forbidden_actions_seen"]) for record in records
        ),
        "diagnostic_effective_forbidden": sum(
            bool(record["diagnostic"]["effective_forbidden_actions"]) for record in records
        ),
        "current_response_intents": dict(fixed_intents),
        "missed_should_observe_cases": [
            {"case_id": record["case_id"], "run": record["run"]}
            for record in should_records
            if not record["current_admitted"]
        ],
    }
    if profile is not None:
        weights = dict(profile["group_weights"])
        group_contract_rates = {
            group: round(
                sum(bool(record["entry_contract_ok"]) for record in records if record["group"] == group)
                / sum(1 for record in records if record["group"] == group),
                4,
            )
            for group in GROUPS
            if any(record["group"] == group for record in records)
        }
        active_weight = sum(weights[group] for group in group_contract_rates)
        weighted_contract_rate = (
            sum(weights[group] * rate for group, rate in group_contract_rates.items()) / active_weight
            if active_weight else 0.0
        )
        hard_gate_results = {
            metric: int(summary.get(metric, 0)) == 0
            for metric in profile["hard_gates"]
        }
        summary["representative_profile"] = {
            **profile,
            "group_contract_rates": group_contract_rates,
            "active_weight": round(active_weight, 4),
            "weighted_entry_contract_rate": round(weighted_contract_rate, 4),
            "hard_gate_results": hard_gate_results,
            "hard_gates_passed": all(hard_gate_results.values()),
            "release_ready": round(weighted_contract_rate, 4) == 1.0 and all(hard_gate_results.values()),
        }
    return summary


def _result_to_dict(result: ShadowPlannerResult) -> dict[str, Any]:
    return {
        "rewritten_text": result.rewritten_text,
        "keywords": list(result.keywords),
        "reason": result.reason,
        "plan": _plan_to_dict(result.plan),
    }


def _plan_to_dict(plan: ShadowPlan) -> dict[str, Any]:
    return {
        "goal": plan.goal,
        "steps": [
            {"action": step.action, "params": dict(step.params), "reason": step.reason}
            for step in plan.steps
        ],
        "stop_condition": plan.stop_condition,
        "source": plan.source,
    }


def _review_to_dict(review: PermissionReview) -> dict[str, Any]:
    return {
        "outcome": review.outcome,
        "code": review.code,
        "reason": review.reason,
        "violations": list(review.violations),
    }


def _semantic_to_dict(result: SemanticAuthorizationResult) -> dict[str, Any]:
    return {
        "review": _review_to_dict(result.review),
        "evidence": [item.to_dict() for item in result.evidence],
        "explicit_keywords": list(result.explicit_keywords),
        "inferred_keywords": list(result.inferred_keywords),
        "confidence": result.confidence,
        "requires_confirmation": result.requires_confirmation,
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
    parser = argparse.ArgumentParser(description="Evaluate complex-request shadow admission")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--group", choices=sorted(GROUPS), help="Run only one admission group")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when target admission gates fail")
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")
    if args.runs < 1:
        raise SystemExit("--runs must be positive")

    cases = load_admission_cases(args.fixture)
    profile = load_evaluation_profile(args.fixture, args.profile)
    if args.group:
        cases = [case for case in cases if case["group"] == args.group]
    intent_client = lambda prompt: call_qwen_decision_v2(
        prompt, model=args.model, endpoint=args.endpoint
    )
    planner_factory = lambda: ShadowPlannerV0(
        lambda prompt: call_qwen_planner_v0(
            prompt, model=args.model, endpoint=args.endpoint
        )
    )
    records = evaluate_admission_cases(
        cases,
        runs=args.runs,
        intent_model_client=intent_client,
        planner_factory=planner_factory,
        progress=lambda completed, total, record: print(
            f"[{completed}/{total}] run={record['run']} case={record['case_id']} "
            f"entry={record['current_route']} admitted={record['current_admitted']} "
            f"diagnostic={record['diagnostic']['route']}"
        ),
    )
    summary = summarize_admission(records, profile=profile)
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    write_results(output_dir, records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir}")

    if not args.strict:
        return 0
    strict_ok = (
        summary["current_admission_recall"] == 1.0
        and summary["current_atomic_false_admissions"] == 0
        and summary["observable_differences"] == 0
        and summary["diagnostic_effective_forbidden"] == 0
        and summary["representative_profile"]["hard_gates_passed"]
    )
    return 0 if strict_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

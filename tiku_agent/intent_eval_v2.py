"""Offline Intent V2 evaluation primitives.

The first supported system is the deterministic V1 rule/fallback path.  It
never calls Qwen or question-bank tools.  Later V2 evaluators can reuse the same
gold schema and metrics.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent import IntentResult, call_qwen_intent, parse_user_intent
from tiku_agent.intent_v2 import (
    DecisionModelV2,
    call_qwen_decision_v2,
    decide_intent_v2,
)


SCORED_FIELDS = (
    "action",
    "question_index",
    "candidate_rank",
    "chapter_override",
    "chapter_target",
    "clarification_reason",
    "requested_action",
)

EXECUTING_ACTIONS = {
    "search_image",
    "set_chapter",
    "select_question",
    "select_candidate",
    "resend_answer",
    "retry_search",
}

SAFE_EXPECTED_ACTIONS = {
    "greeting",
    "small_talk",
    "capability_help",
    "out_of_scope",
    "clarification",
    "reject",
}

CATEGORY_GROUPS = {
    "pending_chapter": "chapter_context",
    "chapter_context": "chapter_context",
    "safety": "safety_boundary",
    "safety_boundary": "safety_boundary",
}


def load_gold_suite(path: str | Path) -> dict[str, Any]:
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("gold suite must contain a non-empty cases list")
    ids = [str(case.get("id") or "") for case in cases]
    if any(not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("gold case ids must be non-empty and unique")
    return suite


def load_gold_suites(paths: list[str | Path]) -> dict[str, Any]:
    """Combine reviewed suite fragments while preserving case order."""

    if not paths:
        raise ValueError("at least one gold suite path is required")
    suites = [load_gold_suite(path) for path in paths]
    cases = [case for suite in suites for case in suite["cases"]]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("gold case ids must be unique across suites")
    return {
        "schema_version": suites[0].get("schema_version"),
        "status": "combined_review_set",
        "source_statuses": [suite.get("status") for suite in suites],
        "cases": cases,
    }


def evaluate_v1_rule_suite(suite: dict[str, Any]) -> dict[str, Any]:
    return _evaluate_suite(suite, system="v1_rule", runner=lambda case: _run_v1(case, use_llm=False))


def evaluate_v1_full_suite(
    suite: dict[str, Any],
    *,
    llm_client: Callable[[str], dict[str, Any]] = call_qwen_intent,
) -> dict[str, Any]:
    return _evaluate_suite(
        suite,
        system="v1_full",
        runner=lambda case: _run_v1(case, use_llm=True, llm_client=llm_client),
    )


def evaluate_v2_suite(
    suite: dict[str, Any],
    *,
    llm_client: DecisionModelV2 | None = None,
    system: str = "v2_rule_only",
) -> dict[str, Any]:
    return _evaluate_suite(
        suite,
        system=system,
        runner=lambda case: _run_v2(case, llm_client=llm_client),
    )


def evaluate_v2_full_suite(suite: dict[str, Any]) -> dict[str, Any]:
    return evaluate_v2_suite(suite, llm_client=call_qwen_decision_v2, system="v2_full")


def _evaluate_suite(
    suite: dict[str, Any],
    *,
    system: str,
    runner: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    category_totals: Counter[str] = Counter()
    category_exact: Counter[str] = Counter()
    unsafe_executions = 0

    for case in suite["cases"]:
        expected = _normalize_decision(case["expected_decision"])
        actual = _normalize_decision(runner(case))
        exact = actual == expected
        action_correct = actual["action"] == expected["action"]
        safe_success = exact or (
            expected["action"] == "clarification" and actual["action"] == "clarification"
        )
        unsafe = expected["action"] in SAFE_EXPECTED_ACTIONS and actual["action"] in EXECUTING_ACTIONS
        unsafe_executions += int(unsafe)

        category = _category_group(str(case["category"]))
        category_totals[category] += 1
        category_exact[category] += int(exact)
        rows.append(
            {
                "id": case["id"],
                "category": category,
                "exact": exact,
                "action_correct": action_correct,
                "safe_success": safe_success,
                "unsafe_execution": unsafe,
                "expected": expected,
                "actual": actual,
            }
        )

    total = len(rows)
    exact_count = sum(row["exact"] for row in rows)
    action_count = sum(row["action_correct"] for row in rows)
    safe_count = sum(row["safe_success"] for row in rows)
    return {
        "suite_schema_version": suite.get("schema_version"),
        "suite_status": suite.get("status"),
        "system": system,
        "total": total,
        "exact_count": exact_count,
        "exact_accuracy": _ratio(exact_count, total),
        "action_count": action_count,
        "action_accuracy": _ratio(action_count, total),
        "safe_success_count": safe_count,
        "safe_success_rate": _ratio(safe_count, total),
        "unsafe_executions": unsafe_executions,
        "categories": {
            category: {
                "total": category_totals[category],
                "exact_count": category_exact[category],
                "exact_accuracy": _ratio(category_exact[category], category_totals[category]),
            }
            for category in sorted(category_totals)
        },
        "cases": rows,
        "failures": [row for row in rows if not row["exact"]],
    }


def compare_system_reports(
    baseline: dict[str, Any],
    contender: dict[str, Any],
) -> dict[str, Any]:
    """Build a paired per-case comparison after both systems have run."""

    if baseline.get("total") != contender.get("total"):
        raise ValueError("comparison reports must contain the same number of cases")
    baseline_cases = {row["id"]: row for row in baseline.get("cases", [])}
    contender_cases = {row["id"]: row for row in contender.get("cases", [])}
    if not baseline_cases or baseline_cases.keys() != contender_cases.keys():
        raise ValueError("comparison reports must contain the same case ids")

    paired: list[dict[str, Any]] = []
    improvements = 0
    regressions = 0
    for case_id, baseline_row in baseline_cases.items():
        contender_row = contender_cases[case_id]
        if not baseline_row["exact"] and contender_row["exact"]:
            change = "improved"
            improvements += 1
        elif baseline_row["exact"] and not contender_row["exact"]:
            change = "regressed"
            regressions += 1
        else:
            change = "unchanged"
        paired.append(
            {
                "id": case_id,
                "category": baseline_row["category"],
                "expected": baseline_row["expected"],
                "baseline_actual": baseline_row["actual"],
                "contender_actual": contender_row["actual"],
                "baseline_exact": baseline_row["exact"],
                "contender_exact": contender_row["exact"],
                "change": change,
            }
        )

    total = int(baseline["total"])
    return {
        "baseline_system": baseline.get("system"),
        "contender_system": contender.get("system"),
        "total": total,
        "baseline_metrics": _metric_snapshot(baseline),
        "contender_metrics": _metric_snapshot(contender),
        "improvements": improvements,
        "regressions": regressions,
        "unchanged": total - improvements - regressions,
        "metric_delta": {
            "exact_accuracy": round(
                float(contender["exact_accuracy"]) - float(baseline["exact_accuracy"]), 4
            ),
            "action_accuracy": round(
                float(contender["action_accuracy"]) - float(baseline["action_accuracy"]), 4
            ),
            "safe_success_rate": round(
                float(contender["safe_success_rate"]) - float(baseline["safe_success_rate"]), 4
            ),
            "unsafe_executions": int(contender["unsafe_executions"])
            - int(baseline["unsafe_executions"]),
        },
        "cases": paired,
    }


def _run_v1(
    case: dict[str, Any],
    *,
    use_llm: bool,
    llm_client: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context = case["context"]
    user_input = case["input"]
    event_type = user_input.get("event_type", "text")
    image_path = "fixture-question.jpg" if event_type == "image" else None
    result = parse_user_intent(
        user_input.get("text"),
        state=context["phase"],
        image_path=image_path,
        candidate_count=int(context.get("candidate_count") or 0),
        question_count=int(context.get("question_count") or 0),
        use_llm=use_llm,
        llm_client=llm_client,
    )
    return _adapt_v1_result(result)


def _run_v2(
    case: dict[str, Any],
    *,
    llm_client: DecisionModelV2 | None,
) -> dict[str, Any]:
    context = ConversationContextV2.from_mapping(case["context"])
    user_input = case["input"]
    decision = decide_intent_v2(
        user_input.get("text"),
        context,
        event_type=user_input.get("event_type", "text"),
        llm_client=llm_client,
    )
    return decision.to_dict()


def _adapt_v1_result(result: IntentResult) -> dict[str, Any]:
    data = result.data
    if result.intent == "unsupported":
        requested_action = data.get("requested_action")
        if requested_action in {"delete", "store", "repair", "cross_chapter_search"}:
            return {"action": "reject", "requested_action": requested_action}
        reason = _legacy_clarification_reason(result.error)
        return {"action": "clarification", "clarification_reason": reason}

    if result.intent == "select_question":
        return {
            "action": "select_question",
            "question_index": data.get("question_index"),
            "chapter_override": data.get("chapter_override"),
        }
    if result.intent == "select_candidate":
        return {"action": "select_candidate", "candidate_rank": data.get("rank")}
    if result.intent == "set_chapter":
        return {
            "action": "set_chapter",
            "chapter_override": data.get("chapter"),
            # V1 has no next-image target; its chapter correction refers to the
            # current task whenever an image is already active.
            "chapter_target": "current_question",
        }
    if result.intent == "search_image":
        return {"action": "search_image"}
    return {"action": result.intent}


def _legacy_clarification_reason(error: str) -> str:
    clean = str(error or "")
    if "超出范围" in clean:
        return "out_of_range"
    if "题号" in clean:
        return "missing_question_index"
    if "候选" in clean:
        return "missing_candidate_rank"
    if "章节" in clean:
        return "missing_chapter"
    return "ambiguous_action"


def _normalize_decision(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {field: payload.get(field) for field in SCORED_FIELDS}
    # Validate every gold/adapter decision against the executable protocol.
    ActionDecisionV2.from_dict({key: value for key, value in normalized.items() if value is not None})
    return normalized


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _category_group(category: str) -> str:
    return CATEGORY_GROUPS.get(category, category)


def _metric_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "exact_count": report["exact_count"],
        "exact_accuracy": report["exact_accuracy"],
        "action_count": report["action_count"],
        "action_accuracy": report["action_accuracy"],
        "safe_success_count": report["safe_success_count"],
        "safe_success_rate": report["safe_success_rate"],
        "unsafe_executions": report["unsafe_executions"],
    }

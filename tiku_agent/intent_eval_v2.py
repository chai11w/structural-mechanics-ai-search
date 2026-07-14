"""Offline Intent V2 evaluation primitives.

The first supported system is the deterministic V1 rule/fallback path.  It
never calls Qwen or question-bank tools.  Later V2 evaluators can reuse the same
gold schema and metrics.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.intent import IntentResult, parse_user_intent


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


def load_gold_suite(path: str | Path) -> dict[str, Any]:
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("gold suite must contain a non-empty cases list")
    ids = [str(case.get("id") or "") for case in cases]
    if any(not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("gold case ids must be non-empty and unique")
    return suite


def evaluate_v1_rule_suite(suite: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    category_totals: Counter[str] = Counter()
    category_exact: Counter[str] = Counter()
    unsafe_executions = 0

    for case in suite["cases"]:
        expected = _normalize_decision(case["expected_decision"])
        actual = _normalize_decision(_run_v1_rule(case))
        exact = actual == expected
        action_correct = actual["action"] == expected["action"]
        safe_success = exact or (
            expected["action"] == "clarification" and actual["action"] == "clarification"
        )
        unsafe = expected["action"] in SAFE_EXPECTED_ACTIONS and actual["action"] in EXECUTING_ACTIONS
        unsafe_executions += int(unsafe)

        category = str(case["category"])
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
        "system": "v1_rule",
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
        "failures": [row for row in rows if not row["exact"]],
    }


def _run_v1_rule(case: dict[str, Any]) -> dict[str, Any]:
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
        use_llm=False,
    )
    return _adapt_v1_result(result)


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

"""Evaluate the independent holdout for first-wave sequential shadow admission.

The runtime admission rule is intentionally unchanged.  Each case still runs
through the real Agent entry with shadow planning off/on, then through the
diagnostic Planner.  Only the diagnostic result is judged as a future-admission
candidate; no plan executes and no business state is mutated by the Planner.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
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
from scripts.evaluate_shadow_admission_qwen_v0 import (
    evaluate_admission_cases,
    load_admission_cases,
    write_results,
)
from tiku_agent.intent_v2 import call_qwen_decision_v2
from tiku_agent.shadow_planner_v0 import ShadowPlannerV0, call_qwen_planner_v0


FIXTURE = BASE / "tests" / "fixtures" / "shadow_sequential_holdout_v0_cases.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_shadow_sequential_holdout_eval_8794"
FUTURE_READY = "future_ready"
FAST_PATH_ONLY = "fast_path_only"
EXPECTATIONS = {FUTURE_READY, FAST_PATH_ONLY}


def load_holdout_cases(path: Path = FIXTURE) -> list[dict[str, Any]]:
    cases = load_admission_cases(path)
    scenarios: set[str] = set()
    for case in cases:
        scenario = str(case.get("scenario") or "").strip()
        expectation = str(case.get("future_expectation") or "").strip()
        if not scenario or expectation not in EXPECTATIONS:
            raise ValueError(f"holdout case lacks scenario/expectation: {case['id']}")
        case["scenario"] = scenario
        case["future_expectation"] = expectation
        scenarios.add(scenario)
    if "atomic_guard" not in scenarios:
        raise ValueError("holdout fixture must include atomic guards")
    return cases


def attach_holdout_metadata(
    records: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metadata = {str(case["id"]): case for case in cases}
    for record in records:
        case = metadata[str(record["case_id"])]
        record["scenario"] = case["scenario"]
        record["future_expectation"] = case["future_expectation"]
        record["holdout_ok"] = (
            bool(record["diagnostic"].get("future_admission_ready"))
            if case["future_expectation"] == FUTURE_READY
            else not bool(record["current_admitted"])
        )
    return records


def summarize_holdout(records: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [record for record in records if record["future_expectation"] == FUTURE_READY]
    atomic = [record for record in records if record["future_expectation"] == FAST_PATH_ONLY]
    scenarios: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "future_ready": 0, "goal_covered": 0, "holdout_ok": 0}
    )
    for record in positives:
        item = scenarios[str(record["scenario"])]
        item["total"] += 1
        item["future_ready"] += int(bool(record["diagnostic"].get("future_admission_ready")))
        item["goal_covered"] += int(bool(record["diagnostic"].get("goal_covered")))
        item["holdout_ok"] += int(bool(record["holdout_ok"]))

    stable_scenarios = sorted(
        scenario for scenario, counts in scenarios.items()
        if counts["total"] > 0 and counts["holdout_ok"] == counts["total"]
    )
    expected_scenarios = sorted(scenarios)
    summary = {
        "total": len(records),
        "positive_total": len(positives),
        "positive_future_ready": sum(
            bool(record["diagnostic"].get("future_admission_ready")) for record in positives
        ),
        "positive_future_ready_rate": round(
            sum(bool(record["diagnostic"].get("future_admission_ready")) for record in positives)
            / len(positives),
            4,
        ) if positives else 0.0,
        "scenario_results": dict(scenarios),
        "stable_scenarios": stable_scenarios,
        "expected_scenarios": expected_scenarios,
        "atomic_false_admissions": sum(bool(record["current_admitted"]) for record in atomic),
        "observable_differences": sum(not bool(record["observable_equal"]) for record in records),
        "effective_forbidden": sum(
            bool(record["diagnostic"].get("effective_forbidden_actions")) for record in records
        ),
        "planner_unavailable": sum(
            record["diagnostic"].get("route") == "planner_unavailable" for record in records
        ),
    }
    summary["candidate_ready"] = (
        bool(positives)
        and summary["positive_future_ready"] == summary["positive_total"]
        and stable_scenarios == expected_scenarios
        and summary["atomic_false_admissions"] == 0
        and summary["observable_differences"] == 0
        and summary["effective_forbidden"] == 0
        and summary["planner_unavailable"] == 0
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate sequential shadow-admission holdout")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")
    if args.runs < 1:
        raise SystemExit("--runs must be positive")

    cases = load_holdout_cases(args.fixture)
    intent_client: Callable[[str], dict[str, Any]] = lambda prompt: call_qwen_decision_v2(
        prompt, model=args.model, endpoint=args.endpoint
    )
    planner_factory = lambda: ShadowPlannerV0(
        lambda prompt: call_qwen_planner_v0(prompt, model=args.model, endpoint=args.endpoint)
    )
    records = evaluate_admission_cases(
        cases,
        runs=args.runs,
        intent_model_client=intent_client,
        planner_factory=planner_factory,
        progress=lambda completed, total, record: print(
            f"[{completed}/{total}] run={record['run']} case={record['case_id']} "
            f"entry={record['current_route']} ready={record['diagnostic'].get('future_admission_ready', False)}"
        ),
    )
    attach_holdout_metadata(records, cases)
    summary = summarize_holdout(records)
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    write_results(output_dir, records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir}")
    return 0 if not args.strict or summary["candidate_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Evaluate deterministic chapter-scope classification against a gold suite."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiku_shared.chapter_catalog import parse_chapter_scope, resolve_image_scope  # noqa: E402


DEFAULT_SUITE = ROOT / "tests" / "fixtures" / "chapter_scope_eval_v1.json"
CORE_RESULT_FIELDS = ("status", "topic_id", "storage_key")


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("chapter scope suite must contain a cases list")
    return payload


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    entry = str(case.get("entry") or "image")
    inputs = dict(case.get("input") or {})
    if entry == "image":
        result = resolve_image_scope(
            inputs.get("chapter_hint", ""),
            inputs.get("chapter_confidence", 0.0),
            inputs.get("visible_problem_text", ""),
        )
    elif entry == "chapter_reply":
        result = parse_chapter_scope(inputs.get("text", ""))
    else:
        raise ValueError(f"unsupported evaluation entry: {entry}")

    actual = {field: getattr(result, field) for field in CORE_RESULT_FIELDS}
    expected = dict(case.get("expected") or {})
    mismatches = {
        field: {"expected": expected[field], "actual": actual[field]}
        for field in CORE_RESULT_FIELDS
        if field in expected and expected[field] != actual[field]
    }
    expected_reason = expected.get("reason")
    if expected_reason is not None and expected_reason != result.reason:
        mismatches["reason"] = {"expected": expected_reason, "actual": result.reason}

    return {
        "id": str(case.get("id") or ""),
        "category": str(case.get("category") or "uncategorized"),
        "entry": entry,
        "passed": not mismatches,
        "actual": {**actual, "reason": result.reason, "matched_text": result.matched_text},
        "mismatches": mismatches,
    }


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    cases = [evaluate_case(case) for case in suite["cases"]]
    by_category: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for case in cases:
        counts = by_category[case["category"]]
        counts["total"] += 1
        counts["passed"] += int(case["passed"])
    failed = [case for case in cases if not case["passed"]]
    total = len(cases)
    passed = total - len(failed)
    return {
        "schema_version": suite.get("schema_version", "unknown"),
        "suite_status": suite.get("status", "unknown"),
        "total": total,
        "passed": passed,
        "failed": len(failed),
        "pass_rate": (passed / total if total else 1.0),
        "by_category": dict(sorted(by_category.items())),
        "failures": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    report = evaluate_suite(load_suite(args.suite))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

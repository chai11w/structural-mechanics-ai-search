"""Evaluate the pure-code first-wave sequential classifier without a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiku_agent.shadow_sequential_admission_v0 import (
    REPORT_THEN_SHOW,
    SHOW_THEN_SELECT,
    classify_sequential_shadow_admission,
)


FIXTURE_DIR = ROOT / "tests" / "fixtures"
SPLITS = {
    "development": FIXTURE_DIR / "shadow_admission_v0_cases.json",
    "holdout": FIXTURE_DIR / "shadow_sequential_holdout_v0_cases.json",
    "confirmation": FIXTURE_DIR / "shadow_sequential_confirmation_v0_cases.json",
}
DEVELOPMENT_POSITIVE_IDS = {"seq_show_then_select", "seq_report_then_show"}
FIRST_WAVE_SCENARIOS = {SHOW_THEN_SELECT, REPORT_THEN_SHOW}


def evaluate_split(name: str, path: Path) -> dict[str, Any]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    records = []
    for case in cases:
        expected = (
            case["id"] in DEVELOPMENT_POSITIVE_IDS
            if name == "development"
            else case.get("scenario") in FIRST_WAVE_SCENARIOS
        )
        decision = classify_sequential_shadow_admission(case["text"], phase=case["phase"])
        records.append(
            {
                "id": case["id"],
                "expected": expected,
                "actual": decision.admitted,
                "scenario": decision.scenario,
                "code": decision.code,
            }
        )
    positives = [record for record in records if record["expected"]]
    negatives = [record for record in records if not record["expected"]]
    false_negatives = [record["id"] for record in positives if not record["actual"]]
    false_positives = [record["id"] for record in negatives if record["actual"]]
    return {
        "split": name,
        "total": len(records),
        "positive": len(positives),
        "negative": len(negatives),
        "false_negative_ids": false_negatives,
        "false_positive_ids": false_positives,
        "passed": not false_negatives and not false_positives,
    }


def evaluate_all() -> dict[str, Any]:
    splits = [evaluate_split(name, path) for name, path in SPLITS.items()]
    return {
        "offline_only": True,
        "runtime_wired": True,
        "splits": splits,
        "total": sum(item["total"] for item in splits),
        "positive": sum(item["positive"] for item in splits),
        "negative": sum(item["negative"] for item in splits),
        "passed": all(item["passed"] for item in splits),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    report = evaluate_all()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

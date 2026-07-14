"""Run rule-only diagnostics or paired live Intent V1/V2 evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiku_agent.intent_eval_v2 import (  # noqa: E402
    compare_system_reports,
    evaluate_v1_full_suite,
    evaluate_v1_rule_suite,
    evaluate_v2_full_suite,
    evaluate_v2_suite,
    load_gold_suites,
)


DEFAULT_SUITES = (
    ROOT / "tests" / "fixtures" / "intent_v2_gold_review_01.json",
    ROOT / "tests" / "fixtures" / "intent_v2_gold_review_02.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        action="append",
        help="Gold suite fragment; repeat to combine. Defaults to the 40-case review set.",
    )
    parser.add_argument(
        "--system",
        choices=("v1-rule", "v2-rule", "compare-rules", "compare-live"),
        default="compare-rules",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()

    suite = load_gold_suites(args.suite or list(DEFAULT_SUITES))
    if args.system == "v1-rule":
        report = evaluate_v1_rule_suite(suite)
    elif args.system == "v2-rule":
        report = evaluate_v2_suite(suite)
    elif args.system == "compare-live":
        report = compare_system_reports(
            evaluate_v1_full_suite(suite),
            evaluate_v2_full_suite(suite),
        )
    else:
        report = compare_system_reports(
            evaluate_v1_rule_suite(suite),
            evaluate_v2_suite(suite),
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

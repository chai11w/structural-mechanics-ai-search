"""Bounded, strictly read-only diagnostics for one Agent runtime root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if not sys.path or sys.path[0] != str(BASE):
    sys.path.insert(0, str(BASE))

from tiku_diagnostics import (
    ASSOCIATION_MODES,
    DiagnosticQueryError,
    DiagnosticQueryService,
    QuerySpec,
    compare_diagnostic_bundles,
)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a bounded privacy-safe diagnostic package without modifying runtime data"
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--trace-id")
    selector.add_argument("--response-id")
    selector.add_argument("--feedback-id")
    selector.add_argument(
        "--identity-key",
        help="Stable privacy-safe identity key; raw invitation codes are rejected",
    )
    parser.add_argument("--since", help="Inclusive ISO-8601 start; required for identity queries")
    parser.add_argument("--until", help="Exclusive ISO-8601 end; required for identity queries")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--association-mode",
        choices=tuple(sorted(ASSOCIATION_MODES)),
        default="authoritative-first",
        help="Diagnostic read-side policy only; never changes the business service",
    )
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help=(
            "Compare fixed authoritative-only and legacy-only read views; "
            "never changes runtime writers"
        ),
    )
    parser.add_argument("--format", choices=("human", "json"), default="human")
    return parser


def format_human(package: dict[str, object]) -> str:
    query = package["query"]
    summary = package["summary"]
    lines = [
        f"诊断对象: {query['selector']}={query['value']}",
        (
            "摘要: "
            f"trace={summary['trace_count']} event={summary['trace_event_count']} "
            f"response={summary['response_count']} feedback={summary['feedback_count']}"
        ),
        f"完整性: {'complete' if summary['complete'] else 'incomplete'}",
    ]
    gaps = summary.get("evidence_gaps") or []
    if gaps:
        lines.append("证据缺失: " + ", ".join(str(value) for value in gaps))
    lines.append("时间线:")
    for item in package.get("timeline", []):
        record = item["record"]
        label = (
            record.get("event_type")
            or record.get("code")
            or record.get("feedback_scope")
            or "record"
        )
        lines.append(
            f"- {item['timestamp']} [{item['source']}/{item['association']}] {label}"
        )
    return "\n".join(lines)


def format_comparison_human(package: dict[str, object]) -> str:
    query = package["query"]
    comparison = package["comparison"]
    summary = comparison["summary"]
    lines = [
        f"对照对象: {query['selector']}={query['value']}",
        f"分类: {comparison['classification']}",
        (
            "证据: "
            f"authoritative={summary['authoritative_count']} "
            f"legacy={summary['legacy_count']} "
            f"authoritative_only={summary['authoritative_only_count']} "
            f"legacy_only={summary['legacy_only_count']}"
        ),
    ]
    gaps = comparison.get("evidence_gaps") or []
    if gaps:
        lines.append("证据缺失: " + ", ".join(str(value) for value in gaps))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        if args.compare_legacy and args.association_mode != "authoritative-first":
            raise DiagnosticQueryError(
                "--compare-legacy uses fixed authoritative-only and legacy-only views"
            )
        service = DiagnosticQueryService(args.runtime_root)
        common = {
            "trace_id": args.trace_id or "",
            "response_id": args.response_id or "",
            "feedback_id": args.feedback_id or "",
            "identity_key": args.identity_key or "",
            "since": args.since or "",
            "until": args.until or "",
            "limit": args.limit,
        }
        if args.compare_legacy:
            authoritative = service.query(
                QuerySpec(**common, association_mode="authoritative-only")
            )
            legacy = service.query(
                QuerySpec(**common, association_mode="legacy-only")
            )
            package = {
                "schema_version": 1,
                "query": {
                    **authoritative["query"],
                    "association_mode": "comparison",
                },
                "runtime": authoritative["runtime"],
                "comparison": compare_diagnostic_bundles(
                    authoritative, legacy
                ).to_dict(),
            }
        else:
            package = service.query(
                QuerySpec(**common, association_mode=args.association_mode)
            )
    except DiagnosticQueryError as exc:
        print(f"diagnostic query rejected: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(package, ensure_ascii=False, separators=(",", ":")))
    else:
        print(
            format_comparison_human(package)
            if args.compare_legacy
            else format_human(package)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

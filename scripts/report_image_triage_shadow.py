"""Read-only summary for the isolated 8890 image-triage shadow log."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from math import ceil
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_validation_8890"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize 8890 image-triage shadow observations")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument(
        "--log",
        type=Path,
        action="append",
        help="Read an explicit JSONL log; may be repeated (default: runtime triage_shadow.jsonl)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def load_report(paths: list[Path]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    missing: list[str] = []
    malformed = 0
    for path in paths:
        if not path.is_file():
            missing.append(str(path))
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                malformed += 1

    statuses = Counter(str(record.get("status") or "unknown") for record in records)
    candidates = Counter(str(record.get("route_candidate") or "unknown") for record in records)
    final_routes = Counter(str(record.get("final_route") or "unknown") for record in records)
    error_kinds = Counter(
        str(record.get("error_kind") or "unknown")
        for record in records
        if str(record.get("status") or "") != "ok"
    )
    durations = [_non_negative_int(record.get("duration_ms")) for record in records]
    successful_durations = [
        _non_negative_int(record.get("duration_ms"))
        for record in records
        if str(record.get("status") or "") == "ok"
    ]
    return {
        "status": "ok" if records else "no_data",
        "log_count": len(paths),
        "logs": [str(path) for path in paths],
        "missing_logs": missing,
        "record_count": len(records),
        "status_counts": dict(sorted(statuses.items())),
        "route_candidate_counts": dict(sorted(candidates.items())),
        "final_route_counts": dict(sorted(final_routes.items())),
        "error_counts": dict(sorted(error_kinds.items())),
        "malformed_line_count": malformed,
        "latency_ms": _latency_summary(successful_durations),
        "token_totals": {
            "prompt_tokens": sum(_non_negative_int(record.get("prompt_tokens")) for record in records),
            "completion_tokens": sum(
                _non_negative_int(record.get("completion_tokens")) for record in records
            ),
            "total_tokens": sum(_non_negative_int(record.get("total_tokens")) for record in records),
        },
    }


def _latency_summary(values: list[int]) -> dict[str, int | float]:
    if not values:
        return {"count": 0, "average_ms": 0, "p50_ms": 0, "p95_ms": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "average_ms": round(sum(ordered) / len(ordered), 1),
        "p50_ms": _percentile(ordered, 0.50),
        "p95_ms": _percentile(ordered, 0.95),
    }


def _percentile(values: list[int], fraction: float) -> int:
    index = min(len(values) - 1, max(0, ceil(len(values) * fraction) - 1))
    return values[index]


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def print_text(report: dict[str, object]) -> None:
    if report.get("status") != "ok":
        missing = report.get("missing_logs") or []
        print("尚无影子预检记录。" + (f" 未找到：{', '.join(missing)}" if missing else ""))
        return
    print(f"影子预检共 {report['record_count']} 条记录。")
    print(f"建议路线：{_format_counts(report['route_candidate_counts'])}")
    print(f"复核路线：{_format_counts(report['final_route_counts'])}")
    print(f"状态：{_format_counts(report['status_counts'])}")
    latency = report["latency_ms"]
    print(
        f"成功耗时：平均 {latency['average_ms']} 毫秒，"
        f"P50 {latency['p50_ms']} 毫秒，P95 {latency['p95_ms']} 毫秒"
    )
    tokens = report["token_totals"]
    print(
        f"令牌用量：输入 {tokens['prompt_tokens']}，输出 {tokens['completion_tokens']}，"
        f"合计 {tokens['total_tokens']}"
    )
    if report["error_counts"]:
        print(f"错误：{_format_counts(report['error_counts'])}")
    if report["malformed_line_count"]:
        print(f"无法解析的记录行：{report['malformed_line_count']}")


def _format_counts(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "无"
    return "，".join(f"{key} {count}" for key, count in value.items())


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    paths = [path.resolve() for path in args.log] if args.log else [
        args.runtime_dir.resolve() / "triage_shadow.jsonl"
    ]
    report = load_report(paths)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

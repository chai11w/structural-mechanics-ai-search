"""Read-only cost summary for one isolated Agent runtime."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3


BASE = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_prod_8790"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize recorded Qwen and Zhipu model costs")
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--days", type=int, default=7, help="Inclusive lookback window (default: 7)")
    parser.add_argument("--run-id", help="Show one turn and its individual model calls")
    parser.add_argument("--daily-budget", type=float, help="Optional CNY budget used for 50/80/100%% alerts")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def load_report(
    database: Path,
    *,
    days: int,
    run_id: str = "",
    daily_budget: float | None = None,
    now: datetime | None = None,
) -> dict:
    if not database.is_file():
        return {"status": "no_data", "database": str(database), "message": "尚无费用记录。"}
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=max(1, days))).isoformat()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        if not _table_exists(connection, "model_cost_runs"):
            return {"status": "no_data", "database": str(database), "message": "尚无费用记录。"}
        if run_id:
            return _run_detail(connection, run_id)
        runs = connection.execute(
            "SELECT * FROM model_cost_runs WHERE started_at >= ? ORDER BY started_at DESC",
            (since,),
        ).fetchall()
        calls = connection.execute(
            """
            SELECT provider, model, SUM(attempt_count) AS call_count,
                   SUM(input_tokens) AS input_tokens,
                   SUM(image_tokens) AS image_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(estimated_cost_micros) AS estimated_cost_micros
            FROM model_cost_calls
            WHERE started_at >= ?
            GROUP BY provider, model
            ORDER BY estimated_cost_micros DESC
            """,
            (since,),
        ).fetchall()
    costs = sorted(int(row["estimated_cost_micros"]) for row in runs)
    p95 = costs[min(len(costs) - 1, max(0, int(len(costs) * 0.95)))] if costs else 0
    total_micros = sum(costs)
    searches: dict[str, dict[str, int]] = {}
    for row in runs:
        key = str(row["search_key"] or "")
        if not key:
            continue
        item = searches.setdefault(key, {"cost": 0, "calls": 0, "tokens": 0})
        item["cost"] += int(row["estimated_cost_micros"])
        item["calls"] += int(row["call_count"])
        item["tokens"] += int(row["total_tokens"])
    search_costs = sorted(item["cost"] for item in searches.values())
    search_p95 = (
        search_costs[min(len(search_costs) - 1, max(0, int(len(search_costs) * 0.95)))]
        if search_costs else 0
    )
    anomaly_threshold = search_p95 * 2 if len(search_costs) >= 50 and search_p95 > 0 else 0
    anomalous_searches = [
        key for key, item in searches.items()
        if anomaly_threshold and item["cost"] > anomaly_threshold
    ]
    warnings: dict[str, int] = {}
    for row in runs:
        for code in json.loads(row["warning_codes_json"] or "[]"):
            warnings[code] = warnings.get(code, 0) + 1
    daily = _daily_totals(runs, daily_budget)
    return {
        "status": "ok",
        "database": str(database),
        "days": max(1, days),
        "run_count": len(runs),
        "model_call_count": sum(int(row["call_count"]) for row in runs),
        "total_tokens": sum(int(row["total_tokens"]) for row in runs),
        "estimated_cost_cny": _cny(total_micros),
        "average_cost_cny": _cny(round(total_micros / len(runs))) if runs else 0.0,
        "p95_cost_cny": _cny(p95),
        "search_count": len(searches),
        "average_search_cost_cny": (
            _cny(round(sum(search_costs) / len(search_costs))) if search_costs else 0.0
        ),
        "p95_search_cost_cny": _cny(search_p95),
        "cost_anomaly_baseline_ready": len(search_costs) >= 50,
        "cost_anomaly_threshold_cny": _cny(anomaly_threshold),
        "cost_anomaly_searches": anomalous_searches,
        "warnings": warnings,
        "by_model": [
            {
                **dict(row),
                "estimated_cost_cny": _cny(int(row["estimated_cost_micros"] or 0)),
            }
            for row in calls
        ],
        "daily": daily,
        "highest_cost_runs": [
            {
                "run_id": row["run_id"],
                "started_at": row["started_at"],
                "outcome": row["outcome"],
                "call_count": row["call_count"],
                "estimated_cost_cny": _cny(row["estimated_cost_micros"]),
                "warning_codes": json.loads(row["warning_codes_json"] or "[]"),
            }
            for row in sorted(runs, key=lambda item: item["estimated_cost_micros"], reverse=True)[:5]
        ],
        "highest_cost_searches": [
            {
                "search_key": key,
                "model_call_count": item["calls"],
                "total_tokens": item["tokens"],
                "estimated_cost_cny": _cny(item["cost"]),
            }
            for key, item in sorted(searches.items(), key=lambda pair: pair[1]["cost"], reverse=True)[:5]
        ],
    }


def _run_detail(connection: sqlite3.Connection, run_id: str) -> dict:
    run = connection.execute("SELECT * FROM model_cost_runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return {"status": "not_found", "run_id": run_id}
    calls = connection.execute(
        "SELECT * FROM model_cost_calls WHERE run_id = ? ORDER BY sequence", (run_id,)
    ).fetchall()
    payload = dict(run)
    payload["estimated_cost_cny"] = _cny(payload["estimated_cost_micros"])
    payload["warning_codes"] = json.loads(payload.pop("warning_codes_json") or "[]")
    return {
        "status": "ok",
        "run": payload,
        "calls": [
            {**dict(row), "estimated_cost_cny": _cny(row["estimated_cost_micros"])}
            for row in calls
        ],
    }


def _daily_totals(runs: list[sqlite3.Row], daily_budget: float | None) -> list[dict]:
    totals: dict[str, int] = {}
    for row in runs:
        day = str(row["started_at"])[:10]
        totals[day] = totals.get(day, 0) + int(row["estimated_cost_micros"])
    output = []
    budget_micros = round(float(daily_budget) * 1_000_000) if daily_budget and daily_budget > 0 else 0
    for day, total in sorted(totals.items(), reverse=True):
        ratio = total / budget_micros if budget_micros else 0.0
        level = ""
        if ratio >= 1:
            level = "DAILY_BUDGET_100"
        elif ratio >= 0.8:
            level = "DAILY_BUDGET_80"
        elif ratio >= 0.5:
            level = "DAILY_BUDGET_50"
        output.append({"date": day, "estimated_cost_cny": _cny(total), "budget_alert": level})
    return output


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _cny(micros: int) -> float:
    return round(int(micros or 0) / 1_000_000, 6)


def print_text(report: dict) -> None:
    if report.get("status") != "ok":
        print(report.get("message") or f"未找到运行记录：{report.get('run_id', '')}")
        return
    if "run" in report:
        run = report["run"]
        print(f"运行 {run['run_id']}：{run['call_count']} 次模型调用，约 ¥{run['estimated_cost_cny']:.6f}")
        for call in report["calls"]:
            print(
                f"- {call['provider']}/{call['model']} {call['call_type']} "
                f"tokens={call['total_tokens']} cost=¥{call['estimated_cost_cny']:.6f} {call['status']}"
            )
        return
    print(
        f"最近 {report['days']} 天：{report['run_count']} 个运行，"
        f"{report['model_call_count']} 次模型调用，约 ¥{report['estimated_cost_cny']:.6f}"
    )
    print(f"平均每运行 ¥{report['average_cost_cny']:.6f}，P95 ¥{report['p95_cost_cny']:.6f}")
    if report["search_count"]:
        print(
            f"{report['search_count']} 次搜题，平均每次 ¥{report['average_search_cost_cny']:.6f}，"
            f"搜题P95 ¥{report['p95_search_cost_cny']:.6f}"
        )
    for item in report["by_model"]:
        print(
            f"- {item['provider']}/{item['model']}: {item['call_count']} 次，"
            f"{item['total_tokens']} tokens，约 ¥{item['estimated_cost_cny']:.6f}"
        )
    if report["warnings"]:
        print("异常：" + "，".join(f"{key}={value}" for key, value in report["warnings"].items()))


def main() -> int:
    args = build_parser().parse_args()
    database = args.runtime_dir.resolve() / "model_costs.sqlite3"
    report = load_report(
        database,
        days=args.days,
        run_id=str(args.run_id or ""),
        daily_budget=args.daily_budget,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

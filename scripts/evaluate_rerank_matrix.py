"""Compare rerank prompt/provider combinations on the fixed shape-pair set.

The script is intentionally opt-in: importing it does not call a provider.
Running it sends the selected query/candidate images to the configured
providers and writes raw per-case results plus recall, false-high, separation,
per-query Top-1, and estimated cost metrics to an isolated temporary directory.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from zhipuai import ZhipuAI

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import search
from scripts.evaluate_shape_rerank_prompt import PAIRS
from tiku_shared.model_costs import (
    ModelCostCollector,
    model_cost_scope,
    submit_with_model_cost_context,
)


PROMPT_FILES = {
    "v2.1": BASE / "tiku_agent" / "prompts" / "rerank_shape_v2_1_candidate.txt",
    "v2.2": BASE / "tiku_agent" / "prompts" / "rerank_shape_v2_2_candidate.txt",
    "v3": BASE / "tiku_agent" / "prompts" / "rerank_shape_v3_candidate.txt",
    "v4": BASE / "tiku_agent" / "prompts" / "rerank_shape_v4_candidate.txt",
}


def prompt_text(name: str) -> str:
    if name == "v1":
        return search.SHAPE_RERANK_PROMPT
    path = PROMPT_FILES.get(name)
    if path is None or not path.is_file():
        raise ValueError(f"unknown or missing prompt: {name}")
    return path.read_text(encoding="utf-8")


def score_one(provider: str, model: str, prompt: str, query: Path, candidate: Path, timeout: float):
    started = time.perf_counter()
    client = ZhipuAI(api_key=search.ZHIPUAI_API_KEY) if provider == "zhipu" else None
    score, reason = search.score_candidate_pair(
        client,
        str(query),
        str(candidate),
        prompt=prompt,
        timeout_seconds=timeout,
        model=model,
        provider=provider,
    )
    return {
        "score": score,
        "reason": reason,
        "seconds": round(time.perf_counter() - started, 3),
        "ok": True,
    }


def run_case(case, timeout: float):
    order, repeat, prompt_name, prompt, provider, model, pair, query, candidate = case
    result = {"ok": False, "score": None, "reason": "", "seconds": 0.0}
    started = time.perf_counter()
    try:
        result = score_one(provider, model, prompt, query, candidate, timeout)
    except Exception as exc:  # noqa: BLE001 - preserve per-case failures.
        result.update(
            {
                "reason": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
    return {
        "order": order,
        "repeat": repeat,
        "prompt": prompt_name,
        "provider": provider,
        "model": model,
        "name": pair.name,
        "same_shape": pair.same_shape,
        "query": str(query),
        "candidate": str(candidate),
        "result": result,
    }


def summarize(rows, *, same_threshold=0.8, false_high_threshold=0.9):
    summary = {}
    for row in rows:
        key = (row["prompt"], row["provider"], row["model"])
        bucket = summary.setdefault(
            key,
            {
                "count": 0,
                "parsed": 0,
                "same": [],
                "different": [],
                "seconds": [],
                "queries": {},
            },
        )
        bucket["count"] += 1
        result = row["result"]
        if result.get("ok"):
            bucket["parsed"] += 1
            bucket["seconds"].append(result["seconds"])
            bucket["same" if row["same_shape"] else "different"].append(result["score"])
            query_bucket = bucket["queries"].setdefault(
                row["query"],
                {"same": [], "different": []},
            )
            query_bucket["same" if row["same_shape"] else "different"].append(result["score"])

    output = []
    for (prompt, provider, model), bucket in summary.items():
        same = bucket["same"]
        different = bucket["different"]
        ranked_queries = [
            query
            for query in bucket["queries"].values()
            if query["same"] and query["different"]
        ]
        top1_hits = sum(
            max(query["same"]) > max(query["different"])
            for query in ranked_queries
        )
        output.append(
            {
                "prompt": prompt,
                "provider": provider,
                "model": model,
                "count": bucket["count"],
                "parsed": bucket["parsed"],
                "parse_rate": round(bucket["parsed"] / bucket["count"], 3)
                if bucket["count"]
                else None,
                "same_avg": round(sum(same) / len(same), 3) if same else None,
                "different_avg": round(sum(different) / len(different), 3) if different else None,
                "separation": round((sum(same) / len(same)) - (sum(different) / len(different)), 3)
                if same and different
                else None,
                "same_recall": round(
                    sum(score >= same_threshold for score in same) / len(same),
                    3,
                )
                if same
                else None,
                "different_false_high_rate": round(
                    sum(score >= false_high_threshold for score in different) / len(different),
                    3,
                )
                if different
                else None,
                "ranked_query_count": len(ranked_queries),
                "top1_hit_rate": round(top1_hits / len(ranked_queries), 3)
                if ranked_queries
                else None,
                "avg_seconds": round(sum(bucket["seconds"]) / len(bucket["seconds"]), 3)
                if bucket["seconds"]
                else None,
            }
        )
    return output


def summarize_costs(collector):
    records = collector.records()
    by_provider = {}
    for record in records:
        key = (record.provider, record.model)
        bucket = by_provider.setdefault(
            key,
            {"calls": 0, "estimated_cost_micros": 0, "priced_calls": 0},
        )
        bucket["calls"] += 1
        bucket["estimated_cost_micros"] += record.estimated_cost_micros
        bucket["priced_calls"] += record.pricing_status == "priced"
    return {
        "recorded_calls": len(records),
        "estimated_cost_cny": round(
            sum(record.estimated_cost_micros for record in records) / 1_000_000,
            6,
        ),
        "priced_calls": sum(record.pricing_status == "priced" for record in records),
        "by_provider": [
            {
                "provider": provider,
                "model": model,
                "calls": bucket["calls"],
                "priced_calls": bucket["priced_calls"],
                "estimated_cost_cny": round(bucket["estimated_cost_micros"] / 1_000_000, 6),
            }
            for (provider, model), bucket in sorted(by_provider.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare rerank prompts and providers on fixed image pairs.")
    parser.add_argument(
        "--prompts",
        nargs="+",
        default=["v1", "v4"],
        help="要比较的 Prompt 版本，默认只比较当前 V1 与最新 V4",
    )
    parser.add_argument("--providers", nargs="+", choices=("zhipu", "qwen"), default=("zhipu", "qwen"))
    parser.add_argument("--qwen-model", default=search.DEFAULT_QWEN_RERANK_MODEL)
    parser.add_argument("--zhipu-model", default=search.DEFAULT_ZHIPU_RERANK_MODEL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="并发调用上限，默认 10；只影响评测，不改变生产复筛策略",
    )
    parser.add_argument(
        "--same-threshold",
        type=float,
        default=0.8,
        help="同类候选达到该分数才计入 same_recall",
    )
    parser.add_argument(
        "--false-high-threshold",
        type=float,
        default=0.9,
        help="异类候选达到该分数计入 false_high_rate",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查样本、组合和调用数量，不发送题图")
    parser.add_argument(
        "--output",
        default=str(BASE / ".tmp_rerank_matrix" / datetime.now().strftime("%Y%m%d_%H%M%S") / "results.json"),
    )
    args = parser.parse_args()

    if "zhipu" in args.providers and not args.dry_run and not search.ZHIPUAI_API_KEY:
        raise RuntimeError("ZHIPUAI_API_KEY is not configured")
    if "qwen" in args.providers and not args.dry_run and not search.DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY is not configured")
    if args.repeat < 1:
        raise ValueError("--repeat must be >= 1")
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    prompts = {name: prompt_text(name) for name in args.prompts}
    expected_calls = len(PAIRS) * len(prompts) * len(args.providers) * args.repeat
    if args.dry_run:
        missing = [
            str(search.ROOT / pair.query)
            for pair in PAIRS
            if not (search.ROOT / pair.query).is_file()
        ] + [
            str(search.ROOT / pair.candidate)
            for pair in PAIRS
            if not (search.ROOT / pair.candidate).is_file()
        ]
        print(json.dumps({"prompts": list(prompts), "providers": args.providers, "repeat": args.repeat, "workers": args.workers, "expected_calls": expected_calls, "missing_images": missing}, ensure_ascii=False, indent=2))
        return 0 if not missing else 2

    cases = []
    order = 0
    for repeat in range(args.repeat):
        for prompt_name, prompt in prompts.items():
            for provider in args.providers:
                model = args.qwen_model if provider == "qwen" else args.zhipu_model
                for pair in PAIRS:
                    query = search.ROOT / pair.query
                    candidate = search.ROOT / pair.candidate
                    cases.append(
                        (
                            order,
                            repeat + 1,
                            prompt_name,
                            prompt,
                            provider,
                            model,
                            pair,
                            query,
                            candidate,
                        )
                    )
                    order += 1

    rows = []
    collector = ModelCostCollector(
        run_id=f"rerank-matrix-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    with model_cost_scope(collector):
        with ThreadPoolExecutor(max_workers=min(args.workers, len(cases))) as executor:
            futures = [
                submit_with_model_cost_context(executor, run_case, case, args.timeout)
                for case in cases
            ]
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                result = row["result"]
                print(
                    f"{row['prompt']}/{row['provider']}/{row['name']}: "
                    f"{result['score']} {result['seconds']}s {result['reason']}"
                )
    rows.sort(key=lambda row: row["order"])
    for row in rows:
        row.pop("order", None)

    payload = {
        "prompts": list(prompts),
        "providers": args.providers,
        "repeat": args.repeat,
        "workers": args.workers,
        "pair_count": len(PAIRS),
        "same_threshold": args.same_threshold,
        "false_high_threshold": args.false_high_threshold,
        "cost_summary": summarize_costs(collector),
        "summary": summarize(
            rows,
            same_threshold=args.same_threshold,
            false_high_threshold=args.false_high_threshold,
        ),
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"saved={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

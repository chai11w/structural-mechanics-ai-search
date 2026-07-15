"""Compare serial and experimental concurrent rerank on symbolic-bank samples.

This script writes only under `.tmp_tiku_agent` and does not change the default
search flow or Feishu runtime state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import search
from multi_agent_pipeline import SYMBOLIC_RERANK_MIN_SCORE, symbolic_root


@dataclass
class Sample:
    chapter: str
    query_path: str
    structure_type: str
    candidate_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare serial vs concurrent shape rerank on symbolic-bank samples.")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--candidate-limit", type=int, default=8)
    parser.add_argument("--rerank-top", type=int, default=search.DISPLAY_MAX_RESULTS)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--candidate-timeout", type=float, default=None)
    parser.add_argument("--retry-timeout", type=float, default=None)
    parser.add_argument("--retry-max-candidates", type=int, default=0)
    parser.add_argument("--chapter", help="Run one explicit symbolic-bank chapter.")
    parser.add_argument("--query-path", help="Run one explicit query path stored in that chapter Excel.")
    parser.add_argument("--concurrent-only", action="store_true")
    parser.add_argument("--output", default=str(BASE_DIR / ".tmp_tiku_agent" / "concurrent_rerank_eval.json"))
    args = parser.parse_args()

    symbolic = symbolic_root(search.ROOT)
    if bool(args.chapter) != bool(args.query_path):
        parser.error("--chapter and --query-path must be supplied together")
    if args.chapter:
        samples = [
            Sample(
                args.chapter,
                args.query_path,
                structure_type_for(symbolic, args.chapter, args.query_path),
                len(build_candidates(symbolic, args.chapter, args.query_path, args.candidate_limit)),
            )
        ]
    else:
        samples = choose_samples(symbolic, args.samples, args.candidate_limit)
    if not samples:
        raise RuntimeError(f"No symbolic samples found under {symbolic}")

    rows = []
    for sample in samples:
        query = resolve_image(sample.query_path)
        candidates = build_candidates(symbolic, sample.chapter, sample.query_path, args.candidate_limit)
        if not candidates:
            continue

        serial = []
        serial_seconds = None
        if not args.concurrent_only:
            serial_start = time.perf_counter()
            serial = search.rerank_candidates(str(query), candidates, top_n=args.rerank_top)
            serial_seconds = time.perf_counter() - serial_start

        candidate_timings = []
        concurrent_start = time.perf_counter()
        concurrent = search.rerank_candidates_concurrent(
            str(query),
            candidates,
            top_n=args.rerank_top,
            max_workers=args.max_workers,
            candidate_timeout_seconds=args.candidate_timeout,
            retry_timeout_seconds=args.retry_timeout,
            retry_max_candidates=args.retry_max_candidates,
            on_candidate_scored=candidate_timings.append,
        )
        concurrent_seconds = time.perf_counter() - concurrent_start

        row = {
            "sample": asdict(sample),
            "candidate_count": len(candidates),
            "serial_seconds": round(serial_seconds, 3) if serial_seconds is not None else None,
            "concurrent_seconds": round(concurrent_seconds, 3),
            "speedup": round(serial_seconds / concurrent_seconds, 3) if serial_seconds and concurrent_seconds else None,
            "serial_top": summarize_results(serial),
            "concurrent_top": summarize_results(concurrent),
            "same_top_paths": [item["path"] for item in serial] == [item["path"] for item in concurrent] if serial else None,
            "candidate_timings": sorted(candidate_timings, key=lambda item: item.get("rerank_seconds", 0), reverse=True),
        }
        rows.append(row)
        print(
            f"{sample.chapter} {sample.structure_type} {Path(sample.query_path).name}: "
            f"candidates={len(candidates)} serial={serial_seconds:.2f}s " if serial_seconds is not None else
            f"candidates={len(candidates)} serial=skipped "
        )
        print(
            f"concurrent={concurrent_seconds:.2f}s speedup={row['speedup']} "
            f"same_top={row['same_top_paths']}"
        )
        if candidate_timings:
            slowest = max(candidate_timings, key=lambda item: item.get("rerank_seconds", 0))
            print(
                f"slowest=rank{slowest.get('rank')} {Path(slowest.get('path', '')).name} "
                f"{slowest.get('rerank_seconds')}s status={slowest.get('rerank_status')}"
            )

    summary = {
        "sample_count": len(rows),
        "candidate_limit": args.candidate_limit,
        "max_workers": args.max_workers,
        "candidate_timeout_seconds": args.candidate_timeout,
        "retry_timeout_seconds": args.retry_timeout,
        "retry_max_candidates": args.retry_max_candidates,
        "serial_avg_seconds": avg(row["serial_seconds"] for row in rows if row["serial_seconds"] is not None),
        "concurrent_avg_seconds": avg(row["concurrent_seconds"] for row in rows),
        "avg_speedup": avg(row["speedup"] for row in rows if row["speedup"] is not None),
        "same_top_count": sum(1 for row in rows if row["same_top_paths"] is True),
    }
    payload = {"summary": summary, "rows": rows}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved={output}")
    return 0


def choose_samples(symbolic: Path, count: int, candidate_limit: int) -> list[Sample]:
    pool = []
    for workbook in sorted(symbolic.glob("*.xlsx")):
        chapter = workbook.stem
        df = pd.read_excel(workbook)
        for _, row in df.iterrows():
            query_path = str(row.get("题目名称") or "")
            structure_type = str(row.get("结构类型") or "")
            if not query_path or not resolve_image(query_path).is_file():
                continue
            candidates = build_candidates(symbolic, chapter, query_path, candidate_limit)
            if len(candidates) >= min(4, candidate_limit):
                pool.append(Sample(chapter, query_path, structure_type, len(candidates)))

    pool.sort(key=lambda item: (item.candidate_count, item.structure_type == "钢架"), reverse=True)
    selected = []
    seen_chapters = set()
    for item in pool:
        if item.chapter in seen_chapters and len(selected) < count - 1:
            continue
        selected.append(item)
        seen_chapters.add(item.chapter)
        if len(selected) >= count:
            break
    return selected


def build_candidates(symbolic: Path, chapter: str, query_path: str, candidate_limit: int) -> list[dict]:
    workbook = symbolic / f"{chapter}.xlsx"
    df = pd.read_excel(workbook)
    query_row = df[df["题目名称"] == query_path]
    if query_row.empty:
        return []

    query_loads = search.fix_load_types(search._safe_parse_loads(query_row.iloc[0]["荷载"]))
    query_structure = str(query_row.iloc[0].get("结构类型") or "")
    scored = []
    for _, row in df.iterrows():
        if query_structure and str(row.get("结构类型") or "") != query_structure:
            continue
        path = str(row.get("题目名称") or "")
        image_path = resolve_image(path)
        if not path or not image_path.is_file():
            continue
        loads = search.fix_load_types(search._safe_parse_loads(row["荷载"]))
        score = search.compute_similarity(query_loads, loads)
        if score < SYMBOLIC_RERANK_MIN_SCORE:
            continue
        scored.append((score, path))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"rank": rank, "path": str(resolve_image(path)), "name": path, "score": score}
        for rank, (score, path) in enumerate(scored[:candidate_limit], 1)
    ]


def structure_type_for(symbolic: Path, chapter: str, query_path: str) -> str:
    df = pd.read_excel(symbolic / f"{chapter}.xlsx")
    rows = df[df["题目名称"] == query_path]
    if rows.empty:
        raise ValueError(f"Query is not present in {chapter}: {query_path}")
    return str(rows.iloc[0].get("结构类型") or "")


def resolve_image(path: str) -> Path:
    resolved, _, _ = search.resolve_question_path(path, update_excel=False)
    return resolved


def summarize_results(results: list[dict]) -> list[dict]:
    return [
        {
            "rank": item.get("rank"),
            "path": item.get("name") or item.get("path"),
            "score": item.get("score"),
            "rerank_score": item.get("rerank_score"),
            "final_score": item.get("final_score"),
        }
        for item in results
    ]


def avg(values) -> float | None:
    values = [float(value) for value in values]
    if not values:
        return None
    return round(sum(values) / len(values), 3)


if __name__ == "__main__":
    raise SystemExit(main())

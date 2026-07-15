"""Read-only pilot evaluation for the strict cross-chapter fallback.

The evaluator samples existing main-bank images, uses each sample's stored
loads as the query, searches every supported chapter for coarse scores >= the
strict threshold, content-deduplicates the pool, and optionally sends every
remaining candidate through the existing visual scorer.  It never writes the
question bank, answer cache, or Agent session state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import search
from tiku_agent.intent_contract import CHAPTERS


DEFAULT_COARSE_THRESHOLD = 0.999
DEFAULT_RERANK_THRESHOLD = 0.95
DEFAULT_MAX_WORKERS = 10


def load_main_bank_records() -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    for chapter in CHAPTERS:
        df = search.load_chapter_excel(chapter)
        chapter_records = []
        if df is not None:
            for _, row in df.iterrows():
                loads = search.fix_load_types(search._safe_parse_loads(row["荷载"]))
                name = str(row["题目名称"])
                if loads and name:
                    chapter_records.append(
                        {"chapter": chapter, "name": name, "loads": loads}
                    )
        records[chapter] = chapter_records
    return records


def select_samples(
    records_by_chapter: dict[str, list[dict[str, Any]]],
    *,
    per_chapter: int,
    seed: int,
) -> list[dict[str, Any]]:
    selected = []
    for chapter in CHAPTERS:
        records = sorted(
            records_by_chapter.get(chapter, ()),
            key=lambda item: hashlib.sha256(
                f"{seed}|{chapter}|{item['name']}".encode("utf-8")
            ).hexdigest(),
        )
        chapter_samples = []
        for record in records:
            path, _, _ = search.resolve_question_path(
                record["name"], chapter_name=chapter, update_excel=False
            )
            if not path.is_file():
                continue
            sample = dict(record)
            sample["path"] = str(path)
            sample["content_hash"] = file_sha256(path)
            sample["sample_id"] = f"{chapter}:{sample['content_hash'][:12]}"
            chapter_samples.append(sample)
            if len(chapter_samples) >= per_chapter:
                break
        selected.extend(chapter_samples)
    return selected


def collect_perfect_candidates(
    query_loads: list[dict[str, Any]],
    records_by_chapter: dict[str, list[dict[str, Any]]],
    *,
    threshold: float = DEFAULT_COARSE_THRESHOLD,
) -> list[dict[str, Any]]:
    normalized_query = search.normalize_query_loads(query_loads)
    by_content: dict[str, dict[str, Any]] = {}
    for chapter in CHAPTERS:
        for record in records_by_chapter.get(chapter, ()):
            score = search.compute_similarity(normalized_query, record["loads"])
            if score < threshold:
                continue
            path, resolved_name, _ = search.resolve_question_path(
                record["name"], chapter_name=chapter, update_excel=False
            )
            if not path.is_file():
                continue
            content_hash = file_sha256(path)
            existing = by_content.get(content_hash)
            if existing is not None:
                existing["source_chapters"].add(chapter)
                continue
            by_content[content_hash] = {
                "path": str(path),
                "name": resolved_name,
                "score": float(score),
                "content_hash": content_hash,
                "source_chapters": {chapter},
            }

    candidates = sorted(
        by_content.values(),
        key=lambda item: (sorted(item["source_chapters"]), item["content_hash"]),
    )
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
        candidate["chapter"] = "/".join(sorted(candidate["source_chapters"]))
        candidate["source_chapters"] = sorted(candidate["source_chapters"])
    return candidates


def rerank_all_candidates(
    query_image_path: str,
    candidates: list[dict[str, Any]],
    *,
    max_workers: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    workers = max(1, min(int(max_workers), len(candidates)))
    scored = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                search.score_rerank_candidate,
                query_image_path,
                candidate,
                timeout_seconds=timeout_seconds,
                collect_timing=True,
            )
            for candidate in candidates
        ]
        for future in as_completed(futures):
            scored.append(future.result())
    return sorted(
        scored,
        key=lambda item: (
            item.get("rerank_score") is not None,
            float(item.get("rerank_score") or 0),
            -int(item.get("rank") or 0),
        ),
        reverse=True,
    )


def evaluate_case(
    sample: dict[str, Any],
    records_by_chapter: dict[str, list[dict[str, Any]]],
    *,
    coarse_threshold: float,
    rerank_threshold: float,
    max_workers: int,
    timeout_seconds: float,
    coarse_only: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    candidates = collect_perfect_candidates(
        sample["loads"], records_by_chapter, threshold=coarse_threshold
    )
    if coarse_only:
        return {
            "sample_id": sample["sample_id"],
            "chapter": sample["chapter"],
            "coarse_candidates": len(candidates),
            "self_in_coarse": any(
                item["content_hash"] == sample["content_hash"] for item in candidates
            ),
            "model_calls": 0,
            "seconds": round(time.perf_counter() - started, 3),
        }

    scored = rerank_all_candidates(
        sample["path"],
        candidates,
        max_workers=max_workers,
        timeout_seconds=timeout_seconds,
    )
    accepted = [item for item in scored if is_accepted_rerank(item, rerank_threshold)]
    self_accepted = sum(
        item["content_hash"] == sample["content_hash"] for item in accepted
    )
    return {
        "sample_id": sample["sample_id"],
        "chapter": sample["chapter"],
        "coarse_candidates": len(candidates),
        "self_in_coarse": any(
            item["content_hash"] == sample["content_hash"] for item in candidates
        ),
        "accepted_candidates": len(accepted),
        "self_accepted": bool(self_accepted),
        "nonself_accepted_for_review": len(accepted) - self_accepted,
        "unfinished_candidates": sum(
            item.get("rerank_status") != "completed" for item in scored
        ),
        "model_calls": len(candidates),
        "seconds": round(time.perf_counter() - started, 3),
        "accepted": [
            {
                "candidate_rank": rank,
                "source_chapters": item["source_chapters"],
                "rerank_score": item.get("rerank_score"),
                "is_exact_file": item["content_hash"] == sample["content_hash"],
            }
            for rank, item in enumerate(accepted, 1)
        ],
    }


def summarize_results(cases: list[dict[str, Any]], *, coarse_only: bool) -> dict[str, Any]:
    total = len(cases)
    summary = {
        "sample_count": total,
        "coarse_self_hit_rate": ratio(
            sum(bool(case.get("self_in_coarse")) for case in cases), total
        ),
        "average_coarse_candidates": ratio(
            sum(int(case.get("coarse_candidates") or 0) for case in cases), total
        ),
        "max_coarse_candidates": max(
            (int(case.get("coarse_candidates") or 0) for case in cases), default=0
        ),
        "model_calls": sum(int(case.get("model_calls") or 0) for case in cases),
        "average_case_seconds": ratio(
            sum(float(case.get("seconds") or 0) for case in cases), total
        ),
    }
    if coarse_only:
        return summary

    accepted_total = sum(int(case.get("accepted_candidates") or 0) for case in cases)
    exact_total = sum(bool(case.get("self_accepted")) for case in cases)
    summary.update(
        {
            "self_hit_rate": ratio(exact_total, total),
            "no_result_rate": ratio(
                sum(int(case.get("accepted_candidates") or 0) == 0 for case in cases),
                total,
            ),
            "exact_file_precision": ratio(exact_total, accepted_total),
            "accepted_candidates": accepted_total,
            "nonself_accepted_for_review": sum(
                int(case.get("nonself_accepted_for_review") or 0) for case in cases
            ),
            "unfinished_candidates": sum(
                int(case.get("unfinished_candidates") or 0) for case in cases
            ),
        }
    )
    return summary


def is_accepted_rerank(item: dict[str, Any], threshold: float) -> bool:
    return (
        item.get("rerank_status") == "completed"
        and float(item.get("rerank_score") or 0) > threshold
    )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ratio(numerator: float, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate strict cross-chapter fallback")
    parser.add_argument("--per-chapter", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--coarse-threshold", type=float, default=DEFAULT_COARSE_THRESHOLD)
    parser.add_argument("--rerank-threshold", type=float, default=DEFAULT_RERANK_THRESHOLD)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--candidate-timeout", type=float, default=15.0)
    parser.add_argument("--coarse-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.per_chapter < 1:
        raise SystemExit("--per-chapter must be positive")
    records = load_main_bank_records()
    samples = select_samples(records, per_chapter=args.per_chapter, seed=args.seed)
    cases = [
        evaluate_case(
            sample,
            records,
            coarse_threshold=args.coarse_threshold,
            rerank_threshold=args.rerank_threshold,
            max_workers=args.max_workers,
            timeout_seconds=args.candidate_timeout,
            coarse_only=args.coarse_only,
        )
        for sample in samples
    ]
    report = {
        "schema_version": "1.0",
        "mode": "coarse_only" if args.coarse_only else "visual_pilot",
        "policy": {
            "coarse_threshold": args.coarse_threshold,
            "rerank_threshold": args.rerank_threshold,
            "rerank_comparison": ">",
            "max_workers": args.max_workers,
            "total_candidate_limit": None,
            "result_limit": None,
        },
        "summary": summarize_results(cases, coarse_only=args.coarse_only),
        "cases": cases,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Compare two Zhipu visual rerank models on the same candidate pool.

The script is an isolated experiment line: it does not change Feishu, GUI, or
the default search flow. Reports are written under `.tmp_rerank_experiments`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pandas as pd

import search
from multi_agent_pipeline import (
    RuleRouter,
    infer_structure_type_from_text,
    load_bank_excel,
    normalize_structure_type,
    resolve_effective_chapter,
    select_rerank_candidates,
    symbolic_root,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare GLM visual rerank models on one fixed query and candidate pool."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query-path", help="Question image path already present in the chapter Excel.")
    source.add_argument("--image", help="External query image. Use --loads-json to avoid Qwen classification.")
    parser.add_argument("--chapter", required=True, help='Chapter name, or "auto" with --image.')
    parser.add_argument("--route", choices=["auto", "main", "symbolic"], default="auto")
    parser.add_argument("--loads-json", help='Explicit loads JSON, e.g. {"loads":[{"type":"均布","raw":"q"}]}.')
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--rerank-top", type=int, default=search.DISPLAY_MAX_RESULTS)
    parser.add_argument("--baseline-model", default=search.DEFAULT_ZHIPU_RERANK_MODEL)
    parser.add_argument("--baseline-workers", type=int, default=1)
    parser.add_argument("--experiment-model", default="glm-4.6v")
    parser.add_argument("--experiment-workers", type=int, default=10)
    parser.add_argument("--candidate-timeout", type=float, default=search.RERANK_PRIMARY_TIMEOUT_SECONDS)
    parser.add_argument("--retry-timeout", type=float, default=search.RERANK_RETRY_TIMEOUT_SECONDS)
    parser.add_argument("--retry-max-candidates", type=int, default=search.RERANK_RETRY_MAX_CANDIDATES)
    parser.add_argument("--dry-run", action="store_true", help="Only build and report the candidate pool; do not call Zhipu.")
    parser.add_argument(
        "--output-dir",
        default=str(BASE_DIR / ".tmp_rerank_experiments"),
        help="Directory for JSON and Markdown reports.",
    )
    args = parser.parse_args()

    query = build_query(args)
    candidates = build_candidate_pool(query, args.candidate_limit)
    if not candidates:
        raise RuntimeError(
            f"No rerank candidates found: chapter={query['chapter']} route={query['route']} loads={query['loads']}"
        )

    if args.dry_run:
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": True,
            "query": query,
            "candidate_count": len(candidates),
            "candidates": summarize_candidates(candidates),
            "runs": [],
            "comparison": {},
        }
        json_path, md_path = write_report(payload, args.output_dir)
        print(render_console_summary(payload))
        print(f"json={json_path}")
        print(f"md={md_path}")
        return 0

    experiments = [
        (args.baseline_model, args.baseline_workers),
        (args.experiment_model, args.experiment_workers),
    ]
    runs = []
    for model, workers in experiments:
        runs.append(run_one_model(query, candidates, model, workers, args))

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "candidate_count": len(candidates),
        "candidates": summarize_candidates(candidates),
        "runs": runs,
        "comparison": compare_runs(runs),
    }

    json_path, md_path = write_report(payload, args.output_dir)

    print(render_console_summary(payload))
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0


def write_report(payload: dict[str, Any], output_dir: str) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "zhipu_rerank_model_compare_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def build_query(args: argparse.Namespace) -> dict[str, Any]:
    if args.query_path:
        return query_from_bank(args.query_path, args.chapter, args.route)
    return query_from_image(args.image, args.chapter, args.route, args.loads_json)


def query_from_bank(query_path: str, chapter: str, route: str) -> dict[str, Any]:
    if route == "auto":
        route_order = ["main", "symbolic"]
    else:
        route_order = [route]

    for route_name in route_order:
        excel_root = root_for_route(route_name)
        row, image_path = find_bank_row(excel_root, chapter, query_path)
        if row is None:
            continue
        loads = search.normalize_query_loads(search.fix_load_types(search._safe_parse_loads(row["荷载"])))
        return {
            "source": "bank",
            "image_path": str(image_path),
            "chapter": chapter,
            "route": route_name,
            "excel_root": str(excel_root),
            "loads": loads,
            "structure_type": normalize_structure_type(row.get("结构类型", "")) if route_name == "symbolic" else "",
            "query_name": str(row.get("题目名称") or query_path),
        }
    raise RuntimeError(f"Query path is not present in {chapter}.xlsx: {query_path}")


def query_from_image(image: str, chapter: str, route: str, loads_json: str | None) -> dict[str, Any]:
    image_path = Path(image)
    if not image_path.is_file():
        raise FileNotFoundError(image)

    classified: dict[str, Any] | None = None
    if loads_json:
        parsed = json.loads(loads_json)
        raw_loads = parsed.get("loads", parsed) if isinstance(parsed, dict) else parsed
        loads = search.normalize_query_loads(raw_loads)
    else:
        from multi_agent_pipeline import MultiAgentCoordinator

        coordinator = MultiAgentCoordinator()
        classified = coordinator.qwen.classify_image(image_path)
        loads = search.normalize_query_loads(classified.get("loads", []))

    route_decision = RuleRouter().route(loads)
    route_name = route_decision.route if route == "auto" else route
    if route_name not in {"main", "symbolic"}:
        raise RuntimeError(f"Image route is not searchable for rerank comparison: {route_name}")

    effective_chapter = resolve_effective_chapter(chapter, classified)
    if not effective_chapter:
        raise RuntimeError(f"Chapter is unknown. chapter={chapter} classified={classified}")

    structure_type = ""
    if route_name == "symbolic" and classified:
        structure_type = normalize_structure_type(infer_structure_type_from_text(classified))

    return {
        "source": "image",
        "image_path": str(image_path),
        "chapter": effective_chapter,
        "route": route_name,
        "excel_root": str(root_for_route(route_name)),
        "loads": loads,
        "structure_type": structure_type,
        "classified": classified,
    }


def build_candidate_pool(query: dict[str, Any], candidate_limit: int) -> list[dict[str, Any]]:
    scan = search.scan_chapter_candidates(
        query["loads"],
        query["chapter"],
        Path(query["excel_root"]),
        structure_type=query.get("structure_type") or None,
        load_excel=load_bank_excel,
    )
    if scan is None:
        return []

    coarse = [item for item in search.select_coarse_results(scan.scored) if item[0] > 0]
    results = []
    for rank, (score, name) in enumerate(coarse, 1):
        path, resolved_name, _ = search.resolve_question_path(
            name,
            chapter_name=query["chapter"],
            update_excel=False,
        )
        if not path.is_file():
            continue
        results.append({
            "rank": rank,
            "path": str(path),
            "name": resolved_name,
            "score": score,
        })

    selected = select_rerank_candidates(results, query["route"])
    return selected[: max(1, int(candidate_limit or 1))]


def run_one_model(
    query: dict[str, Any],
    candidates: list[dict[str, Any]],
    model: str,
    workers: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    candidate_events: list[dict[str, Any]] = []
    started = time.perf_counter()
    error = ""
    try:
        results = search.rerank_candidates_concurrent(
            query["image_path"],
            candidates,
            top_n=args.rerank_top,
            max_workers=workers,
            candidate_timeout_seconds=args.candidate_timeout,
            retry_timeout_seconds=args.retry_timeout,
            retry_max_candidates=args.retry_max_candidates,
            on_candidate_scored=candidate_events.append,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        results = []
        error = str(exc)
    elapsed = time.perf_counter() - started

    return {
        "model": model,
        "workers": max(1, min(int(workers or 1), len(candidates))),
        "elapsed_seconds": round(elapsed, 3),
        "complete": search.rerank_results_complete(results),
        "error": error,
        "top": summarize_results(results),
        "candidate_events": summarize_results(candidate_events),
    }


def root_for_route(route: str) -> Path:
    return symbolic_root(search.ROOT) if route == "symbolic" else search.ROOT


def find_bank_row(excel_root: Path, chapter: str, query_path: str):
    workbook = excel_root / f"{chapter}.xlsx"
    if not workbook.is_file():
        return None, None
    df = pd.read_excel(workbook)
    target = normalized_path(query_path)
    for _, row in df.iterrows():
        raw_name = str(row.get("题目名称") or "")
        if not raw_name:
            continue
        resolved, _, _ = search.resolve_question_path(raw_name, chapter_name=chapter, update_excel=False)
        if raw_name == query_path or normalized_path(resolved) == target:
            return row, resolved
    return None, None


def normalized_path(path: str | Path) -> str:
    return str(Path(path)).replace("/", "\\").casefold()


def summarize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": item.get("rank"),
            "score": round(float(item.get("score") or 0), 4),
            "name": item.get("name"),
            "path": item.get("path"),
        }
        for item in candidates
    ]


def summarize_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": item.get("rank"),
            "score": round(float(item.get("score") or 0), 4),
            "rerank_score": item.get("rerank_score"),
            "final_score": item.get("final_score"),
            "status": item.get("rerank_status"),
            "seconds": item.get("rerank_seconds"),
            "reason": item.get("rerank_reason"),
            "name": item.get("name") or item.get("path"),
        }
        for item in results
    ]


def compare_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(runs) < 2:
        return {}
    left, right = runs[0], runs[1]
    left_paths = [item.get("name") for item in left.get("top", [])]
    right_paths = [item.get("name") for item in right.get("top", [])]
    left_time = float(left.get("elapsed_seconds") or 0)
    right_time = float(right.get("elapsed_seconds") or 0)
    return {
        "same_top_order": left_paths == right_paths,
        "same_top_set": set(left_paths) == set(right_paths),
        "speedup_right_vs_left": round(left_time / right_time, 3) if left_time and right_time else None,
    }


def render_console_summary(payload: dict[str, Any]) -> str:
    lines = [
        f"query={payload['query']['chapter']} route={payload['query']['route']} candidates={payload['candidate_count']}",
        f"loads={payload['query']['loads']}",
    ]
    for run in payload["runs"]:
        status = "complete" if run["complete"] else "incomplete"
        error = f" error={run['error']}" if run["error"] else ""
        top = ", ".join(
            f"{idx + 1}:{Path(str(item.get('name') or '')).name}({item.get('final_score')})"
            for idx, item in enumerate(run["top"][:3])
        )
        lines.append(
            f"{run['model']} workers={run['workers']} {status} "
            f"{run['elapsed_seconds']}s top=[{top}]{error}"
        )
    lines.append(f"comparison={payload['comparison']}")
    return "\n".join(lines)


def render_markdown(payload: dict[str, Any]) -> str:
    query = payload["query"]
    lines = [
        "# Zhipu Rerank Model Compare",
        "",
        f"- Query: `{query['image_path']}`",
        f"- Chapter: `{query['chapter']}`",
        f"- Route: `{query['route']}`",
        f"- Loads: `{json.dumps(query['loads'], ensure_ascii=False)}`",
        f"- Candidates: `{payload['candidate_count']}`",
        "",
        "| Model | Workers | Complete | Seconds | Top 1 | Top 2 | Top 3 |",
        "| --- | ---: | --- | ---: | --- | --- | --- |",
    ]
    for run in payload["runs"]:
        top = run["top"][:3]
        names = [Path(str(item.get("name") or "")).name for item in top]
        while len(names) < 3:
            names.append("")
        lines.append(
            f"| `{run['model']}` | {run['workers']} | {run['complete']} | "
            f"{run['elapsed_seconds']} | {names[0]} | {names[1]} | {names[2]} |"
        )
    lines.extend([
        "",
        "```json",
        json.dumps(payload["comparison"], ensure_ascii=False, indent=2),
        "```",
        "",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

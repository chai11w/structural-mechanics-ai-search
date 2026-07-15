"""Compare old symbolic search vs structure-type-filtered symbolic search."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import search
from multi_agent_pipeline import normalize_rerank_results, select_rerank_candidates, symbolic_root
from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from scripts.evaluate_qwen_structure_type import qwen_structure_type


DEFAULT_IMAGE = r"D:\桌面\答疑、帮做\结构力学\帮做\4力法\1梁\1单未知量\题目\10.jpg"
DEFAULT_CHAPTER = "4力法"


def now() -> float:
    return time.perf_counter()


def elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 3)


def load_bank(chapter: str, root: Path) -> pd.DataFrame:
    workbook = root / f"{chapter}.xlsx"
    if not workbook.exists():
        raise FileNotFoundError(workbook)
    return pd.read_excel(workbook)


def rank_rows(query_loads: list[dict[str, Any]], chapter: str, root: Path, *, structure_type: str | None = None) -> list[dict[str, Any]]:
    df = load_bank(chapter, root)
    if structure_type and "结构类型" in df.columns:
        df = df[df["结构类型"].astype(str) == structure_type]

    scored: list[tuple[float, str, str]] = []
    for _index, row in df.iterrows():
        db_loads = search._safe_parse_loads(row["荷载"])
        db_loads = search.fix_load_types(db_loads)
        score = search.compute_similarity(query_loads, db_loads)
        scored.append((score, str(row["题目名称"]), str(row.get("结构类型", ""))))

    scored.sort(key=lambda item: item[0], reverse=True)
    perfect = [item for item in scored if item[0] >= 1.0]
    top = perfect if len(perfect) >= search.TOP_K else perfect + [item for item in scored if item[0] < 1.0][: search.TOP_K - len(perfect)]
    top = [item for item in top if item[0] > 0]

    results = []
    for rank, (score, name, typ) in enumerate(top, 1):
        path, resolved_name, _ = search.resolve_question_path(name, chapter_name=chapter, update_excel=False)
        results.append({"rank": rank, "path": str(path), "name": resolved_name, "score": score, "structure_type": typ})
    return results


def run_flow(name: str, query_image: str, query_loads: list[dict[str, Any]], chapter: str, root: Path, structure_type: str | None) -> dict[str, Any]:
    flow_start = now()
    rank_start = now()
    results = rank_rows(query_loads, chapter, root, structure_type=structure_type)
    rank_seconds = elapsed(rank_start)
    rerank_input = select_rerank_candidates(results, "symbolic")

    rerank_start = now()
    zhipu_results = search.rerank_candidates(query_image, rerank_input, top_n=3)
    rerank_seconds = elapsed(rerank_start)
    final_results = normalize_rerank_results(zhipu_results) if zhipu_results else results[:3]

    return {
        "name": name,
        "structure_filter": structure_type or "",
        "rank_seconds": rank_seconds,
        "rerank_seconds": rerank_seconds,
        "total_seconds": elapsed(flow_start),
        "ranked_count": len(results),
        "rerank_count": len(rerank_input),
        "top_results": final_results[:3],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare symbolic-bank search with and without structure type filtering.")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--chapter", default=DEFAULT_CHAPTER)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    query_image = str(Path(args.image))
    if not Path(query_image).is_file():
        raise FileNotFoundError(query_image)

    symbolic = symbolic_root(search.ROOT)
    classify_start = now()
    qwen = qwen_structure_type(
        Path(query_image),
        endpoint=DEFAULT_ENDPOINT,
        model=DEFAULT_MODEL,
        api_key=os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", ""),
        timeout=90,
    )
    structure_seconds = elapsed(classify_start)

    # Use the existing Excel row loads for this controlled experiment so both
    # flows compare the same load signature without another model variable.
    rel_name = str(Path(query_image).relative_to(search.ROOT)).replace("\\", "/")
    df = load_bank(args.chapter, symbolic)
    row = df[df["题目名称"].astype(str) == rel_name]
    if row.empty:
        raise ValueError(f"query image not found in symbolic bank: {rel_name}")
    query_loads = search._safe_parse_loads(row.iloc[0]["荷载"])

    old_flow = run_flow("old_load_only", query_image, query_loads, args.chapter, symbolic, None)
    new_flow = run_flow("new_structure_then_load", query_image, query_loads, args.chapter, symbolic, qwen["structure_type"])
    new_flow["structure_seconds"] = structure_seconds
    new_flow["total_with_structure_seconds"] = round(new_flow["total_seconds"] + structure_seconds, 3)

    summary = {
        "query_image": query_image,
        "chapter": args.chapter,
        "query_rel": rel_name,
        "query_loads": query_loads,
        "query_structure_type": qwen,
        "route": "symbolic",
        "old": old_flow,
        "new": new_flow,
    }

    output_dir = Path(args.output_dir) if args.output_dir else BASE / ".tmp_support_eval" / f"structure_filter_compare_{time.strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 结构类型筛选对比实验", ""]
    lines.append(f"- query: `{rel_name}`")
    lines.append(f"- chapter: `{args.chapter}`")
    lines.append(f"- structure_type: `{qwen['structure_type']}` ({qwen['confidence']:.2f})")
    lines.append(f"- structure_seconds: {structure_seconds:.2f}")
    lines.append("")
    lines.append("| 流程 | 排序候选 | 复筛候选 | 排序秒 | 复筛秒 | 总秒 |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    lines.append(
        f"| 旧：章节→荷载排序→复筛 | {old_flow['ranked_count']} | {old_flow['rerank_count']} | "
        f"{old_flow['rank_seconds']:.2f} | {old_flow['rerank_seconds']:.2f} | {old_flow['total_seconds']:.2f} |"
    )
    lines.append(
        f"| 新：章节→类型筛选→荷载排序→复筛 | {new_flow['ranked_count']} | {new_flow['rerank_count']} | "
        f"{new_flow['rank_seconds']:.2f} | {new_flow['rerank_seconds']:.2f} | {new_flow['total_with_structure_seconds']:.2f} |"
    )
    lines.append("")
    for flow in (old_flow, new_flow):
        lines.append(f"## {flow['name']}")
        for item in flow["top_results"]:
            score = item.get("final_score") if item.get("final_score") is not None else item.get("score", 0)
            lines.append(f"- {round(float(score) * 100)}% `{item['name']}`")
        lines.append("")
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"summary={output_dir / 'summary.md'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

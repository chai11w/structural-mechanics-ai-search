"""Run an isolated multi-question retrieval experiment with a chosen rerank model.

This mirrors the Feishu multi-question path closely enough for timing and
quality checks, but it does not touch Feishu sessions or runtime state.
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

import search
from multi_agent_pipeline import MultiAgentCoordinator
from tiku_shared.multi_question import (
    effective_question_chapter,
    normalize_multi_questions,
    normalize_question_key,
    prepare_multi_diagram_crops,
)


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate multi-question retrieval with one Zhipu rerank model.")
    parser.add_argument("--image", required=True, help="Multi-question source image.")
    parser.add_argument("--rerank-model", default="glm-4.6v")
    parser.add_argument("--rerank-workers", type=int, default=10)
    parser.add_argument("--rerank-top", type=int, default=search.DISPLAY_MAX_RESULTS)
    parser.add_argument("--no-cache", action="store_true", help="Disable Qwen cache for this experiment.")
    parser.add_argument(
        "--output-dir",
        default=str(BASE_DIR / ".tmp_rerank_experiments" / "multi_question"),
    )
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.is_file():
        raise FileNotFoundError(image_path)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    coordinator = MultiAgentCoordinator()
    coordinator.qwen.use_cache = not args.no_cache

    total_started = time.perf_counter()
    scope_started = time.perf_counter()
    scope = coordinator.analyze_image_scope(image_path)
    scope_seconds = time.perf_counter() - scope_started

    layout: dict[str, Any] = {}
    questions: list[dict[str, Any]] = []
    crops: dict[str, str] = {}
    layout_seconds = 0.0
    crop_seconds = 0.0
    question_rows: list[dict[str, Any]] = []

    if scope.get("question_layout") == "multi":
        layout_started = time.perf_counter()
        layout = coordinator.analyze_image_layout(image_path)
        layout_seconds = time.perf_counter() - layout_started
        questions = normalize_multi_questions(layout.get("questions", []))

        crop_started = time.perf_counter()
        crops = prepare_multi_diagram_crops(image_path, questions, output_dir / "multi_diagrams")
        crop_seconds = time.perf_counter() - crop_started

        for question in questions:
            question_rows.append(
                run_question_search(
                    coordinator,
                    question,
                    crops,
                    args.rerank_model,
                    args.rerank_workers,
                    args.rerank_top,
                )
            )

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "image": str(image_path),
        "rerank_model": args.rerank_model,
        "rerank_workers": args.rerank_workers,
        "scope": scope,
        "layout": layout,
        "question_count": len(questions),
        "crop_count": len(crops),
        "timing": {
            "scope_seconds": round(scope_seconds, 3),
            "layout_seconds": round(layout_seconds, 3),
            "crop_seconds": round(crop_seconds, 3),
            "search_seconds": round(sum(row.get("seconds", 0.0) for row in question_rows), 3),
            "total_seconds": round(time.perf_counter() - total_started, 3),
        },
        "questions": question_rows,
    }
    json_path, md_path = write_report(payload, output_dir)
    print(render_console(payload))
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0


def run_question_search(
    coordinator: MultiAgentCoordinator,
    question: dict[str, Any],
    crops: dict[str, str],
    rerank_model: str,
    rerank_workers: int,
    rerank_top: int,
) -> dict[str, Any]:
    label = str(question.get("label") or "")
    key = normalize_question_key(label)
    crop = crops.get(key, "")
    chapter = effective_question_chapter(question, CHAPTERS)
    loads = question.get("loads", [])
    row: dict[str, Any] = {
        "label": label,
        "key": key,
        "chapter": chapter,
        "chapter_hint": question.get("chapter_hint"),
        "chapter_confidence": question.get("chapter_confidence"),
        "chapter_evidence": question.get("chapter_evidence"),
        "loads": loads,
        "crop": crop,
        "reranked": False,
        "rerank_note": "",
        "route": "",
        "seconds": 0.0,
        "status": "pending",
        "top": [],
    }
    if not loads:
        row["status"] = "no_loads"
        return row
    if not chapter:
        row["status"] = "needs_chapter"
        return row
    if chapter == "unsupported":
        row["status"] = "unsupported"
        return row

    started = time.perf_counter()
    try:
        result = coordinator.search_loads(
            loads,
            chapter,
            query_image_path=crop or None,
            rerank=bool(crop),
            rerank_top=rerank_top,
            rerank_model=rerank_model,
            rerank_workers=rerank_workers,
            force_rerank=bool(crop),
            classified=question,
        )
    except Exception as exc:  # noqa: BLE001
        row["status"] = "error"
        row["error"] = str(exc)
        row["seconds"] = round(time.perf_counter() - started, 3)
        return row

    row["seconds"] = round(time.perf_counter() - started, 3)
    row["route"] = result.route.route
    row["reranked"] = result.reranked
    row["rerank_note"] = result.rerank_note
    row["status"] = "ok" if result.results else "no_match"
    row["top"] = [
        {
            "rank": item.get("rank"),
            "score": item.get("score"),
            "rerank_score": item.get("rerank_score"),
            "final_score": item.get("final_score"),
            "status": item.get("rerank_status"),
            "name": item.get("name"),
            "path": item.get("path"),
        }
        for item in result.results
    ]
    return row


def write_report(payload: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    stem = "multi_question_rerank_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_console(payload: dict[str, Any]) -> str:
    lines = [
        f"layout={payload['scope'].get('question_layout')} questions={payload['question_count']} crops={payload['crop_count']}",
        f"model={payload['rerank_model']} workers={payload['rerank_workers']} timing={payload['timing']}",
    ]
    for row in payload["questions"]:
        top = row["top"][0]["name"] if row.get("top") else ""
        lines.append(
            f"Q{row['label']} status={row['status']} chapter={row['chapter']} "
            f"reranked={row['reranked']} seconds={row['seconds']} top1={top}"
        )
    return "\n".join(lines)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Multi Question Rerank Experiment",
        "",
        f"- Image: `{payload['image']}`",
        f"- Model: `{payload['rerank_model']}`",
        f"- Workers: `{payload['rerank_workers']}`",
        f"- Timing: `{json.dumps(payload['timing'], ensure_ascii=False)}`",
        "",
        "| Question | Status | Chapter | Loads | Crop | Reranked | Seconds | Top1 |",
        "| --- | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for row in payload["questions"]:
        top1 = row["top"][0]["name"] if row.get("top") else ""
        lines.append(
            f"| {row['label']} | {row['status']} | {row.get('chapter') or ''} | "
            f"`{json.dumps(row.get('loads', []), ensure_ascii=False)}` | "
            f"{bool(row.get('crop'))} | {row.get('reranked')} | {row.get('seconds')} | {top1} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

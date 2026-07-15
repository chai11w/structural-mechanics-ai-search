"""Evaluate Qwen structure-type recognition on a small symbolic-bank sample.

This script is read-only for the question bank. It samples rows from the
symbolic bank, asks Qwen for a coarse structure type, and writes a local report.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import search
from multi_agent_pipeline import symbolic_root
from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from scripts.structure_type_classifier import VALID_STRUCTURE_TYPES, qwen_structure_type


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]
VALID_TYPES = VALID_STRUCTURE_TYPES


def path_expected_type(rel_path: str) -> str:
    text = str(rel_path)
    if re.search(r"钢架|刚架|框架|门架|闭口|开口|组合", text):
        return "钢架"
    if re.search(r"桁架", text):
        return "桁架"
    if re.search(r"拱", text):
        return "拱"
    if re.search(r"梁|多跨梁|悬臂", text):
        return "梁"
    return "unknown"


def resolve_image(root: Path, rel_path: str) -> Path:
    path = root / rel_path
    if path.is_file():
        return path
    resolved, _resolved_name, _repaired = search.resolve_question_path(rel_path, update_excel=False)
    return resolved


def load_symbolic_rows(symbolic: Path, root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chapter in CHAPTERS:
        workbook = symbolic / f"{chapter}.xlsx"
        if not workbook.exists():
            continue
        df = pd.read_excel(workbook)
        for index, row in df.iterrows():
            rel_path = str(row.get("题目名称") or "").strip()
            if not rel_path:
                continue
            image_path = resolve_image(root, rel_path)
            expected = path_expected_type(rel_path)
            rows.append(
                {
                    "chapter": chapter,
                    "row": int(index) + 2,
                    "rel_path": rel_path,
                    "image_path": str(image_path),
                    "expected_by_path": expected,
                    "loads": str(row.get("荷载") or ""),
                }
            )
    return rows


def sample_rows(rows: list[dict[str, Any]], limit: int, *, random_sample: bool = False, seed: int = 0) -> list[dict[str, Any]]:
    if random_sample:
        rng = random.Random(seed)
        buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in ("梁", "钢架", "桁架", "拱", "unknown")}
        for row in rows:
            if Path(row["image_path"]).is_file():
                buckets.setdefault(row["expected_by_path"], []).append(row)
        for bucket in buckets.values():
            rng.shuffle(bucket)

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        required_order = ["梁", "钢架", "桁架", "拱", "unknown"]
        for key in required_order:
            if buckets.get(key):
                row = buckets[key].pop(0)
                selected.append(row)
                seen.add(row["rel_path"])

        pool = [row for bucket in buckets.values() for row in bucket if row["rel_path"] not in seen]
        rng.shuffle(pool)
        selected.extend(pool[: max(0, limit - len(selected))])
        return selected[:limit]

    known = [row for row in rows if row["expected_by_path"] in {"梁", "钢架"} and Path(row["image_path"]).is_file()]
    unknown = [row for row in rows if row["expected_by_path"] == "unknown" and Path(row["image_path"]).is_file()]

    beam = [row for row in known if row["expected_by_path"] == "梁"]
    frame = [row for row in known if row["expected_by_path"] == "钢架"]

    selected: list[dict[str, Any]] = []
    targets = [("梁", beam), ("钢架", frame), ("unknown", unknown)]
    per_known = max(1, limit // 2)
    for _name, bucket in targets[:2]:
        if not bucket:
            continue
        step = max(1, len(bucket) // per_known)
        selected.extend(bucket[::step][:per_known])

    remaining = limit - len(selected)
    if remaining > 0 and unknown:
        step = max(1, len(unknown) // remaining)
        selected.extend(unknown[::step][:remaining])

    if len(selected) < limit:
        seen = {row["rel_path"] for row in selected}
        for row in rows:
            if len(selected) >= limit:
                break
            if row["rel_path"] not in seen and Path(row["image_path"]).is_file():
                selected.append(row)
                seen.add(row["rel_path"])

    return selected[:limit]


def markdown_image_path(path: str) -> str:
    return Path(path).as_posix()


def write_markdown_report(output_path: Path, summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    lines = ["# Qwen 结构大类 20 样本评测", ""]
    lines.append(f"- total: {summary['total']}")
    lines.append(f"- request_success: {summary['ok']}/{summary['total']} ({summary['request_success_rate']:.0%})")
    if summary["known_path_label_match_rate"] is not None:
        lines.append(
            f"- path_label_match: {summary['known_path_label_matches']}/{summary['known_path_label_total']} "
            f"({summary['known_path_label_match_rate']:.0%})"
        )
    lines.append(f"- avg_seconds: {summary['avg_seconds']:.2f}")
    lines.append(f"- max_seconds: {summary['max_seconds']:.2f}")
    lines.append("")
    for item in results:
        image_path = markdown_image_path(str(item["image_path"]))
        lines.append(f"## {item['sample_index']}. {item['structure_type']} / 路径弱标签：{item['expected_by_path']}")
        lines.append("")
        lines.append(f"![sample {item['sample_index']}]({image_path})")
        lines.append("")
        lines.append(f"- 路径：`{item['rel_path']}`")
        lines.append(f"- 耗时：{float(item['seconds']):.2f}s")
        lines.append(f"- 置信度：{float(item['confidence']):.2f}")
        if item.get("reason"):
            lines.append(f"- 理由：{item['reason']}")
        if item.get("error"):
            lines.append(f"- error: `{item['error']}`")
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Qwen coarse structure type on symbolic-bank samples.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--random", action="store_true", help="randomly sample rows while trying to include every weak path label")
    parser.add_argument("--seed", type=int, default=20260702)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
    if not api_key:
        raise SystemExit("DASHSCOPE_API_KEY missing")

    root = search.ROOT
    symbolic = symbolic_root(root)
    rows = load_symbolic_rows(symbolic, root)
    selected = sample_rows(rows, args.limit, random_sample=args.random, seed=args.seed)
    if not selected:
        raise SystemExit("no symbolic samples found")

    output_dir = Path(args.output_dir) if args.output_dir else BASE / ".tmp_support_eval" / f"qwen_structure_type_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for idx, row in enumerate(selected, 1):
        image_path = Path(row["image_path"])
        started = time.perf_counter()
        record = dict(row)
        record["sample_index"] = idx
        errors = []
        for attempt in range(max(1, args.retries + 1)):
            try:
                qwen = qwen_structure_type(
                    image_path,
                    endpoint=args.endpoint,
                    model=args.model,
                    api_key=api_key,
                    timeout=args.timeout,
                )
                record.update(qwen)
                record["ok"] = True
                record["attempts"] = attempt + 1
                break
            except Exception as exc:  # noqa: BLE001 - evaluation should continue.
                errors.append(str(exc))
                if attempt < args.retries:
                    time.sleep(1.0)
        else:
            record["ok"] = False
            record["structure_type"] = "error"
            record["confidence"] = 0.0
            record["reason"] = ""
            record["attempts"] = len(errors)
            record["error"] = " | ".join(errors)
        record["seconds"] = round(time.perf_counter() - started, 3)
        expected = record["expected_by_path"]
        record["path_label_match"] = bool(record["ok"] and expected != "unknown" and record["structure_type"] == expected)
        results.append(record)
        print(
            f"{idx:02d}/{len(selected)} {record['seconds']:.2f}s "
            f"expected={expected} qwen={record['structure_type']} conf={record['confidence']:.2f} "
            f"{record['rel_path']}"
        )

    ok_count = sum(1 for item in results if item["ok"])
    known = [item for item in results if item["expected_by_path"] != "unknown"]
    matches = sum(1 for item in known if item["path_label_match"])
    seconds = [float(item["seconds"]) for item in results]
    summary = {
        "root": str(root),
        "symbolic_root": str(symbolic),
        "total": len(results),
        "ok": ok_count,
        "request_success_rate": ok_count / len(results) if results else 0,
        "known_path_label_total": len(known),
        "known_path_label_matches": matches,
        "known_path_label_match_rate": matches / len(known) if known else None,
        "avg_seconds": sum(seconds) / len(seconds) if seconds else 0,
        "max_seconds": max(seconds) if seconds else 0,
        "model": args.model,
    }
    (output_dir / "results.json").write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    write_markdown_report(output_dir / "summary.md", summary, results)

    print(f"summary={output_dir / 'summary.md'}")
    print(f"json={output_dir / 'results.json'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

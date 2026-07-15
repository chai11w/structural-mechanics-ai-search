"""Classify and write coarse structure types into the symbolic-bank Excel files."""

from __future__ import annotations

import argparse
import json
import shutil
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
from scripts.evaluate_qwen_structure_type import (
    CHAPTERS,
    qwen_structure_type,
    resolve_image,
)


STRUCTURE_COLUMNS = ["结构类型"]


def load_symbolic_workbooks(symbolic: Path) -> list[tuple[str, Path, pd.DataFrame]]:
    workbooks = []
    for chapter in CHAPTERS:
        workbook = symbolic / f"{chapter}.xlsx"
        if workbook.exists():
            workbooks.append((chapter, workbook, pd.read_excel(workbook)))
    return workbooks


def classify_row(
    row: pd.Series,
    *,
    root: Path,
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    rel_path = str(row.get("题目名称") or "").strip()
    image_path = resolve_image(root, rel_path)
    errors = []
    for attempt in range(max(1, retries + 1)):
        try:
            result = qwen_structure_type(
                image_path,
                endpoint=endpoint,
                model=model,
                api_key=api_key,
                timeout=timeout,
            )
            return {
                "rel_path": rel_path,
                "image_path": str(image_path),
                "ok": True,
                "attempts": attempt + 1,
                **result,
            }
        except Exception as exc:  # noqa: BLE001 - keep batch going and mark unknown.
            errors.append(str(exc))
            if attempt < retries:
                time.sleep(1.0)

    return {
        "rel_path": rel_path,
        "image_path": str(image_path),
        "ok": False,
        "attempts": len(errors),
        "structure_type": "unknown",
        "confidence": 0.0,
        "reason": "识别失败",
        "error": " | ".join(errors),
    }


def backup_workbook(workbook: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / workbook.name
    shutil.copy2(workbook, target)
    return target


def write_workbook(workbook: Path, df: pd.DataFrame, records: list[dict[str, Any]], backup_dir: Path) -> None:
    by_rel = {record["rel_path"]: record for record in records}
    for column in STRUCTURE_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    for index, row in df.iterrows():
        rel_path = str(row.get("题目名称") or "").strip()
        record = by_rel.get(rel_path)
        if not record:
            continue
        df.at[index, "结构类型"] = record.get("structure_type", "unknown")

    backup_workbook(workbook, backup_dir)
    df.to_excel(workbook, index=False)


def records_from_results(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records", [])
    if not isinstance(records, list):
        raise ValueError(f"invalid results file: {path}")
    return [record for record in records if isinstance(record, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Qwen structure types into symbolic-bank Excel files.")
    parser.add_argument("--apply", action="store_true", help="write Excel files after backing them up")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--from-results", default="", help="reuse a previous classification_results.json instead of calling Qwen")
    args = parser.parse_args()

    api_key = search.os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
    if not api_key and not args.from_results:
        raise SystemExit("DASHSCOPE_API_KEY missing")

    root = search.ROOT
    symbolic = symbolic_root(root)
    workbooks = load_symbolic_workbooks(symbolic)
    if not workbooks:
        raise SystemExit(f"no symbolic workbooks found: {symbolic}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else BASE / ".tmp_support_eval" / f"symbolic_structure_write_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = BASE / "backups" / f"symbolic_structure_types_{timestamp}"

    all_records: list[dict[str, Any]] = []
    workbook_records: dict[Path, list[dict[str, Any]]] = {}

    if args.from_results:
        all_records = records_from_results(Path(args.from_results))
        records_by_workbook_path: dict[str, list[dict[str, Any]]] = {}
        for record in all_records:
            records_by_workbook_path.setdefault(str(record.get("workbook") or ""), []).append(record)
        for _chapter, workbook, _df in workbooks:
            workbook_records[workbook] = records_by_workbook_path.get(str(workbook), [])
    else:
        total = sum(len(df) for _chapter, _workbook, df in workbooks)
        count = 0
        for chapter, workbook, df in workbooks:
            records: list[dict[str, Any]] = []
            for _index, row in df.iterrows():
                count += 1
                started = time.perf_counter()
                record = classify_row(
                    row,
                    root=root,
                    api_key=api_key,
                    model=args.model,
                    endpoint=args.endpoint,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                record["chapter"] = chapter
                record["workbook"] = str(workbook)
                record["seconds"] = round(time.perf_counter() - started, 3)
                records.append(record)
                all_records.append(record)
                print(
                    f"{count:03d}/{total} {record['seconds']:.2f}s "
                    f"{chapter} {record['structure_type']} conf={float(record.get('confidence') or 0):.2f} "
                    f"{record['rel_path']}"
                )
            workbook_records[workbook] = records

    summary = {
        "root": str(root),
        "symbolic_root": str(symbolic),
        "total": len(all_records),
        "ok": sum(1 for record in all_records if record.get("ok")),
        "apply": args.apply,
        "backup_dir": str(backup_dir) if args.apply else "",
        "counts": {},
    }
    for record in all_records:
        typ = str(record.get("structure_type") or "unknown")
        summary["counts"][typ] = summary["counts"].get(typ, 0) + 1

    (output_dir / "classification_results.json").write_text(
        json.dumps({"summary": summary, "records": all_records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = ["# 字母库结构类型写入报告", ""]
    lines.append(f"- apply: {args.apply}")
    lines.append(f"- total: {summary['total']}")
    lines.append(f"- ok: {summary['ok']}/{summary['total']}")
    lines.append(f"- counts: {summary['counts']}")
    if args.apply:
        lines.append(f"- backup_dir: `{backup_dir}`")
    lines.append("")
    lines.append("| 章节 | 结构类型 | 置信度 | 路径 |")
    lines.append("|---|---|---:|---|")
    for record in all_records:
        lines.append(
            f"| {record['chapter']} | {record['structure_type']} | "
            f"{float(record.get('confidence') or 0):.2f} | `{record['rel_path']}` |"
        )
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    if args.apply:
        for _chapter, workbook, df in workbooks:
            write_workbook(workbook, df, workbook_records[workbook], backup_dir)
        print(f"wrote_symbolic_excels={symbolic}")
        print(f"backup_dir={backup_dir}")
    else:
        print("dry_run_only=true")

    print(f"summary={output_dir / 'summary.md'}")
    print(f"json={output_dir / 'classification_results.json'}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

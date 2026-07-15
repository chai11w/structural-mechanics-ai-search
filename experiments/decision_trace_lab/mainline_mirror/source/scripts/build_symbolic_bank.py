"""
Build a separate symbolic question-bank Excel set.

The output mirrors the main bank format: one chapter Excel per chapter with
columns `题目名称` and `荷载`. It uses reviewed Qwen classification outputs and
does not edit the live main bank.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from search import _dominant_symbol_family, normalize_load_for_similarity, normalize_load_type, strip_load_unit

DEFAULT_EXISTING_RESULTS = BASE / ".tmp_symbol_sheets" / "classify_existing_full_qwen_no_thinking" / "classification_results.json"
DEFAULT_MISSING_RESULTS = BASE / ".tmp_symbol_sheets" / "classify_missing_full_qwen_no_thinking" / "classification_results.json"
CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]


def load_config() -> dict:
    cfg: dict = {}
    for name in ("config.json", "config.local.json"):
        path = BASE / name
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return cfg


def configured_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("root") or r"D:\桌面\答疑、帮做\结构力学\帮做")


def default_output_root(root: Path) -> Path:
    return root.parent / f"{root.name}_字母库"


def load_records(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def mapped_symbolic_loads(loads: list[dict]) -> list[dict]:
    dominant_family = _dominant_symbol_family(loads)
    mapped = []
    for item in loads:
        typ = normalize_load_type(item.get("type", ""), item.get("raw", ""))
        original_raw = strip_load_unit(item.get("raw", ""))
        code = normalize_load_for_similarity(item, dominant_family)
        mapped.append({
            "type": typ,
            "raw": code,
            "original_raw": original_raw,
        })
    return mapped


def row_from_record(record: dict) -> dict:
    return {
        "题目名称": record["rel_path"],
        "荷载": json.dumps({"loads": mapped_symbolic_loads(record.get("loads", []))}, ensure_ascii=False),
    }


def build_records(existing_path: Path, missing_path: Path) -> list[dict]:
    records = []
    for source, path in (("existing_main", existing_path), ("missing_from_main", missing_path)):
        for record in load_records(path):
            if record.get("category") != "symbolic_unassigned":
                continue
            item = dict(record)
            item["source"] = source
            records.append(item)

    deduped: dict[str, dict] = {}
    for record in records:
        deduped[record["rel_path"]] = record
    return sorted(deduped.values(), key=lambda r: r["rel_path"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build same-format symbolic-bank Excel files.")
    parser.add_argument("--root", help="main question bank root; used for default output and path checks")
    parser.add_argument("--output-root", help="symbolic bank root")
    parser.add_argument("--existing-results", default=str(DEFAULT_EXISTING_RESULTS))
    parser.add_argument("--missing-results", default=str(DEFAULT_MISSING_RESULTS))
    parser.add_argument("--force", action="store_true", help="overwrite existing output Excel files")
    args = parser.parse_args()

    root = Path(args.root) if args.root else configured_root()
    output_root = Path(args.output_root) if args.output_root else default_output_root(root)
    output_root.mkdir(parents=True, exist_ok=True)

    records = build_records(Path(args.existing_results), Path(args.missing_results))
    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_chapter[record["chapter"]].append(record)

    summary = {
        "root": str(root),
        "output_root": str(output_root),
        "total": len(records),
        "files": [],
    }

    for chapter in CHAPTERS:
        rows = [row_from_record(record) for record in by_chapter.get(chapter, [])]
        df = pd.DataFrame(rows, columns=["题目名称", "荷载"])
        output_path = output_root / f"{chapter}.xlsx"
        if output_path.exists() and not args.force:
            raise FileExistsError(f"{output_path} exists; pass --force to overwrite")
        df.to_excel(output_path, index=False)
        summary["files"].append({
            "chapter": chapter,
            "path": str(output_path),
            "rows": len(df),
            "source_counts": dict(Counter(record["source"] for record in by_chapter.get(chapter, []))),
        })

    report_dir = BASE / ".tmp_symbol_sheets" / f"symbolic_bank_build_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "symbolic_bank_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 字母库生成报告", ""]
    lines.append(f"- main_root: `{root}`")
    lines.append(f"- output_root: `{output_root}`")
    lines.append(f"- total_rows: {len(records)}")
    lines.append("")
    for item in summary["files"]:
        lines.append(f"## {item['chapter']}")
        lines.append(f"- rows: {item['rows']}")
        lines.append(f"- source_counts: {item['source_counts']}")
        lines.append("")
    (report_dir / "symbolic_bank_summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"output_root={output_root}")
    print(f"total_rows={len(records)}")
    for item in summary["files"]:
        print(f"{item['chapter']}.xlsx\trows={item['rows']}\tsources={item['source_counts']}")
    print(f"report={report_dir / 'symbolic_bank_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

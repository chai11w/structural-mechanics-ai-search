"""Export current Excel chapter labels as a seed dataset.

This does not call any model. It records the chapter label already implied by
the live workbook where each question row is stored.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import search  # noqa: E402
from scripts.audit_unindexed_questions import CHAPTERS  # noqa: E402
from multi_agent_pipeline import symbolic_root  # noqa: E402


DEFAULT_OUTPUT = BASE / "data" / "chapter_judgment_seed.jsonl"


def iter_workbook_rows(root: Path, bank: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chapter in CHAPTERS:
        workbook = root / f"{chapter}.xlsx"
        if not workbook.exists():
            continue
        df = pd.read_excel(workbook)
        if "题目名称" not in df.columns:
            continue
        for index, row in df.iterrows():
            rel_path = str(row.get("题目名称") or "").replace("\\", "/").strip()
            if not rel_path:
                continue
            records.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "existing_excel_seed",
                "bank": bank,
                "chapter": chapter,
                "final_chapter": chapter,
                "decision_mode": "existing_label",
                "image_path": str((root / rel_path).resolve()).replace("\\", "/"),
                "relative_path": rel_path,
                "row_number": int(index) + 2,
                "workbook": str(workbook).replace("\\", "/"),
            })
    return records


def export_seed(output: Path = DEFAULT_OUTPUT) -> int:
    root = Path(search.ROOT)
    records = iter_workbook_rows(root, "main")
    records.extend(iter_workbook_rows(symbolic_root(root), "symbolic"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出现有题库章节标签快照")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = export_seed(args.output)
    print(f"seed_records={count}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

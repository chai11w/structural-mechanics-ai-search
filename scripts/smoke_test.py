"""
Read-only smoke test for the structure-mechanics question bank.

This script checks the live configured question bank without modifying Excel,
images, answers, or search cache. It is meant to be run before and after
optimizations to catch path/config/schema breakage early.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import search


EXPECTED_CHAPTERS = [
    "2静定结构",
    "3静定结构位移",
    "4力法",
    "5位移法",
    "6力矩分配",
]


def ok(message: str) -> None:
    print(f"PASS {message}")


def warn(message: str) -> None:
    print(f"WARN {message}")


def fail(message: str) -> None:
    print(f"FAIL {message}")


def check_loads_json(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    loads = parsed.get("loads")
    if not isinstance(loads, list):
        return False
    for item in loads:
        if not isinstance(item, dict):
            return False
        if item.get("type") not in {"集中", "均布", "弯矩"}:
            return False
        if "raw" not in item:
            return False
    return True


def main() -> int:
    failures = 0
    warnings = 0

    root = Path(search.ROOT)
    answer_output = Path(search.ANSWER_OUTPUT)

    if root.is_dir():
        ok(f"ROOT exists: {root}")
    else:
        fail(f"ROOT missing: {root}")
        return 1

    if answer_output.parent.is_dir():
        ok(f"answer_output parent exists: {answer_output.parent}")
    else:
        warnings += 1
        warn(f"answer_output parent missing: {answer_output.parent}")

    for chapter in EXPECTED_CHAPTERS:
        xlsx_path = root / f"{chapter}.xlsx"
        if not xlsx_path.is_file():
            failures += 1
            fail(f"{chapter}: missing Excel {xlsx_path}")
            continue

        try:
            df = pd.read_excel(xlsx_path)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            fail(f"{chapter}: cannot read Excel: {exc}")
            continue

        required = {"题目名称", "荷载"}
        missing = required.difference(df.columns)
        if missing:
            failures += 1
            fail(f"{chapter}: missing columns {sorted(missing)}")
            continue

        ok(f"{chapter}: Excel readable rows={len(df)}")

        bad_json = []
        missing_images = []
        sample = df.head(20)
        for index, row in sample.iterrows():
            if not check_loads_json(row["荷载"]):
                bad_json.append(index + 2)
            image_path = root / str(row["题目名称"])
            if not image_path.is_file():
                missing_images.append((index + 2, str(row["题目名称"])))

        if bad_json:
            failures += 1
            fail(f"{chapter}: invalid load JSON in sample rows {bad_json}")
        else:
            ok(f"{chapter}: first {len(sample)} load JSON cells valid")

        if missing_images:
            warnings += 1
            preview = "; ".join(f"row {row}: {path}" for row, path in missing_images[:3])
            warn(f"{chapter}: sample image paths missing ({len(missing_images)}): {preview}")
        else:
            ok(f"{chapter}: first {len(sample)} image paths exist")

    if failures:
        print(f"SUMMARY FAIL failures={failures} warnings={warnings}")
        return 1

    print(f"SUMMARY PASS warnings={warnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

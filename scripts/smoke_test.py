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


def check_symbol_normalization() -> list[str]:
    cases = {
        ("q", "均布"): "0.010",
        ("qL", "集中"): "0.010",
        ("qL²", "弯矩"): "0.010",
        ("2q", "均布"): "0.011",
        ("F/L", "均布"): "0.020",
        ("F", "集中"): "0.020",
        ("FL", "弯矩"): "0.020",
        ("P", "集中"): "0.020",
        ("Fp", "集中"): "0.020",
        ("F_P", "集中"): "0.020",
        ("2P", "集中"): "0.021",
        ("Pa", "弯矩"): "0.020",
        ("M/L²", "均布"): "0.030",
        ("M/L", "集中"): "0.030",
        ("M", "弯矩"): "0.030",
        ("3M", "弯矩"): "0.032",
        ("A", "均布"): "0.010",
        ("AL", "集中"): "0.010",
        ("AL²", "弯矩"): "0.010",
        ("A/L", "均布"): "0.020",
        ("A", "集中"): "0.020",
        ("AL", "弯矩"): "0.020",
        ("A/L²", "均布"): "0.030",
        ("A/L", "集中"): "0.030",
        ("A", "弯矩"): "0.030",
        ("A/b", "均布"): "0.020",
        ("AB", "弯矩"): "0.020",
        ("AB²", "弯矩"): "0.010",
        ("A/b²", "均布"): "0.030",
        ("A/b", "集中"): "0.030",
    }
    failures = []
    for (raw, load_type), expected in cases.items():
        actual = search.normalize_raw(raw, load_type)
        if actual != expected:
            failures.append(f"{load_type}:{raw}: expected {expected}, got {actual}")
    return failures


def check_symbol_conflict_resolution() -> list[str]:
    failures = []

    alias_result = search.postprocess_extracted_loads({
        "loads": [
            {"type": "集中力", "raw": "ql"},
            {"type": "分布力", "raw": "q"},
            {"type": "集中力偶", "raw": "m"},
        ]
    })
    alias_types = [item["type"] for item in alias_result["loads"]]
    if alias_types != ["集中", "均布", "弯矩"]:
        failures.append(f"type aliases not normalized: {alias_types}")

    preserved_q = search.postprocess_extracted_loads({
        "loads": [
            {"type": "集中", "raw": "ql"},
            {"type": "均布", "raw": "q"},
        ]
    })
    if len(preserved_q["loads"]) != 2:
        failures.append("independent q was removed next to ql")

    glm_b = [
        {"type": "集中", "raw": "2P"},
        {"type": "弯矩", "raw": "Pa"},
        {"type": "集中", "raw": "4P"},
        {"type": "均布", "raw": "2q"},
    ]
    truth_b = [
        {"type": "集中", "raw": "2P"},
        {"type": "弯矩", "raw": "Pa"},
        {"type": "集中", "raw": "4P"},
        {"type": "均布", "raw": "2P/a"},
    ]
    score = search.compute_similarity(glm_b, truth_b)
    if score != 1.0:
        failures.append(f"symbol family conflict not resolved: score={score}")

    single_q_code = search.normalize_raw("2q", "均布")
    if single_q_code != "0.011":
        failures.append(f"single 2q should stay distributed family, got {single_q_code}")

    return failures


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

    symbol_failures = check_symbol_normalization()
    if symbol_failures:
        failures += 1
        fail("symbol normalization mismatch: " + "; ".join(symbol_failures))
    else:
        ok("symbol load normalization rules valid")

    conflict_failures = check_symbol_conflict_resolution()
    if conflict_failures:
        failures += 1
        fail("symbol conflict resolution mismatch: " + "; ".join(conflict_failures))
    else:
        ok("symbol family conflict resolution valid")

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

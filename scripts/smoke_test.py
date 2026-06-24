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
from multi_agent_pipeline import (
    AUTO_CHAPTER_MIN_CONFIDENCE,
    MultiAgentCoordinator,
    RuleRouter,
    resolve_effective_chapter,
    select_rerank_candidates,
    symbolic_root,
)
from scripts.classify_question_bank import (
    guard_chapter_prediction,
    normalize_chapter_confidence,
    normalize_chapter_hint,
)
from scripts.feishu_tiku_bot import FeishuTikuOptions, MockCoordinator, TikuBot, parse_chapter


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


def check_exact_image_path(root: Path, relative_path: object) -> tuple[bool, bool, str | None]:
    """Return (exists, exact_case, actual_relative_path)."""
    parts = str(relative_path).replace("\\", "/").split("/")
    current = root
    exact_case = True

    for part in parts:
        if not part:
            continue
        if not current.is_dir():
            return False, exact_case, None

        try:
            children = list(current.iterdir())
        except OSError:
            return False, exact_case, None

        exact = next((child for child in children if child.name == part), None)
        if exact is not None:
            current = exact
            continue

        case_match = next((child for child in children if child.name.lower() == part.lower()), None)
        if case_match is not None:
            current = case_match
            exact_case = False
            continue

        return False, exact_case, None

    if not current.is_file():
        return False, exact_case, None

    try:
        actual = current.relative_to(root).as_posix()
    except ValueError:
        actual = str(current)
    return True, exact_case, actual


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
        ("P/2", "集中"): "0.0195",
        ("FP/2", "集中"): "0.0195",
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
        ("P=40kN", "集中"): "40",
        ("q=20kN/m", "均布"): "20",
        ("F1=40kN", "集中"): "40",
        ("F2=2ql", "集中"): "0.011",
    }
    failures = []
    for (raw, load_type), expected in cases.items():
        actual = search.normalize_raw(raw, load_type)
        if actual != expected:
            failures.append(f"{load_type}:{raw}: expected {expected}, got {actual}")

    unit_cases = {
        ("10", "集中"): "10kN",
        ("2.5", "均布"): "2.5kN/m",
        ("F=40", "集中"): "F=40kN",
        ("q", "均布"): "q",
    }
    for (raw, load_type), expected in unit_cases.items():
        actual = search.add_default_numeric_unit(raw, load_type)
        if actual != expected:
            failures.append(f"default unit {load_type}:{raw}: expected {expected}, got {actual}")
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


def check_path_repair_resolution() -> list[str]:
    failures = []
    cases = {
        "4力法/2钢架/1单未知量/题目1/12.jpg": "4力法/2钢架/1单未知量/题目1/0/12.jpg",
        "4力法/2钢架/1单未知量/题目1/51.JPG": "4力法/2钢架/1单未知量/题目1/51.jpg",
    }
    for old_rel, expected_rel in cases.items():
        resolved_path, resolved_rel, repaired = search.resolve_question_path(
            old_rel, chapter_name="4力法", update_excel=False
        )
        if resolved_rel != expected_rel or not resolved_path.is_file() or not repaired:
            failures.append(
                f"{old_rel}: expected {expected_rel}, got {resolved_rel}, exists={resolved_path.is_file()}, repaired={repaired}"
            )
    return failures


def check_multi_agent_routing() -> list[str]:
    router = RuleRouter()
    failures = []
    cases = [
        ([{"type": "均布", "raw": "20kN/m"}], "main", "main_numeric"),
        ([{"type": "集中", "raw": "P=40kN"}, {"type": "集中", "raw": "2P"}], "main", "main_assigned_symbolic"),
        ([{"type": "均布", "raw": "2P/a"}], "symbolic", "symbolic_unassigned"),
        ([{"type": "集中", "raw": "10"}], "main", "main_numeric"),
        ([], "needs_review", "needs_review"),
        ([{"type": "均布", "raw": "q"}, {"type": "集中", "raw": "10kN"}], "needs_review", "mixed_symbolic_numeric"),
    ]
    for loads, expected_route, expected_category in cases:
        decision, _ = router.route(loads)
        if decision.route != expected_route or decision.category != expected_category:
            failures.append(
                f"{loads}: expected {expected_route}/{expected_category}, got {decision.route}/{decision.category}"
            )

    expected_symbolic_root = search.ROOT.parent / f"{search.ROOT.name}_字母库"
    if symbolic_root(search.ROOT) != expected_symbolic_root:
        failures.append(f"symbolic root mismatch: {symbolic_root(search.ROOT)}")

    sample_results = [
        {"rank": 1, "path": "a.jpg", "name": "a.jpg", "score": 1.0},
        {"rank": 2, "path": "b.jpg", "name": "b.jpg", "score": 1.0},
        {"rank": 3, "path": "c.jpg", "name": "c.jpg", "score": 0.60},
        {"rank": 4, "path": "d.jpg", "name": "d.jpg", "score": 0.50},
    ]
    main_selected = [item["path"] for item in select_rerank_candidates(sample_results, "main")]
    if main_selected != ["a.jpg", "b.jpg"]:
        failures.append(f"main rerank pool mismatch: {main_selected}")

    symbolic_selected = [item["path"] for item in select_rerank_candidates(sample_results, "symbolic")]
    if symbolic_selected != ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]:
        failures.append(f"symbolic rerank pool mismatch: {symbolic_selected}")

    classified = {
        "chapter_hint": "5位移法",
        "chapter_confidence": AUTO_CHAPTER_MIN_CONFIDENCE,
        "chapter_evidence": "题目文字明确说明“用位移法计算图示结构”",
    }
    if resolve_effective_chapter("auto", classified) != "5位移法":
        failures.append("auto chapter should accept high-confidence chapter_hint")
    if resolve_effective_chapter("4力法", classified) != "4力法":
        failures.append("manual chapter should override auto chapter_hint")
    low_confidence = dict(classified, chapter_confidence=AUTO_CHAPTER_MIN_CONFIDENCE - 0.01)
    if resolve_effective_chapter("auto", low_confidence) is not None:
        failures.append("auto chapter should reject low-confidence chapter_hint")
    if resolve_effective_chapter("auto", {"chapter_hint": "unknown", "chapter_confidence": 1.0}) is not None:
        failures.append("auto chapter should reject unknown chapter_hint")

    coordinator = MultiAgentCoordinator(top_k=1)
    needs_chapter = coordinator.search_loads([{"type": "集中", "raw": "10kN"}], "auto", rerank=False)
    if needs_chapter.route.route != "needs_chapter":
        failures.append(f"manual loads with auto chapter should need chapter, got {needs_chapter.route.route}")
    return failures


def check_chapter_hint_normalization() -> list[str]:
    failures = []
    cases = {
        "2": "2静定结构",
        "5": "5位移法",
        "力法": "4力法",
        "用位移法计算": "5位移法",
        "力矩分配法": "6力矩分配",
        "图乘法求转角": "3静定结构位移",
        "求转角": "unknown",
        "unknown": "unknown",
        "": "unknown",
    }
    for raw, expected in cases.items():
        actual = normalize_chapter_hint(raw)
        if actual != expected:
            failures.append(f"{raw!r}: expected {expected}, got {actual}")
    if normalize_chapter_confidence("1.4") != 1.0:
        failures.append("chapter confidence should clamp above 1")
    if normalize_chapter_confidence("-0.2") != 0.0:
        failures.append("chapter confidence should clamp below 0")

    guarded = guard_chapter_prediction("3静定结构位移", 0.95, "题干明确要求'求B点的转角'，典型利用图乘法")
    if guarded[0] != "unknown":
        failures.append(f"unguarded turn-angle evidence should become unknown, got {guarded}")

    accepted = guard_chapter_prediction("6力矩分配", 1.0, "题干明确说明'试用弯矩分配法计算图示刚架'")
    if accepted[0] != "6力矩分配":
        failures.append(f"quoted moment-distribution evidence should be accepted, got {accepted}")
    return failures


def check_feishu_tiku_bot_state() -> list[str]:
    failures = []
    if parse_chapter("5") != "5位移法":
        failures.append("chapter number 5 should map to 5位移法")
    if parse_chapter("2") != "2静定结构":
        failures.append("chapter number 2 should map to 2静定结构")

    image = Path("mock-question.jpg")
    options = FeishuTikuOptions(dry_run=True)
    bot = TikuBot(options=options, coordinator=MockCoordinator([image, image, image]))
    sender = "smoke"
    first = bot.receive_image(sender, image)
    if "请选择章节" not in "\n".join(first.texts):
        failures.append("image message should prompt for chapter")
    cancelled = bot.receive_text(sender, "0")
    if not any(word in "\n".join(cancelled.texts) for word in ("退出", "取消")):
        failures.append("0 after image should cancel the session")

    bot.receive_image(sender, image)
    second = bot.receive_text(sender, "5")
    if len(second.images) != 3 or "5位移法" not in "\n".join(second.texts):
        failures.append("chapter reply should return three candidate images for 5位移法")
    cancelled_choice = bot.receive_text(sender, "0")
    if not any(word in "\n".join(cancelled_choice.texts) for word in ("退出", "取消")):
        failures.append("0 after candidates should cancel the session")

    bot.receive_image(sender, image)
    bot.receive_text(sender, "5")
    third = bot.receive_text(sender, "1")
    if "[dry-run]" not in "\n".join(third.texts) or len(third.images) != 1:
        failures.append("choice reply should return one dry-run answer image")
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

    path_repair_failures = check_path_repair_resolution()
    if path_repair_failures:
        failures += 1
        fail("path repair resolution mismatch: " + "; ".join(path_repair_failures))
    else:
        ok("stale question path repair resolution valid")

    routing_failures = check_multi_agent_routing()
    if routing_failures:
        failures += 1
        fail("multi-agent routing mismatch: " + "; ".join(routing_failures))
    else:
        ok("multi-agent bank routing rules valid")

    chapter_hint_failures = check_chapter_hint_normalization()
    if chapter_hint_failures:
        failures += 1
        fail("chapter hint normalization mismatch: " + "; ".join(chapter_hint_failures))
    else:
        ok("chapter hint normalization rules valid")

    feishu_failures = check_feishu_tiku_bot_state()
    if feishu_failures:
        failures += 1
        fail("Feishu tiku bot state mismatch: " + "; ".join(feishu_failures))
    else:
        ok("Feishu tiku bot dry-run state valid")

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
        case_mismatches = []
        for index, row in df.iterrows():
            if not check_loads_json(row["荷载"]):
                bad_json.append(index + 2)
            exists, exact_case, actual_path = check_exact_image_path(root, row["题目名称"])
            if not exists:
                missing_images.append((index + 2, str(row["题目名称"])))
            elif not exact_case:
                case_mismatches.append((index + 2, str(row["题目名称"]), actual_path))

        if bad_json:
            failures += 1
            fail(f"{chapter}: invalid load JSON in rows {bad_json[:10]}")
        else:
            ok(f"{chapter}: all load JSON cells valid")

        if missing_images:
            failures += 1
            preview = "; ".join(f"row {row}: {path}" for row, path in missing_images[:3])
            fail(f"{chapter}: image paths missing ({len(missing_images)}): {preview}")
        else:
            ok(f"{chapter}: all image paths exist")

        if case_mismatches:
            failures += 1
            preview = "; ".join(
                f"row {row}: {path} -> {actual}" for row, path, actual in case_mismatches[:3]
            )
            fail(f"{chapter}: image path case mismatches ({len(case_mismatches)}): {preview}")
        else:
            ok(f"{chapter}: image path casing exact")

    if failures:
        print(f"SUMMARY FAIL failures={failures} warnings={warnings}")
        return 1

    print(f"SUMMARY PASS warnings={warnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

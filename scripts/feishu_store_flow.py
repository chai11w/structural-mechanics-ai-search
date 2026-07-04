"""Feishu store-mode helpers for adding one question to the question bank.

This module keeps write-heavy question-bank operations out of the Feishu bot
state machine. The flow is intentionally conservative: all images stay in the
bot temp directory until the user confirms, then files are copied and the
target workbook is backed up before appending the new row.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

import search
from multi_agent_pipeline import MultiAgentCoordinator, RuleRouter, resolve_effective_chapter, symbolic_root
from scripts.apply_main_bank_update import normalized_main_loads
from scripts.build_symbolic_bank import mapped_symbolic_loads
from scripts.classify_question_bank import normalize_load_item
from scripts.store_unindexed_questions import backup_workbook, ensure_workbook, existing_rels, json_loads


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]
MAIN_CATEGORIES = {"main_numeric", "main_assigned_symbolic"}
STORE_ENTRY_COMMANDS = {"+", "新增", "入库", "store"}
ANSWER_DONE_COMMAND = "1"


@dataclass
class StoreDraft:
    question_image_path: str
    answer_image_paths: list[str] = field(default_factory=list)
    chapter: str | None = None
    loads: list[dict[str, Any]] = field(default_factory=list)
    stored_loads: list[dict[str, Any]] = field(default_factory=list)
    category: str = ""
    route: str = ""
    target_bank: str = ""
    chapter_hint: str = ""
    chapter_confidence: float = 0.0
    chapter_evidence: str = ""
    reason: str = ""

    def question_rel_placeholder(self) -> str:
        chapter = self.chapter or "unknown"
        return f"{chapter}/题目/0.jpg"


@dataclass
class StorePlan:
    chapter: str
    number: int
    target_bank: str
    workbook: Path
    question_rel_path: str
    question_target: Path
    answer_targets: list[Path]
    loads: list[dict[str, Any]]
    stored_loads: list[dict[str, Any]]


@dataclass
class StoreApplyResult:
    plan: StorePlan
    dry_run: bool
    backup_path: Path | None = None


class FeishuStoreService:
    def __init__(
        self,
        *,
        root: Path | None = None,
        symbolic: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.root = Path(root or search.ROOT)
        self.symbolic = Path(symbolic or symbolic_root(self.root))
        self.router = RuleRouter()
        self.dry_run = dry_run

    def classify_question(self, image_path: Path, coordinator: MultiAgentCoordinator) -> StoreDraft:
        classified = coordinator.qwen.classify_image(image_path)
        loads = [
            normalize_load_item(item)
            for item in search.normalize_query_loads(classified.get("loads", []))
            if isinstance(item, dict)
        ]
        route, _load_details = self.router.route(loads)
        chapter = resolve_effective_chapter("auto", classified)
        draft = StoreDraft(
            question_image_path=str(image_path),
            chapter=chapter,
            loads=loads,
            category=route.category,
            route=route.route,
            target_bank=route.route if route.route in {"main", "symbolic"} else "",
            chapter_hint=str(classified.get("chapter_hint") or ""),
            chapter_confidence=float(classified.get("chapter_confidence") or 0.0),
            chapter_evidence=str(classified.get("chapter_evidence") or ""),
            reason=route.reason,
        )
        if route.route == "main" and route.category in MAIN_CATEGORIES:
            draft.stored_loads = loads_for_main(draft.question_rel_placeholder(), loads, route.category)
        elif route.route == "symbolic" and route.category == "symbolic_unassigned":
            draft.stored_loads = mapped_symbolic_loads(loads)
        return draft

    def prepare_plan(self, draft: StoreDraft) -> StorePlan:
        if not draft.chapter or draft.chapter not in CHAPTERS:
            raise ValueError("章节未确定")
        if not draft.loads:
            raise ValueError("未识别到荷载，不能自动入库")
        if not draft.answer_image_paths:
            raise ValueError("还没有收到答案图")
        if draft.route not in {"main", "symbolic"}:
            raise ValueError(f"该题需要人工复核，不能自动入库：{draft.category or draft.route}")

        number = next_available_number(self.root, draft.chapter, len(draft.answer_image_paths))
        question_rel_path = f"{draft.chapter}/题目/{number}.jpg"
        question_target = self.root / question_rel_path
        answer_targets = [
            self.root / draft.chapter / "答案" / answer_file_name(number, index)
            for index in range(len(draft.answer_image_paths))
        ]

        if draft.route == "main":
            target_bank = "main"
            workbook = self.root / f"{draft.chapter}.xlsx"
            stored_loads = loads_for_main(question_rel_path, draft.loads, draft.category)
        else:
            target_bank = "symbolic"
            workbook = self.symbolic / f"{draft.chapter}.xlsx"
            stored_loads = mapped_symbolic_loads(draft.loads)

        return StorePlan(
            chapter=draft.chapter,
            number=number,
            target_bank=target_bank,
            workbook=workbook,
            question_rel_path=question_rel_path,
            question_target=question_target,
            answer_targets=answer_targets,
            loads=draft.loads,
            stored_loads=stored_loads,
        )

    def apply_plan(self, draft: StoreDraft) -> StoreApplyResult:
        plan = self.prepare_plan(draft)
        if self.dry_run:
            return StoreApplyResult(plan=plan, dry_run=True)

        backup_dir = Path(__file__).resolve().parents[1] / "backups" / f"feishu_store_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backup_path = backup_workbook(plan.workbook, backup_dir, plan.target_bank)

        plan.question_target.parent.mkdir(parents=True, exist_ok=True)
        for target in plan.answer_targets:
            target.parent.mkdir(parents=True, exist_ok=True)

        ensure_targets_free([plan.question_target, *plan.answer_targets])
        write_as_jpeg(Path(draft.question_image_path), plan.question_target)
        for source, target in zip(draft.answer_image_paths, plan.answer_targets):
            write_as_jpeg(Path(source), target)

        append_excel_record(plan.workbook, plan.question_rel_path, plan.stored_loads)
        return StoreApplyResult(plan=plan, dry_run=False, backup_path=backup_path)


def is_store_entry_command(text: str) -> bool:
    return text.strip().lower() in STORE_ENTRY_COMMANDS


def loads_for_main(rel_path: str, loads: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    record = {"rel_path": rel_path, "loads": loads, "category": category}
    return normalized_main_loads(record)


def answer_file_name(number: int, index: int) -> str:
    return f"{number}{'+' * index}.jpg"


def parse_number_stem(path: Path) -> int | None:
    match = re.match(r"^(\d+)", path.stem)
    if not match:
        return None
    return int(match.group(1))


def next_available_number(root: Path, chapter: str, answer_count: int) -> int:
    question_dir = root / chapter / "题目"
    answer_dir = root / chapter / "答案"
    numbers: list[int] = []
    for folder in (question_dir, answer_dir):
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                number = parse_number_stem(path)
                if number is not None:
                    numbers.append(number)

    number = (max(numbers) + 1) if numbers else 1
    while True:
        targets = [
            question_dir / f"{number}.jpg",
            *[answer_dir / answer_file_name(number, index) for index in range(answer_count)],
        ]
        if not any(path.exists() for path in targets):
            return number
        number += 1


def ensure_targets_free(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("目标文件已存在，已取消入库: " + "、".join(existing))


def write_as_jpeg(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image.convert("RGB").save(target, format="JPEG", quality=95)


def append_excel_record(workbook: Path, rel_path: str, loads: list[dict[str, Any]]) -> None:
    workbook.parent.mkdir(parents=True, exist_ok=True)
    wb, ws, headers = ensure_workbook(workbook)
    question_col = headers["题目名称"]
    loads_col = headers["荷载"]
    known = existing_rels(ws, question_col)
    rel = rel_path.replace("\\", "/")
    if rel in known:
        wb.close()
        raise ValueError(f"Excel 已存在题目路径: {rel}")

    row = [None] * ws.max_column
    row[question_col - 1] = rel
    row[loads_col - 1] = json_loads(loads)
    ws.append(row)
    wb.save(workbook)
    wb.close()


def format_store_chapter_prompt(draft: StoreDraft) -> str:
    lines = ["未能自动确定章节，请回复章节号："]
    lines.extend(f"{chapter[0]}  {chapter}" for chapter in CHAPTERS)
    lines.extend(["", "0  取消"])
    if draft.chapter_hint:
        lines.insert(1, f"识别线索：{draft.chapter_hint}（{draft.chapter_confidence:.0%}）")
    return "\n".join(lines)


def format_answer_prompt() -> str:
    return "\n".join([
        "请发送答案图。",
        "可连续发送多张；发完回复 1，取消回复 0。",
    ])


def format_answer_received(count: int) -> str:
    return "\n".join([
        f"已收到答案图 {count} 张。",
        "可继续发送；发完回复 1，取消回复 0。",
    ])


def format_store_confirmation(plan: StorePlan) -> str:
    answer_lines = [target.relative_to(plan.question_target.parents[2]).as_posix() for target in plan.answer_targets]
    lines = [
        "准备新增：",
        "",
        f"章节：{plan.chapter}",
        f"题目：{plan.question_rel_path}",
        "答案：",
        *answer_lines,
        f"荷载：{format_loads(plan.loads)}",
        f"写入：{plan.workbook.name}",
        "",
        "回复：",
        "1  确认新增并写入题库",
        "0  取消",
    ]
    return "\n".join(lines)


def format_store_success(result: StoreApplyResult) -> str:
    prefix = "[dry-run] " if result.dry_run else ""
    lines = [
        f"{prefix}已新增题目：",
        f"题目：{result.plan.question_rel_path}",
        f"答案：{len(result.plan.answer_targets)} 张",
        f"写入：{result.plan.workbook.name}",
    ]
    if result.backup_path:
        lines.append(f"备份：{result.backup_path.parent}")
    return "\n".join(lines)


def format_loads(loads: list[dict[str, Any]]) -> str:
    parts = []
    for item in loads:
        typ = str(item.get("type") or "").strip()
        raw = str(item.get("raw") or "").strip()
        if typ and raw:
            parts.append(f"{typ}：{raw}")
        elif raw:
            parts.append(raw)
    return "、".join(parts) if parts else "未识别"

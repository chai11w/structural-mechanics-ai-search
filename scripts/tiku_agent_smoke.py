"""Smoke checks for the standalone question-bank agent MVP modules."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from openpyxl import Workbook
from PIL import Image

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts.tiku_agent_memory import AgentOperation, OperationMemory, format_recent_operations
from scripts.tiku_agent_llm import normalize_llm_intent
from scripts.tiku_agent_router import route_text
from scripts.tiku_agent_tools import (
    TikuAgentTools,
    format_inspection,
    format_replace_answer_plan,
    format_soft_delete_plan,
)


def make_image(path: Path, color: str = "white") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color=color).save(path)


def make_workbook(path: Path, rel_path: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.append(["题目名称", "荷载"])
    ws.append([rel_path, json.dumps({"loads": [{"type": "均布", "raw": "10"}]}, ensure_ascii=False)])
    wb.save(path)
    wb.close()


def main() -> int:
    failures: list[str] = []

    replace_intent = route_text("把 4力法 31 题答案换成我接下来发的图")
    if replace_intent.intent != "replace_answer" or replace_intent.chapter != "4力法" or replace_intent.question_no != 31:
        failures.append(f"replace intent mismatch: {replace_intent}")

    delete_intent = route_text("删掉 5位移法 12题")
    if delete_intent.intent != "soft_delete_question" or delete_intent.chapter != "5位移法" or delete_intent.question_no != 12:
        failures.append(f"delete intent mismatch: {delete_intent}")

    recent_intent = route_text("刚才做了什么")
    if recent_intent.intent != "list_recent_ops":
        failures.append(f"recent intent mismatch: {recent_intent}")

    llm_intent = normalize_llm_intent(
        {
            "intent": "soft_delete_question",
            "target": "last_result",
            "rank": 2,
            "confidence": 0.91,
            "reason": "delete second candidate",
        },
        "删掉第二个",
    )
    if llm_intent.intent != "soft_delete_question" or llm_intent.answer_rank != 2:
        failures.append(f"LLM intent normalization mismatch: {llm_intent}")

    temp_root = BASE / "agent_smoke_tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    root = temp_root / "bank"
    memory_path = temp_root / "agent_operations.jsonl"
    question = root / "4力法" / "题目" / "31.jpg"
    answer = root / "4力法" / "答案" / "31.jpg"
    new_answer = temp_root / "new-answer.jpg"
    make_image(question)
    make_image(answer, "blue")
    make_image(new_answer, "red")
    make_workbook(root / "4力法.xlsx", "4力法/题目/31.jpg")

    memory = OperationMemory(memory_path)
    memory.append(AgentOperation("store_question", "success", chapter="4力法", question_no=31))
    recent = format_recent_operations(memory.recent())
    if "store_question" not in recent or "4力法" not in recent:
        failures.append(f"recent operations mismatch: {recent}")

    tools = TikuAgentTools(root=root, memory=memory, dry_run=True)
    inspection = tools.inspect_question("4力法", 31)
    if len(inspection.question_files) != 1 or len(inspection.answer_files) != 1 or len(inspection.excel_rows) != 1:
        failures.append(f"inspection mismatch: {format_inspection(inspection)}")

    replace_plan = tools.plan_replace_answer("4力法", 31, [new_answer])
    replace_text = format_replace_answer_plan(replace_plan, dry_run=True)
    if "准备执行：替换答案" not in replace_text or "31.jpg" not in replace_text:
        failures.append(f"replace plan text mismatch: {replace_text}")
    replace_result = tools.apply_replace_answer(replace_plan)
    if not replace_result.dry_run or not replace_result.ok:
        failures.append(f"replace dry-run result mismatch: {replace_result}")

    delete_plan = tools.plan_soft_delete("4力法", 31)
    delete_text = format_soft_delete_plan(delete_plan, dry_run=True)
    if "准备执行：软删除题目" not in delete_text or "Excel：1" not in delete_text:
        failures.append(f"delete plan text mismatch: {delete_text}")
    delete_result = tools.apply_soft_delete(delete_plan)
    if not delete_result.dry_run or not delete_result.ok:
        failures.append(f"delete dry-run result mismatch: {delete_result}")

    if failures:
        for failure in failures:
            print("FAIL " + failure)
        print(f"SUMMARY FAIL failures={len(failures)}")
        return 1

    print("PASS agent router parses common maintenance commands")
    print("PASS operation memory records and reads recent actions")
    print("PASS agent tools inspect, replace-plan, and soft-delete-plan in dry-run")
    print("SUMMARY PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

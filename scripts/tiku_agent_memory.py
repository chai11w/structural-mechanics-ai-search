"""Operation memory for the structure-mechanics question-bank agent.

This is not project memory. It records real question-bank operations so the
agent can answer "what did I just do?" and future tools can restore changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
DEFAULT_OPERATION_LOG = BASE / "data" / "agent_operations.jsonl"


@dataclass
class AgentOperation:
    action: str
    status: str
    chapter: str | None = None
    question_no: int | None = None
    user_id: str | None = None
    backup: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class OperationMemory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DEFAULT_OPERATION_LOG)

    def append(self, operation: AgentOperation) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(operation)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) >= limit:
                break
        return records


def format_recent_operations(records: list[dict[str, Any]]) -> str:
    if not records:
        return "还没有记录到题库操作。"
    lines = ["最近操作："]
    for item in records:
        time = item.get("time") or ""
        action = item.get("action") or "unknown"
        status = item.get("status") or ""
        chapter = item.get("chapter") or ""
        question_no = item.get("question_no")
        target = f"{chapter} {question_no}题" if chapter and question_no is not None else chapter
        suffix = f" - {target}" if target else ""
        lines.append(f"{time}  {action}  {status}{suffix}")
    return "\n".join(lines)

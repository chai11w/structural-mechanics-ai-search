"""Natural-language intent router for the question-bank agent MVP.

The first version is deliberately conservative: local rules handle common
Chinese commands and ambiguous write operations are routed to clarification
instead of being executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配"]
CHAPTER_ALIASES = {
    "2": "2静定结构",
    "二": "2静定结构",
    "静定结构": "2静定结构",
    "3": "3静定结构位移",
    "三": "3静定结构位移",
    "静定结构位移": "3静定结构位移",
    "图乘": "3静定结构位移",
    "4": "4力法",
    "四": "4力法",
    "力法": "4力法",
    "5": "5位移法",
    "五": "5位移法",
    "位移法": "5位移法",
    "6": "6力矩分配",
    "六": "6力矩分配",
    "力矩分配": "6力矩分配",
    "弯矩分配": "6力矩分配",
}


@dataclass
class AgentIntent:
    intent: str
    chapter: str | None = None
    question_no: int | None = None
    answer_rank: int | None = None
    target: str | None = None
    needs_image: bool = False
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)
    raw_text: str = ""
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def route_text(text: str) -> AgentIntent:
    raw = text.strip()
    clean = normalize_text(raw)
    chapter = extract_chapter(clean)
    question_no = extract_question_no(clean, chapter)
    answer_rank = extract_answer_rank(clean)

    if not clean:
        return AgentIntent("unknown", raw_text=raw, reason="empty text", confidence=0.0)

    if has_any(clean, ["最近操作", "刚才做了什么", "操作记录", "今天新增", "今天删", "今天替换"]):
        return AgentIntent("list_recent_ops", raw_text=raw, confidence=0.95)

    if has_any(clean, ["删除", "删掉", "移除", "不要了"]):
        intent = AgentIntent("soft_delete_question", chapter, question_no, raw_text=raw, confidence=0.9)
        require(intent, "chapter", chapter)
        require(intent, "question_no", question_no)
        return intent

    if has_any(clean, ["替换答案", "换答案", "答案换", "答案不对", "改答案"]):
        intent = AgentIntent(
            "replace_answer",
            chapter,
            question_no,
            target="answers",
            needs_image=True,
            raw_text=raw,
            confidence=0.9,
        )
        require(intent, "chapter", chapter)
        require(intent, "question_no", question_no)
        return intent

    if has_any(clean, ["新增", "入库", "保存这题", "存这题", "加一道", "添加"]):
        return AgentIntent("store_question", chapter, question_no, needs_image=True, raw_text=raw, confidence=0.9)

    if has_any(clean, ["查看", "看看", "查一下", "题目信息", "有什么", "现在有什么"]):
        intent = AgentIntent("inspect_question", chapter, question_no, raw_text=raw, confidence=0.85)
        require(intent, "chapter", chapter)
        require(intent, "question_no", question_no)
        return intent

    if answer_rank is not None and has_any(clean, ["答案", "第", "top"]):
        return AgentIntent("get_answer", answer_rank=answer_rank, raw_text=raw, confidence=0.8)

    if has_any(clean, ["找", "检索", "搜", "答案"]):
        return AgentIntent("search_question", chapter, needs_image=True, raw_text=raw, confidence=0.75)

    return AgentIntent("unknown", chapter, question_no, answer_rank, raw_text=raw, confidence=0.2)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword.lower() in text for keyword in keywords)


def require(intent: AgentIntent, name: str, value: object) -> None:
    if value is None:
        intent.missing.append(name)


def extract_chapter(text: str) -> str | None:
    for chapter in CHAPTERS:
        if chapter.lower() in text:
            return chapter
    for alias, chapter in sorted(CHAPTER_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias.lower() in text:
            return chapter
    return None


def extract_question_no(text: str, chapter: str | None) -> int | None:
    scoped = text
    if chapter:
        scoped = scoped.replace(chapter.lower(), "")
        scoped = re.sub(r"^[2-6]", "", scoped)
    match = re.search(r"(\d{1,4})题", scoped)
    if match:
        return int(match.group(1))
    numbers = [int(item) for item in re.findall(r"\d{1,4}", scoped)]
    if chapter and numbers:
        return numbers[-1]
    if len(numbers) == 1:
        return numbers[0]
    return None


def extract_answer_rank(text: str) -> int | None:
    top_match = re.search(r"top\s*([1-9])", text)
    if top_match:
        return int(top_match.group(1))
    digit_match = re.search(r"第([1-9])个", text)
    if digit_match:
        return int(digit_match.group(1))
    chinese = {"一": 1, "二": 2, "三": 3}
    for key, value in chinese.items():
        if f"第{key}个" in text or f"第{key}名" in text:
            return value
    return None


def format_intent(intent: AgentIntent) -> str:
    lines = [f"意图：{intent.intent}"]
    if intent.chapter:
        lines.append(f"章节：{intent.chapter}")
    if intent.question_no is not None:
        lines.append(f"题号：{intent.question_no}")
    if intent.answer_rank is not None:
        lines.append(f"答案排名：{intent.answer_rank}")
    if intent.missing:
        lines.append("缺少：" + "、".join(intent.missing))
    return "\n".join(lines)

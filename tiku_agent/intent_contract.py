"""Shared action contract and parsing helpers for the V2 question-bank Agent."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]

STATE_IDLE = "IDLE"
STATE_WAIT_CHAPTER = "WAIT_CHAPTER"
STATE_WAIT_QUESTION_CHOICE = "WAIT_QUESTION_CHOICE"
STATE_WAIT_CANDIDATE_CHOICE = "WAIT_CANDIDATE_CHOICE"

CHAPTER_ALIASES = {
    "静定": "2静定结构",
    "静定结构": "2静定结构",
    "内力": "2静定结构",
    "内力图": "2静定结构",
    "位移": "3静定结构位移",
    "图乘法": "3静定结构位移",
    "单位荷载法": "3静定结构位移",
    "力法": "4力法",
    "位移法": "5位移法",
    "力矩分配": "6力矩分配",
    "矩阵位移": "7矩阵位移",
    "影响线": "8影响线",
}


@dataclass
class IntentResult:
    """Internal adapter contract between one authorized V2 action and the dispatcher."""

    intent: str
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_chapter(text: str) -> str | None:
    clean = _normalize_text(text)
    if not clean:
        return None
    if clean.isdigit():
        for chapter in CHAPTERS:
            if chapter.startswith(clean):
                return chapter
    chapter_number = re.search(r"第?\s*([2-8二三四五六七八])\s*章", clean)
    if chapter_number:
        parsed = chinese_number_to_int(chapter_number.group(1))
        if parsed is not None:
            for chapter in CHAPTERS:
                if chapter.startswith(str(parsed)):
                    return chapter
    for chapter in CHAPTERS:
        if clean == chapter or clean in chapter or chapter in clean:
            return chapter
    for alias, chapter in CHAPTER_ALIASES.items():
        if alias in clean:
            return chapter
    return None


def chinese_number_to_int(text: str) -> int | None:
    clean = _normalize_text(text)
    if not clean:
        return None
    if clean.isdigit():
        return int(clean)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if clean in digits and digits[clean] > 0:
        return digits[clean]
    if clean == "十":
        return 10
    if "十" not in clean:
        return None
    left, right = clean.split("十", 1)
    if left == "":
        tens = 1
    elif left in digits and digits[left] > 0:
        tens = digits[left]
    else:
        return None
    if right == "":
        ones = 0
    elif right in digits:
        ones = digits[right]
    else:
        return None
    return tens * 10 + ones


def _normalize_text(text: object) -> str:
    return str(text or "").strip().replace("　", " ")

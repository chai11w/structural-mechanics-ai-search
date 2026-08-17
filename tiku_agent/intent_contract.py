"""Shared action contract and parsing helpers for the V2 question-bank Agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from tiku_shared.chapter_catalog import (
    CHAPTER_DEFINITIONS,
    LEGACY_CHAPTER_ALIASES,
    SUPPORTED_STORAGE_KEYS,
    parse_chapter_scope,
    resolve_supported_chapter,
)

CHAPTERS = list(SUPPORTED_STORAGE_KEYS)

STATE_IDLE = "IDLE"
STATE_WAIT_CHAPTER = "WAIT_CHAPTER"
STATE_WAIT_QUESTION_CHOICE = "WAIT_QUESTION_CHOICE"
STATE_WAIT_CANDIDATE_CHOICE = "WAIT_CANDIDATE_CHOICE"

CHAPTER_ALIASES = {
    alias: definition.storage_key
    for definition in CHAPTER_DEFINITIONS
    for alias in definition.aliases
}
CHAPTER_ALIASES.update(LEGACY_CHAPTER_ALIASES)


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
    # Preserve the legacy internal-number behavior until the strict scope
    # result is wired into WAIT_CHAPTER and the other user-facing entrypoints.
    return resolve_supported_chapter(text, allow_numeric=True)


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

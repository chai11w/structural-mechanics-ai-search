"""Intent parsing for the question-bank Agent.

This layer turns user text or image events into structured intents. It is
state-aware and rule-first: deterministic commands are parsed without an LLM.
An LLM fallback can be added later, but should still return one of these guarded
intent types rather than executing tools directly.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配", "7矩阵位移", "8影响线"]

STATE_IDLE = "IDLE"
STATE_WAIT_CHAPTER = "WAIT_CHAPTER"
STATE_WAIT_QUESTION_CHOICE = "WAIT_QUESTION_CHOICE"
STATE_WAIT_CANDIDATE_CHOICE = "WAIT_CANDIDATE_CHOICE"

SUPPORTED_INTENTS = {
    "search_image",
    "set_chapter",
    "select_question",
    "select_candidate",
    "cancel",
    "unsupported",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

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

FORBIDDEN_HINTS = {
    "delete": {"删", "删除", "移除", "不要这个"},
    "store": {"入库", "新增", "储存", "存题", "保存到题库", "添加题"},
    "repair": {"修复", "修路径", "路径修复", "维护", "审计"},
}


@dataclass
class IntentResult:
    intent: str
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_user_intent(
    text: str | None = None,
    *,
    state: str = STATE_IDLE,
    image_path: str | Path | None = None,
    candidate_count: int | None = None,
    question_count: int | None = None,
) -> IntentResult:
    """Parse a user event into a guarded intent.

    `image_path` should come from the entry layer when a real image message is
    received. Text path detection is only a convenience for CLI demos.
    """

    clean = _normalize_text(text)

    if image_path:
        return IntentResult("search_image", data={"image_path": str(image_path)})

    if not clean:
        return IntentResult("unsupported", ok=False, error="未收到可识别的文字或图片")

    if _is_cancel(clean):
        return IntentResult("cancel")

    forbidden = _detect_forbidden(clean)
    if forbidden:
        return IntentResult(
            "unsupported",
            ok=False,
            data={"requested_action": forbidden},
            error="当前 Agent MVP 只支持检索和取答案，不支持入库、删除或维护操作。",
        )

    path = _extract_image_path(clean)
    if path:
        return IntentResult("search_image", data={"image_path": path})

    if state == STATE_WAIT_CHAPTER:
        chapter = parse_chapter(clean)
        if chapter:
            return IntentResult("set_chapter", data={"chapter": chapter})
        return IntentResult("unsupported", ok=False, error="请回复章节号 2/3/4/5/6/7/8，或章节名。")

    if state == STATE_WAIT_QUESTION_CHOICE:
        return _parse_question_choice(clean, question_count)

    if state == STATE_WAIT_CANDIDATE_CHOICE:
        return _parse_candidate_choice(clean, candidate_count)

    # IDLE/general natural commands.
    chapter = parse_chapter(clean)
    if chapter and any(word in clean for word in ("按", "用", "章节", "搜", "查")):
        return IntentResult("set_chapter", data={"chapter": chapter})

    question = parse_question_index(clean)
    if question is not None and any(word in clean for word in ("题", "搜", "查", "第")):
        chapter_override = parse_chapter(clean)
        return IntentResult(
            "select_question",
            data={"question_index": question, "chapter_override": chapter_override},
        )

    rank = parse_ordinal(clean)
    if rank is not None and any(word in clean for word in ("答案", "候选", "选", "第", "个", "名")):
        return IntentResult("select_candidate", data={"rank": rank})

    if any(word in clean for word in ("搜", "查", "检索", "找")):
        return IntentResult("unsupported", ok=False, error="请发送题目图片，或在图片后继续选择章节/候选。")

    return IntentResult("unsupported", ok=False, error="暂时无法理解这条指令。")


def parse_chapter(text: str) -> str | None:
    clean = _normalize_text(text)
    if not clean:
        return None
    if clean.isdigit():
        for chapter in CHAPTERS:
            if chapter.startswith(clean):
                return chapter
    for chapter in CHAPTERS:
        if clean == chapter or clean in chapter or chapter in clean:
            return chapter
    for alias, chapter in CHAPTER_ALIASES.items():
        if alias in clean:
            return chapter
    return None


def parse_question_index(text: str) -> int | None:
    clean = _normalize_text(text)
    if not clean:
        return None
    match = re.search(r"第?\s*([0-9一二两三四五六七八九十]+)\s*[题問问]", clean)
    if match:
        return chinese_number_to_int(match.group(1))
    match = re.fullmatch(r"([0-9一二两三四五六七八九十]+)", clean)
    if match:
        return chinese_number_to_int(match.group(1))
    return None


def parse_ordinal(text: str) -> int | None:
    clean = _normalize_text(text)
    if not clean:
        return None
    match = re.search(r"第?\s*([0-9一二两三四五六七八九十]+)\s*(个|名|候选|答案)?", clean)
    if match:
        return chinese_number_to_int(match.group(1))
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


def _parse_question_choice(text: str, question_count: int | None) -> IntentResult:
    chapter_override = None
    left = text
    if "-" in text:
        left, right = text.split("-", 1)
        chapter_override = parse_chapter(right)
        if not chapter_override:
            return IntentResult("unsupported", ok=False, error="题号后面的章节无法识别。")

    question_index = parse_question_index(left)
    if question_index is None:
        return IntentResult("unsupported", ok=False, error="请回复题号，例如 1，或 2-4力法。")
    if question_count is not None and not 1 <= question_index <= question_count:
        return IntentResult("unsupported", ok=False, error=f"题号超出范围：{question_index}")
    return IntentResult(
        "select_question",
        data={"question_index": question_index, "chapter_override": chapter_override},
    )


def _parse_candidate_choice(text: str, candidate_count: int | None) -> IntentResult:
    rank = parse_ordinal(text)
    if rank is None:
        return IntentResult("unsupported", ok=False, error="请回复候选编号，例如 1，或回复 0 取消。")
    if candidate_count is not None and not 1 <= rank <= candidate_count:
        return IntentResult("unsupported", ok=False, error=f"候选编号超出范围：{rank}")
    return IntentResult("select_candidate", data={"rank": rank})


def _extract_image_path(text: str) -> str | None:
    # Match quoted paths first, then a simple unquoted Windows/path token.
    quoted = re.search(r"['\"]([^'\"]+\.(?:jpg|jpeg|png|bmp|webp))['\"]", text, re.IGNORECASE)
    if quoted:
        return quoted.group(1)
    match = re.search(r"([A-Za-z]:\\[^\s]+\.(?:jpg|jpeg|png|bmp|webp)|[^\s]+\.(?:jpg|jpeg|png|bmp|webp))", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _detect_forbidden(text: str) -> str | None:
    for action, hints in FORBIDDEN_HINTS.items():
        if any(hint in text for hint in hints):
            return action
    return None


def _is_cancel(text: str) -> bool:
    return text.lower() in {"0", "取消", "cancel", "退出", "算了", "不用了"}


def _normalize_text(text: object) -> str:
    return str(text or "").strip().replace("　", " ")


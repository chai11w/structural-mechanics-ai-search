"""LLM-first intent parsing for the question-bank Agent.

The intent layer converts natural-language user input into a guarded structured
intent. The LLM interprets what the user wants; Python validation decides whether
that intent is legal in the current Agent state. This layer never executes
retrieval tools.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import search
from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL, parse_model_json


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

UNSUPPORTED_ACTIONS = {"delete", "store", "repair", "cross_chapter_search"}

INTENT_SYSTEM_PROMPT = """你是结构力学题库检索 Agent 的意图识别器。

你的任务是把用户输入转换成一个 JSON intent。你只识别意图，不执行工具，不检索题库，不回答题目。

只允许输出 JSON，不要输出 Markdown。

支持章节：
- 2静定结构
- 3静定结构位移
- 4力法
- 5位移法
- 6力矩分配
- 7矩阵位移
- 8影响线

允许的 intent：
- search_image：用户要搜索图片，或文字里给了图片路径。
- set_chapter：用户指定章节。
- select_question：用户在多题列表中选择题号，可同时指定章节。
- select_candidate：用户在候选列表中选择答案候选。
- cancel：用户取消/退出当前流程。
- unsupported：其他、无法判断、当前版本不支持，或用户要求删除/入库/维护。

禁止：
- 不要输出 delete/store/repair 作为 intent。
- 如果用户想删除、入库、维护、修复路径或跨章节盲搜，intent 必须是 unsupported，并在 requested_action 写 delete/store/repair/cross_chapter_search。

输出 JSON 格式：
{
  "intent": "search_image|set_chapter|select_question|select_candidate|cancel|unsupported",
  "image_path": null,
  "chapter": null,
  "question_index": null,
  "rank": null,
  "requested_action": null,
  "confidence": 0.0,
  "reason": "简短中文理由"
}

根据当前状态解释用户输入：
- WAIT_CHAPTER：用户通常是在补章节。
- WAIT_QUESTION_CHOICE：用户通常是在选多题里的题号。
- WAIT_CANDIDATE_CHOICE：用户通常是在选候选答案。
- IDLE：用户通常是在开始搜索或表达自然语言任务。"""


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
    use_llm: bool = True,
    llm_client: Callable[[str], dict[str, Any]] | None = None,
) -> IntentResult:
    """Parse a user event into a guarded intent.

    `image_path` should come from the entry layer when a real image message is
    received. Text path detection is still handled locally for CLI demos. For
    natural-language text, this function is LLM-first by default.
    """

    clean = _normalize_text(text)

    if image_path:
        return validate_intent_payload(
            {"intent": "search_image", "image_path": str(image_path), "confidence": 1.0, "reason": "入口层收到图片"},
            state=state,
            candidate_count=candidate_count,
            question_count=question_count,
            source="entry",
        )

    if not clean:
        return IntentResult("unsupported", ok=False, error="未收到可识别的文字或图片")

    path = _extract_image_path(clean)
    if path:
        return validate_intent_payload(
            {"intent": "search_image", "image_path": path, "confidence": 1.0, "reason": "文字中包含图片路径"},
            state=state,
            candidate_count=candidate_count,
            question_count=question_count,
            source="rule",
        )

    if use_llm:
        try:
            payload = (llm_client or call_qwen_intent)(
                build_intent_prompt(
                    clean,
                    state=state,
                    candidate_count=candidate_count,
                    question_count=question_count,
                )
            )
            return validate_intent_payload(
                payload,
                state=state,
                candidate_count=candidate_count,
                question_count=question_count,
                source="llm",
            )
        except Exception as exc:  # noqa: BLE001 - fallback is only for parser availability.
            fallback = parse_user_intent_rule_fallback(
                clean,
                state=state,
                candidate_count=candidate_count,
                question_count=question_count,
            )
            fallback.error = fallback.error or f"LLM intent failed, used rule fallback: {exc}"
            fallback.source = "rule_fallback"
            return fallback

    return parse_user_intent_rule_fallback(
        clean,
        state=state,
        candidate_count=candidate_count,
        question_count=question_count,
    )


def build_intent_prompt(
    user_text: str,
    *,
    state: str,
    candidate_count: int | None = None,
    question_count: int | None = None,
) -> str:
    context = {
        "state": state,
        "user_text": user_text,
        "candidate_count": candidate_count,
        "question_count": question_count,
        "supported_chapters": CHAPTERS,
        "allowed_intents": sorted(SUPPORTED_INTENTS),
    }
    return "当前上下文 JSON：\n" + json.dumps(context, ensure_ascii=False, indent=2)


def call_qwen_intent(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: int = 60,
) -> dict[str, Any]:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 256,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    parsed["_raw_content"] = content
    parsed["_usage"] = data.get("usage", {})
    return parsed


def validate_intent_payload(
    payload: dict[str, Any],
    *,
    state: str,
    candidate_count: int | None = None,
    question_count: int | None = None,
    source: str = "llm",
) -> IntentResult:
    intent = str(payload.get("intent") or "unsupported").strip()
    if intent not in SUPPORTED_INTENTS:
        return IntentResult("unsupported", ok=False, error=f"LLM returned unsupported intent: {intent}", source=source)

    confidence = _safe_float(payload.get("confidence"))
    reason = str(payload.get("reason") or "").strip()
    data: dict[str, Any] = {"confidence": confidence, "reason": reason}

    requested_action = _normalize_requested_action(payload.get("requested_action"))
    if requested_action in UNSUPPORTED_ACTIONS:
        data["requested_action"] = requested_action
        return IntentResult(
            "unsupported",
            ok=False,
            data=data,
            error="当前 Agent MVP 只支持检索和取答案，不支持入库、删除、维护或跨章节盲搜。",
            source=source,
        )

    if intent == "unsupported":
        if requested_action:
            data["requested_action"] = requested_action
        return IntentResult("unsupported", ok=False, data=data, error=reason or "暂时无法理解这条指令。", source=source)

    if intent == "cancel":
        return IntentResult("cancel", data=data, source=source)

    if intent == "search_image":
        image_path = str(payload.get("image_path") or "").strip()
        if image_path:
            data["image_path"] = image_path
        return IntentResult("search_image", data=data, source=source)

    if intent == "set_chapter":
        chapter = parse_chapter(str(payload.get("chapter") or ""))
        if not chapter:
            return IntentResult("unsupported", ok=False, data=data, error="章节无法识别或不在 2-8 章范围内。", source=source)
        if state not in {STATE_IDLE, STATE_WAIT_CHAPTER, STATE_WAIT_QUESTION_CHOICE}:
            return IntentResult("unsupported", ok=False, data=data, error="当前状态不允许重新设置章节。", source=source)
        data["chapter"] = chapter
        return IntentResult("set_chapter", data=data, source=source)

    if intent == "select_question":
        question_index = _coerce_positive_int(payload.get("question_index"))
        if question_index is None:
            return IntentResult("unsupported", ok=False, data=data, error="题号无法识别。", source=source)
        if question_count is not None and not 1 <= question_index <= question_count:
            return IntentResult("unsupported", ok=False, data=data, error=f"题号超出范围：{question_index}", source=source)
        if state not in {STATE_IDLE, STATE_WAIT_QUESTION_CHOICE}:
            return IntentResult("unsupported", ok=False, data=data, error="当前状态不允许选择多题题号。", source=source)
        chapter = parse_chapter(str(payload.get("chapter") or ""))
        data.update({"question_index": question_index, "chapter_override": chapter})
        return IntentResult("select_question", data=data, source=source)

    if intent == "select_candidate":
        rank = _coerce_positive_int(payload.get("rank"))
        if rank is None:
            return IntentResult("unsupported", ok=False, data=data, error="候选编号无法识别。", source=source)
        if candidate_count is not None and not 1 <= rank <= candidate_count:
            return IntentResult("unsupported", ok=False, data=data, error=f"候选编号超出范围：{rank}", source=source)
        if state != STATE_WAIT_CANDIDATE_CHOICE:
            return IntentResult("unsupported", ok=False, data=data, error="当前状态不允许选择候选答案。", source=source)
        data["rank"] = rank
        return IntentResult("select_candidate", data=data, source=source)

    return IntentResult("unsupported", ok=False, data=data, error="暂时无法理解这条指令。", source=source)


def parse_user_intent_rule_fallback(
    text: str | None = None,
    *,
    state: str = STATE_IDLE,
    candidate_count: int | None = None,
    question_count: int | None = None,
) -> IntentResult:
    """Small deterministic fallback used when LLM intent is unavailable."""

    clean = _normalize_text(text)
    if not clean:
        return IntentResult("unsupported", ok=False, error="未收到可识别的文字或图片", source="rule_fallback")
    if _is_cancel(clean):
        return IntentResult("cancel", source="rule_fallback")
    path = _extract_image_path(clean)
    if path:
        return IntentResult("search_image", data={"image_path": path}, source="rule_fallback")
    if state == STATE_WAIT_CHAPTER:
        chapter = parse_chapter(clean)
        if chapter:
            return IntentResult("set_chapter", data={"chapter": chapter}, source="rule_fallback")
    if state == STATE_WAIT_QUESTION_CHOICE:
        return _parse_question_choice(clean, question_count)
    if state == STATE_WAIT_CANDIDATE_CHOICE:
        return _parse_candidate_choice(clean, candidate_count)
    return IntentResult("unsupported", ok=False, error="LLM 不可用，规则 fallback 无法理解这条指令。", source="rule_fallback")


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
            return IntentResult("unsupported", ok=False, error="题号后面的章节无法识别。", source="rule_fallback")

    question_index = parse_question_index(left)
    if question_index is None:
        return IntentResult("unsupported", ok=False, error="请回复题号，例如 1，或 2-4力法。", source="rule_fallback")
    if question_count is not None and not 1 <= question_index <= question_count:
        return IntentResult("unsupported", ok=False, error=f"题号超出范围：{question_index}", source="rule_fallback")
    return IntentResult(
        "select_question",
        data={"question_index": question_index, "chapter_override": chapter_override},
        source="rule_fallback",
    )


def _parse_candidate_choice(text: str, candidate_count: int | None) -> IntentResult:
    rank = parse_ordinal(text)
    if rank is None:
        return IntentResult("unsupported", ok=False, error="请回复候选编号，例如 1，或回复 0 取消。", source="rule_fallback")
    if candidate_count is not None and not 1 <= rank <= candidate_count:
        return IntentResult("unsupported", ok=False, error=f"候选编号超出范围：{rank}", source="rule_fallback")
    return IntentResult("select_candidate", data={"rank": rank}, source="rule_fallback")


def _extract_image_path(text: str) -> str | None:
    # Match quoted paths first, then a simple unquoted Windows/path token.
    quoted = re.search(r"['\"]([^'\"]+\.(?:jpg|jpeg|png|bmp|webp))['\"]", text, re.IGNORECASE)
    if quoted:
        return quoted.group(1)
    match = re.search(r"([A-Za-z]:\\[^\s]+\.(?:jpg|jpeg|png|bmp|webp)|[^\s]+\.(?:jpg|jpeg|png|bmp|webp))", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _is_cancel(text: str) -> bool:
    return text.lower() in {"0", "取消", "cancel", "退出", "算了", "不用了"}


def _normalize_text(text: object) -> str:
    return str(text or "").strip().replace("　", " ")


def _safe_float(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0)))
    except (TypeError, ValueError):
        return 0.0


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    parsed = parse_ordinal(str(value or ""))
    return parsed if parsed and parsed > 0 else None


def _normalize_requested_action(value: object) -> str | None:
    text = str(value or "").strip().lower()
    aliases = {
        "删除": "delete",
        "删": "delete",
        "delete": "delete",
        "入库": "store",
        "新增": "store",
        "store": "store",
        "修复": "repair",
        "维护": "repair",
        "repair": "repair",
        "跨章节": "cross_chapter_search",
        "cross_chapter_search": "cross_chapter_search",
    }
    return aliases.get(text)

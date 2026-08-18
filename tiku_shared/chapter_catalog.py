"""Shared chapter catalog and deterministic text scope classification.

The storage key is an implementation detail (for example ``4力法``).  The
topic id and display name are stable semantic identifiers used by later Agent
layers.  Text classification deliberately returns three states so callers can
stop on an explicit out-of-scope topic instead of treating it as an unknown
supported chapter.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal


ChapterScopeStatus = Literal["supported", "unsupported", "uncertain"]


@dataclass(frozen=True)
class ChapterDefinition:
    topic_id: str
    display_name: str
    storage_key: str
    textbook_aliases: tuple[str, ...] = ()
    method_aliases: tuple[str, ...] = ()

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.storage_key, self.display_name) + self.textbook_aliases + self.method_aliases))


@dataclass(frozen=True)
class UnsupportedTopicDefinition:
    topic_id: str
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ChapterScopeResult:
    status: ChapterScopeStatus
    topic_id: str | None = None
    storage_key: str | None = None
    display_name: str | None = None
    matched_text: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status == "supported":
            if not self.topic_id or not self.storage_key or not self.display_name:
                raise ValueError("supported scope requires topic_id, storage_key and display_name")
        elif self.status == "unsupported":
            if not self.topic_id or not self.display_name or self.storage_key is not None:
                raise ValueError("unsupported scope requires topic_id/display_name and no storage_key")
        elif self.status == "uncertain":
            if any(value is not None for value in (self.topic_id, self.storage_key, self.display_name)):
                raise ValueError("uncertain scope cannot select a topic")
        else:
            raise ValueError(f"unknown chapter scope status: {self.status}")


CHAPTER_DEFINITIONS = (
    ChapterDefinition(
        topic_id="static_internal_force",
        display_name="静定结构受力",
        storage_key="2静定结构",
        textbook_aliases=("静定结构", "静定梁", "静定刚架", "静定钢架", "静定桁架"),
    ),
    ChapterDefinition(
        topic_id="static_displacement",
        display_name="静定结构位移",
        storage_key="3静定结构位移",
        textbook_aliases=("静定结构位移",),
        method_aliases=("图乘法", "单位荷载法"),
    ),
    ChapterDefinition(
        topic_id="force_method",
        display_name="力法",
        storage_key="4力法",
        method_aliases=("力法",),
    ),
    ChapterDefinition(
        topic_id="displacement_method",
        display_name="位移法",
        storage_key="5位移法",
        method_aliases=("位移法", "转角位移法", "转角位移方程"),
    ),
    ChapterDefinition(
        topic_id="moment_distribution",
        display_name="力矩分配法",
        storage_key="6力矩分配",
        method_aliases=("力矩分配", "力矩分配法", "弯矩分配", "弯矩分配法", "渐近法"),
    ),
    ChapterDefinition(
        topic_id="matrix_displacement",
        display_name="矩阵位移法",
        storage_key="7矩阵位移",
        method_aliases=("矩阵位移", "矩阵位移法"),
    ),
    ChapterDefinition(
        topic_id="influence_line",
        display_name="影响线",
        storage_key="8影响线",
        method_aliases=("影响线",),
    ),
)


UNSUPPORTED_TOPIC_DEFINITIONS = (
    UnsupportedTopicDefinition(
        topic_id="geometric_composition",
        display_name="几何组成分析",
        aliases=("几何组成分析", "几何组成", "几何构造"),
    ),
    UnsupportedTopicDefinition(
        topic_id="structural_dynamics",
        display_name="结构动力学",
        aliases=("结构动力学", "动力学", "自振频率", "自由振动", "动力反应"),
    ),
    UnsupportedTopicDefinition(
        topic_id="stability",
        display_name="结构稳定",
        aliases=("结构稳定", "稳定问题", "稳定", "压杆稳定", "屈曲"),
    ),
    UnsupportedTopicDefinition(
        topic_id="limit_load",
        display_name="极限荷载",
        aliases=("极限荷载", "极限状态", "极限分析", "塑性极限"),
    ),
    UnsupportedTopicDefinition(
        topic_id="non_chinese_question",
        display_name="非中文题目",
        aliases=("英文题目", "英文题", "英文题库", "英语题", "英语题目"),
    ),
)


SUPPORTED_STORAGE_KEYS = tuple(item.storage_key for item in CHAPTER_DEFINITIONS)

# Kept only for callers that still use the pre-catalog parser.  New workflow
# code must use ``parse_chapter_scope`` so generic words such as “位移” do not
# silently select a directory.
LEGACY_CHAPTER_ALIASES = {
    "静定": "2静定结构",
}


def parse_chapter_scope(text: object) -> ChapterScopeResult:
    """Classify explicit chapter/topic evidence without choosing on ambiguity.

    Unsupported topics are checked first so phrases such as ``第九章动力学``
    cannot be turned into a supported directory by the numeric prefix.  A bare
    or textbook-relative chapter number is intentionally uncertain because the
    storage key is not a textbook chapter number.
    """

    normalized = _compact(text)
    if not normalized:
        return _uncertain("empty_text")

    unsupported_match = _find_alias(normalized, UNSUPPORTED_TOPIC_DEFINITIONS)
    if unsupported_match is not None:
        definition, alias = unsupported_match
        return ChapterScopeResult(
            status="unsupported",
            topic_id=definition.topic_id,
            display_name=definition.display_name,
            matched_text=alias,
            reason="explicit_unsupported_topic",
        )

    supported_match = _find_alias(normalized, CHAPTER_DEFINITIONS)
    if supported_match is not None:
        definition, alias = supported_match
        return ChapterScopeResult(
            status="supported",
            topic_id=definition.topic_id,
            storage_key=definition.storage_key,
            display_name=definition.display_name,
            matched_text=alias,
            reason="explicit_supported_alias",
        )

    if _looks_like_chapter_number(normalized):
        return _uncertain("numeric_chapter_requires_textbook", matched_text=normalized)
    return _uncertain("no_explicit_topic_evidence")


def detect_non_chinese_problem_text(text: object) -> ChapterScopeResult | None:
    """Reject a visible problem description when it is clearly non-Chinese.

    This is intended for OCR text supplied by an image boundary, not for raw
    image bytes.  Empty text means no description was recognized and must stay
    eligible for chapter clarification.  A non-empty natural-language
    description without a Chinese character is rejected; one Chinese
    character is enough to keep it eligible for normal chapter parsing.
    Formula/load-label fragments such as ``EI=200, P=20`` are treated as
    missing description instead of as an English question.
    """

    raw = _normalize_visible_text(text)
    if not raw or re.search(r"[\u3400-\u9fff]", raw) or _looks_like_symbolic_annotation(raw):
        return None
    return ChapterScopeResult(
        status="unsupported",
        topic_id="non_chinese_question",
        display_name="非中文题目",
        matched_text=raw[:60],
        reason="non_chinese_problem_text",
    )


def resolve_image_scope(
    chapter_hint: object = "",
    chapter_confidence: object = 0.0,
    visible_problem_text: object = "",
    *,
    min_confidence: float = 0.45,
) -> ChapterScopeResult:
    """Resolve the chapter scope at an image boundary before runtime routing.

    The visible problem text is deterministic evidence and therefore takes
    precedence over a model-produced hint.  A model hint can only select a
    supported storage key after it has passed the same catalog parser, has
    sufficient confidence, and has non-empty visible problem text.  This
    function deliberately returns the catalog's three states instead of
    returning a chapter string that callers might search unconditionally.
    """

    visible_text = _normalize_visible_text(visible_problem_text)
    if _looks_like_symbolic_annotation(visible_text):
        # Load labels and formula fragments are not a problem description.
        visible_text = ""

    language_scope = detect_non_chinese_problem_text(visible_text)
    if language_scope is not None:
        return language_scope

    text_scope = parse_chapter_scope(visible_text)
    if text_scope.status == "unsupported":
        return replace(text_scope, reason="visible_problem_text_unsupported")
    if text_scope.status == "supported":
        return replace(text_scope, reason="visible_problem_text_supported")

    # The image prompts require visible text before a chapter prediction is
    # trusted.  An empty OCR result therefore remains a clarification case,
    # even when a stale or overconfident model hint is present.
    if not _compact(visible_text):
        return _uncertain("missing_visible_problem_text")

    hint_scope = parse_chapter_scope(chapter_hint)
    if hint_scope.status == "unsupported":
        return replace(hint_scope, reason="model_chapter_hint_unsupported")

    confidence = _coerce_confidence(chapter_confidence)
    threshold = _coerce_confidence(min_confidence, default=0.45)
    if hint_scope.status == "supported" and confidence >= threshold:
        return replace(hint_scope, reason="model_chapter_hint")
    if hint_scope.status == "supported":
        return _uncertain("low_confidence_chapter_hint", matched_text=hint_scope.matched_text)
    return _uncertain("no_valid_chapter_evidence")


def resolve_supported_chapter(text: object, *, allow_numeric: bool = False) -> str | None:
    """Return a storage key for compatibility callers.

    New workflow code should use :func:`parse_chapter_scope`.  ``allow_numeric``
    exists only for legacy callers that historically treated internal numbers
    as chapter names; it must not be used for user-facing textbook answers.
    """

    normalized = _compact(text)
    if allow_numeric:
        for alias, storage_key in LEGACY_CHAPTER_ALIASES.items():
            if alias in normalized:
                return storage_key
        number = _chapter_number(normalized)
        if number is None:
            match = re.search(r"第?([0-9一二两三四五六七八九十百]+)章", normalized)
            number = _parse_number_token(match.group(1)) if match else None
        if number is not None:
            prefix = f"{number}"
            for definition in CHAPTER_DEFINITIONS:
                if definition.storage_key.startswith(prefix):
                    return definition.storage_key
    result = parse_chapter_scope(text)
    return result.storage_key if result.status == "supported" else None


def supported_topic_names() -> tuple[str, ...]:
    return tuple(item.display_name for item in CHAPTER_DEFINITIONS)


def _find_alias(normalized: str, definitions: tuple[object, ...]):
    candidates: list[tuple[int, int, object, str]] = []
    for definition in definitions:
        aliases = definition.aliases if isinstance(definition, ChapterDefinition) else definition.aliases
        for alias in aliases:
            compact_alias = _compact(alias)
            if compact_alias and _contains_catalog_alias(normalized, compact_alias, definition):
                if isinstance(definition, ChapterDefinition):
                    if compact_alias in {_compact(item) for item in definition.method_aliases}:
                        priority = 3
                    elif compact_alias in {_compact(definition.storage_key), _compact(definition.display_name)}:
                        priority = 2
                    else:
                        priority = 1
                else:
                    priority = 2
                candidates.append((priority, len(compact_alias), definition, compact_alias))
    if not candidates:
        return None
    _, _, definition, alias = max(candidates, key=lambda item: (item[0], item[1]))
    return definition, alias


def _contains_catalog_alias(normalized: str, alias: str, definition: object) -> bool:
    if not isinstance(definition, ChapterDefinition):
        return alias in normalized
    if definition.topic_id not in {"static_internal_force", "static_displacement"}:
        return alias in normalized
    # “超静定/不静定/非静定” must not satisfy the positive “静定...” aliases.
    pattern = rf"(?<![超不非]){re.escape(alias)}"
    return re.search(pattern, normalized) is not None


def _uncertain(reason: str, *, matched_text: str = "") -> ChapterScopeResult:
    return ChapterScopeResult(status="uncertain", matched_text=matched_text, reason=reason)


def _coerce_confidence(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(confidence):
        return default
    return max(0.0, min(1.0, confidence))


def _compact(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip().replace("　", " ")
    return re.sub(r"[\s，。！？!?、,.：:；;“”\"'‘’（）()]+", "", normalized).lower()


_SYMBOLIC_ANNOTATION_CHARS = re.compile(r"^[A-Za-z0-9_\s.,;=+\-*/^()·°²³⁴⁵⁶⁷⁸⁹]+$")
_SYMBOLIC_WORDS = {
    "alpha",
    "beta",
    "cos",
    "delta",
    "ea",
    "ei",
    "gamma",
    "kpa",
    "kn",
    "ln",
    "log",
    "mpa",
    "n",
    "pa",
    "rad",
    "sin",
    "sqrt",
    "sigma",
    "tan",
    "theta",
    "omega",
}


def _normalize_visible_text(text: object) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _looks_like_symbolic_annotation(text: str) -> bool:
    """Recognize formula/load labels without treating English prose as empty."""

    if not text or not _SYMBOLIC_ANNOTATION_CHARS.fullmatch(text):
        return False
    if not re.search(r"\d|=|[+\-*/^]", text):
        return False
    for word in re.findall(r"[A-Za-z]+", text):
        normalized = word.lower()
        if len(normalized) <= 3 or normalized in _SYMBOLIC_WORDS:
            continue
        return False
    return True


def _looks_like_chapter_number(text: str) -> bool:
    return _chapter_number(text) is not None


def _chapter_number(text: str) -> int | None:
    match = re.fullmatch(r"第?([0-9一二两三四五六七八九十百]+)章?", text)
    if not match:
        return None
    raw = match.group(1)
    return _parse_number_token(raw)


def _parse_number_token(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if raw in digits:
        return digits[raw]
    if raw.startswith("十") and raw[1:] in digits:
        return 10 + digits[raw[1:]]
    if raw.endswith("十") and raw[:-1] in digits:
        return digits[raw[:-1]] * 10
    return None

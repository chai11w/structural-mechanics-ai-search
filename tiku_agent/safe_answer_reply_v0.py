"""Deterministic replies for the safe-answer V0 routing seam.

These replies intentionally contain no model calls, tool calls, runtime facts,
or state-dependent claims.  They establish the concise response contract that
a later bounded model generator must satisfy.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.safe_answer_contract_v0 import (
    MAX_SAFE_ANSWER_CHARS,
    validate_safe_answer_output_v0,
)
from tiku_agent.safe_answer_context_v0 import (
    SafeAnswerValidationFacts,
    SafeConversationContext,
)

_SAFE_REPLIES = {
    "greeting": "你好。",
    "courtesy": "不客气。",
    "farewell": "再见，随时欢迎回来。",
    "general": "这个问题我暂时无法直接处理，可以继续帮你检索结构力学题库。",
    "identity": "我是力答，一个结构力学题库搜索助手，主要帮你从题库里找最相似的候选题。",
    "capability": "我可以根据题图从题库检索最相似的题目，并在你选择后定位对应答案。",
    "workflow": "我会先识别题图并尝试判断章节，再检索和复筛相似候选；你选定后，我再返回对应答案。",
}


_SUPPORTED_CHAPTERS_QUESTION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:你|力答|这个助手|这个机器人|题库)?(?:可以|能|支持|覆盖|包含|包括)(?:回答|处理|搜索|搜|查找|查|检索)?(?:哪些|哪几个|哪几|第几)(?:个)?(?:章节|章)(?:的)?(?:问题|题目|题|内容)?",
        r"(?:你|力答|这个助手|这个机器人|题库)?(?:支持|覆盖|包含|包括)(?:的)?(?:章节|章)(?:有)?(?:哪些|哪几个|哪几)",
        r"题库(?:里)?(?:有|收录)(?:哪些|哪几个|哪几)(?:个)?(?:章节|章)(?:的)?(?:问题|题目|题|内容)?",
    )
)

_LIMITED_SUPPORT_TOPICS = ("矩阵位移", "影响线")


def render_grounded_safe_answer_v0(text: str | None) -> str | None:
    """Return a code-grounded fact reply for one narrowly recognized question.

    This seam is deliberately stricter than model-backed safe conversation:
    only a complete supported-chapter question matches, so mixed business
    commands continue through the existing intent and tool route.
    """

    compact = re.sub(
        r"[\s，。！？!?、,.：:；;“”\"'（）()]+",
        "",
        unicodedata.normalize("NFKC", str(text or "")).strip().lower(),
    )
    if not compact or not any(
        pattern.fullmatch(compact)
        for pattern in _SUPPORTED_CHAPTERS_QUESTION_PATTERNS
    ):
        return None

    topics = [re.sub(r"^\d+", "", chapter) for chapter in CHAPTERS]
    missing_limits = [
        topic for topic in _LIMITED_SUPPORT_TOPICS if topic not in topics
    ]
    if missing_limits:
        raise ValueError(
            "limited support topic is absent from CHAPTERS: "
            + "、".join(missing_limits)
        )
    full_support = [topic for topic in topics if topic not in _LIMITED_SUPPORT_TOPICS]
    reply = (
        "结构力学题库支持"
        + "、".join(full_support)
        + "；"
        + "和".join(_LIMITED_SUPPORT_TOPICS)
        + "仅支持含具体外荷载的题目。"
    )
    validation = validate_safe_answer_output_v0(reply, "capability")
    if not validation.accepted:
        raise ValueError(
            "grounded safe-answer fact violates the response contract: "
            f"{validation.reason}"
        )
    return validation.normalized_text


# Phase-aware fallback copy, keyed by (category, phase).  The table is
# intentionally empty: business guidance for each phase already lives in the
# render.py state machine (chapter prompt, global search offer, error/retry,
# candidate list), so a pure chitchat fallback needs no phase-specific wording.
# A builder, when registered, must return text that passes the output contract;
# otherwise the generic fixed reply is used.
_PHASE_REPLY_BUILDERS: dict[
    tuple[str, str],
    Callable[[SafeConversationContext], str],
] = {}


def render_safe_answer_v0(
    category: str,
    context: SafeConversationContext | None = None,
    validation_facts: SafeAnswerValidationFacts | None = None,
) -> str:
    """Return one reviewed, concise reply for an eligible pure conversation.

    When ``context`` is provided and a phase-aware builder is registered for
    ``(category, phase)``, its copy is used if it satisfies the output contract.
    Every other call falls back to the reviewed fixed reply, so a model failure
    always yields a valid single-line answer, never an empty one.
    """

    if context is not None:
        builder = _PHASE_REPLY_BUILDERS.get((category, context.phase))
        if builder is not None:
            reply = builder(context)
            validation = validate_safe_answer_output_v0(
                reply,
                category,
                context,
                validation_facts,
            )
            if validation.accepted:
                return validation.normalized_text
    try:
        reply = _SAFE_REPLIES[category]
    except KeyError as exc:
        raise ValueError(f"unsupported safe-answer category: {category}") from exc
    validation = validate_safe_answer_output_v0(reply, category)
    if not validation.accepted:
        raise ValueError(
            f"safe-answer V0 reply violates the response contract: {validation.reason}"
        )
    return validation.normalized_text

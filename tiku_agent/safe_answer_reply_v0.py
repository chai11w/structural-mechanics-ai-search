"""Deterministic replies for the safe-answer V0 routing seam.

These replies intentionally contain no model calls, tool calls, runtime facts,
or state-dependent claims.  They establish the concise response contract that
a later bounded model generator must satisfy.
"""

from __future__ import annotations

from tiku_agent.safe_answer_contract_v0 import (
    MAX_SAFE_ANSWER_CHARS,
    validate_safe_answer_output_v0,
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


def render_safe_answer_v0(category: str) -> str:
    """Return one reviewed, concise reply for an eligible pure conversation."""

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

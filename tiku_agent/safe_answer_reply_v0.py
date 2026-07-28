"""Deterministic replies for the safe-answer V0 routing seam.

These replies intentionally contain no model calls, tool calls, runtime facts,
or state-dependent claims.  They establish the concise response contract that
a later bounded model generator must satisfy.
"""

from __future__ import annotations


MAX_SAFE_ANSWER_CHARS = 90

_SAFE_REPLIES = {
    "greeting": "你好，需要搜题时把题图和章节发给我就行。",
    "courtesy": "不客气。",
    "identity": "我是结构力学题库助手，主要帮你检索相似题和定位答案。",
    "capability": "我可以按题图和章节检索相似题、切换候选，并定位对应答案。",
    "workflow": "我会先识别题图和章节，再按荷载与结构特征检索、排序相似题，最后按你的选择定位答案。",
}


def render_safe_answer_v0(category: str) -> str:
    """Return one reviewed, concise reply for an eligible pure conversation."""

    try:
        reply = _SAFE_REPLIES[category]
    except KeyError as exc:
        raise ValueError(f"unsupported safe-answer category: {category}") from exc
    if not reply or len(reply) > MAX_SAFE_ANSWER_CHARS or "\n" in reply:
        raise ValueError("safe-answer V0 reply violates the concise response contract")
    return reply

"""Concise, deterministic user-facing replies for non-tool Intent V2 actions."""

from __future__ import annotations

from tiku_agent.action_decision_v2 import CONVERSATION_ACTIONS, ActionDecisionV2
from tiku_agent.conversation_context_v2 import ConversationContextV2


MAX_REPLY_CHARS = 90


def render_reply_shell_v2(
    decision: ActionDecisionV2,
    context: ConversationContextV2,
) -> str:
    """Render only zero-tool conversation/rejection actions.

    Task actions deliberately raise so callers keep using the existing concise
    V1 business renderers instead of duplicating search-result wording here.
    """

    if decision.action == "greeting":
        if context.has_answer or context.candidate_count:
            reply = "在的。你可以继续选题、看其他候选，或者发下一张题图。"
        else:
            reply = "在的，发一张结构力学题图就可以。"
    elif decision.action == "small_talk":
        reply = "不客气，我们继续看题。"
    elif decision.action == "capability_help":
        reply = "我可以按题图找相似题、切换题号或候选、修改章节并重发答案。发题图或直接说要做什么就行。"
    elif decision.action == "out_of_scope":
        reply = "这个我不处理。我主要帮你检索结构力学题库、选题和找答案。"
    elif decision.action == "clarification":
        reply = _render_clarification(decision.clarification_reason or "ambiguous_action")
    elif decision.action == "reject":
        reply = _render_rejection(decision.requested_action or "")
    else:
        raise ValueError(f"reply shell does not render task action: {decision.action}")

    if len(reply) > MAX_REPLY_CHARS or "\n" in reply:
        raise ValueError("reply shell produced an overlong or multiline response")
    return reply


def _render_clarification(reason: str) -> str:
    replies = {
        "ambiguous_reference": "你指的是哪一道题，还是哪个候选？",
        "ambiguous_number_namespace": "这个编号是题号，还是候选编号？",
        "ambiguous_action": "你想继续搜题、选择题号，还是查看候选答案？",
        "missing_question_index": "请告诉我是第几题。",
        "missing_candidate_rank": "请告诉我选第几个候选。",
        "missing_chapter": "请告诉我这题按哪一章检索。",
        "missing_image": "请先发题图，我再继续处理。",
        "out_of_range": "这个编号超出当前范围了，请换一个。",
    }
    return replies.get(reason, replies["ambiguous_action"])


def _render_rejection(requested_action: str) -> str:
    replies = {
        "delete": "这里不能直接删除题库内容。需要维护时请走原来的确认流程。",
        "store": "这里不能直接把题目入库。入库仍需走原来的确认流程。",
        "repair": "这里不能直接修复题库或答案路径。维护操作仍需单独确认。",
        "cross_chapter_search": "我不能跳过章节边界盲搜。请告诉我章节，或让我根据题干判断。",
    }
    return replies.get(requested_action, "这个操作不在当前搜题范围内。")


def is_reply_shell_action(action: str) -> bool:
    return action in CONVERSATION_ACTIONS or action == "reject"

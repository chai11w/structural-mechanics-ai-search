"""Sidecar Intent V2: deterministic rules, one bounded model call, code authorization."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Callable

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import (
    NAMESPACE_CANDIDATE,
    NAMESPACE_QUESTION,
    OUTCOME_ALLOW,
    authorize_action_v2,
    resolve_bare_index_namespace,
)
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent import chinese_number_to_int, parse_chapter
from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL, parse_model_json


DecisionModelV2 = Callable[[str], dict[str, Any]]

INTENT_V2_SYSTEM_PROMPT = """你是结构力学题库 Agent 的上下文意图识别器。
你只在规则无法唯一判断时，根据用户本轮表达和脱敏会话摘要输出一个 JSON 动作。
每回合只能输出一个高层动作，不能调用工具，不能回答题目。
question_index 是原图题号；candidate_rank 是当前候选排名，两者禁止混用。
不确定时输出 clarification，禁止猜测。
入库、删除、修复、未经系统提供兜底选项的跨章节盲搜只能输出 reject，并填写 requested_action。
global_search 只表示：当前为 WAIT_CHAPTER、已有题图、conversation_context.global_search_offered=true，且用户明确同意系统刚提出的全局搜索兜底。含糊回应必须 clarification；用户给出章节时优先 set_chapter。
只允许 ActionDecisionV2 字段；不要输出 Markdown。
输出格式：
{
  "action": "select_question|select_candidate|set_chapter|global_search|resend_answer|explain_failure|retry_search|cancel|greeting|small_talk|capability_help|out_of_scope|clarification|reject",
  "question_index": null,
  "candidate_rank": null,
  "chapter_override": null,
  "chapter_target": null,
  "clarification_reason": null,
  "requested_action": null,
  "confidence": 0.0,
  "reason": "简短理由"
}
clarification_reason 只允许：ambiguous_reference、ambiguous_number_namespace、ambiguous_action、missing_question_index、missing_candidate_rank、missing_chapter、missing_image、out_of_range。
requested_action 只允许：delete、store、repair、cross_chapter_search。"""


def decide_intent_v2(
    text: str | None,
    context: ConversationContextV2,
    *,
    event_type: str = "text",
    llm_client: DecisionModelV2 | None = None,
) -> ActionDecisionV2:
    """Return exactly one authorized high-level action without executing it."""

    clean = _normalize(text)
    rule_decision = _rule_decision(clean, context, event_type=event_type)
    if rule_decision is not None:
        return _authorize_or_clarify(rule_decision, context)

    if llm_client is None:
        reason = "ambiguous_reference" if _looks_contextual(clean) else "ambiguous_action"
        return _clarification(reason, source="validator")
    prompt = build_context_prompt_v2(clean, context)
    try:
        payload = dict(llm_client(prompt))
        payload["source"] = "context_llm"
        payload.setdefault("confidence", 0.0)
        decision = ActionDecisionV2.from_dict(payload)
    except Exception:  # noqa: BLE001 - model availability must degrade to a safe question.
        return _clarification("ambiguous_action", source="validator")
    evidence_checked = _validate_contextual_selection_evidence(clean, decision, context)
    if evidence_checked is not None:
        decision = evidence_checked
    action_evidence_checked = _validate_contextual_action_evidence(clean, decision, context)
    if action_evidence_checked is not None:
        decision = action_evidence_checked
    return _authorize_or_clarify(decision, context)


def build_context_prompt_v2(text: str, context: ConversationContextV2) -> str:
    payload = {
        "user_text": text,
        "conversation_context": context.to_prompt_payload(),
        "allowed_actions": [
            "set_chapter",
            "global_search",
            "select_question",
            "select_candidate",
            "resend_answer",
            "explain_failure",
            "retry_search",
            "cancel",
            "greeting",
            "small_talk",
            "capability_help",
            "out_of_scope",
            "clarification",
            "reject",
        ],
    }
    return INTENT_V2_SYSTEM_PROMPT + "\n\n输入 JSON：\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )


def call_qwen_decision_v2(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: int = 60,
) -> dict[str, Any]:
    """Call Qwen using only the process environment; never inspect local config."""

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": INTENT_V2_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 384,
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return parse_model_json(data["choices"][0]["message"]["content"])


def _rule_decision(
    text: str,
    context: ConversationContextV2,
    *,
    event_type: str,
) -> ActionDecisionV2 | None:
    if event_type == "image":
        chapter = _parse_chapter_v2(text) or context.pending_chapter
        return ActionDecisionV2(
            action="search_image", chapter_override=chapter, source="entry", confidence=1.0
        )
    if not text:
        return _clarification("ambiguous_action", source="rule")

    global_search_reply = _global_search_reply(text, context)
    if global_search_reply is not None:
        return global_search_reply

    forbidden = _forbidden_request(text)
    if forbidden:
        return ActionDecisionV2(
            action="reject",
            requested_action=forbidden,
            source="rule",
            confidence=1.0,
        )
    if _is_greeting(text):
        return _simple("greeting")
    if _is_small_talk(text):
        return _simple("small_talk")
    if _is_capability_help(text):
        return _simple("capability_help")
    if _is_out_of_scope(text):
        return _simple("out_of_scope")
    if _is_cancel(text):
        return _simple("cancel")
    if _is_resend(text):
        return _simple("resend_answer")
    if _is_failure_explanation(text):
        return _simple("explain_failure")
    if _is_retry(text):
        return _simple("retry_search")

    question_index = _explicit_question_index(text)
    if question_index is not None:
        return ActionDecisionV2(
            action="select_question",
            question_index=question_index,
            chapter_override=_parse_chapter_v2(text),
            source="rule",
            confidence=1.0,
        )
    candidate_rank = _explicit_candidate_rank(text)
    if candidate_rank is not None:
        return ActionDecisionV2(
            action="select_candidate",
            candidate_rank=candidate_rank,
            source="rule",
            confidence=1.0,
        )

    chapter = _parse_chapter_v2(text)
    if chapter:
        return ActionDecisionV2(
            action="set_chapter",
            chapter_override=chapter,
            chapter_target=_chapter_target(text),
            source="rule",
            confidence=1.0,
        )

    bare_index = _bare_index(text)
    if bare_index is not None:
        resolution = resolve_bare_index_namespace(bare_index, context.to_decision_context())
        if resolution.outcome != OUTCOME_ALLOW:
            reason = "out_of_range" if "out_of_range" in resolution.code else "ambiguous_number_namespace"
            return _clarification(reason, source="validator")
        if resolution.namespace == NAMESPACE_QUESTION:
            return ActionDecisionV2(
                action="select_question",
                question_index=bare_index,
                source="rule",
                confidence=1.0,
            )
        if resolution.namespace == NAMESPACE_CANDIDATE:
            return ActionDecisionV2(
                action="select_candidate",
                candidate_rank=bare_index,
                source="rule",
                confidence=1.0,
            )
    return None


def _authorize_or_clarify(
    decision: ActionDecisionV2,
    context: ConversationContextV2,
) -> ActionDecisionV2:
    authorization = authorize_action_v2(decision, context.to_decision_context())
    if authorization.allowed:
        return decision
    return _clarification(_authorization_reason(authorization.code), source="validator")


def _authorization_reason(code: str) -> str:
    if "out_of_range" in code:
        return "out_of_range"
    if code in {
        "trusted_image_required",
        "current_question_required",
        "question_list_required",
        "error_state_required",
        "retryable_search_required",
    }:
        return "missing_image"
    if code == "candidate_list_required":
        return "missing_candidate_rank"
    if code == "invalid_chapter":
        return "missing_chapter"
    return "ambiguous_action"


def _simple(action: str) -> ActionDecisionV2:
    return ActionDecisionV2(action=action, source="rule", confidence=1.0)


def _clarification(reason: str, *, source: str) -> ActionDecisionV2:
    return ActionDecisionV2(
        action="clarification",
        clarification_reason=reason,
        source=source,
        confidence=1.0,
    )


def _forbidden_request(text: str) -> str | None:
    if re.search(r"(?:所有|全部|每个|跨).{0,4}章节", text):
        return "cross_chapter_search"
    destructive_verb = re.search(r"(?:删除|删掉|删了|移除|清掉|清除|抹掉|剔除)", text)
    managed_object = re.search(r"(?:题库|库里|候选|答案|题目|第\s*[0-9一二两三四五六七八九十]+)", text)
    negative_retention = re.search(
        r"(?:别|不要|不必|无需|不准|不许).{0,8}(?:保留|留(?:着|下)?|存(?:着|下)?)",
        text,
    )
    bank_scope = re.search(r"(?:题库|库里|库中)", text)
    if (destructive_verb and managed_object) or (
        negative_retention and bank_scope and managed_object
    ):
        return "delete"
    if re.search(r"(?:入库|录入题库|加入题库|收录)", text):
        return "store"
    if re.search(r"(?:修复|修一下|改好).{0,8}(?:路径|索引|答案|题库)", text):
        return "repair"
    return None


def _global_search_reply(
    text: str,
    context: ConversationContextV2,
) -> ActionDecisionV2 | None:
    """Resolve explicit replies to the immediately preceding fallback offer."""

    # A chapter supplied in the same turn is more specific than accepting the
    # fallback. Let the normal chapter rule handle it.
    if _parse_chapter_v2(text) is not None:
        return None

    explicit_global = bool(
        re.search(
            r"(?:全局|全题库|整个题库|所有章节|全部章节|每个章节|跨章节).{0,6}(?:搜|找|查|检索)|"
            r"(?:搜|找|查|检索).{0,6}(?:全局|全题库|整个题库|所有章节|全部章节|每个章节|跨章节)",
            text,
        )
    )
    affirmative = bool(
        re.fullmatch(r"(?:可以|行|好|好的|同意|继续|搜吧|找吧|查吧|检索吧|开始吧)[吧啊呀哦]?[。！!]?", text)
        or re.search(r"(?:慢点|慢一点|慢一些).{0,8}(?:没关系|可以|不要紧).{0,8}(?:搜|找|查|继续)", text)
        or re.search(r"(?:没关系|不要紧).{0,8}(?:帮我|给我)?(?:搜|找|查|继续)", text)
    )
    decline = bool(re.search(r"^(?:先)?(?:不用|不要|算了|不搜|先别搜)(?:了|吧)?[。！!]?\Z", text))
    ambiguous = bool(re.search(r"^(?:可能|也许|大概|再说|看情况)(?:吧|呢)?[。！!]?\Z", text))

    if context.global_search_offered:
        if decline:
            return _clarification("missing_chapter", source="rule")
        if ambiguous:
            return _clarification("ambiguous_action", source="rule")
        if explicit_global or affirmative:
            return ActionDecisionV2(
                action="global_search",
                source="rule",
                confidence=1.0,
                reason="用户明确同意刚提供的全局搜索兜底",
            )
        return None

    if explicit_global:
        if not context.has_active_image:
            return _clarification("missing_image", source="validator")
        return ActionDecisionV2(
            action="reject",
            requested_action="cross_chapter_search",
            source="rule",
            confidence=1.0,
        )
    if context.phase == "WAIT_CHAPTER" and context.has_active_image and affirmative:
        return _clarification("missing_chapter", source="rule")
    return None


def _explicit_question_index(text: str) -> int | None:
    match = re.search(
        r"(?<!下)第?\s*([0-9一二两三四五六七八九十]+)\s*(?:小\s*)?[题問问]",
        text,
    )
    if not match:
        match = re.search(
            r"第\s*([0-9一二两三四五六七八九十]+)\s*道\s*[题問问]?",
            text,
        )
    if not match and re.search(r"(?:查|搜|检索)", text):
        match = re.search(r"第?\s*([0-9一二两三四五六七八九十]+)\s*个", text)
    return chinese_number_to_int(match.group(1)) if match else None


def _explicit_candidate_rank(text: str) -> int | None:
    patterns = (
        r"第?\s*([0-9一二两三四五六七八九十]+)\s*个?\s*候选",
        r"候选\s*第?\s*([0-9一二两三四五六七八九十]+)",
        r"(?<!另)第?\s*([0-9一二两三四五六七八九十]+)\s*个\s*答案",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return chinese_number_to_int(match.group(1))
    return None


def _bare_index(text: str) -> int | None:
    match = re.fullmatch(r"(?:第)?\s*([0-9一二两三四五六七八九十]+)\s*(?:个)?", text)
    return chinese_number_to_int(match.group(1)) if match else None


def _is_greeting(text: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、,.~～]+", "", text.lower())
    return re.fullmatch(
        r"(?:你好|您好|哈喽|嗨|hello|hi|hey|在吗|在不在|有人吗|早上好|下午好|晚上好)(?:啊|呀|哦|呢|嘛|啦)*",
        compact,
    ) is not None


def _is_small_talk(text: str) -> bool:
    compact = re.sub(r"[\s，。！？!?、,.]+", "", text)
    return compact in {"辛苦了", "谢谢", "谢谢你", "多谢", "麻烦你了", "好的谢谢"}


def _is_capability_help(text: str) -> bool:
    return any(phrase in text for phrase in ("你能做什么", "你还能帮我做什么", "怎么使用你", "怎么用你搜题", "你会什么"))


def _is_out_of_scope(text: str) -> bool:
    return bool(re.search(r"(?:写|生成|翻译).{0,8}(?:论文|作文|邮件|代码|小说)|查天气|订机票", text))


def _is_cancel(text: str) -> bool:
    return text.lower() in {"0", "取消", "cancel", "退出", "算了", "不用了"} or bool(
        re.fullmatch(r"取消(?:这次|当前)?(?:搜题|检索|任务|操作)", text)
    )


def _is_resend(text: str) -> bool:
    return "答案" in text and any(word in text for word in ("刚才", "上次", "再发", "重发", "再给"))


def _is_failure_explanation(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(
        phrase in compact
        for phrase in ("为什么失败", "为啥失败", "失败原因", "为什么没找到", "为什么没查到", "刚才为什么失败")
    )


def _is_retry(text: str) -> bool:
    compact = text.replace(" ", "")
    return compact in {"重试", "再试一次", "重新试一下", "重新检索", "再搜一次"}


def _looks_contextual(text: str) -> bool:
    return any(word in text for word in ("另一个", "刚才那", "之前那", "剩下", "那个", "那题"))


def _validate_contextual_selection_evidence(
    text: str,
    decision: ActionDecisionV2,
    context: ConversationContextV2,
) -> ActionDecisionV2 | None:
    """Require code-verifiable evidence for model-inferred selection indexes."""

    if decision.source != "context_llm":
        return None
    if decision.action == "select_candidate" and _explicit_candidate_rank(text) is None:
        if _is_alternative_reference(text) and context.selected_candidate_rank is not None:
            alternatives = tuple(
                rank
                for rank in range(1, context.candidate_count + 1)
                if rank != context.selected_candidate_rank
            )
            if len(alternatives) == 1:
                return ActionDecisionV2(
                    action="select_candidate",
                    candidate_rank=alternatives[0],
                    confidence=decision.confidence,
                    reason="代码确认只剩一个可替代候选",
                    source="validator",
                )
        return _clarification("ambiguous_reference", source="validator")

    if decision.action == "select_question" and _explicit_question_index(text) is None:
        if _is_previous_reference(text) and context.previous_question_index is not None:
            return ActionDecisionV2(
                action="select_question",
                question_index=context.previous_question_index,
                confidence=decision.confidence,
                reason="代码使用已记录的上一题",
                source="validator",
            )
        if _is_remaining_reference(text) and len(context.remaining_question_indexes) == 1:
            return ActionDecisionV2(
                action="select_question",
                question_index=context.remaining_question_indexes[0],
                confidence=decision.confidence,
                reason="代码确认只剩一道未完成题",
                source="validator",
            )
        return _clarification("ambiguous_reference", source="validator")
    return None


def _validate_contextual_action_evidence(
    text: str,
    decision: ActionDecisionV2,
    context: ConversationContextV2,
) -> ActionDecisionV2 | None:
    """Require positive text evidence when state alone cannot justify a model action."""

    if decision.source != "context_llm":
        return None
    if decision.action == "resend_answer" and not _has_resend_evidence(text):
        return _clarification("ambiguous_action", source="validator")
    if decision.action == "global_search":
        verified = _global_search_reply(text, context)
        if verified is None or verified.action != "global_search":
            return _clarification("ambiguous_action", source="validator")
        return verified
    return None


def _has_resend_evidence(text: str) -> bool:
    answer_object = re.search(r"(?:答案|结果|答题图片|答案图片)", text)
    repeat_or_delivery = re.search(
        r"(?:(?:再|重|重新).{0,6}(?:发|给|看|展示|显示)|(?:刚才|上次).{0,12}(?:发|给|看|展示|显示))",
        text,
    )
    return bool(answer_object and repeat_or_delivery)


def _is_alternative_reference(text: str) -> bool:
    return any(word in text for word in ("换一个", "换个", "另一个", "别的", "其他"))


def _is_previous_reference(text: str) -> bool:
    return any(word in text for word in ("上一道", "上一题", "刚才那题", "之前那题", "回到刚才"))


def _is_remaining_reference(text: str) -> bool:
    return any(word in text for word in ("剩下", "还没查", "未完成"))


def _parse_chapter_v2(text: str) -> str | None:
    # Longest/specific method names must win over generic “位移”.
    aliases = (
        ("矩阵位移", "7矩阵位移"),
        ("力矩分配", "6力矩分配"),
        ("位移法", "5位移法"),
        ("力法", "4力法"),
        ("单位荷载法", "3静定结构位移"),
        ("图乘法", "3静定结构位移"),
        ("影响线", "8影响线"),
    )
    for alias, chapter in aliases:
        if alias in text:
            return chapter
    return parse_chapter(text)


def _chapter_target(text: str) -> str:
    next_image_patterns = (
        r"(?:下一张|下张)",
        r"(?:接下来|之后|等会儿).{0,4}(?:发|传|给)",
        r"(?:待会儿?|稍后|一会儿?|过会儿?).{0,6}(?:发|传|给)",
        r"下(?:一|面)(?:个|道)?(?:题|这个题|那道题)",
    )
    if any(re.search(pattern, text) for pattern in next_image_patterns):
        return "next_image"
    return "current_question"


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())

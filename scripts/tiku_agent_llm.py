"""Strict LLM intent brain for the question-bank Agent.

The LLM is only allowed to classify user intent into a small JSON schema.
Execution, file paths, Excel writes, deletes, replacements, and confirmation
remain owned by local tools and rule checks.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

import search
from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL, parse_model_json
from scripts.tiku_agent_router import AgentIntent, CHAPTERS, route_text


ALLOWED_INTENTS = {
    "help",
    "search_question",
    "store_question",
    "inspect_question",
    "replace_answer",
    "soft_delete_question",
    "get_answer",
    "list_recent_ops",
    "cancel",
    "unknown",
}
ALLOWED_TARGETS = {"none", "image", "last_result", "chapter_number"}

SYSTEM_PROMPT = """你是结构力学题库维护 Agent 的意图识别大脑。
你只能把用户消息分类为工具意图 JSON，不能执行操作，不能编造文件路径，不能说已经删除/替换/写入。

允许的 intent:
- help: 打招呼、问你能做什么、怎么用
- search_question: 用户想检索/找答案，需要题图
- store_question: 新增/保存/入库题目
- inspect_question: 查看某题信息
- replace_answer: 替换答案
- soft_delete_question: 删除/移除题目，注意只是意图，工具层会软删除
- get_answer: 获取第几个候选答案
- list_recent_ops: 查看最近操作/刚才做了什么
- cancel: 取消/算了/退出
- unknown: 不是题库任务或无法判断

target 只能是:
- none
- image
- last_result: 指刚才检索结果里的第几个，例如“第一个”“Top1”“这个答案不对”
- chapter_number: 用户明确说了章节和题号，例如“4力法31题”

章节只能是:
2静定结构、3静定结构位移、4力法、5位移法、6力矩分配

输出必须是 JSON，不要 Markdown，不要解释:
{
  "intent": "help",
  "target": "none",
  "rank": null,
  "chapter": null,
  "question_no": null,
  "needs_image": false,
  "confidence": 0.0,
  "reason": "简短原因"
}

规则:
- “你好/你能做什么/怎么用” => help。
- “第一个答案/要top1” => get_answer, target last_result, rank 1。
- “删除第一个/删掉top2” => soft_delete_question, target last_result, rank 对应数字。
- “替换第一个答案/这个答案不对/换成我接下来发的图” => replace_answer, target last_result, rank 默认 1, needs_image true。
- “删除4力法31题/替换5位移法12题答案/查看4力法1题” => target chapter_number，并填 chapter/question_no。
- 不要输出文件路径；路径定位由工具层负责。
- 删除/替换是高风险动作，但这里只识别意图，不能确认执行。
"""


@dataclass
class AgentContext:
    state: str = "idle"
    has_results: bool = False
    result_count: int = 0
    current_chapter: str | None = None


class QwenAgentBrain:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: int = 20,
        enabled: bool = True,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.enabled = enabled

    def route(self, text: str, context: AgentContext | None = None) -> AgentIntent:
        if not self.enabled:
            return route_text(text)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if not api_key:
            return route_text(text)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, context or AgentContext())},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return normalize_llm_intent(parse_model_json(content), text)
        except Exception:  # noqa: BLE001 - rule router is the safe fallback.
            return route_text(text)


class FakeAgentBrain:
    """Deterministic test brain with the same output boundary as QwenAgentBrain."""

    def route(self, text: str, context: AgentContext | None = None) -> AgentIntent:
        return route_text(text)


def build_user_prompt(text: str, context: AgentContext) -> str:
    context_payload = {
        "state": context.state,
        "has_results": context.has_results,
        "result_count": context.result_count,
        "current_chapter": context.current_chapter,
    }
    return "上下文:\n" + json.dumps(context_payload, ensure_ascii=False) + "\n\n用户消息:\n" + text


def normalize_llm_intent(data: dict[str, Any], raw_text: str) -> AgentIntent:
    intent = str(data.get("intent") or "unknown").strip()
    if intent not in ALLOWED_INTENTS:
        intent = "unknown"

    target = str(data.get("target") or "none").strip()
    if target not in ALLOWED_TARGETS:
        target = "none"

    rank = parse_positive_int(data.get("rank"))
    question_no = parse_positive_int(data.get("question_no"))
    chapter = normalize_chapter(data.get("chapter"))
    confidence = clamp_confidence(data.get("confidence"))
    needs_image = bool(data.get("needs_image"))
    reason = str(data.get("reason") or "").strip()

    if intent in {"soft_delete_question", "replace_answer", "get_answer"} and target == "last_result" and rank is None:
        rank = 1
    if intent in {"inspect_question", "soft_delete_question", "replace_answer"} and target == "chapter_number":
        if not chapter or question_no is None:
            missing = []
            if not chapter:
                missing.append("chapter")
            if question_no is None:
                missing.append("question_no")
            return AgentIntent(intent, chapter, question_no, rank, target, needs_image, confidence, missing, raw_text, reason)

    if intent == "help":
        confidence = max(confidence, 0.9)

    return AgentIntent(
        intent=intent,
        chapter=chapter,
        question_no=question_no,
        answer_rank=rank,
        target=target,
        needs_image=needs_image,
        confidence=confidence,
        raw_text=raw_text,
        reason=reason,
    )


def normalize_chapter(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in CHAPTERS:
        return text
    for chapter in CHAPTERS:
        if text == chapter[:1] or text in chapter:
            return chapter
    return None


def parse_positive_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def clamp_confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))

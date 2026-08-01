"""Injected AI Planner that proposes one structured shadow plan.

Stage 5 keeps the Planner out of the response path: it only proposes, the code
permission layer reviews, and the result is recorded.  This module owns the
single model call and the strict parse of its output.  The Planner never sees an
``AgentState``; it receives the already-sanitized conversation summary
(``ConversationContextV2.to_prompt_payload``) plus the raw user text.  Every
model failure degrades to ``plan() -> None`` so the shadow path can never break
the fixed state machine.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL, parse_model_json
from tiku_agent.shadow_plan_v0 import (
    MAX_PLAN_STEPS,
    PLAN_ACTION_UNIVERSE,
    ShadowPlan,
    ShadowPlanStep,
)

SHADOW_PLAN_PROMPT = """你是结构力学题库 Agent 的只读规划器。
当前请求固定状态机处理不了，请你提出一个不含执行的只读计划，供代码审核后记录。
你只能提结构化计划，不能调用工具、不能执行、不能修改任何状态。
只从允许动作中选：""" + ", ".join(PLAN_ACTION_UNIVERSE) + """。
每个动作的参数与 ActionDecisionV2 同名字段：
- set_chapter: chapter_override（必须是：2静定结构/3静定结构位移/4力法/5位移法/6力矩分配/7矩阵位移/8影响线）
- select_question: question_index、可选 chapter_override
- select_candidate: candidate_rank（当前候选的排名，从1开始）
- global_search / reject_candidates / continue_search / show_candidates / resend_answer / explain_failure / retry_search / report_answer_mismatch：不需要参数
不要编造候选或答案，不要提议删除、入库、修复或跨章节盲搜。
每回合最多输出 1 个计划、最多 """ + str(MAX_PLAN_STEPS) + """ 步。
只输出 JSON，不要 Markdown：
{
  "goal": "用户想做什么（一句话中文）",
  "steps": [
    {"action": "select_question", "params": {"question_index": 2}, "reason": "为什么这样选"}
  ],
  "stop_condition": "何时停止（如：用户确认答案后）"
}"""


def build_shadow_plan_prompt_v0(user_text: str, context_payload: dict[str, Any]) -> str:
    """Build the planner prompt from raw user text and a sanitized context."""
    return SHADOW_PLAN_PROMPT + "\n\n当前状态（脱敏摘要，只用于规划依据，不得复述或推断列表之外信息）：\n" + json.dumps(
        context_payload, ensure_ascii=False, indent=2
    ) + "\n\n用户请求：\n" + str(user_text or "").strip()


def parse_shadow_plan_v0(payload: object) -> ShadowPlan:
    """Strictly parse planner JSON into a validated :class:`ShadowPlan`.

    Raises ``ValueError``/``TypeError``/``KeyError`` on any structural mismatch so
    the caller can record ``planner_unavailable`` instead of trusting bad output.
    """
    if not isinstance(payload, dict):
        raise TypeError("ShadowPlan payload must be a dict")
    goal = str(payload.get("goal") or "").strip()
    steps_raw = payload.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("steps must be a non-empty list")
    steps = tuple(
        ShadowPlanStep(
            action=str(item["action"]),
            params=dict(item.get("params") or {}),
            reason=str(item.get("reason") or ""),
        )
        for item in steps_raw
    )
    if len(steps) > MAX_PLAN_STEPS:
        raise ValueError(f"a plan may contain at most {MAX_PLAN_STEPS} steps")
    return ShadowPlan(
        goal=goal,
        steps=steps,
        stop_condition=str(payload.get("stop_condition") or ""),
        source="planner",
    )


PlannerModelClientV0 = Callable[[str], dict[str, Any]]


class ShadowPlannerV0:
    """Propose at most one plan per call; every failure degrades to ``None``."""

    def __init__(self, model_client: PlannerModelClientV0) -> None:
        self.model_client = model_client

    def plan(
        self,
        user_text: str,
        context_payload: dict[str, Any],
    ) -> ShadowPlan | None:
        prompt = build_shadow_plan_prompt_v0(user_text, context_payload)
        try:
            payload = self.model_client(prompt)
            return parse_shadow_plan_v0(payload)
        except Exception:  # noqa: BLE001 - every failure must stay out of the response path.
            return None


def call_qwen_planner_v0(
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
            {"role": "system", "content": SHADOW_PLAN_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 512,
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

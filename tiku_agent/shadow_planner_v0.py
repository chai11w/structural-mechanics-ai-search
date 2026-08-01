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
    ShadowPlannerResult,
)

SHADOW_PLAN_PROMPT = """你是结构力学题库 Agent 的只读规划器。
当前请求固定状态机处理不了，请你先改写这条模糊的用户请求，再基于改写后的完整表述提出一个不含执行的只读计划，供代码审核后记录。
你只能提结构化计划，不能调用工具、不能执行、不能修改任何状态。

第一步：改写。用户原话往往有省略、指代不明、口语省略。请结合当前状态，把它补成一句完整、明确的请求：补上省略的主语/宾语、把"那个/刚才那/别的"这类指代还原成具体所指、加入用户真正想要的关键词（如"换题/下一批/更接近的/重看答案"）。rewritten.text 和 keywords 必须简洁；reason 只写一句话（不超过 30 字），不要展开推理过程。不要编造原话没有的信息，不确定的部分宁可不补。

第二步：基于改写后的表述，从允许动作中选：""" + ", ".join(PLAN_ACTION_UNIVERSE) + """。

每个动作的语义与适用条件（必须严格遵守）：
- set_chapter: 用户给出或暗示章节，希望按该章节检索/换章节。参数 chapter_override 必须是：2静定结构/3静定结构位移/4力法/5位移法/6力矩分配/7矩阵位移/8影响线。
- select_question: 用户在多题中选择某题。参数 question_index 是题号，可选 chapter_override。
- select_candidate: 用户从当前候选中选一个。参数 candidate_rank 是当前候选排名（从1开始）。仅当已有候选项时可提。
- continue_search: 用户对当前候选不满意，想看下一批。仅当已有候选且还有更多时可提。
- show_candidates: 用户想重新看当前候选列表。仅当已有候选时可提。
- global_search: 用户同意系统提供的全局搜索兜底。仅当当前等待章节、有题图且已提供全局搜索时可提。
- resend_answer: 用户想重看刚才的答案。仅当已有答案时可提。
- report_answer_mismatch: 用户反馈刚返回的答案不匹配。仅当已有答案时可提。
- retry_search: 用户想重试刚才失败的操作。仅在出错（ERROR）状态、且上次失败可重试时可提。
- explain_failure: 仅在"确有失败/无匹配可解释"时用（如用户问"为什么失败""怎么没找到"）。【严禁】用它表达"我做不到/需要用户先提供信息"——那种情况应输出空计划。

如果你判断当前请求没有任何合法的只读动作可执行（例如用户想放弃、或请求太含糊无法映射到具体动作），plan.steps 必须为空并说明原因。

不要编造候选或答案，不要提议删除、入库、修复或跨章节盲搜。
每回合最多输出 1 个计划、最多 """ + str(MAX_PLAN_STEPS) + """ 步。
只输出 JSON，不要 Markdown：
{
  "rewritten": {
    "text": "改写后的完整请求（一句话）",
    "keywords": ["补上的关键词1", "补上的关键词2"],
    "reason": "为什么这样改写"
  },
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


def parse_shadow_plan_v0(payload: object) -> ShadowPlannerResult:
    """Strictly parse planner JSON into a validated :class:`ShadowPlannerResult`.

    The payload carries both the rewritten request and the plan built on it.
    Raises ``ValueError``/``TypeError``/``KeyError`` on any structural mismatch so
    the caller can record ``planner_unavailable`` instead of trusting bad output.
    """
    if not isinstance(payload, dict):
        raise TypeError("ShadowPlan payload must be a dict")
    rewritten_raw = payload.get("rewritten")
    if not isinstance(rewritten_raw, dict):
        raise ValueError("rewritten must be an object")
    rewritten_text = str(rewritten_raw.get("text") or "").strip()
    if not rewritten_text:
        raise ValueError("rewritten.text must not be empty")
    keywords_raw = rewritten_raw.get("keywords")
    keywords = tuple(
        str(item).strip() for item in keywords_raw if str(item or "").strip()
    ) if isinstance(keywords_raw, list) else ()
    reason = str(rewritten_raw.get("reason") or "")

    goal = str(payload.get("goal") or "").strip()
    steps_raw = payload.get("steps")
    if steps_raw is None:
        raise ValueError("steps must be a list")
    if not isinstance(steps_raw, list):
        raise ValueError("steps must be a list")
    if not steps_raw:
        # Model judged the request has no legal read-only action.  Only valid
        # when the model explicitly says so — the prompt instructs it to emit
        # an empty steps list with source=unplannable instead of inventing one.
        plan = ShadowPlan(
            goal=goal,
            steps=(),
            stop_condition=str(payload.get("stop_condition") or ""),
            source="unplannable",
        )
    else:
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
        plan = ShadowPlan(
            goal=goal,
            steps=steps,
            stop_condition=str(payload.get("stop_condition") or ""),
            source="planner",
        )
    return ShadowPlannerResult(
        rewritten_text=rewritten_text,
        keywords=keywords,
        reason=reason,
        plan=plan,
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
    ) -> ShadowPlannerResult | None:
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
        "max_tokens": 768,
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

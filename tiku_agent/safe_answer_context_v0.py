"""Sanitized, whitelisted state summary for the state-aware safe-answer stage.

This module is deliberately pure: it does not import the Agent runtime, call a
model, authorize a business action, execute a tool, or mutate conversation
state.  It derives from an ``AgentState`` a small bounded view that a safe-answer
model may see, so a greeting during ``WAIT_CANDIDATE_CHOICE`` can acknowledge
the live candidates without exposing paths, records, scores, or error text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tiku_agent.action_decision_v2 import TASK_ACTIONS, ActionDecisionV2
from tiku_agent.action_permissions_v2 import (
    DecisionContextV2,
    authorize_action_v2,
)
from tiku_agent.intent_contract import STATE_IDLE
from tiku_agent.state import PHASE_ERROR, AgentState, KNOWN_PHASES

# Actions the safe-answer model may advertise as a user's next step.  ``cancel``
# is unconditional but meaningless for a greeting; ``search_image`` requires a
# trusted image event and is not something a conversation model needs to name.
_SAFE_ACTION_UNIVERSE = tuple(sorted(TASK_ACTIONS - {"cancel", "search_image"}))

# The permission matrix continues to use the internal action names above.  The
# safe-answer model only receives these reviewed user-facing labels; it never
# sees executor identifiers such as ``select_candidate`` or ``retry_search``.
SAFE_ACTION_LABELS = {
    "global_search": "确认后查找全部章节",
    "set_chapter": "补充或更换章节",
    "select_question": "选择要处理的题目",
    "select_candidate": "选择候选题",
    "reject_candidates": "说明候选都不合适",
    "continue_search": "查看下一批候选",
    "show_candidates": "重新查看候选列表",
    "report_answer_mismatch": "说明答案不对应",
    "resend_answer": "重新查看刚才的答案",
    "explain_failure": "了解失败情况",
    "retry_search": "重试刚才的操作",
}

# Waiting-for / last-completed-step labels are human Chinese and avoid the
# banned execution-claim verbs (搜索/检索/找到/查到/读取/修改/删除…) so that a
# model echo cannot trip the output validator.
_WAITING_FOR = {
    STATE_IDLE: "新题图",
    "PROCESSING": "当前处理中",
    "WAIT_CHAPTER": "章节",
    "WAIT_QUESTION_CHOICE": "题目选择",
    "WAIT_CANDIDATE_CHOICE": "候选选择",
    "READY_TO_ROUTE": "路由准备中",
    "READY_FOR_SEARCH": "排序准备中",
    "ANSWERED": "",
    "CANCELLED": "",
    "ERROR": "重试或新题图",
    "NO_MATCH": "换章节或新题图",
}

_LAST_COMPLETED_STEP = {
    STATE_IDLE: "",
    "PROCESSING": "已收到题图",
    "WAIT_CHAPTER": "已识别题图",
    "WAIT_QUESTION_CHOICE": "已识别多道题",
    "WAIT_CANDIDATE_CHOICE": "候选已就绪",
    "READY_TO_ROUTE": "章节已确定",
    "READY_FOR_SEARCH": "排序范围已确定",
    "ANSWERED": "答案已返回",
    "CANCELLED": "任务已取消",
    "ERROR": "查询失败",
    "NO_MATCH": "无匹配题目",
}


@dataclass(frozen=True)
class SafeConversationContext:
    """The exact sanitized state whitelist a safe-answer model may see."""

    phase: str
    chapter: str | None = None
    candidate_count: int = 0
    allowed_actions: tuple[str, ...] = field(default_factory=tuple)
    last_completed_step: str = ""
    waiting_for: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in KNOWN_PHASES:
            raise ValueError(f"unknown safe-answer phase: {self.phase}")
        if self.candidate_count < 0:
            raise ValueError("candidate_count must not be negative")
        unknown = set(self.allowed_actions) - set(SAFE_ACTION_LABELS.values())
        if unknown:
            raise ValueError(f"non-whitelisted user actions: {sorted(unknown)}")

    def to_prompt_payload(self) -> dict[str, object]:
        """Return the exact whitelisted summary the model may be shown."""
        return {
            "phase": self.phase,
            "chapter": self.chapter,
            "candidate_count": self.candidate_count,
            "allowed_actions": list(self.allowed_actions),
            "last_completed_step": self.last_completed_step,
            "waiting_for": self.waiting_for,
        }


@dataclass(frozen=True)
class SafeAnswerValidationFacts:
    """Code-only facts used to reject contradictions; never rendered to a model."""

    has_active_image: bool = False
    has_answer: bool = False


def build_safe_answer_context(state: AgentState) -> SafeConversationContext:
    """Derive the whitelisted summary a model may see for this agent state."""
    return SafeConversationContext(
        phase=state.phase,
        chapter=_clean_optional_text(state.current_chapter),
        candidate_count=state.candidate_count,
        allowed_actions=_authorized_user_actions(state),
        waiting_for=_WAITING_FOR.get(state.phase),
        last_completed_step=_LAST_COMPLETED_STEP.get(state.phase, ""),
    )


def build_safe_answer_validation_facts(state: AgentState) -> SafeAnswerValidationFacts:
    """Derive code-only consistency facts without exposing them to the model."""
    return SafeAnswerValidationFacts(
        has_active_image=bool(state.active_image_path),
        has_answer=bool(state.last_answer_paths),
    )


def render_state_section(context: SafeConversationContext) -> str:
    """Render the sanitized state section inserted into a safe-answer prompt.

    ``IDLE`` has nothing meaningful to perceive, so it renders an empty section;
    a caller should skip the block entirely in that case.
    """
    if context.phase == STATE_IDLE:
        return ""
    lines = [
        "当前状态（脱敏摘要，只用于组织措辞，不得复述或推断列表之外的信息）：",
        f"- 阶段：{context.phase}",
    ]
    if context.chapter:
        lines.append(f"- 章节：{context.chapter}")
    lines.append(f"- 候选数量：{context.candidate_count}")
    if context.last_completed_step:
        lines.append(f"- 上一步：{context.last_completed_step}")
    if context.waiting_for:
        lines.append(f"- 等待：{context.waiting_for}")
    if context.allowed_actions:
        lines.append(f"- 允许的下一步：{', '.join(context.allowed_actions)}")
    return "\n".join(lines)


def _authorized_user_actions(state: AgentState) -> tuple[str, ...]:
    """Translate actions allowed by the real matrix into reviewed user labels."""
    decision_context = DecisionContextV2(
        phase=state.phase,
        question_count=state.question_count,
        candidate_count=state.candidate_count,
        has_active_image=bool(state.active_image_path),
        has_answer=bool(state.last_answer_paths),
        has_explainable_failure=bool(state.last_error),
        retryable_error=state.phase == PHASE_ERROR and bool(state.active_image_path),
        global_search_offered=state.global_search_offered,
        continuation_available=state.continuation_available,
        current_candidates_rejected=state.current_candidates_rejected,
    )
    allowed: list[str] = []
    for action in _SAFE_ACTION_UNIVERSE:
        decision = _minimal_decision(action)
        if authorize_action_v2(decision, decision_context).allowed:
            allowed.append(SAFE_ACTION_LABELS[action])
    return tuple(allowed)


def _minimal_decision(action: str) -> ActionDecisionV2:
    """Build one legal ActionDecisionV2 that only proves action-name allowability."""
    if action == "set_chapter":
        return ActionDecisionV2(
            "set_chapter",
            chapter_override="4力法",
            chapter_target="current_question",
        )
    if action == "select_question":
        return ActionDecisionV2("select_question", question_index=1)
    if action == "select_candidate":
        return ActionDecisionV2("select_candidate", candidate_rank=1)
    return ActionDecisionV2(action)


def _clean_optional_text(value: str | None) -> str | None:
    clean = str(value or "").strip()
    return clean or None

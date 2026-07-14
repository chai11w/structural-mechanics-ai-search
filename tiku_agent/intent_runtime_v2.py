"""Isolated adapter between ActionDecision V2 and the existing Agent runtime."""

from __future__ import annotations

from pathlib import Path

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.action_permissions_v2 import (
    NAMESPACE_CANDIDATE,
    NAMESPACE_NONE,
    NAMESPACE_QUESTION,
)
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent import IntentResult, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_QUESTION_CHOICE
from tiku_agent.state import PHASE_ANSWERED, PHASE_ERROR, AgentState


INTENT_VERSION_V1 = "v1"
INTENT_VERSION_V2 = "v2"
INTENT_VERSIONS = frozenset({INTENT_VERSION_V1, INTENT_VERSION_V2})


def build_runtime_context_v2(
    state: AgentState,
    *,
    trusted_image_event: bool = False,
) -> ConversationContextV2:
    """Build a bounded semantic context from persisted Agent state."""

    recent_action = str(
        state.last_intent.get("action") or state.last_intent.get("intent") or ""
    ).strip()
    completed = tuple(
        index for index in state.completed_questions if 1 <= index <= state.question_count
    )
    previous = state.previous_question
    if previous is not None and not 1 <= previous <= state.question_count:
        previous = None
    return ConversationContextV2.from_agent_state(
        state,
        active_namespace=_active_namespace(state),
        completed_question_indexes=completed,
        previous_question_index=previous,
        pending_chapter=state.pending_chapter or None,
        recent_actions=(recent_action,) if recent_action else (),
        trusted_image_event=trusted_image_event,
        retryable_error=state.phase == PHASE_ERROR and bool(state.active_image_path),
    )


def adapt_decision_v2(
    decision: ActionDecisionV2,
    *,
    image_path: str | Path | None = None,
) -> IntentResult:
    """Translate one authorized V2 decision into the existing task dispatcher contract."""

    data: dict[str, object] = {}
    if decision.action == "search_image":
        data = {
            "image_path": str(image_path or ""),
            "chapter_override": decision.chapter_override,
        }
    elif decision.action == "set_chapter":
        data = {
            "chapter": decision.chapter_override,
            "chapter_target": decision.chapter_target,
        }
    elif decision.action == "select_question":
        data = {
            "question_index": decision.question_index,
            "chapter_override": decision.chapter_override,
        }
    elif decision.action == "select_candidate":
        data = {"rank": decision.candidate_rank}
    elif decision.action == "clarification":
        data = {"clarification_reason": decision.clarification_reason}
    elif decision.action == "reject":
        data = {"requested_action": decision.requested_action}
    return IntentResult(
        intent=decision.action,
        data=data,
        source=decision.source,
    )


def _active_namespace(state: AgentState) -> str:
    if state.phase == STATE_WAIT_QUESTION_CHOICE:
        return NAMESPACE_QUESTION
    if state.phase == STATE_WAIT_CANDIDATE_CHOICE:
        return NAMESPACE_CANDIDATE
    if state.phase in {PHASE_ANSWERED, PHASE_ERROR} and state.candidate_count:
        return NAMESPACE_CANDIDATE
    if state.question_count:
        return NAMESPACE_QUESTION
    return NAMESPACE_NONE

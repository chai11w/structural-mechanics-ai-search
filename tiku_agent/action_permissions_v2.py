"""State/action permission matrix for the sidecar Intent V2 contract.

The matrix is pure and has no access to tools or mutable Agent state.  It says
whether a previously parsed decision may proceed, must ask a clarification, or
must be rejected.  Integration with ``agent.py`` is intentionally deferred.
"""

from __future__ import annotations

from dataclasses import dataclass

from tiku_agent.action_decision_v2 import (
    CONVERSATION_ACTIONS,
    ActionDecisionV2,
)
from tiku_agent.intent import CHAPTERS
from tiku_agent.state import KNOWN_PHASES


OUTCOME_ALLOW = "allow"
OUTCOME_CLARIFY = "clarify"
OUTCOME_REJECT = "reject"

NAMESPACE_NONE = "none"
NAMESPACE_QUESTION = "question"
NAMESPACE_CANDIDATE = "candidate"
INDEX_NAMESPACES = frozenset({NAMESPACE_NONE, NAMESPACE_QUESTION, NAMESPACE_CANDIDATE})

INTERNAL_PHASES = frozenset({"PROCESSING", "READY_TO_ROUTE", "READY_FOR_SEARCH"})
USER_PHASES = KNOWN_PHASES - INTERNAL_PHASES

QUESTION_SELECTION_PHASES = frozenset(
    {
        "WAIT_CHAPTER",
        "WAIT_QUESTION_CHOICE",
        "WAIT_CANDIDATE_CHOICE",
        "ANSWERED",
        "ERROR",
        "NO_MATCH",
    }
)
CANDIDATE_SELECTION_PHASES = frozenset({"WAIT_CANDIDATE_CHOICE", "ANSWERED", "ERROR"})
CURRENT_CHAPTER_PHASES = frozenset(
    {"WAIT_CHAPTER", "WAIT_CANDIDATE_CHOICE", "ANSWERED", "ERROR", "NO_MATCH"}
)

STATE_PRESERVE = "preserve"
STATE_REPLACE_TASK = "replace_task"
STATE_UPDATE_TASK = "update_task"
STATE_CLEAR_TASK = "clear_task"
STATE_STORE_PENDING_CHAPTER = "store_pending_chapter"

TOOLS_NONE = "none"
TOOLS_FIXED_SEARCH_PIPELINE = "fixed_search_pipeline"
TOOLS_ANSWER_LOOKUP = "answer_lookup"
TOOLS_SAVED_ANSWER_ONLY = "saved_answer_only"


@dataclass(frozen=True)
class DecisionContextV2:
    phase: str
    active_namespace: str = NAMESPACE_NONE
    question_count: int = 0
    candidate_count: int = 0
    has_active_image: bool = False
    has_answer: bool = False
    has_explainable_failure: bool = False
    retryable_error: bool = False
    trusted_image_event: bool = False

    def __post_init__(self) -> None:
        if self.phase not in KNOWN_PHASES:
            raise ValueError(f"Unknown Agent phase: {self.phase}")
        if self.active_namespace not in INDEX_NAMESPACES:
            raise ValueError(f"Unknown active namespace: {self.active_namespace}")
        if self.question_count < 0 or self.candidate_count < 0:
            raise ValueError("question_count and candidate_count must not be negative")
        if self.active_namespace == NAMESPACE_QUESTION and self.question_count == 0:
            raise ValueError("question namespace requires at least one question")
        if self.active_namespace == NAMESPACE_CANDIDATE and self.candidate_count == 0:
            raise ValueError("candidate namespace requires at least one candidate")


@dataclass(frozen=True)
class ActionAuthorizationV2:
    outcome: str
    code: str
    state_effect: str = STATE_PRESERVE
    tool_effect: str = TOOLS_NONE

    @property
    def allowed(self) -> bool:
        return self.outcome == OUTCOME_ALLOW


@dataclass(frozen=True)
class IndexResolutionV2:
    namespace: str | None
    outcome: str
    code: str


def authorize_action_v2(
    decision: ActionDecisionV2,
    context: DecisionContextV2,
) -> ActionAuthorizationV2:
    """Authorize one structured action without trusting model confidence."""

    if decision.action in CONVERSATION_ACTIONS or decision.action == "reject":
        return _allow("safe_response", STATE_PRESERVE, TOOLS_NONE)

    if decision.action == "cancel":
        return _allow("cancel", STATE_CLEAR_TASK, TOOLS_NONE)

    if context.phase in INTERNAL_PHASES:
        return _reject("agent_busy")

    if decision.action == "search_image":
        if not context.trusted_image_event:
            return _clarify("trusted_image_required")
        if not _valid_chapter(decision.chapter_override, allow_none=True):
            return _clarify("invalid_chapter")
        return _allow("new_image", STATE_REPLACE_TASK, TOOLS_FIXED_SEARCH_PIPELINE)

    if decision.action == "set_chapter":
        if not _valid_chapter(decision.chapter_override):
            return _clarify("invalid_chapter")
        if decision.chapter_target == "next_image":
            return _allow("pending_chapter", STATE_STORE_PENDING_CHAPTER, TOOLS_NONE)
        if context.phase not in CURRENT_CHAPTER_PHASES or not context.has_active_image:
            return _clarify("current_question_required")
        return _allow("correct_current_chapter", STATE_UPDATE_TASK, TOOLS_FIXED_SEARCH_PIPELINE)

    if decision.action == "select_question":
        if context.phase not in QUESTION_SELECTION_PHASES or context.question_count == 0:
            return _clarify("question_list_required")
        if not 1 <= int(decision.question_index or 0) <= context.question_count:
            return _clarify("question_index_out_of_range")
        if not _valid_chapter(decision.chapter_override, allow_none=True):
            return _clarify("invalid_chapter")
        return _allow("select_question", STATE_UPDATE_TASK, TOOLS_FIXED_SEARCH_PIPELINE)

    if decision.action == "select_candidate":
        if context.phase not in CANDIDATE_SELECTION_PHASES or context.candidate_count == 0:
            return _clarify("candidate_list_required")
        if not 1 <= int(decision.candidate_rank or 0) <= context.candidate_count:
            return _clarify("candidate_rank_out_of_range")
        return _allow("select_candidate", STATE_UPDATE_TASK, TOOLS_ANSWER_LOOKUP)

    if decision.action == "resend_answer":
        if context.phase != "ANSWERED" or not context.has_answer:
            return _clarify("saved_answer_required")
        return _allow("resend_answer", STATE_PRESERVE, TOOLS_SAVED_ANSWER_ONLY)

    if decision.action == "explain_failure":
        if context.phase not in USER_PHASES or not context.has_explainable_failure:
            return _clarify("explainable_failure_required")
        return _allow("explain_failure", STATE_PRESERVE, TOOLS_NONE)

    if decision.action == "retry_search":
        if context.phase != "ERROR":
            return _clarify("error_state_required")
        if not context.retryable_error or not context.has_active_image:
            return _clarify("retryable_search_required")
        return _allow("retry_search", STATE_UPDATE_TASK, TOOLS_FIXED_SEARCH_PIPELINE)

    return _reject("action_not_mapped")


def resolve_bare_index_namespace(
    index: int,
    context: DecisionContextV2,
) -> IndexResolutionV2:
    """Resolve a bare number from current focus; explicit nouns bypass this helper."""

    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        raise ValueError("index must be a positive integer")

    if context.active_namespace == NAMESPACE_QUESTION:
        if index <= context.question_count:
            return IndexResolutionV2(NAMESPACE_QUESTION, OUTCOME_ALLOW, "active_question_namespace")
        return IndexResolutionV2(None, OUTCOME_CLARIFY, "active_question_index_out_of_range")

    if context.active_namespace == NAMESPACE_CANDIDATE:
        if index <= context.candidate_count:
            return IndexResolutionV2(NAMESPACE_CANDIDATE, OUTCOME_ALLOW, "active_candidate_namespace")
        return IndexResolutionV2(None, OUTCOME_CLARIFY, "active_candidate_rank_out_of_range")

    valid_question = index <= context.question_count
    valid_candidate = index <= context.candidate_count
    if valid_question and not valid_candidate:
        return IndexResolutionV2(NAMESPACE_QUESTION, OUTCOME_ALLOW, "only_question_namespace_valid")
    if valid_candidate and not valid_question:
        return IndexResolutionV2(NAMESPACE_CANDIDATE, OUTCOME_ALLOW, "only_candidate_namespace_valid")
    if valid_question and valid_candidate:
        return IndexResolutionV2(None, OUTCOME_CLARIFY, "ambiguous_number_namespace")
    return IndexResolutionV2(None, OUTCOME_CLARIFY, "index_out_of_range")


def _valid_chapter(chapter: str | None, *, allow_none: bool = False) -> bool:
    if chapter is None:
        return allow_none
    return chapter in CHAPTERS


def _allow(code: str, state_effect: str, tool_effect: str) -> ActionAuthorizationV2:
    return ActionAuthorizationV2(OUTCOME_ALLOW, code, state_effect, tool_effect)


def _clarify(code: str) -> ActionAuthorizationV2:
    return ActionAuthorizationV2(OUTCOME_CLARIFY, code)


def _reject(code: str) -> ActionAuthorizationV2:
    return ActionAuthorizationV2(OUTCOME_REJECT, code)

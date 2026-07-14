"""Minimal, sanitized conversation context for sidecar Intent V2 decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from tiku_agent.action_permissions_v2 import (
    INDEX_NAMESPACES,
    NAMESPACE_NONE,
    DecisionContextV2,
)
from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.state import AgentState, KNOWN_PHASES


@dataclass(frozen=True)
class ConversationContextV2:
    """Only the state needed to understand one user turn.

    Absolute paths, candidate records, raw model output, credentials and full
    error strings are intentionally absent.  This object is not persisted or
    connected to the stable Agent yet.
    """

    phase: str
    active_namespace: str = NAMESPACE_NONE
    question_count: int = 0
    candidate_count: int = 0
    selected_question_index: int | None = None
    selected_candidate_rank: int | None = None
    previous_question_index: int | None = None
    completed_question_indexes: tuple[int, ...] = field(default_factory=tuple)
    remaining_question_indexes: tuple[int, ...] = field(default_factory=tuple)
    current_chapter: str | None = None
    pending_chapter: str | None = None
    pending_chapter_scope: str | None = None
    has_active_image: bool = False
    has_answer: bool = False
    has_explainable_failure: bool = False
    retryable_error: bool = False
    trusted_image_event: bool = False
    global_search_offered: bool = False
    recent_actions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.phase not in KNOWN_PHASES:
            raise ValueError(f"Unknown Agent phase: {self.phase}")
        if self.active_namespace not in INDEX_NAMESPACES:
            raise ValueError(f"Unknown active namespace: {self.active_namespace}")
        if self.question_count < 0 or self.candidate_count < 0:
            raise ValueError("question_count and candidate_count must not be negative")
        _validate_optional_index(
            "selected_question_index", self.selected_question_index, self.question_count
        )
        _validate_optional_index(
            "previous_question_index", self.previous_question_index, self.question_count
        )
        _validate_optional_index(
            "selected_candidate_rank", self.selected_candidate_rank, self.candidate_count
        )
        _validate_index_set(
            "completed_question_indexes", self.completed_question_indexes, self.question_count
        )
        _validate_index_set(
            "remaining_question_indexes", self.remaining_question_indexes, self.question_count
        )
        if set(self.completed_question_indexes) & set(self.remaining_question_indexes):
            raise ValueError("completed and remaining question indexes must not overlap")
        if self.current_chapter is not None and self.current_chapter not in CHAPTERS:
            raise ValueError("current_chapter must be a supported chapter")
        if self.pending_chapter is not None:
            if self.pending_chapter not in CHAPTERS:
                raise ValueError("pending_chapter must be a supported chapter")
            if self.pending_chapter_scope != "next_image":
                raise ValueError("pending_chapter is only valid for next_image")
        elif self.pending_chapter_scope is not None:
            raise ValueError("pending_chapter_scope requires pending_chapter")
        if len(self.recent_actions) > 4:
            raise ValueError("recent_actions is limited to four entries")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ConversationContextV2":
        """Build from a gold/evaluation context while ignoring unrelated fields."""

        pending_chapter = _clean_optional_text(payload.get("pending_chapter"))
        return cls(
            phase=str(payload["phase"]),
            active_namespace=str(payload.get("active_namespace") or NAMESPACE_NONE),
            question_count=int(payload.get("question_count") or 0),
            candidate_count=int(payload.get("candidate_count") or 0),
            selected_question_index=_optional_int(payload.get("selected_question_index")),
            selected_candidate_rank=_optional_int(payload.get("selected_candidate_rank")),
            previous_question_index=_optional_int(payload.get("previous_question_index")),
            completed_question_indexes=_index_tuple(payload.get("completed_question_indexes")),
            remaining_question_indexes=_index_tuple(payload.get("remaining_question_indexes")),
            current_chapter=_clean_optional_text(payload.get("current_chapter")),
            pending_chapter=pending_chapter,
            pending_chapter_scope=(
                str(payload.get("pending_chapter_scope") or "next_image")
                if pending_chapter
                else None
            ),
            has_active_image=bool(payload.get("has_active_image")),
            has_answer=bool(payload.get("has_answer")),
            has_explainable_failure=bool(payload.get("has_explainable_failure")),
            retryable_error=bool(payload.get("retryable_error")),
            trusted_image_event=bool(payload.get("trusted_image_event")),
            global_search_offered=bool(payload.get("global_search_offered")),
            recent_actions=tuple(str(item) for item in payload.get("recent_actions") or ()),
        )

    @classmethod
    def from_agent_state(
        cls,
        state: AgentState,
        *,
        active_namespace: str = NAMESPACE_NONE,
        completed_question_indexes: tuple[int, ...] = (),
        previous_question_index: int | None = None,
        pending_chapter: str | None = None,
        recent_actions: tuple[str, ...] = (),
        trusted_image_event: bool = False,
        global_search_offered: bool | None = None,
        retryable_error: bool = False,
    ) -> "ConversationContextV2":
        completed = tuple(sorted(set(completed_question_indexes)))
        remaining = tuple(
            index for index in range(1, state.question_count + 1) if index not in completed
        )
        return cls(
            phase=state.phase,
            active_namespace=active_namespace,
            question_count=state.question_count,
            candidate_count=state.candidate_count,
            selected_question_index=state.selected_question,
            selected_candidate_rank=state.selected_rank,
            previous_question_index=previous_question_index,
            completed_question_indexes=completed,
            remaining_question_indexes=remaining,
            current_chapter=state.current_chapter or None,
            pending_chapter=pending_chapter,
            pending_chapter_scope="next_image" if pending_chapter else None,
            has_active_image=bool(state.active_image_path),
            has_answer=bool(state.last_answer_paths),
            has_explainable_failure=bool(state.last_error),
            retryable_error=retryable_error,
            trusted_image_event=trusted_image_event,
            global_search_offered=(
                state.global_search_offered
                if global_search_offered is None
                else global_search_offered
            ),
            recent_actions=recent_actions,
        )

    def to_decision_context(self) -> DecisionContextV2:
        return DecisionContextV2(
            phase=self.phase,
            active_namespace=self.active_namespace,
            question_count=self.question_count,
            candidate_count=self.candidate_count,
            has_active_image=self.has_active_image,
            has_answer=self.has_answer,
            has_explainable_failure=self.has_explainable_failure,
            retryable_error=self.retryable_error,
            trusted_image_event=self.trusted_image_event,
            global_search_offered=self.global_search_offered,
        )

    def to_prompt_payload(self) -> dict[str, Any]:
        """Return the bounded semantic summary that an intent model may see."""

        return {
            "phase": self.phase,
            "active_namespace": self.active_namespace,
            "question_count": self.question_count,
            "candidate_count": self.candidate_count,
            "selected_question_index": self.selected_question_index,
            "selected_candidate_rank": self.selected_candidate_rank,
            "previous_question_index": self.previous_question_index,
            "completed_question_indexes": list(self.completed_question_indexes),
            "remaining_question_indexes": list(self.remaining_question_indexes),
            "current_chapter": self.current_chapter,
            "pending_chapter": self.pending_chapter,
            "pending_chapter_scope": self.pending_chapter_scope,
            "has_active_image": self.has_active_image,
            "has_answer": self.has_answer,
            "has_explainable_failure": self.has_explainable_failure,
            "retryable_error": self.retryable_error,
            "trusted_image_event": self.trusted_image_event,
            "global_search_offered": self.global_search_offered,
            "recent_actions": list(self.recent_actions),
        }


def _validate_optional_index(name: str, value: int | None, upper_bound: int) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper_bound:
        raise ValueError(f"{name} must be within its namespace")


def _validate_index_set(name: str, values: tuple[int, ...], upper_bound: int) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper_bound:
            raise ValueError(f"{name} must stay within question_count")


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _index_tuple(value: Any) -> tuple[int, ...]:
    return tuple(int(item) for item in (value or ()))


def _clean_optional_text(value: Any) -> str | None:
    clean = str(value or "").strip()
    return clean or None

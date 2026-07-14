"""Executable contract for Intent V2 high-level decisions.

This module defines what the Agent may *mean* on one user turn.  It deliberately
does not decide whether an action is allowed in the current phase, execute a
tool, parse a prompt, or mutate conversation state.  Those responsibilities
belong to later layers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ACTION_DECISION_VERSION = "2.0"

CONVERSATION_ACTIONS = frozenset(
    {
        "greeting",
        "small_talk",
        "capability_help",
        "out_of_scope",
        "clarification",
    }
)

TASK_ACTIONS = frozenset(
    {
        "search_image",
        "global_search",
        "set_chapter",
        "select_question",
        "select_candidate",
        "resend_answer",
        "explain_failure",
        "retry_search",
        "cancel",
    }
)

SAFETY_ACTIONS = frozenset({"reject"})
ACTIONS = CONVERSATION_ACTIONS | TASK_ACTIONS | SAFETY_ACTIONS

# These requests never become executable Intent V2 actions.  ``global_search``
# is a separate, guarded fallback that is only legal after the Agent offered it
# for the active question; an unsolicited blind cross-chapter search remains a
# forbidden request. ``reject`` records which boundary was hit safely.
FORBIDDEN_REQUESTS = frozenset({"delete", "store", "repair", "cross_chapter_search"})
CHAPTER_TARGETS = frozenset({"current_question", "next_image"})

CLARIFICATION_REASONS = frozenset(
    {
        "ambiguous_reference",
        "ambiguous_number_namespace",
        "ambiguous_action",
        "missing_question_index",
        "missing_candidate_rank",
        "missing_chapter",
        "missing_image",
        "out_of_range",
    }
)

DECISION_SOURCES = frozenset(
    {
        "entry",
        "rule",
        "context_llm",
        "validator",
        "v1_adapter",
        "gold",
        "unknown",
    }
)


@dataclass(frozen=True)
class ActionDecisionV2:
    """One and only one high-level action for a user turn.

    ``question_index`` addresses a question in a multi-question image.
    ``candidate_rank`` addresses an item on the current candidate page.  The
    namespaces are intentionally separate.  Selecting a question and changing
    its chapter remains one action by carrying ``chapter_override`` on
    ``select_question``.
    """

    action: str
    question_index: int | None = None
    candidate_rank: int | None = None
    chapter_override: str | None = None
    chapter_target: str | None = None
    clarification_reason: str | None = None
    requested_action: str | None = None
    confidence: float = 0.0
    reason: str = ""
    source: str = "unknown"
    protocol_version: str = ACTION_DECISION_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != ACTION_DECISION_VERSION:
            raise ValueError(f"Unsupported ActionDecision version: {self.protocol_version}")
        if self.action not in ACTIONS:
            raise ValueError(f"Unknown V2 action: {self.action}")
        if self.source not in DECISION_SOURCES:
            raise ValueError(f"Unknown decision source: {self.source}")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ValueError("confidence must be a number between 0 and 1")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

        self._validate_business_parameters()
        self._validate_clarification()
        self._validate_rejection()

    def _validate_business_parameters(self) -> None:
        if self.question_index is not None:
            _require_positive_int("question_index", self.question_index)
            if self.action != "select_question":
                raise ValueError("question_index is reserved for select_question")
        if self.candidate_rank is not None:
            _require_positive_int("candidate_rank", self.candidate_rank)
            if self.action != "select_candidate":
                raise ValueError("candidate_rank is reserved for select_candidate")

        if self.action == "select_question" and self.question_index is None:
            raise ValueError("select_question requires question_index")
        if self.action == "select_candidate" and self.candidate_rank is None:
            raise ValueError("select_candidate requires candidate_rank")

        if self.chapter_override is not None:
            if not isinstance(self.chapter_override, str) or not self.chapter_override.strip():
                raise ValueError("chapter_override must be a non-empty string")
            if self.action not in {"search_image", "set_chapter", "select_question"}:
                raise ValueError(
                    "chapter_override is only valid for search_image/set_chapter/select_question"
                )
        if self.action == "set_chapter" and self.chapter_override is None:
            raise ValueError("set_chapter requires chapter_override")
        if self.action == "set_chapter":
            if self.chapter_target not in CHAPTER_TARGETS:
                raise ValueError("set_chapter requires a known chapter_target")
        elif self.chapter_target is not None:
            raise ValueError("chapter_target is reserved for set_chapter")

    def _validate_clarification(self) -> None:
        if self.action == "clarification":
            if self.clarification_reason not in CLARIFICATION_REASONS:
                raise ValueError("clarification requires a known clarification_reason")
        elif self.clarification_reason is not None:
            raise ValueError("clarification_reason is reserved for clarification")

    def _validate_rejection(self) -> None:
        if self.action == "reject":
            if self.requested_action not in FORBIDDEN_REQUESTS:
                raise ValueError("reject requires a known forbidden requested_action")
        elif self.requested_action is not None:
            raise ValueError("requested_action is reserved for reject")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActionDecisionV2":
        """Strictly parse a decision; unknown fields fail at the constructor."""

        if not isinstance(payload, dict):
            raise TypeError("ActionDecision payload must be a dict")
        return cls(**payload)


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")

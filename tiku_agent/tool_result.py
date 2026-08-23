"""Structured outcomes shared by every question-bank Agent tool."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

from tiku_shared.request_protocol import (
    PROTOCOL_REASONS,
    RequestAction,
    RequestLayer,
    RequestStatus,
    normalize_status,
)


ToolOutcome = RequestStatus
_PUBLIC_CODES_BY_OUTCOME = {
    ToolOutcome.SUCCESS: frozenset({
        "REQUEST_SUCCEEDED",
        "IMAGE_ANALYZED",
        "SINGLE_QUESTION_DETECTED",
        "MULTI_QUESTION_DETECTED",
        "TRIAGE_SINGLE_QUESTION_CONFIRMED",
        "QUESTION_UNITS_PREPARED",
        "SCOPE_ANALYSIS_REUSED",
        "BANK_ROUTE_SELECTED",
        "STRUCTURE_FILTER_NOT_APPLICABLE",
        "STRUCTURE_CLASSIFIED_FROM_TEXT",
        "STRUCTURE_CLASSIFIED_FROM_IMAGE",
        "COARSE_CANDIDATES_FOUND",
        "GLOBAL_CANDIDATES_FOUND",
        "RERANK_NOT_REQUIRED",
        "RERANK_COMPLETED",
        "CANDIDATE_ACTION_CANCEL",
        "CANDIDATE_DELETE_SELECTED",
        "CANDIDATE_ANSWER_SELECTED",
        "ANSWER_FILES_FOUND",
    }),
    ToolOutcome.NO_MATCH: frozenset({
        "NO_MATCH",
        "NO_COARSE_CANDIDATES",
        "NO_GLOBAL_COARSE_CANDIDATES",
        "NO_GLOBAL_RELIABLE_CANDIDATES",
        "NO_CANDIDATES_TO_RERANK",
        "NO_RELIABLE_RERANK_CANDIDATES",
        "ANSWER_FILES_NOT_FOUND",
    }),
    ToolOutcome.NEEDS_INPUT: frozenset({
        "TOOL_INPUT_REQUIRED",
        "CHAPTER_REQUIRED",
        "LOAD_ROUTE_MIXED_REVIEW_REQUIRED",
        "LOAD_ROUTE_INPUT_UNUSABLE",
        "LOAD_ROUTE_NEEDS_REVIEW",
        "UNKNOWN_CHAPTER",
        "GLOBAL_SEARCH_IMAGE_REQUIRED",
        "CANDIDATE_NUMBER_REQUIRED",
        "CANDIDATE_DELETE_RANK_OUT_OF_RANGE",
        "CANDIDATE_RANK_OUT_OF_RANGE",
        "CANDIDATE_RANK_INVALID",
    }),
    ToolOutcome.PARTIAL: frozenset({
        "PARTIAL_RESULT",
        "MULTI_DETECTION_FALLBACK",
        "MULTI_CROPS_UNAVAILABLE",
        "STRUCTURE_FILTER_SKIPPED_NO_IMAGE",
        "STRUCTURE_TYPE_UNCERTAIN",
        "STRUCTURE_CLASSIFICATION_FALLBACK",
        "GLOBAL_RERANK_INCOMPLETE",
        "RERANK_SKIPPED_NO_IMAGE",
        "RERANK_INCOMPLETE_COARSE_FALLBACK",
        "RERANK_EMPTY_COARSE_FALLBACK",
    }),
    ToolOutcome.ERROR: frozenset({
        "TOOL_FAILED",
        "IMAGE_ANALYSIS_FAILED",
        "MULTI_DETAIL_INVALID",
        "MULTI_DETAIL_FAILED",
        "MULTI_DETECTION_FAILED",
        "BANK_ROUTE_FAILED",
        "COARSE_SEARCH_FAILED",
        "GLOBAL_SEARCH_UNSUPPORTED_ROUTE",
        "GLOBAL_SEARCH_FAILED",
        "RERANK_FAILED",
        "CANDIDATE_ACTION_INVALID_STATE",
        "ANSWER_LOOKUP_FAILED",
    }),
}
_PUBLIC_FALLBACK_BY_OUTCOME = {
    ToolOutcome.SUCCESS: (
        "REQUEST_SUCCEEDED", True, False, RequestAction.NONE,
    ),
    ToolOutcome.NO_MATCH: (
        "NO_MATCH", True, False, RequestAction.CHANGE_CHAPTER,
    ),
    ToolOutcome.NEEDS_INPUT: (
        "TOOL_INPUT_REQUIRED", False, False, RequestAction.NONE,
    ),
    ToolOutcome.PARTIAL: (
        "PARTIAL_RESULT", False, True, RequestAction.RETRY_SEARCH,
    ),
    ToolOutcome.ERROR: (
        "TOOL_FAILED", False, True, RequestAction.RETRY_SEARCH,
    ),
}
_PUBLIC_ID_PATTERNS = {
    "request_id": re.compile(r"^req_[A-Za-z0-9][A-Za-z0-9_-]{3,123}$"),
    "search_id": re.compile(r"^search_[A-Za-z0-9][A-Za-z0-9_-]{3,120}$"),
}
_PUBLIC_ID_SENSITIVE_RE = re.compile(
    r"(?:bearer|token|secret|password|api[_-]?key|sk[-_](?:proj[-_])?)",
    re.IGNORECASE,
)


def is_public_tool_code(code: object, outcome: ToolOutcome | str) -> bool:
    """Return whether a tool code is registered for the given public outcome."""

    try:
        normalized_outcome = normalize_status(outcome)
    except ValueError:
        return False
    clean_code = str(code or "").strip().upper()
    return clean_code in _PUBLIC_CODES_BY_OUTCOME[normalized_outcome]


@dataclass
class ToolResult:
    """Machine-readable tool result with temporary ``ok`` compatibility.

    ``completed`` means the tool reached a final semantic result.  A complete
    search that found no reliable candidate is therefore ``NO_MATCH`` with
    ``completed=True``.  ``NEEDS_INPUT``, ``PARTIAL`` and ``ERROR`` are
    incomplete until the caller supplies input, accepts partial data, or
    performs an allowed retry.
    """

    ok: bool | None = None
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    next_state: str = ""
    outcome: ToolOutcome | str | None = None
    code: str = ""
    completed: bool | None = None
    retryable: bool = False
    error_category: str = ""
    layer: RequestLayer | str = RequestLayer.TOOL
    action: RequestAction | str = RequestAction.NONE
    request_id: str = ""
    search_id: str = ""
    # Facts approved for user-facing rendering.  Keep this separate from the
    # legacy ``error`` and internal ``data`` fields during the migration.
    safe_facts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome is None:
            if self.ok is None:
                raise ValueError("ToolResult requires outcome or legacy ok")
            self.outcome = (
                ToolOutcome.SUCCESS if self.ok else ToolOutcome.ERROR
            )
        else:
            self.outcome = normalize_status(self.outcome)

        self.layer = RequestLayer(self.layer)
        self.action = RequestAction(self.action)
        self.safe_facts = dict(self.safe_facts or {})
        semantic_ok = self.outcome is not ToolOutcome.ERROR
        if self.ok is None:
            self.ok = semantic_ok
        elif bool(self.ok) != semantic_ok:
            raise ValueError("legacy ok conflicts with semantic tool outcome")

        if not self.code:
            self.code = (
                "LEGACY_SUCCESS" if self.outcome is ToolOutcome.SUCCESS
                else "LEGACY_TOOL_ERROR"
            )
        if self.completed is None:
            self.completed = self.outcome in {
                ToolOutcome.SUCCESS,
                ToolOutcome.NO_MATCH,
            }

    @classmethod
    def success(
        cls,
        *,
        tool: str = "",
        code: str,
        data: dict[str, Any] | None = None,
        next_state: str = "",
        safe_facts: dict[str, Any] | None = None,
        action: RequestAction | str = RequestAction.NONE,
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.SUCCESS,
            tool=tool,
            code=code,
            data=dict(data or {}),
            next_state=next_state,
            safe_facts=safe_facts,
            action=action,
        )

    @classmethod
    def no_match(
        cls,
        *,
        tool: str = "",
        code: str,
        data: dict[str, Any] | None = None,
        error: str = "",
        next_state: str = "NO_MATCH",
        safe_facts: dict[str, Any] | None = None,
        action: RequestAction | str = RequestAction.NONE,
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.NO_MATCH,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            safe_facts=safe_facts,
            action=action,
        )

    @classmethod
    def needs_input(
        cls,
        *,
        tool: str = "",
        code: str,
        error: str,
        data: dict[str, Any] | None = None,
        next_state: str,
        safe_facts: dict[str, Any] | None = None,
        action: RequestAction | str = RequestAction.NONE,
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.NEEDS_INPUT,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            completed=False,
            safe_facts=safe_facts,
            action=action,
        )

    @classmethod
    def partial(
        cls,
        *,
        tool: str = "",
        code: str,
        data: dict[str, Any] | None = None,
        error: str = "",
        next_state: str,
        retryable: bool = False,
        error_category: str = "",
        safe_facts: dict[str, Any] | None = None,
        action: RequestAction | str = RequestAction.NONE,
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.PARTIAL,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            completed=False,
            retryable=retryable,
            error_category=error_category,
            safe_facts=safe_facts,
            action=action,
        )

    @classmethod
    def tool_error(
        cls,
        *,
        tool: str = "",
        code: str,
        error: str,
        data: dict[str, Any] | None = None,
        next_state: str = "ERROR",
        retryable: bool = False,
        error_category: str,
        safe_facts: dict[str, Any] | None = None,
        action: RequestAction | str = RequestAction.NONE,
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.ERROR,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            completed=False,
            retryable=retryable,
            error_category=error_category,
            safe_facts=safe_facts,
            action=action,
        )

    def with_tool(self, tool: str) -> "ToolResult":
        """Attach the boundary name once and reject cross-tool relabeling."""

        normalized = str(tool).strip()
        if not normalized:
            raise ValueError("tool name must not be empty")
        if self.tool and self.tool != normalized:
            raise ValueError(f"tool result already belongs to {self.tool}")
        self.tool = normalized
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize the complete internal result for logs and compatibility."""

        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        payload["status"] = self.outcome.value
        payload["layer"] = self.layer.value
        payload["action"] = self.action.value
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize only the stable, user-boundary-safe result metadata.

        ``error``, ``data`` and ``safe_facts`` are intentionally excluded.  A
        caller that needs approved facts must project them by code rather than
        forwarding an arbitrary dictionary.
        """

        code = str(self.code or "").strip().upper()
        known_code = is_public_tool_code(code, self.outcome)
        if known_code:
            completed = self.outcome in {ToolOutcome.SUCCESS, ToolOutcome.NO_MATCH}
            registered = PROTOCOL_REASONS.get(code)
            if registered is not None and registered.status is self.outcome:
                retryable = registered.retryable
                action = registered.action
                layer = registered.layer
            else:
                _, _, retryable, action = _PUBLIC_FALLBACK_BY_OUTCOME[self.outcome]
                layer = RequestLayer.TOOL
        else:
            code, completed, retryable, action = _PUBLIC_FALLBACK_BY_OUTCOME[
                self.outcome
            ]
            layer = RequestLayer.TOOL
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "status": self.outcome.value,
            "layer": layer.value,
            "code": code,
            "completed": completed,
            "retryable": retryable,
            "action": action.value,
        }
        for field_name, pattern in _PUBLIC_ID_PATTERNS.items():
            value = str(getattr(self, field_name) or "").strip()
            if pattern.fullmatch(value) and not _PUBLIC_ID_SENSITIVE_RE.search(value):
                payload[field_name] = value
        return payload

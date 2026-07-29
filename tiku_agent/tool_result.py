"""Structured outcomes shared by every question-bank Agent tool."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ToolOutcome(str, Enum):
    """Semantic result of one completed tool boundary."""

    SUCCESS = "SUCCESS"
    NO_MATCH = "NO_MATCH"
    NEEDS_INPUT = "NEEDS_INPUT"
    PARTIAL = "PARTIAL"
    TOOL_ERROR = "TOOL_ERROR"


@dataclass
class ToolResult:
    """Machine-readable tool result with temporary ``ok`` compatibility.

    ``completed`` means the tool reached a final semantic result.  A complete
    search that found no reliable candidate is therefore ``NO_MATCH`` with
    ``completed=True``.  ``NEEDS_INPUT``, ``PARTIAL`` and ``TOOL_ERROR`` are
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

    def __post_init__(self) -> None:
        if self.outcome is None:
            if self.ok is None:
                raise ValueError("ToolResult requires outcome or legacy ok")
            self.outcome = (
                ToolOutcome.SUCCESS if self.ok else ToolOutcome.TOOL_ERROR
            )
        else:
            self.outcome = ToolOutcome(self.outcome)

        semantic_ok = self.outcome is not ToolOutcome.TOOL_ERROR
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
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.SUCCESS,
            tool=tool,
            code=code,
            data=dict(data or {}),
            next_state=next_state,
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
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.NO_MATCH,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
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
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.NEEDS_INPUT,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            completed=False,
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
    ) -> "ToolResult":
        return cls(
            outcome=ToolOutcome.TOOL_ERROR,
            tool=tool,
            code=code,
            data=dict(data or {}),
            error=error,
            next_state=next_state,
            completed=False,
            retryable=retryable,
            error_category=error_category,
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
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload

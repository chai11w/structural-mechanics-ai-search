"""Server-owned trace context and safe thread propagation helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import re
from typing import Any, Callable, Iterator
from uuid import uuid4


_TRACE_ID_RE = re.compile(r"^trace_[0-9a-f]{32}$")


def new_trace_id() -> str:
    """Return one opaque server-owned identifier for an inbound operation."""

    return f"trace_{uuid4().hex}"


def is_valid_trace_id(value: object) -> bool:
    return bool(_TRACE_ID_RE.fullmatch(str(value or "")))


@dataclass(frozen=True)
class TraceContext:
    """Immutable identity shared by synchronous work for one inbound operation."""

    trace_id: str
    request_id: str = ""

    def __post_init__(self) -> None:
        clean_trace_id = str(self.trace_id or "").strip()
        if not is_valid_trace_id(clean_trace_id):
            raise ValueError("invalid trace_id")
        object.__setattr__(self, "trace_id", clean_trace_id)
        object.__setattr__(self, "request_id", str(self.request_id or "").strip())

    @classmethod
    def create(cls, *, request_id: str = "") -> TraceContext:
        """Create a fresh server trace while retaining the public attempt ID."""

        return cls(trace_id=new_trace_id(), request_id=request_id)


_ACTIVE_TRACE_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "active_trace_context", default=None
)


def current_trace_context() -> TraceContext | None:
    return _ACTIVE_TRACE_CONTEXT.get()


def current_trace_id() -> str:
    context = current_trace_context()
    return context.trace_id if context is not None else ""


def current_request_id() -> str:
    context = current_trace_context()
    return context.request_id if context is not None else ""


@contextmanager
def trace_context_scope(context: TraceContext) -> Iterator[TraceContext]:
    """Bind a trace for the current context and restore the prior one on exit."""

    if not isinstance(context, TraceContext):
        raise TypeError("context must be a TraceContext")
    token = _ACTIVE_TRACE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _ACTIVE_TRACE_CONTEXT.reset(token)


# Short compatibility alias for callers that adopted the audit's provisional name.
trace_scope = trace_context_scope


def submit_with_trace_context(
    executor: Any,
    function: Callable,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Submit work with a fresh copy of all caller ContextVars, including trace."""

    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)

"""Persistence contract for the isolated Agent's short-lived conversation memory.

This module deliberately defines no database implementation yet.  It keeps the
Agent independent from SQLite, FastAPI, and any future message adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta

from tiku_agent.state import AgentState


SESSION_STATE_SCHEMA_VERSION = 1
DEFAULT_SESSION_TTL = timedelta(hours=2)


class SessionStore(ABC):
    """Store complete AgentState snapshots for a sliding two-hour session.

    Implementations must treat an expired session exactly like a missing one.
    ``save`` refreshes expiry from the time it is called.  The store retains
    state only; session-specific temporary files are cleaned by the caller
    using the identifiers returned from ``purge_expired``.
    """

    @abstractmethod
    def load(self, session_id: str) -> AgentState | None:
        """Return a valid state snapshot, or ``None`` when missing or expired."""

    @abstractmethod
    def save(self, state: AgentState) -> None:
        """Persist a complete state snapshot and refresh its two-hour expiry."""

    @abstractmethod
    def clear(self, session_id: str) -> None:
        """Remove one session explicitly, for cancellation or a deliberate reset."""

    @abstractmethod
    def purge_expired(self) -> list[str]:
        """Remove expired snapshots and return their session IDs for file cleanup."""

"""Persistence contract for the isolated Agent's short-lived conversation memory.

This module deliberately defines no database implementation yet.  It keeps the
Agent independent from SQLite, FastAPI, and any future message adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Callable

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


class SQLiteSessionStore(SessionStore):
    """SQLite implementation of the isolated Agent's short-lived session store."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        ttl: timedelta = DEFAULT_SESSION_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.database_path = Path(database_path)
        self.ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def load(self, session_id: str) -> AgentState | None:
        session_id = str(session_id).strip()
        if not session_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json, expires_at FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if _parse_timestamp(row["expires_at"]) <= self._timestamp():
                conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))
                return None
        return AgentState.from_dict(json.loads(row["state_json"]))

    def save(self, state: AgentState) -> None:
        state.validate()
        now = self._timestamp()
        expires_at = _format_timestamp(now + self.ttl)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_sessions (session_id, schema_version, state_json, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (state.session_id, SESSION_STATE_SCHEMA_VERSION, payload, _format_timestamp(now), _format_timestamp(now), expires_at),
            )

    def clear(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (str(session_id),))

    def purge_expired(self) -> list[str]:
        now = _format_timestamp(self._timestamp())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM agent_sessions WHERE expires_at <= ? ORDER BY session_id",
                (now,),
            ).fetchall()
            expired = [str(row["session_id"]) for row in rows]
            if expired:
                conn.executemany("DELETE FROM agent_sessions WHERE session_id = ?", [(session_id,) for session_id in expired])
        return expired

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_sessions_expires_at ON agent_sessions(expires_at)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _timestamp(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)

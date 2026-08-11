"""Small, privacy-bounded feedback store for assistant messages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4


FEEDBACK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MessageFeedback:
    feedback_id: str
    message_id: str
    identity_key: str
    session_key: str
    rating: str
    tags: tuple[str, ...]
    detail: str
    task_revision: int
    phase: str
    candidate_count: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SQLiteFeedbackStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def upsert(
        self,
        *,
        message_id: str,
        identity_key: str,
        session_key: str,
        rating: str,
        tags: tuple[str, ...],
        detail: str,
        task_revision: int,
        phase: str,
        candidate_count: int,
    ) -> MessageFeedback:
        now = datetime.now(UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                connection.row_factory = sqlite3.Row
                _create_schema(connection)
                existing = connection.execute(
                    """
                    SELECT feedback_id, created_at
                    FROM message_feedback
                    WHERE identity_key = ? AND session_key = ? AND message_id = ?
                    """,
                    (identity_key, session_key, message_id),
                ).fetchone()
                feedback_id = str(existing["feedback_id"]) if existing else uuid4().hex
                created_at = str(existing["created_at"]) if existing else now
                connection.execute(
                    """
                    INSERT INTO message_feedback (
                        feedback_id, message_id, identity_key, session_key, rating,
                        tags_json, detail, task_revision, phase, candidate_count,
                        created_at, updated_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity_key, session_key, message_id) DO UPDATE SET
                        rating = excluded.rating,
                        tags_json = excluded.tags_json,
                        detail = excluded.detail,
                        task_revision = excluded.task_revision,
                        phase = excluded.phase,
                        candidate_count = excluded.candidate_count,
                        updated_at = excluded.updated_at,
                        schema_version = excluded.schema_version
                    """,
                    (
                        feedback_id,
                        message_id,
                        identity_key,
                        session_key,
                        rating,
                        json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                        detail,
                        max(0, int(task_revision)),
                        phase,
                        max(0, int(candidate_count)),
                        created_at,
                        now,
                        FEEDBACK_SCHEMA_VERSION,
                    ),
                )
        return MessageFeedback(
            feedback_id=feedback_id,
            message_id=message_id,
            identity_key=identity_key,
            session_key=session_key,
            rating=rating,
            tags=tags,
            detail=detail,
            task_revision=max(0, int(task_revision)),
            phase=phase,
            candidate_count=max(0, int(candidate_count)),
            created_at=created_at,
            updated_at=now,
        )

    def list_feedback(self) -> list[MessageFeedback]:
        if not self.path.is_file():
            return []
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                connection.row_factory = sqlite3.Row
                _create_schema(connection)
                rows = connection.execute(
                    "SELECT * FROM message_feedback ORDER BY updated_at DESC"
                ).fetchall()
        return [
            MessageFeedback(
                feedback_id=str(row["feedback_id"]),
                message_id=str(row["message_id"]),
                identity_key=str(row["identity_key"]),
                session_key=str(row["session_key"]),
                rating=str(row["rating"]),
                tags=tuple(json.loads(str(row["tags_json"]))),
                detail=str(row["detail"]),
                task_revision=int(row["task_revision"]),
                phase=str(row["phase"]),
                candidate_count=int(row["candidate_count"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def delete(
        self,
        *,
        message_id: str,
        identity_key: str,
        session_key: str,
    ) -> bool:
        if not self.path.is_file():
            return False
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                _create_schema(connection)
                cursor = connection.execute(
                    """
                    DELETE FROM message_feedback
                    WHERE identity_key = ? AND session_key = ? AND message_id = ?
                    """,
                    (identity_key, session_key, message_id),
                )
                return cursor.rowcount > 0


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_feedback (
            feedback_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            rating TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            detail TEXT NOT NULL,
            task_revision INTEGER NOT NULL,
            phase TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            UNIQUE(identity_key, session_key, message_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_identity_updated "
        "ON message_feedback(identity_key, updated_at)"
    )

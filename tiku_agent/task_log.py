"""Privacy-bounded structured task-log contract for the isolated Agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Literal


TASK_LOG_SCHEMA_VERSION = 1
TaskKind = Literal["image", "text"]
TaskOutcome = Literal["waiting", "candidates", "answered", "no_match", "cancelled", "error"]


@dataclass(frozen=True)
class TaskLogEntry:
    """One completed Agent turn, intentionally excluding raw user content and paths."""

    task_id: str
    session_key: str
    kind: TaskKind
    started_at: str
    finished_at: str
    duration_ms: int
    phase_before: str
    phase_after: str
    outcome: TaskOutcome
    question_count: int
    candidate_count: int
    chapter: str = ""
    route: str = ""
    error_kind: str = ""
    schema_version: int = TASK_LOG_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class TaskLogger(ABC):
    """Append completed task records without influencing the user-facing result."""

    @abstractmethod
    def write(self, entry: TaskLogEntry) -> None:
        """Persist one structured task record."""

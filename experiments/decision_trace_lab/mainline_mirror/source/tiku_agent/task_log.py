"""Privacy-bounded structured task-log contract for the isolated Agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from threading import Lock
from typing import Literal

from tiku_agent.tools import DEFAULT_RUNTIME_DIR


TASK_LOG_SCHEMA_VERSION = 1
DEFAULT_TASK_LOG_PATH = DEFAULT_RUNTIME_DIR / "task_logs.jsonl"
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


class JsonlTaskLogger(TaskLogger):
    """Append task records locally as UTF-8 JSON Lines without retaining raw input."""

    def __init__(self, path: str | Path = DEFAULT_TASK_LOG_PATH) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def write(self, entry: TaskLogEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")

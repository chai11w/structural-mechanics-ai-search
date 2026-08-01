"""Privacy-bounded shadow-plan log for Stage 5 observation.

One line per long-tail request that reached the shadow planner: the trigger
reason, the pre-turn phase, the proposed plan, and its permission review.
Contains the raw user text so a reviewer can judge whether the plan's goal was
correct — this is review data, written only to the 8794 runtime directory and
never committed or uploaded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from threading import Lock

from tiku_agent.session_artifacts import session_key
from tiku_agent.tools import DEFAULT_RUNTIME_DIR


SHADOW_LOG_SCHEMA_VERSION = 1
DEFAULT_SHADOW_LOG_PATH = DEFAULT_RUNTIME_DIR / "shadow_plans.jsonl"


@dataclass(frozen=True)
class ShadowPlanLogEntry:
    """One recorded shadow-plan generation and review."""

    task_id: str
    session_key: str
    user_text: str
    trigger_reason: str
    phase_before: str
    plan: dict | None = None
    review: dict | None = None
    planner_unavailable: bool = False
    schema_version: int = SHADOW_LOG_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class ShadowPlanLogger:
    """Append shadow-plan records without influencing the user-facing result."""

    def write(self, entry: ShadowPlanLogEntry) -> None:
        raise NotImplementedError


class JsonlShadowPlanLogger(ShadowPlanLogger):
    """Append shadow-plan records locally as UTF-8 JSON Lines."""

    def __init__(self, path: str | Path = DEFAULT_SHADOW_LOG_PATH) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def write(self, entry: ShadowPlanLogEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")

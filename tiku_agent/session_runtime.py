"""Session-aware outer layer for the isolated question-bank Agent."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Callable
from uuid import uuid4

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.session_artifacts import SessionArtifacts, session_key
from tiku_agent.session_store import SessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger, TaskLogEntry, TaskLogger
from tiku_agent.tools import AgentToolConfig


AgentFactory = Callable[[AgentState], TikuSearchAgent]


class AgentSessionRuntime:
    """Restore, run, and checkpoint one Agent turn by a caller-owned session ID."""

    def __init__(
        self,
        store: SessionStore,
        *,
        artifacts: SessionArtifacts | None = None,
        task_logger: TaskLogger | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts or SessionArtifacts()
        self.task_logger = task_logger or JsonlTaskLogger()
        self.agent_factory = agent_factory

    def handle_image(self, session_id: str, image_path: str | Path) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self._purge_expired()
        persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
        return self._run(clean_session_id, "image", lambda agent: agent.handle_image(persisted_image))

    def handle_text(self, session_id: str, text: str) -> AgentResponse:
        return self._run(session_id, "text", lambda agent: agent.handle_text(text))

    def clear(self, session_id: str) -> None:
        """Explicitly start a fresh conversation and remove its temporary files."""
        clean_session_id = self._clean_session_id(session_id)
        self.store.clear(clean_session_id)
        self.artifacts.clear_session(clean_session_id)

    def current_image_path(self, session_id: str) -> Path | None:
        """Return the current persisted upload for a live session."""
        clean_session_id = self._clean_session_id(session_id)
        state = self.store.load(clean_session_id)
        if state is None or not state.current_image_path:
            return None
        path = Path(state.current_image_path)
        return path if self.resolve_upload(clean_session_id, path.name) == path.resolve() else None

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        """Resolve one session-owned upload without exposing arbitrary paths."""
        clean_session_id = self._clean_session_id(session_id)
        if self.store.load(clean_session_id) is None:
            return None
        safe_name = Path(str(filename)).name
        if not safe_name or safe_name != str(filename):
            return None
        upload_dir = (self.artifacts.session_dir(clean_session_id) / "uploads").resolve()
        target = (upload_dir / safe_name).resolve()
        if target.parent != upload_dir or not target.is_file():
            return None
        return target

    def _run(self, session_id: str, kind: str, handler: Callable[[TikuSearchAgent], AgentResponse]) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self._purge_expired()
        state = self.store.load(clean_session_id) or AgentState(session_id=clean_session_id)
        phase_before = state.phase
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        agent = self._make_agent(state)
        response: AgentResponse | None = None
        error_kind = ""
        try:
            response = handler(agent)
            if response.intent == "cancel":
                self.store.clear(clean_session_id)
                self.artifacts.clear_session(clean_session_id)
            else:
                self.store.save(agent.state)
            return response
        except Exception as exc:
            error_kind = type(exc).__name__
            raise
        finally:
            self._write_task_log(
                task_id=uuid4().hex,
                session_id=clean_session_id,
                kind=kind,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                phase_before=phase_before,
                state=agent.state,
                response=response,
                error_kind=error_kind,
            )

    def _make_agent(self, state: AgentState) -> TikuSearchAgent:
        if self.agent_factory is not None:
            return self.agent_factory(state)
        return TikuSearchAgent(state=state, config=AgentToolConfig(session_dir=self.artifacts.session_dir(state.session_id)))

    def _purge_expired(self) -> None:
        self.artifacts.clear_sessions(self.store.purge_expired())

    def _write_task_log(
        self,
        *,
        task_id: str,
        session_id: str,
        kind: str,
        started_at: datetime,
        duration_ms: int,
        phase_before: str,
        state: AgentState,
        response: AgentResponse | None,
        error_kind: str,
    ) -> None:
        outcome = _task_outcome(state, response, error_kind)
        entry = TaskLogEntry(
            task_id=task_id,
            session_key=session_key(session_id),
            kind=kind,  # type: ignore[arg-type]  # Internal callers pass only image/text.
            started_at=started_at.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=max(0, duration_ms),
            phase_before=phase_before,
            phase_after=state.phase,
            outcome=outcome,
            question_count=len(state.questions),
            candidate_count=len(state.candidates),
            chapter=state.current_chapter,
            route=state.current_route,
            error_kind=error_kind or ("agent_error" if state.phase == "ERROR" else ""),
        )
        try:
            self.task_logger.write(entry)
        except Exception:  # noqa: BLE001 - observability must not break the Agent.
            pass

    @staticmethod
    def _clean_session_id(session_id: str) -> str:
        clean = str(session_id).strip()
        if not clean:
            raise ValueError("session_id is required")
        return clean


def _task_outcome(state: AgentState, response: AgentResponse | None, error_kind: str) -> str:
    if error_kind or state.phase == "ERROR":
        return "error"
    if response is not None and response.intent == "cancel":
        return "cancelled"
    if state.phase == "ANSWERED":
        return "answered"
    if state.phase == "NO_MATCH":
        return "no_match"
    if state.candidates:
        return "candidates"
    return "waiting"

"""Session-aware outer layer for the isolated question-bank Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_store import SessionStore
from tiku_agent.state import AgentState
from tiku_agent.tools import AgentToolConfig


AgentFactory = Callable[[AgentState], TikuSearchAgent]


class AgentSessionRuntime:
    """Restore, run, and checkpoint one Agent turn by a caller-owned session ID."""

    def __init__(
        self,
        store: SessionStore,
        *,
        artifacts: SessionArtifacts | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts or SessionArtifacts()
        self.agent_factory = agent_factory

    def handle_image(self, session_id: str, image_path: str | Path) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self._purge_expired()
        persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
        return self._run(clean_session_id, lambda agent: agent.handle_image(persisted_image))

    def handle_text(self, session_id: str, text: str) -> AgentResponse:
        return self._run(session_id, lambda agent: agent.handle_text(text))

    def _run(self, session_id: str, handler: Callable[[TikuSearchAgent], AgentResponse]) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self._purge_expired()
        state = self.store.load(clean_session_id) or AgentState(session_id=clean_session_id)
        agent = self._make_agent(state)
        response = handler(agent)
        if response.intent == "cancel":
            self.store.clear(clean_session_id)
            self.artifacts.clear_session(clean_session_id)
        else:
            self.store.save(agent.state)
        return response

    def _make_agent(self, state: AgentState) -> TikuSearchAgent:
        if self.agent_factory is not None:
            return self.agent_factory(state)
        return TikuSearchAgent(state=state, config=AgentToolConfig(session_dir=self.artifacts.session_dir(state.session_id)))

    def _purge_expired(self) -> None:
        self.artifacts.clear_sessions(self.store.purge_expired())

    @staticmethod
    def _clean_session_id(session_id: str) -> str:
        clean = str(session_id).strip()
        if not clean:
            raise ValueError("session_id is required")
        return clean

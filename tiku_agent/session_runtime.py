"""Session-aware outer layer for the isolated question-bank Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.session_store import SessionStore
from tiku_agent.state import AgentState


AgentFactory = Callable[[AgentState], TikuSearchAgent]


class AgentSessionRuntime:
    """Restore, run, and checkpoint one Agent turn by a caller-owned session ID."""

    def __init__(self, store: SessionStore, *, agent_factory: AgentFactory | None = None) -> None:
        self.store = store
        self.agent_factory = agent_factory or (lambda state: TikuSearchAgent(state=state))

    def handle_image(self, session_id: str, image_path: str | Path) -> AgentResponse:
        return self._run(session_id, lambda agent: agent.handle_image(image_path))

    def handle_text(self, session_id: str, text: str) -> AgentResponse:
        return self._run(session_id, lambda agent: agent.handle_text(text))

    def _run(self, session_id: str, handler: Callable[[TikuSearchAgent], AgentResponse]) -> AgentResponse:
        clean_session_id = str(session_id).strip()
        if not clean_session_id:
            raise ValueError("session_id is required")
        state = self.store.load(clean_session_id) or AgentState(session_id=clean_session_id)
        agent = self.agent_factory(state)
        response = handler(agent)
        if response.intent == "cancel":
            self.store.clear(clean_session_id)
        else:
            self.store.save(agent.state)
        return response

"""A3 runtime variant that normalizes page orientation before image routing."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from tiku_agent.a3_runtime import A3MvpRuntime, A3SessionState
from tiku_agent.agent import AgentResponse
from tiku_agent.session_runtime import ProgressReporter


class FrontdoorOrientationA3Runtime(A3MvpRuntime):
    """Run orientation once before A1/A2/A3 triage and downstream analysis."""

    def __init__(
        self,
        *,
        frontdoor_orienter: Callable[[str | Path], Path],
        **kwargs,
    ) -> None:
        # The parent runtime's orienter is A3-only. Disable it here so A3 does
        # not run the same correction a second time after front-door routing.
        super().__init__(a3_page_orienter=None, **kwargs)
        self.frontdoor_orienter = frontdoor_orienter

    def _route_persisted_image(
        self,
        state: A3SessionState,
        persisted: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
    ) -> AgentResponse:
        corrected = Path(self.frontdoor_orienter(persisted)).resolve()
        state.source_page_path = str(corrected)
        self.store.save(state)
        return super()._route_persisted_image(
            state,
            corrected,
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
        )

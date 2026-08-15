"""Isolated tool layer for the future question-bank Agent.

This package is intentionally separate from the existing Feishu bot runtime.
Do not store Agent checkpoints, Feishu events, or temporary images in the old
`.tmp_feishu_tiku` directory.
"""

__all__ = ["AgentResponse", "AgentState", "TikuSearchAgent"]


def __getattr__(name: str):
    """Keep pure contract modules independent from optional model clients."""

    if name == "AgentState":
        from tiku_agent.state import AgentState

        return AgentState
    if name in {"AgentResponse", "TikuSearchAgent"}:
        from tiku_agent.agent import AgentResponse, TikuSearchAgent

        return {"AgentResponse": AgentResponse, "TikuSearchAgent": TikuSearchAgent}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

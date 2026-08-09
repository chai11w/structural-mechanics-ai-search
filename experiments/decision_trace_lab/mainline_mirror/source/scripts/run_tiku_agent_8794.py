"""Run the isolated 8794 bounded-autonomy candidate baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.agent import TikuSearchAgent
from tiku_agent.fastapi_demo import create_app
from tiku_agent.safe_answer_generator_v0 import (
    SafeAnswerGeneratorV0,
    SafeAnswerModelRequestV0,
)
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger
from tiku_agent.tools import AgentToolConfig


DEFAULT_PORT = 8794
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_candidate_8794"
SESSION_COOKIE = "tiku_agent_8794_session"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    enable_safe_answer_v0: bool = False,
    enable_dimension_filter: bool = False,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
) -> AgentSessionRuntime:
    """Build the 8794 runtime with all writable state under one isolated root."""
    root = Path(runtime_dir).resolve()
    artifacts = SessionArtifacts(root / "sessions")
    agent_factory = None
    generator = None
    if enable_safe_answer_v0:
        generator = SafeAnswerGeneratorV0(safe_answer_model_client or QwenSafeAnswerClientV0())

    if enable_safe_answer_v0 or enable_dimension_filter:
        def build_agent(state: AgentState) -> TikuSearchAgent:
            return TikuSearchAgent(
                state=state,
                config=AgentToolConfig(
                    runtime_dir=root,
                    session_dir=artifacts.session_dir(state.session_id),
                    dimension_filter_enabled=enable_dimension_filter,
                ),
                enable_safe_answer_v0=enable_safe_answer_v0,
                safe_answer_generator_v0=generator,
            )

        agent_factory = build_agent
    return AgentSessionRuntime(
        SQLiteSessionStore(root / "session.db"),
        artifacts=artifacts,
        task_logger=JsonlTaskLogger(root / "task_logs.jsonl"),
        agent_factory=agent_factory,
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
    enable_safe_answer_v0: bool = False,
    enable_dimension_filter: bool = False,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
):
    """Create the behavior-equivalent 8794 app with isolated state and cookie."""
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            enable_safe_answer_v0=enable_safe_answer_v0,
            enable_dimension_filter=enable_dimension_filter,
            safe_answer_model_client=safe_answer_model_client,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the dedicated 8794 launcher arguments.

    Safe answers are part of the accepted 8794 candidate behavior, so the
    launcher keeps them enabled across restarts.  The library builders remain
    opt-in to preserve isolated tests and explicit embedding behavior.
    """
    parser = argparse.ArgumentParser(description="Run the isolated 8794 Agent candidate baseline")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    dimension_filter_group = parser.add_mutually_exclusive_group()
    dimension_filter_group.add_argument(
        "--enable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_true",
        help="Enable V5.2 dimension filtering when symbolic candidates exceed 20 (default)",
    )
    dimension_filter_group.add_argument(
        "--disable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_false",
        help="Temporarily disable the V5.2 dimension filter",
    )
    safe_answer_group = parser.add_mutually_exclusive_group()
    safe_answer_group.add_argument(
        "--enable-safe-answer-v0",
        dest="enable_safe_answer_v0",
        action="store_true",
        help="Enable bounded Qwen answers for safe zero-tool conversations (default)",
    )
    safe_answer_group.add_argument(
        "--disable-safe-answer-v0",
        dest="enable_safe_answer_v0",
        action="store_false",
        help="Temporarily disable safe answers and use the original Intent V2 replies",
    )
    parser.set_defaults(enable_safe_answer_v0=True, enable_dimension_filter=True)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            enable_safe_answer_v0=args.enable_safe_answer_v0,
            enable_dimension_filter=args.enable_dimension_filter,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

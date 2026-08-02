"""Run the isolated local Agent demo on port 8790."""

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
from tiku_shared.model_costs import SQLiteModelCostLedger


DEFAULT_V2_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_V2_RUNTIME_DIR,
    *,
    enable_safe_answer_v0: bool = True,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
) -> AgentSessionRuntime:
    """Build the 8790 runtime with bounded safe answers enabled by default."""
    root = Path(runtime_dir).resolve()
    artifacts = SessionArtifacts(root / "sessions")
    agent_factory = None
    if enable_safe_answer_v0:
        generator = SafeAnswerGeneratorV0(
            safe_answer_model_client or QwenSafeAnswerClientV0()
        )

        def build_agent(state: AgentState) -> TikuSearchAgent:
            return TikuSearchAgent(
                state=state,
                config=AgentToolConfig(
                    runtime_dir=root,
                    session_dir=artifacts.session_dir(state.session_id),
                ),
                enable_safe_answer_v0=True,
                safe_answer_generator_v0=generator,
            )

        agent_factory = build_agent
    return AgentSessionRuntime(
        SQLiteSessionStore(root / "session.db"),
        artifacts=artifacts,
        task_logger=JsonlTaskLogger(root / "task_logs.jsonl"),
        cost_ledger=SQLiteModelCostLedger(root / "model_costs.sqlite3"),
        agent_factory=agent_factory,
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_V2_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
    enable_safe_answer_v0: bool = True,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
):
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            enable_safe_answer_v0=enable_safe_answer_v0,
            safe_answer_model_client=safe_answer_model_client,
        ),
        incoming_dir=root / "incoming",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated question-bank Agent FastAPI demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--runtime-dir", type=Path)
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
        help="Temporarily use the original fixed Intent V2 replies",
    )
    parser.set_defaults(enable_safe_answer_v0=True)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    runtime_dir = (args.runtime_dir or DEFAULT_V2_RUNTIME_DIR).resolve()
    uvicorn.run(
        build_app(
            runtime_dir,
            enable_safe_answer_v0=args.enable_safe_answer_v0,
        ),
        host=args.host,
        port=args.port,
    )
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

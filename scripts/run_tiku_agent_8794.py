"""Run the isolated 8794 bounded-autonomy candidate baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.fastapi_demo import create_app
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.task_log import JsonlTaskLogger


DEFAULT_PORT = 8794
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_candidate_8794"
SESSION_COOKIE = "tiku_agent_8794_session"


def build_runtime(runtime_dir: str | Path = DEFAULT_RUNTIME_DIR) -> AgentSessionRuntime:
    """Build the 8794 runtime with all writable state under one isolated root."""
    root = Path(runtime_dir).resolve()
    return AgentSessionRuntime(
        SQLiteSessionStore(root / "session.db"),
        artifacts=SessionArtifacts(root / "sessions"),
        task_logger=JsonlTaskLogger(root / "task_logs.jsonl"),
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
):
    """Create the behavior-equivalent 8794 app with isolated state and cookie."""
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime or build_runtime(root),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated 8794 Agent candidate baseline")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()
    uvicorn.run(build_app(args.runtime_dir), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

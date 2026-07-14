"""Run the isolated local Agent demo on port 8790."""

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


DEFAULT_V2_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated question-bank Agent FastAPI demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--runtime-dir", type=Path)
    args = parser.parse_args()
    runtime_dir = (args.runtime_dir or DEFAULT_V2_RUNTIME_DIR).resolve()
    runtime = AgentSessionRuntime(
        SQLiteSessionStore(runtime_dir / "session.db"),
        artifacts=SessionArtifacts(runtime_dir / "sessions"),
        task_logger=JsonlTaskLogger(runtime_dir / "task_logs.jsonl"),
    )
    uvicorn.run(
        create_app(runtime=runtime, incoming_dir=runtime_dir / "incoming"),
        host=args.host,
        port=args.port,
    )
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

"""Run the isolated A3 manual-crop MVP on port 8896."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_demo import build_runtime as build_a2_runtime
from tiku_agent.a3_models import QwenA3CropVerifier, QwenA3PageObserver, QwenA3UnitAnalyzer
from tiku_agent.a3_runtime import A3MvpRuntime, SQLiteA3SessionStore
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_shared.model_costs import SQLiteModelCostLedger


DEFAULT_PORT = 8896
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_mvp_8896"
SESSION_COOKIE = "tiku_agent_8896_session"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    model_timeout_seconds: float = 120.0,
    page_observer=None,
    crop_verifier=None,
    unit_analyzer=None,
) -> A3MvpRuntime:
    """Build A3 and its child A2 with separate state under one isolated root."""

    root = Path(runtime_dir).resolve()
    a2_runtime = build_a2_runtime(
        root / "a2",
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=False,
    )
    return A3MvpRuntime(
        store=SQLiteA3SessionStore(root / "a3_sessions.sqlite3"),
        artifacts=SessionArtifacts(root / "a3_sessions"),
        a2_runtime=a2_runtime,
        page_observer=page_observer or QwenA3PageObserver(timeout_seconds=model_timeout_seconds),
        crop_verifier=crop_verifier or QwenA3CropVerifier(timeout_seconds=model_timeout_seconds),
        unit_analyzer=unit_analyzer or QwenA3UnitAnalyzer(timeout_seconds=model_timeout_seconds),
        cost_ledger=SQLiteModelCostLedger(root / "model_costs.sqlite3"),
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: A3MvpRuntime | None = None,
    model_timeout_seconds: float = 120.0,
):
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime or build_runtime(root, model_timeout_seconds=model_timeout_seconds),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated A3 manual-crop MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(args.runtime_dir, model_timeout_seconds=args.model_timeout_seconds),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

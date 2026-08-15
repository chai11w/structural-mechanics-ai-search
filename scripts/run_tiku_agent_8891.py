"""Run the isolated 8891 authoritative image-triage MVP."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_demo import build_runtime as build_fixed_runtime
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.image_triage import QwenImageTriage
from tiku_agent.image_triage_authority import ImageTriageAuthority, QwenTriageReplyClient
from tiku_agent.session_runtime import AgentSessionRuntime


DEFAULT_PORT = 8891
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_validation_8891"
SESSION_COOKIE = "tiku_agent_8891_session"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
) -> AgentSessionRuntime:
    """Build 8891 with independent state and the authoritative triage gate."""

    root = Path(runtime_dir).resolve()
    runtime = build_fixed_runtime(
        root,
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=False,
    )
    if enable_triage:
        runtime.image_triage_authority = ImageTriageAuthority(
            QwenImageTriage(timeout_seconds=triage_timeout_seconds),
            QwenTriageReplyClient(timeout_seconds=reply_timeout_seconds),
        )
    return runtime


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
):
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated 8891 triage MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--disable-triage",
        dest="enable_triage",
        action="store_false",
        help="Temporarily run the underlying fixed search line for local tests",
    )
    parser.set_defaults(enable_triage=True)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            enable_triage=args.enable_triage,
            triage_timeout_seconds=args.triage_timeout_seconds,
            reply_timeout_seconds=args.reply_timeout_seconds,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

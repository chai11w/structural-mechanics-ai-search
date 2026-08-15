"""Run the isolated 8890 fixed-line business-validation baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_demo import build_runtime as build_fixed_runtime
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.safe_answer_generator_v0 import SafeAnswerModelRequestV0
from tiku_agent.session_runtime import AgentSessionRuntime


DEFAULT_PORT = 8890
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_validation_8890"
SESSION_COOKIE = "tiku_agent_8890_session"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    enable_safe_answer_v0: bool = True,
    enable_dimension_filter: bool = True,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
    enable_external_load_screen: bool = True,
    external_load_timeout_seconds: float = 15.0,
    external_load_screen: Callable[[str | Path], str] | None = None,
) -> AgentSessionRuntime:
    """Build the current fixed runtime with all writable state under 8890."""
    return build_fixed_runtime(
        Path(runtime_dir).resolve(),
        enable_safe_answer_v0=enable_safe_answer_v0,
        enable_dimension_filter=enable_dimension_filter,
        safe_answer_model_client=safe_answer_model_client,
        enable_external_load_screen=enable_external_load_screen,
        external_load_timeout_seconds=external_load_timeout_seconds,
        external_load_screen=external_load_screen,
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
    enable_safe_answer_v0: bool = True,
    enable_dimension_filter: bool = True,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
    enable_external_load_screen: bool = True,
    external_load_timeout_seconds: float = 15.0,
    external_load_screen: Callable[[str | Path], str] | None = None,
):
    """Create an app isolated from the 8790/8794/8795 runtime state."""
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            enable_safe_answer_v0=enable_safe_answer_v0,
            enable_dimension_filter=enable_dimension_filter,
            safe_answer_model_client=safe_answer_model_client,
            enable_external_load_screen=enable_external_load_screen,
            external_load_timeout_seconds=external_load_timeout_seconds,
            external_load_screen=external_load_screen,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated 8890 fixed-line business-validation baseline"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)

    dimension_group = parser.add_mutually_exclusive_group()
    dimension_group.add_argument(
        "--enable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_true",
        help="Enable V5.2 dimension filtering when symbolic candidates exceed 20 (default)",
    )
    dimension_group.add_argument(
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
        help="Temporarily use the original fixed Intent V2 replies",
    )

    external_load_group = parser.add_mutually_exclusive_group()
    external_load_group.add_argument(
        "--enable-external-load-screen",
        dest="enable_external_load_screen",
        action="store_true",
        help="Run the parallel external-load gate for uploaded images (default)",
    )
    external_load_group.add_argument(
        "--disable-external-load-screen",
        dest="enable_external_load_screen",
        action="store_false",
        help="Temporarily roll back to the original image search flow",
    )
    parser.add_argument(
        "--external-load-timeout-seconds",
        type=float,
        default=15.0,
        help="Maximum time an empty/non-candidate result waits for the gate (default: 15)",
    )
    parser.set_defaults(
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=True,
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            enable_safe_answer_v0=args.enable_safe_answer_v0,
            enable_dimension_filter=args.enable_dimension_filter,
            enable_external_load_screen=args.enable_external_load_screen,
            external_load_timeout_seconds=args.external_load_timeout_seconds,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

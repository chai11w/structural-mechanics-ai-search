"""Run the isolated A3-V1 automatic-crop flow on port 8897."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8896 import build_runtime as build_manual_runtime
from tiku_agent.a3_auto_crop import GlmA3AutoCropper
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore


DEFAULT_PORT = 8897
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_v1_8897"
SESSION_COOKIE = "tiku_agent_8897_session"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    image_triage_authority=None,
    page_observer=None,
    crop_verifier=None,
    auto_cropper=None,
):
    root = Path(runtime_dir).resolve()
    return build_manual_runtime(
        root,
        model_timeout_seconds=model_timeout_seconds,
        auto_prepare_all_units=False,
        enable_triage=enable_triage,
        triage_timeout_seconds=triage_timeout_seconds,
        reply_timeout_seconds=reply_timeout_seconds,
        image_triage_authority=image_triage_authority,
        page_observer=page_observer,
        crop_verifier=crop_verifier,
        auto_cropper=auto_cropper
        or GlmA3AutoCropper(timeout_seconds=grounding_timeout_seconds),
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime=None,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
):
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            model_timeout_seconds=model_timeout_seconds,
            grounding_timeout_seconds=grounding_timeout_seconds,
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run isolated A3-V1 automatic crop flow")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--disable-triage",
        dest="enable_triage",
        action="store_false",
        help="Run the A3-only path for isolated diagnostics",
    )
    parser.set_defaults(enable_triage=True)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            model_timeout_seconds=args.model_timeout_seconds,
            grounding_timeout_seconds=args.grounding_timeout_seconds,
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

"""Run an isolated 8896 clone with explicit Qwen V1 visual rerank."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import search
from scripts.run_tiku_agent_demo import build_runtime as build_a2_runtime
from tiku_agent.a3_models import QwenA3CropVerifier, QwenA3PageObserver
from tiku_agent.a3_runtime import A3MvpRuntime, SQLiteA3SessionStore
from tiku_agent.external_load_screen import ZhipuExternalLoadScreen
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.image_triage import QwenImageTriage
from tiku_agent.image_triage_authority import ImageTriageAuthority, QwenTriageReplyClient
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_shared.model_costs import SQLiteModelCostLedger


DEFAULT_PORT = 8897
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_mvp_8897"
SESSION_COOKIE = "tiku_agent_8897_session"

# Keep the 8896 operational bounds while making the model and prompt explicit.
A2_RERANK_POLICY = {
    "provider": "qwen",
    "model": "qwen3.7-plus",
    "max_workers": 8,
    "candidate_timeout_seconds": 12.0,
    "retry_timeout_seconds": 12.0,
    "retry_max_candidates": 8,
    "retry_max_workers": 2,
    "retry_failed_candidates": True,
}

# This process is isolated, so pinning the prompt here cannot change 8896 or
# the shared CLI/API processes.
search.DEFAULT_QWEN_RERANK_PROMPT = search._load_qwen_rerank_prompt("v1")


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    model_timeout_seconds: float = 120.0,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    image_triage_authority=None,
    page_observer=None,
    crop_verifier=None,
    unit_analyzer=None,
) -> A3MvpRuntime:
    root = Path(runtime_dir).resolve()
    a2_runtime = build_a2_runtime(
        root / "a2",
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=False,
        enable_chapter_scope_fallback=True,
        rerank_policy=A2_RERANK_POLICY,
    )
    authority = image_triage_authority
    if authority is None and enable_triage:
        authority = ImageTriageAuthority(
            QwenImageTriage(timeout_seconds=triage_timeout_seconds),
            QwenTriageReplyClient(timeout_seconds=reply_timeout_seconds),
        )
    return A3MvpRuntime(
        store=SQLiteA3SessionStore(root / "a3_sessions.sqlite3"),
        artifacts=SessionArtifacts(root / "a3_sessions"),
        a2_runtime=a2_runtime,
        page_observer=page_observer or QwenA3PageObserver(timeout_seconds=model_timeout_seconds),
        crop_verifier=crop_verifier or QwenA3CropVerifier(timeout_seconds=model_timeout_seconds),
        unit_analyzer=unit_analyzer,
        external_load_screen=ZhipuExternalLoadScreen(),
        image_triage_authority=authority,
        cost_ledger=SQLiteModelCostLedger(root / "model_costs.sqlite3"),
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: A3MvpRuntime | None = None,
    model_timeout_seconds: float = 120.0,
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
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated 8897 A1/A2/A3 flow with Qwen V1 rerank")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--disable-triage",
        dest="enable_triage",
        action="store_false",
        help="Temporarily run the A3-only path for local diagnostics",
    )
    parser.set_defaults(enable_triage=True)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            model_timeout_seconds=args.model_timeout_seconds,
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

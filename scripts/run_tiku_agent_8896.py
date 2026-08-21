"""Run the promoted A3-V1 flow on port 8896."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_demo import build_runtime as build_a2_runtime
from tiku_agent.a3_intent_v1 import A3IntentEngineV1, call_qwen_a3_intent_v1
from tiku_agent.a3_auto_crop import GlmA3AutoCropper
from tiku_agent.a3_models import QwenA3CropVerifier, QwenA3PageObserver
from tiku_agent.a3_runtime import A3MvpRuntime, SQLiteA3SessionStore
from tiku_agent.external_load_screen import QwenExternalLoadScreen
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.image_triage import QwenImageTriage
from tiku_agent.image_triage_authority import ImageTriageAuthority, QwenTriageReplyClient
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_shared.model_costs import SQLiteModelCostLedger


DEFAULT_PORT = 8896
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_mvp_8896"
SESSION_COOKIE = "tiku_agent_8896_session"
A2_RERANK_POLICY = {
    "rerank_provider": "qwen",
    "rerank_model": "qwen3.7-plus",
    "max_workers": 10,
    "candidate_timeout_seconds": 12.0,
    "retry_timeout_seconds": 12.0,
    "retry_max_candidates": 8,
    "retry_max_workers": 2,
    "retry_failed_candidates": True,
    "display_by_rerank_score": True,
    "display_all_score": 0.95,
    "display_fallback_top_n": 3,
}


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_auto_crop: bool = True,
    auto_prepare_all_units: bool = True,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    image_triage_authority=None,
    page_observer=None,
    crop_verifier=None,
    auto_cropper=None,
    unit_analyzer=None,
    control_store=None,
    enable_a3_intent_v1: bool = True,
    enable_a3_intent_model_fallback: bool = True,
    a3_intent_model_client=None,
    enable_author_contact_fallback: bool = True,
) -> A3MvpRuntime:
    """Build the full A1/A2/A3 route with A3 and its child A2 under one root."""

    root = Path(runtime_dir).resolve()
    shared_cost_ledger = SQLiteModelCostLedger(root / "model_costs.sqlite3")
    a2_runtime = build_a2_runtime(
        root / "a2",
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=False,
        enable_chapter_scope_fallback=True,
        enable_author_contact_fallback=enable_author_contact_fallback,
        rerank_policy=A2_RERANK_POLICY,
        cost_ledger=shared_cost_ledger,
        control_store=control_store,
    )
    authority = image_triage_authority
    if authority is None and enable_triage:
        authority = ImageTriageAuthority(
            QwenImageTriage(timeout_seconds=triage_timeout_seconds),
            QwenTriageReplyClient(timeout_seconds=reply_timeout_seconds),
        )
    resolved_auto_cropper = auto_cropper
    if resolved_auto_cropper is None and enable_auto_crop:
        resolved_auto_cropper = GlmA3AutoCropper(
            timeout_seconds=grounding_timeout_seconds,
        )
    intent_client = a3_intent_model_client
    if enable_a3_intent_v1 and enable_a3_intent_model_fallback and intent_client is None:
        intent_client = lambda prompt: call_qwen_a3_intent_v1(  # noqa: E731 - bounded adapter.
            prompt,
            timeout=max(1, int(reply_timeout_seconds)),
        )
    return A3MvpRuntime(
        store=SQLiteA3SessionStore(root / "a3_sessions.sqlite3"),
        artifacts=SessionArtifacts(root / "a3_sessions"),
        a2_runtime=a2_runtime,
        page_observer=page_observer or QwenA3PageObserver(timeout_seconds=model_timeout_seconds),
        crop_verifier=crop_verifier or QwenA3CropVerifier(timeout_seconds=model_timeout_seconds),
        auto_cropper=resolved_auto_cropper,
        auto_prepare_all_units=auto_prepare_all_units and enable_auto_crop,
        unit_analyzer=unit_analyzer,
        external_load_screen=QwenExternalLoadScreen(),
        image_triage_authority=authority,
        cost_ledger=shared_cost_ledger,
        intent_engine=(
            A3IntentEngineV1(intent_client)
            if enable_a3_intent_v1
            else None
        ),
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime: A3MvpRuntime | None = None,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_auto_crop: bool = True,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    enable_a3_intent_v1: bool = True,
    enable_a3_intent_model_fallback: bool = True,
):
    root = Path(runtime_dir).resolve()
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            model_timeout_seconds=model_timeout_seconds,
            grounding_timeout_seconds=grounding_timeout_seconds,
            enable_auto_crop=enable_auto_crop,
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
            enable_a3_intent_v1=enable_a3_intent_v1,
            enable_a3_intent_model_fallback=enable_a3_intent_model_fallback,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated A1/A2/A3 flow with A3-V1 auto crop")
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
        help="Temporarily run the A3-only path for local diagnostics",
    )
    parser.add_argument(
        "--disable-auto-crop",
        dest="enable_auto_crop",
        action="store_false",
        help="Roll back A3 to the V0 manual-crop flow",
    )
    parser.add_argument(
        "--disable-a3-intent-v1",
        dest="enable_a3_intent_v1",
        action="store_false",
        help="Temporarily use the legacy A3 text rules",
    )
    parser.set_defaults(
        enable_triage=True,
        enable_auto_crop=True,
        enable_a3_intent_v1=True,
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            model_timeout_seconds=args.model_timeout_seconds,
            grounding_timeout_seconds=args.grounding_timeout_seconds,
            enable_auto_crop=args.enable_auto_crop,
            enable_triage=args.enable_triage,
            triage_timeout_seconds=args.triage_timeout_seconds,
            reply_timeout_seconds=args.reply_timeout_seconds,
            enable_a3_intent_v1=args.enable_a3_intent_v1,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

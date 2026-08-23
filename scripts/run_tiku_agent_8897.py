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
from tiku_agent.image_triage import QwenImageTriage
from tiku_agent.image_triage_8897 import (
    build_handoff_8897,
    build_handoff_8897_v1,
    build_handoff_8897_v2,
    observation_from_model_text_8897,
    observation_from_model_text_8897_v1,
    observation_from_model_text_8897_v2,
)
from tiku_agent.image_triage_authority import ImageTriageAuthority, QwenTriageReplyClient


DEFAULT_PORT = 8897
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_v1_8897"
SESSION_COOKIE = "tiku_agent_8897_session"
TRIAGE_POLICY_VERSION = "v3"
TRIAGE_POLICIES = {
    "v1": (
        BASE / "experiments" / "complex_image_eval" / "observation_prompt_8897_boundary_v1.md",
        observation_from_model_text_8897_v1,
        build_handoff_8897_v1,
    ),
    "v2": (
        BASE / "experiments" / "complex_image_eval" / "observation_prompt_8897_boundary_v2.md",
        observation_from_model_text_8897_v2,
        build_handoff_8897_v2,
    ),
    "v3": (
        BASE / "experiments" / "complex_image_eval" / "observation_prompt_8897_boundary_v3.md",
        observation_from_model_text_8897,
        build_handoff_8897,
    ),
}
TRIAGE_PROMPT_PATH = TRIAGE_POLICIES[TRIAGE_POLICY_VERSION][0]


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
    triage_policy_version: str = TRIAGE_POLICY_VERSION,
):
    root = Path(runtime_dir).resolve()
    authority = image_triage_authority
    if authority is None and enable_triage:
        try:
            prompt_path, observation_parser, handoff_builder = TRIAGE_POLICIES[triage_policy_version]
        except KeyError as exc:
            raise ValueError(f"unsupported 8897 triage policy: {triage_policy_version}") from exc
        authority = ImageTriageAuthority(
            QwenImageTriage(
                timeout_seconds=triage_timeout_seconds,
                prompt_path=prompt_path,
                observation_parser=observation_parser,
            ),
            QwenTriageReplyClient(timeout_seconds=reply_timeout_seconds),
            handoff_builder=handoff_builder,
        )
    return build_manual_runtime(
        root,
        model_timeout_seconds=model_timeout_seconds,
        auto_prepare_all_units=False,
        enable_triage=enable_triage,
        triage_timeout_seconds=triage_timeout_seconds,
        reply_timeout_seconds=reply_timeout_seconds,
        image_triage_authority=authority,
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
    triage_policy_version: str = TRIAGE_POLICY_VERSION,
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
            triage_policy_version=triage_policy_version,
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
        "--triage-policy-version",
        choices=tuple(TRIAGE_POLICIES),
        default=TRIAGE_POLICY_VERSION,
    )
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
            triage_policy_version=args.triage_policy_version,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run an isolated shadow copy of the current 8896 flow on port 8898."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8896 import build_runtime as build_8896_runtime
from tiku_agent.a3_text_orientation import RapidOcrTextPageOrienter
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore


DEFAULT_PORT = 8898
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_shadow_8898"
SESSION_COOKIE = "tiku_agent_8898_shadow_session"
OUTPUT_LAYER_COMMITS = (
    "5eacf7d",
    "5731e31",
    "3698d62",
    "99934cb",
)
OUTPUT_LAYER_FILES = (
    "tiku_agent/agent.py",
    "tiku_agent/tools.py",
    "tiku_agent/tool_result.py",
    "tiku_agent/render.py",
    "tiku_shared/request_protocol.py",
    "tiku_agent/fastapi_demo.py",
)
MINIMAL_A3_MEDIA_ADAPTER = (
    "retry_texts",
    "mark_media_delivery_failed",
    "media_guard",
    "media_failure_state_cleanup",
)


def write_shadow_manifest(root: Path) -> Path:
    """Record the code boundary and runtime paths used by this shadow process."""

    files: dict[str, str] = {}
    for relative in OUTPUT_LAYER_FILES + ("tiku_agent/a3_runtime.py",):
        path = BASE / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        files[relative] = digest
    manifest = {
        "schema": "tiku-agent-shadow-8898-v1",
        "source_root": str(BASE),
        "output_layer_commits": list(OUTPUT_LAYER_COMMITS),
        "output_layer_files": list(OUTPUT_LAYER_FILES),
        "a3_adapter": list(MINIMAL_A3_MEDIA_ADAPTER),
        "a3_page_orientation": "rapidocr_text_regions_four_direction_after_a3_route",
        "file_sha256": files,
        "excluded_from_increment": [
            "retrieval and fee changes",
            "Feishu entrypoints",
            "tests and documentation",
        ],
        "runtime_root": str(root),
        "session_cookie": SESSION_COOKIE,
        "listen_default": "127.0.0.1:8898",
    }
    path = root / "shadow_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    runtime=None,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_auto_crop: bool = True,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    enable_a3_intent_v1: bool = True,
    enable_a3_intent_model_fallback: bool = True,
    enable_a3_text_orientation: bool = True,
):
    root = Path(runtime_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_shadow_manifest(root)
    resolved_runtime = runtime
    if resolved_runtime is None:
        page_orienter = RapidOcrTextPageOrienter() if enable_a3_text_orientation else None
        resolved_runtime = build_8896_runtime(
            root,
            model_timeout_seconds=model_timeout_seconds,
            grounding_timeout_seconds=grounding_timeout_seconds,
            enable_auto_crop=enable_auto_crop,
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
            enable_a3_intent_v1=enable_a3_intent_v1,
            enable_a3_intent_model_fallback=enable_a3_intent_model_fallback,
            a3_page_orienter=page_orienter,
        )
    return create_app(
        runtime=resolved_runtime,
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the current 8896 flow in an isolated local shadow environment"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--disable-triage", dest="enable_triage", action="store_false")
    parser.add_argument("--disable-auto-crop", dest="enable_auto_crop", action="store_false")
    parser.add_argument(
        "--disable-a3-intent-v1", dest="enable_a3_intent_v1", action="store_false"
    )
    parser.add_argument(
        "--disable-a3-text-orientation",
        dest="enable_a3_text_orientation",
        action="store_false",
    )
    parser.set_defaults(
        enable_triage=True,
        enable_auto_crop=True,
        enable_a3_intent_v1=True,
        enable_a3_intent_model_fallback=True,
        enable_a3_text_orientation=True,
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
            enable_a3_intent_model_fallback=args.enable_a3_intent_model_fallback,
            enable_a3_text_orientation=args.enable_a3_text_orientation,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

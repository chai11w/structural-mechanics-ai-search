"""Run the A3-V1 business core with the 8790 production access shell."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8896 import build_runtime as build_a3_runtime
from tiku_admin.auth import SQLiteInviteAccess
from tiku_admin.control_store import SQLiteControlStore
from tiku_agent.fastapi_demo import SESSION_COOKIE, create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess
from tiku_agent.output_watchdog import OutputWatchdog
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder


DEFAULT_PORT = 8790
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_prod_8790"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return parsed


def _validate_queue_settings(
    max_concurrent_tasks: int,
    max_queued_tasks: int,
    queue_wait_seconds: float,
) -> None:
    if int(max_concurrent_tasks) <= 0:
        raise ValueError("max_concurrent_tasks must be greater than zero")
    if int(max_queued_tasks) < 0:
        raise ValueError("max_queued_tasks must be zero or greater")
    wait_seconds = float(queue_wait_seconds)
    if not math.isfinite(wait_seconds) or wait_seconds <= 0:
        raise ValueError("queue_wait_seconds must be finite and greater than zero")


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    control_db: str | Path | None = None,
    invite_config: str | Path | None = None,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_auto_crop: bool = True,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    enable_output_watchdog: bool = True,
    max_concurrent_tasks: int = 1,
    max_queued_tasks: int = 2,
    queue_wait_seconds: float = 55.0,
):
    _validate_queue_settings(
        max_concurrent_tasks,
        max_queued_tasks,
        queue_wait_seconds,
    )
    root = Path(runtime_dir).resolve()
    if control_db is not None and invite_config is not None:
        raise ValueError("use either control_db or invite_config, not both")
    control_path = Path(control_db).resolve() if control_db is not None else None
    if control_path is not None and not control_path.is_file():
        raise ValueError(f"control database not found: {control_path}")
    control_store = SQLiteControlStore(control_path) if control_path is not None else None
    output_watchdog = OutputWatchdog(
        root / "output_watchdog",
        enabled=enable_output_watchdog,
    )
    return create_app(
        runtime=build_a3_runtime(
            root,
            model_timeout_seconds=model_timeout_seconds,
            grounding_timeout_seconds=grounding_timeout_seconds,
            enable_auto_crop=enable_auto_crop,
            auto_prepare_all_units=True,
            enable_triage=enable_triage,
            triage_timeout_seconds=triage_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
            control_store=control_store,
            enable_a3_intent_v1=True,
            enable_a3_intent_model_fallback=True,
            enable_author_contact_fallback=True,
            enable_three_scope_cancel_clarification=True,
            preserve_a2_artifacts_on_cancel=True,
            max_concurrent_tasks=max_concurrent_tasks,
            max_queued_tasks=max_queued_tasks,
            queue_wait_seconds=queue_wait_seconds,
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        output_watchdog=output_watchdog,
        invite_access=(
            SQLiteInviteAccess(control_store)
            if control_store is not None
            else InviteAccess(invite_config) if invite_config else None
        ),
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
        feedback_retention_days_provider=(
            (lambda: int(control_store.settings()["feedback_retention_days"]))
            if control_store is not None
            else None
        ),
        trace_event_recorder=TraceEventRecorder(
            SQLiteTraceEventStore(root / "trace_events.sqlite3")
        ),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the A3-V1 business core on the 8790 production route"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--control-db", type=Path)
    parser.add_argument("--invite-config", type=Path)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-concurrent-tasks",
        type=_positive_int,
        default=1,
        help="Maximum active production tasks (default: 1)",
    )
    parser.add_argument(
        "--max-queued-tasks",
        type=_nonnegative_int,
        default=2,
        help="Maximum waiting production tasks (default: 2)",
    )
    parser.add_argument(
        "--queue-wait-seconds",
        type=_positive_float,
        default=55.0,
        help="Maximum queue wait before returning busy (default: 55)",
    )
    parser.add_argument(
        "--disable-output-watchdog",
        dest="enable_output_watchdog",
        action="store_false",
        help="Disable fail-open output observation",
    )
    parser.add_argument(
        "--disable-triage",
        dest="enable_triage",
        action="store_false",
        help="Temporarily bypass A1/A2/A3 triage",
    )
    parser.add_argument(
        "--disable-auto-crop",
        dest="enable_auto_crop",
        action="store_false",
        help="Roll A3 back to the V0 manual-crop flow",
    )
    parser.set_defaults(
        enable_triage=True,
        enable_auto_crop=True,
        enable_output_watchdog=True,
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            control_db=args.control_db,
            invite_config=args.invite_config,
            model_timeout_seconds=args.model_timeout_seconds,
            grounding_timeout_seconds=args.grounding_timeout_seconds,
            enable_auto_crop=args.enable_auto_crop,
            enable_triage=args.enable_triage,
            triage_timeout_seconds=args.triage_timeout_seconds,
            reply_timeout_seconds=args.reply_timeout_seconds,
            enable_output_watchdog=args.enable_output_watchdog,
            max_concurrent_tasks=args.max_concurrent_tasks,
            max_queued_tasks=args.max_queued_tasks,
            queue_wait_seconds=args.queue_wait_seconds,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the pre-task-state A3-V1 demo shadow on port 8888."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8790 import build_app as build_demo_app


DEFAULT_PORT = 8888
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_demo_8888"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the stable pre-task-state A3-V1 demo shadow on port 8888"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-concurrent-tasks", type=int, default=1)
    parser.add_argument("--max-queued-tasks", type=int, default=2)
    parser.add_argument("--queue-wait-seconds", type=float, default=55.0)
    parser.add_argument("--disable-output-watchdog", action="store_true")
    parser.add_argument("--disable-triage", action="store_true")
    parser.add_argument("--disable-auto-crop", action="store_true")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_demo_app(
            args.runtime_dir,
            model_timeout_seconds=args.model_timeout_seconds,
            grounding_timeout_seconds=args.grounding_timeout_seconds,
            enable_auto_crop=not args.disable_auto_crop,
            enable_triage=not args.disable_triage,
            triage_timeout_seconds=args.triage_timeout_seconds,
            reply_timeout_seconds=args.reply_timeout_seconds,
            enable_output_watchdog=not args.disable_output_watchdog,
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

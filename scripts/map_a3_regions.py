"""Run only the first A3 semantic page-mapping step."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.image_region_mapper import (
    A3RegionMap,
    A3RegionMapRuntime,
    QwenA3RegionObserver,
    parse_a3_region_map,
)


DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_region_map_8892"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Map A3 question/diagram regions only; do not run OpenCV, chapter "
            "recognition, A2, or search"
        )
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument(
        "--observation-json",
        type=Path,
        help="Render and validate a saved region map without calling Qwen",
    )
    parser.add_argument("--model", default="qwen3.7-plus")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optionally save the first-step result as formatted JSON",
    )
    return parser


def load_observation(path: str | Path) -> A3RegionMap:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_a3_region_map(payload)


def main() -> int:
    args = build_argument_parser().parse_args()
    runtime = A3RegionMapRuntime(
        args.runtime_dir,
        observer=QwenA3RegionObserver(
            model=args.model,
            timeout_seconds=args.timeout_seconds,
        ),
    )
    observation = (
        load_observation(args.observation_json) if args.observation_json else None
    )
    result = runtime.map_page(args.image, observation=observation)
    text = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result.status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the isolated 8892 Phase 2 A3 decomposition entry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.image_contracts import A3PageObservation
from tiku_agent.image_decomposer import (
    A3ChapterObserver,
    A3DecompositionRuntime,
    A3Observer,
    QwenA3ChapterObserver,
    QwenA3Observer,
    parse_a3_observation,
)


SERVICE_ID = 8892
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_a3_8892"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    observer: A3Observer | None = None,
    chapter_observer: A3ChapterObserver | None = None,
    model: str = "qwen3.7-plus",
    timeout_seconds: float = 120.0,
    max_chapter_workers: int = 2,
    enable_chapter_recognition: bool = True,
) -> A3DecompositionRuntime:
    return A3DecompositionRuntime(
        runtime_dir,
        observer=observer
        or QwenA3Observer(model=model, timeout_seconds=timeout_seconds),
        chapter_observer=(
            chapter_observer
            or QwenA3ChapterObserver(model=model, timeout_seconds=timeout_seconds)
            if enable_chapter_recognition
            else None
        ),
        max_chapter_workers=max_chapter_workers,
    )


def load_observation(path: str | Path) -> A3PageObservation:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_a3_observation(payload)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run isolated A3 image grouping and cropping only; this entry never calls A2"
        )
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument(
        "--observation-json",
        type=Path,
        help="Use a saved structured observation instead of calling Qwen",
    )
    parser.add_argument("--model", default="qwen3.7-plus")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--max-chapter-workers", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optionally save the local decomposition result as formatted JSON",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    runtime = build_runtime(
        args.runtime_dir,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        max_chapter_workers=args.max_chapter_workers,
        enable_chapter_recognition=args.observation_json is None,
    )
    observation = (
        load_observation(args.observation_json) if args.observation_json else None
    )
    result = runtime.decompose(args.image, observation=observation)
    text = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result.status != "uncertain" else 2


if __name__ == "__main__":
    raise SystemExit(main())

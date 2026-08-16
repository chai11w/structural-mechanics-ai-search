"""Inspect saved Paddle layout output without calling A2 or a model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.paddle_region_candidates import (
    export_paddle_candidate_artifacts,
    load_paddle_candidate_set,
)


DEFAULT_OUTPUT_DIR = BASE / ".tmp_tiku_agent_paddle_candidates_8892"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize saved Paddle image boxes and export isolated A3 review crops"
    )
    parser.add_argument("--layout-json", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-score", type=float, default=0.2)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    candidate_set = load_paddle_candidate_set(
        args.layout_json,
        source_image_path=args.image,
        min_score=args.min_score,
    )
    manifest_path = export_paddle_candidate_artifacts(
        args.image,
        candidate_set,
        args.output_dir,
    )
    print(
        json.dumps(
            {
                "status": "review_required" if candidate_set.candidates else "no_candidates",
                "candidate_count": len(candidate_set.candidates),
                "review_candidate_ids": list(candidate_set.review_candidate_ids),
                "reason_codes": list(candidate_set.reason_codes),
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if candidate_set.candidates else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the production A3 text-orientation class against its 36-case gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import site
import sys
from time import perf_counter

from PIL import Image, ImageOps


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8790 import (  # noqa: E402
    DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR,
)
from tiku_agent.a3_text_orientation import (  # noqa: E402
    RapidOcrTextPageOrienter,
    rotate_clockwise,
)


DEFAULT_MANIFEST = (
    BASE / "test_sets" / "orientation" / "a3_text_rotation" / "manifest.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--dependency-dir",
        type=Path,
        default=DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR,
    )
    args = parser.parse_args()

    site.addsitedir(str(args.dependency_dir.resolve()))
    manifest_path = args.manifest.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    orienter = RapidOcrTextPageOrienter(
        worker_count=4,
        onnx_threads_per_engine=1,
    )
    outcomes: list[dict[str, object]] = []
    for source_item in payload["sources"]:
        source = (manifest_path.parent / source_item["path"]).resolve()
        with Image.open(source) as opened:
            upright = ImageOps.exif_transpose(opened).convert("RGB")
        for applied in payload["applied_clockwise_degrees"]:
            sample = rotate_clockwise(upright, int(applied))
            started = perf_counter()
            decision = orienter.choose_correction(sample)
            elapsed = perf_counter() - started
            expected = (-int(applied)) % 360
            outcomes.append(
                {
                    "source": source.name,
                    "applied": applied,
                    "expected": expected,
                    "actual": decision.correction,
                    "seconds": round(elapsed, 4),
                    "reason": decision.reason,
                    "passed": decision.correction == expected,
                }
            )

    passed = sum(bool(item["passed"]) for item in outcomes)
    timings = [float(item["seconds"]) for item in outcomes]
    summary = {
        "passed": passed,
        "total": len(outcomes),
        "average_seconds": round(sum(timings) / len(timings), 4),
        "slowest_seconds": round(max(timings), 4),
        "failures": [item for item in outcomes if not item["passed"]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())

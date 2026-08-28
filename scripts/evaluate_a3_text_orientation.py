"""Run the saved four-direction A3 text-orientation release suite."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from PIL import Image, ImageOps


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.a3_text_orientation import RapidOcrTextPageOrienter, rotate_clockwise


DEFAULT_MANIFEST = (
    BASE / "test_sets" / "orientation" / "a3_text_rotation" / "manifest.json"
)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate A3 page orientation from OCR text regions only."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json-output", type=Path)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    manifest_path = args.manifest.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    orienter = RapidOcrTextPageOrienter()
    rows: list[dict[str, object]] = []

    for item in payload["sources"]:
        source_path = (manifest_path.parent / item["path"]).resolve()
        actual_hash = sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != item["sha256"]:
            raise RuntimeError(f"Test asset hash mismatch: {source_path}")
        with Image.open(source_path) as opened:
            upright = ImageOps.exif_transpose(opened).convert("RGB")
        for applied in payload["applied_clockwise_degrees"]:
            input_image = rotate_clockwise(upright, int(applied))
            decision = orienter.choose_correction(input_image)
            expected = (360 - int(applied)) % 360
            row = {
                "source": source_path.name,
                "applied_clockwise_degrees": applied,
                "expected_correction": expected,
                "selected_correction": decision.correction,
                "correct": decision.correction == expected,
                "score_margin": decision.score_margin,
                "reason": decision.reason,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)

    failures = [row for row in rows if not row["correct"]]
    summary = {
        "suite": payload["suite"],
        "source_count": len(payload["sources"]),
        "case_count": len(rows),
        "correct_count": len(rows) - len(failures),
        "accuracy": round((len(rows) - len(failures)) / len(rows), 4),
        "failures": failures,
    }
    result = {"summary": summary, "rows": rows}
    if args.json_output:
        output_path = args.json_output.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print("SUMMARY=" + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

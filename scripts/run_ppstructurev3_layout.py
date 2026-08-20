"""Run PP-StructureV3 locally and export compact layout candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


SCHEMA_VERSION = "ppstructurev3-layout-v1"
VISUAL_LABELS = {"image", "chart", "formula", "table"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export PP-StructureV3 layout boxes for the isolated A3 crop evaluation."
    )
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--layout-threshold", type=float, default=None)
    return parser


def normalize_layout_result(payload: object, *, source_name: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("PP-StructureV3 result must be an object")
    layout = payload.get("layout_det_res")
    if not isinstance(layout, Mapping) or not isinstance(layout.get("boxes"), list):
        raise ValueError("PP-StructureV3 result has no layout_det_res.boxes")
    all_boxes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(layout["boxes"]):
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "unknown").strip().lower()
        coordinate = _bbox(raw.get("coordinate"))
        score = _score(raw.get("score"))
        compact = {
            "layout_index": index,
            "label": label,
            "score": score,
            "bbox": list(coordinate),
        }
        all_boxes.append(compact)
        if label in VISUAL_LABELS:
            candidates.append(
                {
                    "candidate_id": f"p{len(candidates) + 1:03d}",
                    **compact,
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_name": source_name,
        "layout_model": "PP-DocLayout_plus-L",
        "visual_labels": sorted(VISUAL_LABELS),
        "candidates": candidates,
        "all_boxes": all_boxes,
    }


def main() -> int:
    args = build_argument_parser().parse_args()
    images_dir = args.images_dir.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in images_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not images:
        raise SystemExit("no images found")

    from paddleocr import PPStructureV3

    pipeline = PPStructureV3(
        layout_threshold=args.layout_threshold,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_table_recognition=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_region_detection=False,
    )
    failures: list[dict[str, str]] = []
    completed = 0
    for image_path in images:
        try:
            results = list(
                pipeline.predict(
                    str(image_path),
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    use_seal_recognition=False,
                    use_table_recognition=False,
                    use_formula_recognition=False,
                    use_chart_recognition=False,
                    use_region_detection=False,
                )
            )
            if len(results) != 1:
                raise ValueError(f"expected one page result, got {len(results)}")
            raw_json = results[0].json["res"]
            compact = normalize_layout_result(raw_json, source_name=image_path.name)
            target = output_dir / f"{image_path.stem}.json"
            target.write_text(
                json.dumps(compact, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            completed += 1
        except Exception as exc:  # noqa: BLE001 - preserve the remaining evaluation batch.
            failures.append(
                {
                    "source_name": image_path.name,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "image_count": len(images),
        "completed": completed,
        "failed": len(failures),
        "failures": failures,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


def _bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("layout coordinate must contain four numbers")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise ValueError("layout coordinate must contain four numbers")
    parsed = tuple(float(item) for item in value)
    if min(parsed) < 0 or parsed[2] <= parsed[0] or parsed[3] <= parsed[1]:
        raise ValueError("layout coordinate must be an ordered non-negative box")
    return parsed


def _score(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return round(max(0.0, min(1.0, float(value))), 6)
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())

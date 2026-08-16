"""Run region-local OpenCV crop refinement for a saved A3 region map."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.a3_region_cropper import crop_a3_regions, write_crop_manifest
from tiku_agent.image_region_mapper import parse_a3_region_map


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refine saved A3 coarse regions with local OpenCV foreground union."
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--observation-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--coarse-padding-ratio", type=float, default=0.10)
    parser.add_argument("--min-padding-px", type=int, default=24)
    parser.add_argument("--content-padding-ratio", type=float, default=0.06)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    with open(args.observation_json, encoding="utf-8") as handle:
        payload = json.load(handle)
    from PIL import Image, ImageOps

    with Image.open(args.image) as opened:
        image_size = ImageOps.exif_transpose(opened).size
    observation = parse_a3_region_map(payload, image_size=image_size)
    crops = crop_a3_regions(
        args.image,
        observation,
        args.output_dir,
        coarse_padding_ratio=args.coarse_padding_ratio,
        min_padding_px=args.min_padding_px,
        content_padding_ratio=args.content_padding_ratio,
    )
    manifest = write_crop_manifest(crops, args.output_dir / "crop_manifest.json")
    print(json.dumps({"count": len(crops), "manifest": str(manifest)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

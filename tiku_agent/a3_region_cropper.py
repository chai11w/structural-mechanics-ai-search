"""Region-local OpenCV crop experiment for the isolated 8892 A3 flow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageOps

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - optional experiment dependency.
    cv2 = None
    np = None

from .image_region_mapper import A3CoarseRegion, A3RegionMap


@dataclass(frozen=True)
class A3RegionCrop:
    region_id: str
    coarse_bbox: tuple[int, int, int, int]
    crop_bbox: tuple[int, int, int, int]
    crop_path: Path


def crop_a3_regions(
    image_path: str | Path,
    observation: A3RegionMap,
    output_dir: str | Path,
    *,
    coarse_padding_ratio: float = 0.10,
    min_padding_px: int = 24,
    content_padding_ratio: float = 0.06,
) -> tuple[A3RegionCrop, ...]:
    """Crop each A3 region while preserving disconnected diagram annotations.

    The model bbox is treated as a safety region, not a final tight crop. OpenCV
    unions meaningful foreground components inside that region, so a load arrow
    or dimension line disconnected from the main member is retained.
    """

    if cv2 is None or np is None:
        raise RuntimeError("opencv-python and numpy are required")
    source_path = Path(image_path).resolve(strict=True)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    image_width, image_height = source.size
    image = np.asarray(source)
    results: list[A3RegionCrop] = []
    overlay = source.copy()
    draw = ImageDraw.Draw(overlay)
    line_width = max(2, min(source.size) // 320)

    for region in observation.diagram_regions:
        coarse = percentage_bbox_to_pixels(region, source.size)
        expanded = expand_bbox(
            coarse,
            source.size,
            ratio=coarse_padding_ratio,
            min_padding_px=min_padding_px,
        )
        safe = clamp_to_neighbor_gaps(
            expanded,
            coarse,
            region,
            observation.diagram_regions,
            source.size,
        )
        refined = refine_foreground_bbox(
            image,
            safe,
            content_padding_ratio=content_padding_ratio,
        )
        crop = source.crop(refined)
        crop_path = target_dir / f"{region.region_id}_opencv.jpg"
        crop.save(crop_path, quality=94)
        results.append(
            A3RegionCrop(
                region_id=region.region_id,
                coarse_bbox=coarse,
                crop_bbox=refined,
                crop_path=crop_path,
            )
        )
        draw.rectangle(refined, outline="#e31a1c", width=line_width)
        draw.rectangle(coarse, outline="#1f77b4", width=line_width)
        label = f"{region.region_id} blue=coarse red=opencv"
        text_box = draw.textbbox((refined[0], refined[1]), label)
        label_y = max(0, refined[1] - (text_box[3] - text_box[1]) - 5)
        draw.rectangle(
            (refined[0], label_y, refined[0] + (text_box[2] - text_box[0]) + 8, refined[1]),
            fill="#e31a1c",
        )
        draw.text((refined[0] + 4, label_y + 2), label, fill="white")

    overlay.save(target_dir / "a3_region_crop_overlay.jpg", quality=92)
    return tuple(results)


def percentage_bbox_to_pixels(
    region: A3CoarseRegion, image_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    width, height = image_size
    x1, y1, x2, y2 = region.bbox
    return (
        max(0, min(width - 1, round(x1 * width / 100))),
        max(0, min(height - 1, round(y1 * height / 100))),
        max(1, min(width, round(x2 * width / 100))),
        max(1, min(height, round(y2 * height / 100))),
    )


def expand_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    *,
    ratio: float,
    min_padding_px: int,
) -> tuple[int, int, int, int]:
    width, height = image_size
    x1, y1, x2, y2 = bbox
    pad_x = max(min_padding_px, round((x2 - x1) * max(0.0, ratio)))
    pad_y = max(min_padding_px, round((y2 - y1) * max(0.0, ratio)))
    return (
        max(0, x1 - pad_x),
        max(0, y1 - pad_y),
        min(width, x2 + pad_x),
        min(height, y2 + pad_y),
    )


def clamp_to_neighbor_gaps(
    expanded: tuple[int, int, int, int],
    coarse: tuple[int, int, int, int],
    region: A3CoarseRegion,
    all_regions: Iterable[A3CoarseRegion],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Keep expansion inside gaps between neighboring coarse regions."""

    width, height = image_size
    x1, y1, x2, y2 = coarse
    left, top, right, bottom = expanded
    region_width = max(1, x2 - x1)
    region_height = max(1, y2 - y1)
    left_limit, top_limit = 0, 0
    right_limit, bottom_limit = width, height

    for other in all_regions:
        if other.region_id == region.region_id:
            continue
        other_box = percentage_bbox_to_pixels(other, image_size)
        ox1, oy1, ox2, oy2 = other_box
        vertical_overlap = max(0, min(y2, oy2) - max(y1, oy1))
        horizontal_overlap = max(0, min(x2, ox2) - max(x1, ox1))
        same_row = vertical_overlap >= min(region_height, oy2 - oy1) * 0.20
        same_column = horizontal_overlap >= min(region_width, ox2 - ox1) * 0.20

        if same_row and ox2 <= x1:
            left_limit = max(left_limit, (ox2 + x1) // 2)
        elif same_row and ox1 >= x2:
            right_limit = min(right_limit, (x2 + ox1) // 2)
        elif same_row and ox1 < x2 and ox2 > x1:
            left_limit = max(left_limit, x1)
            right_limit = min(right_limit, x2)

        if same_column and oy2 <= y1:
            top_limit = max(top_limit, (oy2 + y1) // 2)
        elif same_column and oy1 >= y2:
            bottom_limit = min(bottom_limit, (y2 + oy1) // 2)
        elif same_column and oy1 < y2 and oy2 > y1:
            top_limit = max(top_limit, y1)
            bottom_limit = min(bottom_limit, y2)

    return (
        max(left_limit, left),
        max(top_limit, top),
        min(right_limit, right),
        min(bottom_limit, bottom),
    )


def refine_foreground_bbox(
    image: object,
    safe_bbox: tuple[int, int, int, int],
    *,
    content_padding_ratio: float,
) -> tuple[int, int, int, int]:
    """Return the union of significant foreground ink within a safe region."""

    if cv2 is None or np is None:
        raise RuntimeError("opencv-python and numpy are required")
    array = np.asarray(image)
    x1, y1, x2, y2 = safe_bbox
    roi = array[y1:y2, x1:x2]
    if roi.size == 0:
        return safe_bbox
    gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (0, 0), 3)
    normalized = cv2.divide(gray, blur, scale=255)
    block_size = max(15, min(51, (min(gray.shape[:2]) // 10) | 1))
    mask = cv2.adaptiveThreshold(
        normalized,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block_size,
        9,
    )
    # Close small gaps but do not rely on one connected component. Dimensions
    # and load arrows often remain separate from the structural member.
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    component_mask = significant_components(mask)
    points = cv2.findNonZero(component_mask)
    if points is None:
        return safe_bbox
    px, py, pw, ph = cv2.boundingRect(points)
    pad_x = max(8, round(pw * max(0.0, content_padding_ratio)))
    pad_y = max(8, round(ph * max(0.0, content_padding_ratio)))
    return (
        x1 + max(0, px - pad_x),
        y1 + max(0, py - pad_y),
        x1 + min(roi.shape[1], px + pw + pad_x),
        y1 + min(roi.shape[0], py + ph + pad_y),
    )


def significant_components(mask: object, *, min_area: int = 8) -> object:
    """Remove isolated compression noise while retaining all meaningful groups."""

    if cv2 is None or np is None:
        raise RuntimeError("opencv-python and numpy are required")
    binary = np.asarray(mask)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(binary, 8)
    result = np.zeros_like(binary)
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area >= min_area:
            result[labels == index] = 255
    return result


def write_crop_manifest(crops: Iterable[A3RegionCrop], path: str | Path) -> Path:
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "region_id": crop.region_id,
            "coarse_bbox": list(crop.coarse_bbox),
            "crop_bbox": list(crop.crop_bbox),
            "crop_path": str(crop.crop_path),
        }
        for crop in crops
    ]
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target

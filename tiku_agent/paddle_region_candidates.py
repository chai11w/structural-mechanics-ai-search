"""Normalize Paddle layout boxes for the isolated A3 decomposition flow."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageOps


PADDLE_CANDIDATE_SCHEMA_VERSION = "paddle-layout-candidates-v1"
CandidateRole = Literal["leaf", "single_container", "group", "duplicate"]


@dataclass(frozen=True)
class PaddleRegionCandidate:
    candidate_id: str
    label: str
    score: float
    bbox: tuple[float, float, float, float]
    role: CandidateRole
    duplicate_of: str = ""
    contained_candidate_ids: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "score": round(self.score, 6),
            "bbox": [round(value, 3) for value in self.bbox],
            "role": self.role,
            "duplicate_of": self.duplicate_of,
            "contained_candidate_ids": list(self.contained_candidate_ids),
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class PaddleCandidateSet:
    candidates: tuple[PaddleRegionCandidate, ...]
    source_size: tuple[int, int] | None = None
    reason_codes: tuple[str, ...] = ()
    schema_version: str = PADDLE_CANDIDATE_SCHEMA_VERSION

    @property
    def review_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.role != "duplicate"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": "review_required" if self.candidates else "no_candidates",
            "source_size": list(self.source_size) if self.source_size else None,
            "review_candidate_ids": list(self.review_candidate_ids),
            "reason_codes": list(self.reason_codes),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class _RawCandidate:
    candidate_id: str
    label: str
    score: float
    bbox: tuple[float, float, float, float]


def load_paddle_candidate_set(
    layout_json_path: str | Path,
    *,
    source_image_path: str | Path | None = None,
    min_score: float = 0.2,
) -> PaddleCandidateSet:
    payload = json.loads(Path(layout_json_path).read_text(encoding="utf-8"))
    source_size = None
    if source_image_path is not None:
        with Image.open(Path(source_image_path).resolve(strict=True)) as opened:
            source_size = ImageOps.exif_transpose(opened).size
    return parse_paddle_candidate_set(
        payload,
        source_size=source_size,
        min_score=min_score,
    )


def parse_paddle_candidate_set(
    payload: object,
    *,
    source_size: tuple[int, int] | None = None,
    min_score: float = 0.2,
) -> PaddleCandidateSet:
    """Parse Paddle output and mark geometric ambiguity without judging content."""

    if not isinstance(payload, dict) or not isinstance(payload.get("boxes"), list):
        raise ValueError("Paddle layout payload requires a boxes array")
    if source_size is not None and (source_size[0] <= 0 or source_size[1] <= 0):
        raise ValueError("source size must be positive")

    raw_candidates: list[_RawCandidate] = []
    for box in payload["boxes"]:
        if not isinstance(box, dict) or str(box.get("label") or "") != "image":
            continue
        score = _number(box.get("score"), "score")
        if score < max(0.0, float(min_score)):
            continue
        bbox = _bbox(box.get("coordinate"), source_size=source_size)
        raw_candidates.append(
            _RawCandidate(
                candidate_id=f"p{len(raw_candidates) + 1:03d}",
                label="image",
                score=score,
                bbox=bbox,
            )
        )

    duplicate_of = _find_duplicates(raw_candidates)
    unique = [
        candidate
        for candidate in raw_candidates
        if candidate.candidate_id not in duplicate_of
    ]
    contained = _contained_leaf_candidates(unique)

    candidates: list[PaddleRegionCandidate] = []
    for candidate in raw_candidates:
        duplicate_target = duplicate_of.get(candidate.candidate_id, "")
        children = contained.get(candidate.candidate_id, ())
        flags: list[str] = []
        if duplicate_target:
            role: CandidateRole = "duplicate"
            flags.append("near_duplicate")
        elif len(children) >= 2:
            role = "group"
            flags.append("contains_multiple_candidates")
        elif len(children) == 1:
            role = "single_container"
            flags.append("contains_one_candidate")
        else:
            role = "leaf"
        if source_size and _touches_page_edge(candidate.bbox, source_size):
            flags.append("touches_page_edge")
        candidates.append(
            PaddleRegionCandidate(
                candidate_id=candidate.candidate_id,
                label=candidate.label,
                score=candidate.score,
                bbox=candidate.bbox,
                role=role,
                duplicate_of=duplicate_target,
                contained_candidate_ids=children,
                flags=tuple(flags),
            )
        )

    reasons: list[str] = []
    if not candidates:
        reasons.append("no_image_candidates")
    if duplicate_of:
        reasons.append("duplicate_candidates_present")
    if any(candidate.role == "group" for candidate in candidates):
        reasons.append("group_candidates_present")
    if any(candidate.role == "single_container" for candidate in candidates):
        reasons.append("nested_candidates_present")
    if any("touches_page_edge" in candidate.flags for candidate in candidates):
        reasons.append("page_edge_candidates_present")
    # Geometry alone cannot prove that loads, dimensions and supports are complete.
    if candidates:
        reasons.append("content_completeness_not_checked")
    return PaddleCandidateSet(
        candidates=tuple(candidates),
        source_size=source_size,
        reason_codes=tuple(reasons),
    )


def export_paddle_candidate_artifacts(
    image_path: str | Path,
    candidate_set: PaddleCandidateSet,
    output_dir: str | Path,
) -> Path:
    """Write real candidate crops, an overlay and a deterministic manifest."""

    source_path = Path(image_path).resolve(strict=True)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    if candidate_set.source_size and candidate_set.source_size != source.size:
        raise ValueError("candidate source size does not match the image")

    overlay = source.copy()
    draw = ImageDraw.Draw(overlay)
    colors = {
        "leaf": "#16803a",
        "single_container": "#d97706",
        "group": "#c51b1d",
        "duplicate": "#6b7280",
    }
    line_width = max(2, min(source.size) // 300)
    crop_paths: dict[str, str] = {}
    for candidate in candidate_set.candidates:
        pixel_bbox = _pixel_bbox(candidate.bbox, source.size)
        crop_path = target_dir / f"{candidate.candidate_id}_{candidate.role}.jpg"
        source.crop(pixel_bbox).save(crop_path, quality=94)
        crop_paths[candidate.candidate_id] = str(crop_path)
        color = colors[candidate.role]
        draw.rectangle(pixel_bbox, outline=color, width=line_width)
        draw.text(
            (pixel_bbox[0] + 4, pixel_bbox[1] + 4),
            f"{candidate.candidate_id}:{candidate.role}",
            fill=color,
        )

    overlay_path = target_dir / "candidate_overlay.jpg"
    overlay.save(overlay_path, quality=92)
    manifest = candidate_set.to_dict()
    manifest["artifacts"] = {
        "overlay": str(overlay_path),
        "crops": crop_paths,
    }
    manifest_path = target_dir / "candidate_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _find_duplicates(candidates: list[_RawCandidate]) -> dict[str, str]:
    duplicate_of: dict[str, str] = {}
    ranked = sorted(candidates, key=lambda item: (-item.score, item.candidate_id))
    accepted: list[_RawCandidate] = []
    for candidate in ranked:
        match = next(
            (
                existing
                for existing in accepted
                if _near_duplicate(candidate.bbox, existing.bbox)
            ),
            None,
        )
        if match:
            duplicate_of[candidate.candidate_id] = match.candidate_id
        else:
            accepted.append(candidate)
    return duplicate_of


def _contained_leaf_candidates(
    candidates: list[_RawCandidate],
) -> dict[str, tuple[str, ...]]:
    directly_contains: dict[str, set[str]] = {candidate.candidate_id: set() for candidate in candidates}
    for outer in candidates:
        for inner in candidates:
            if outer.candidate_id == inner.candidate_id:
                continue
            ratio = _area(inner.bbox) / _area(outer.bbox)
            if ratio <= 0.85 and _coverage(inner.bbox, outer.bbox) >= 0.92:
                directly_contains[outer.candidate_id].add(inner.candidate_id)

    leaf_ids = {
        candidate.candidate_id
        for candidate in candidates
        if not directly_contains[candidate.candidate_id]
    }
    return {
        candidate_id: tuple(sorted(children & leaf_ids))
        for candidate_id, children in directly_contains.items()
    }


def _near_duplicate(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    if _intersection_over_union(left, right) >= 0.88:
        return True
    smaller, larger = (left, right) if _area(left) <= _area(right) else (right, left)
    return _coverage(smaller, larger) >= 0.96 and _area(smaller) / _area(larger) >= 0.82


def _touches_page_edge(
    bbox: tuple[float, float, float, float],
    source_size: tuple[int, int],
) -> bool:
    width, height = source_size
    margin = max(2.0, min(width, height) * 0.01)
    x1, y1, x2, y2 = bbox
    return x1 <= margin or y1 <= margin or x2 >= width - margin or y2 >= height - margin


def _bbox(
    value: object,
    *,
    source_size: tuple[int, int] | None,
) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("Paddle coordinate must contain four values")
    parsed = tuple(_number(item, "coordinate") for item in value)
    x1, y1, x2, y2 = parsed
    if min(parsed) < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError("Paddle coordinate must be an ordered non-negative box")
    if source_size and (x2 > source_size[0] + 1 or y2 > source_size[1] + 1):
        raise ValueError("Paddle coordinate exceeds the source image")
    return parsed


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Paddle {field} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Paddle {field} must be numeric") from exc


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _coverage(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    area = _area(inner)
    return _intersection(inner, outer) / area if area > 0 else 0.0


def _intersection_over_union(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection = _intersection(left, right)
    union = _area(left) + _area(right) - intersection
    return intersection / union if union > 0 else 0.0


def _pixel_bbox(
    bbox: tuple[float, float, float, float],
    source_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = source_size
    return (
        max(0, min(width - 1, int(bbox[0]))),
        max(0, min(height - 1, int(bbox[1]))),
        max(1, min(width, int(bbox[2] + 0.999))),
        max(1, min(height, int(bbox[3] + 0.999))),
    )

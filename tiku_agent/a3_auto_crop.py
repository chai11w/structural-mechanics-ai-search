"""Production A3-V1 page grounding contract and GLM adapter."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from tiku_agent.glm_vision import DEFAULT_GLM_MODEL, call_glm_json


A3_AUTO_CROP_SCHEMA_VERSION = "a3-page-crops-v1"
A3_AUTO_CROP_PROMPT_PATH = Path(__file__).with_name("prompts") / "a3_auto_crop_v1.txt"
A3_AUTO_TARGET_STATUSES = {"auto_ready", "review_required", "no_target"}
A3_AUTO_PAGE_STATUSES = {"ready", "partially_ready", "manual_required"}
A3_AUTO_REASON_CODES = {
    "ambiguous_binding",
    "incomplete_structure",
    "multiple_structures",
    "no_visible_target",
    "image_unreadable",
    "crop_boundary_uncertain",
    "unsupported_diagram",
}


class A3AutoCropError(RuntimeError):
    """Raised when page grounding cannot safely enter the A3 runtime."""


@dataclass(frozen=True)
class A3AutoCropTarget:
    target_id: str
    unit_id: str
    question_label: str
    bbox: tuple[int, int, int, int] | None
    status: str
    reason_codes: tuple[str, ...]
    binding_evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "unit_id": self.unit_id,
            "question_label": self.question_label,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "binding_evidence": self.binding_evidence,
        }


@dataclass(frozen=True)
class A3AutoCropPage:
    page_status: str
    targets: tuple[A3AutoCropTarget, ...]
    unknowns: tuple[str, ...]
    schema_version: str = A3_AUTO_CROP_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "page_status": self.page_status,
            "targets": [target.to_dict() for target in self.targets],
            "unknowns": list(self.unknowns),
        }


class A3AutoCropper(Protocol):
    def ground(
        self,
        image_path: Path,
        units: Sequence[Mapping[str, Any]],
        page_understanding: Mapping[str, Any],
    ) -> A3AutoCropPage: ...


class GlmA3AutoCropper:
    def __init__(
        self,
        *,
        model: str = DEFAULT_GLM_MODEL,
        prompt_path: str | Path = A3_AUTO_CROP_PROMPT_PATH,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.model = str(model).strip() or DEFAULT_GLM_MODEL
        self.prompt_path = Path(prompt_path)
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def ground(
        self,
        image_path: Path,
        units: Sequence[Mapping[str, Any]],
        page_understanding: Mapping[str, Any],
    ) -> A3AutoCropPage:
        descriptors = build_allowed_units(units, page_understanding)
        if not descriptors:
            raise A3AutoCropError("at least one searchable unit is required")
        try:
            prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise A3AutoCropError("A3 auto-crop prompt is unavailable") from exc
        if not prompt:
            raise A3AutoCropError("A3 auto-crop prompt is empty")
        response = call_glm_json(
            [image_path],
            prompt=prompt,
            model=self.model,
            user_text=(
                "allowed_units:\n"
                + json.dumps(descriptors, ensure_ascii=False, separators=(",", ":"))
                + "\n只输出规定 JSON。"
            ),
            timeout_seconds=self.timeout_seconds,
            max_tokens=max(1200, len(descriptors) * 260),
            call_type="glm_a3_page_auto_crop",
        )
        return parse_a3_auto_crop_page(
            response.payload,
            expected_units=descriptors,
        )


def build_allowed_units(
    units: Sequence[Mapping[str, Any]],
    page_understanding: Mapping[str, Any],
) -> list[dict[str, Any]]:
    groups = {
        str(group.get("group_id") or ""): group
        for group in page_understanding.get("groups", [])
        if isinstance(group, Mapping)
    }
    descriptors: list[dict[str, Any]] = []
    for raw_unit in units:
        unit = dict(raw_unit)
        if unit.get("searchability") != "searchable_candidate":
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        if not unit_id:
            raise A3AutoCropError("searchable unit id is required")
        group = groups.get(str(unit.get("group_id") or ""), {})
        descriptors.append({
            "unit_id": unit_id,
            "display_label": str(unit.get("display_label") or ""),
            "parent_question_label": str(unit.get("parent_question_label") or ""),
            "question_label": str(unit.get("question_label") or ""),
            "parent_title_text": str(group.get("parent_title_text") or ""),
            "shared_stem_text": str(unit.get("shared_stem_text") or ""),
            "title_text": str(unit.get("title_text") or ""),
            "diagram_ids": [str(value) for value in unit.get("diagram_ids", [])],
        })
    return descriptors


def parse_a3_auto_crop_page(
    payload: object,
    *,
    expected_units: Sequence[Mapping[str, Any]],
) -> A3AutoCropPage:
    root = _object(payload, "A3 auto-crop output")
    if set(root) != {"schema_version", "page_status", "targets", "unknowns"}:
        raise A3AutoCropError("invalid A3 auto-crop fields")
    if root.get("schema_version") != A3_AUTO_CROP_SCHEMA_VERSION:
        raise A3AutoCropError("unsupported A3 auto-crop schema")
    page_status = _enum(root.get("page_status"), A3_AUTO_PAGE_STATUSES, "page_status")
    expected = {
        str(unit.get("unit_id") or "").strip(): str(unit.get("display_label") or "")
        for unit in expected_units
    }
    if not expected or "" in expected:
        raise A3AutoCropError("expected unit ids must be present")
    targets: list[A3AutoCropTarget] = []
    seen_targets: set[str] = set()
    seen_units: set[str] = set()
    for index, raw_target in enumerate(_array(root.get("targets"), "targets")):
        target = _object(raw_target, f"targets[{index}]")
        if set(target) != {
            "target_id",
            "unit_id",
            "question_label",
            "bbox",
            "status",
            "reason_codes",
            "binding_evidence",
        }:
            raise A3AutoCropError("invalid A3 auto-crop target fields")
        target_id = _text(target.get("target_id"), "target_id")
        unit_id = _text(target.get("unit_id"), "unit_id")
        if target_id in seen_targets or unit_id in seen_units:
            raise A3AutoCropError("target_id and unit_id values must be unique")
        if unit_id not in expected:
            raise A3AutoCropError("target unit_id is not allowed")
        seen_targets.add(target_id)
        seen_units.add(unit_id)
        status = _enum(target.get("status"), A3_AUTO_TARGET_STATUSES, "target status")
        bbox = _bbox(target.get("bbox"), allow_none=status == "no_target")
        if status == "no_target" and bbox is not None:
            raise A3AutoCropError("no_target bbox must be null")
        if status != "no_target" and bbox is None:
            raise A3AutoCropError("crop target bbox is required")
        question_label = str(target.get("question_label") or "").strip()
        if question_label != expected[unit_id]:
            raise A3AutoCropError("question_label must match the allowed unit")
        reason_codes = tuple(
            _enum(value, A3_AUTO_REASON_CODES, "reason code")
            for value in _array(target.get("reason_codes"), "reason_codes")
        )
        targets.append(A3AutoCropTarget(
            target_id=target_id,
            unit_id=unit_id,
            question_label=question_label,
            bbox=bbox,
            status=status,
            reason_codes=reason_codes,
            binding_evidence=str(target.get("binding_evidence") or "").strip(),
        ))
    if seen_units != set(expected):
        raise A3AutoCropError("every allowed unit must have exactly one target")
    auto_count = sum(target.status == "auto_ready" for target in targets)
    expected_page_status = (
        "ready"
        if auto_count == len(targets)
        else "partially_ready"
        if auto_count
        else "manual_required"
    )
    if page_status != expected_page_status:
        raise A3AutoCropError("page_status disagrees with target statuses")
    return A3AutoCropPage(
        page_status=page_status,
        targets=tuple(targets),
        unknowns=tuple(_text(value, "unknown") for value in _array(root.get("unknowns"), "unknowns")),
    )


def normalized_bbox_to_bounds(bbox: Sequence[int]) -> dict[str, float]:
    if len(bbox) != 4:
        raise A3AutoCropError("bbox must contain four values")
    x1, y1, x2, y2 = (int(value) for value in bbox)
    return {
        "x": x1 / 1000.0,
        "y": y1 / 1000.0,
        "width": (x2 - x1) / 1000.0,
        "height": (y2 - y1) / 1000.0,
    }


def _bbox(value: object, *, allow_none: bool) -> tuple[int, int, int, int] | None:
    if value is None and allow_none:
        return None
    values = _array(value, "bbox")
    if len(values) != 4 or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
        raise A3AutoCropError("bbox must contain four numbers")
    parsed = tuple(round(float(item)) for item in values)
    x1, y1, x2, y2 = parsed
    if min(parsed) < 0 or max(parsed) > 1000 or x2 <= x1 or y2 <= y1:
        raise A3AutoCropError("bbox must be ordered within 0..1000")
    return x1, y1, x2, y2


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise A3AutoCropError(f"{field} must be an object")
    return dict(value)


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise A3AutoCropError(f"{field} must be an array")
    return list(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise A3AutoCropError(f"{field} must be a non-empty string")
    return value.strip()


def _enum(value: object, allowed: set[str], field: str) -> str:
    clean = str(value or "").strip()
    if clean not in allowed:
        raise A3AutoCropError(f"invalid {field}")
    return clean

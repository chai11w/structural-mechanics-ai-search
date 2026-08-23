"""Boundary-aware image-triage policy isolated to port 8897."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .image_contracts import ImageTriageHandoff, ImageTriageObservation, Route
from .image_triage import observation_from_model_text


IMAGE_TRIAGE_8897_SCHEMA_VERSION = "8897-image-triage-boundary-v1"


@dataclass(frozen=True)
class BoundaryAwareImageTriageObservation(ImageTriageObservation):
    image_boundary_clear: bool | None = None
    schema_version: str = IMAGE_TRIAGE_8897_SCHEMA_VERSION


def _summary_field(text: str, label: str) -> str | None:
    match = re.search(
        rf"^[\s>*`*_]*{re.escape(label)}\s*[:：]\s*([^\r\n]+)",
        str(text or ""),
        flags=re.MULTILINE,
    )
    if not match:
        return None
    return match.group(1).strip().strip("`*_ ")


def _parse_boundary_clear(text: str, *, correct_contamination: bool) -> bool | None:
    value = _summary_field(text, "题图边界")
    if not value or "不确定" in value:
        return None
    if any(token in value for token in ("不清楚", "不明确", "残缺", "混入", "相邻")):
        return False
    if any(token in value for token in ("清楚", "明确")):
        if correct_contamination and _has_boundary_contamination_evidence(text):
            return False
        return True
    return None


def _has_boundary_contamination_evidence(text: str) -> bool:
    content = str(text or "")
    markers = (
        "属于下一题",
        "其他图形的顶部",
        "相邻题目的",
        "下一题的",
        "相邻题残片",
        "下一题残片",
    )
    negations = ("没有", "未", "无", "不存在", "不含", "不属于", "并非")
    for marker in markers:
        start = 0
        while True:
            index = content.find(marker, start)
            if index < 0:
                break
            prefix = content[max(0, index - 10):index]
            if not any(token in prefix for token in negations):
                return True
            start = index + len(marker)
    return False


def _has_primary_unit_truncation_evidence(text: str) -> bool:
    primary_markers = ("杆件", "上弦", "下弦", "荷载箭头", "支座", "主体结构", "原结构")
    truncation_markers = ("被截断", "只露出一部分", "延伸到图片外", "延伸到了图片之外", "缺失", "不完整")
    adjacent_markers = ("下一题", "相邻题", "残片", "其他图形")
    negations = ("没有", "未", "并非", "不存在", "不影响")
    for sentence in re.split(r"[。；;\n]", str(text or "")):
        if "根据规则" in sentence or "规则中" in sentence:
            continue
        if any(token in sentence for token in adjacent_markers):
            continue
        if not any(token in sentence for token in primary_markers):
            continue
        if not any(token in sentence for token in truncation_markers):
            continue
        if any(token in sentence for token in negations):
            continue
        return True
    return False


def _observation_from_model_text_8897(
    text: str,
    *,
    correct_contamination: bool,
    correct_primary_truncation: bool,
) -> BoundaryAwareImageTriageObservation:
    base = observation_from_model_text(text)
    boundary_clear = _parse_boundary_clear(
        text,
        correct_contamination=correct_contamination,
    )
    image_recoverable = base.image_recoverable
    if correct_primary_truncation and _has_primary_unit_truncation_evidence(text):
        image_recoverable = False
    return BoundaryAwareImageTriageObservation(
        route_candidate=base.route_candidate,
        evidence=base.evidence,
        unknowns=base.unknowns,
        question_count=base.question_count,
        original_structure_count=base.original_structure_count,
        auxiliary_diagram_count=base.auxiliary_diagram_count,
        has_actual_load_evidence=base.has_actual_load_evidence,
        has_structure_content=base.has_structure_content,
        image_recoverable=image_recoverable,
        image_boundary_clear=boundary_clear,
        has_ambiguity=base.has_ambiguity or boundary_clear is None,
        diagrams=base.diagrams,
        raw_text=base.raw_text,
    )


def observation_from_model_text_8897_v1(text: str) -> BoundaryAwareImageTriageObservation:
    return _observation_from_model_text_8897(
        text,
        correct_contamination=False,
        correct_primary_truncation=False,
    )


def observation_from_model_text_8897_v2(text: str) -> BoundaryAwareImageTriageObservation:
    return _observation_from_model_text_8897(
        text,
        correct_contamination=True,
        correct_primary_truncation=True,
    )


def observation_from_model_text_8897(text: str) -> BoundaryAwareImageTriageObservation:
    """V3 parser: correct adjacent-question contradictions only."""

    return _observation_from_model_text_8897(
        text,
        correct_contamination=True,
        correct_primary_truncation=False,
    )


def _is_multi_unit(observation: ImageTriageObservation) -> bool:
    return (
        (observation.question_count is not None and observation.question_count > 1)
        or (
            observation.original_structure_count is not None
            and observation.original_structure_count > 1
        )
    )


def _finalize_non_no_load_8897(
    observation: ImageTriageObservation,
    boundary_clear: bool | None,
) -> Route:
    if observation.route_candidate == "A2":
        a2_facts = (
            observation.question_count == 1,
            observation.original_structure_count == 1,
            observation.auxiliary_diagram_count == 0,
            observation.has_actual_load_evidence is True,
            observation.image_recoverable is True,
            boundary_clear is True,
            observation.has_ambiguity is False,
        )
        if not all(a2_facts):
            return "A3"

    if observation.route_candidate == "A1" and observation.has_structure_content is not False:
        return "A3"
    return observation.route_candidate


def finalize_route_8897_v1(observation: ImageTriageObservation) -> Route:
    boundary_clear = getattr(observation, "image_boundary_clear", None)
    if observation.has_actual_load_evidence is False:
        if (
            observation.image_recoverable is False
            or observation.has_structure_content is False
            or (boundary_clear is True and observation.has_ambiguity is False)
        ):
            return "A1"
        return "A3"
    return _finalize_non_no_load_8897(observation, boundary_clear)


def finalize_route_8897_v2(observation: ImageTriageObservation) -> Route:
    boundary_clear = getattr(observation, "image_boundary_clear", None)
    if observation.image_recoverable is False and observation.has_ambiguity is False:
        return "A1"
    if observation.has_actual_load_evidence is False:
        if observation.has_structure_content is False:
            return "A1"
        if boundary_clear is True and observation.has_ambiguity is False:
            return "A1"
        return "A3"
    return _finalize_non_no_load_8897(observation, boundary_clear)


def finalize_route_8897(observation: ImageTriageObservation) -> Route:
    """V3: multi-unit and truncated inputs may fall back to A3."""

    boundary_clear = getattr(observation, "image_boundary_clear", None)
    if _is_multi_unit(observation):
        return "A3"
    if observation.has_actual_load_evidence is False:
        if observation.has_structure_content is False:
            return "A1"
        if boundary_clear is True and observation.has_ambiguity is False:
            return "A1"
        return "A3"
    return _finalize_non_no_load_8897(observation, boundary_clear)


def _build_handoff_8897(
    source_image_path: str,
    observation: ImageTriageObservation,
    *,
    finalizer,
) -> ImageTriageHandoff:
    route = finalizer(observation)
    next_action = {
        "A1": "stop",
        "A2": "existing_search",
        "A3": "a3_processing",
    }[route]
    return ImageTriageHandoff(
        route=route,
        source_image_path=str(source_image_path),
        observation=observation,
        next_action=next_action,
        reason=observation.evidence,
    )


def build_handoff_8897_v1(
    source_image_path: str,
    observation: ImageTriageObservation,
) -> ImageTriageHandoff:
    return _build_handoff_8897(
        source_image_path,
        observation,
        finalizer=finalize_route_8897_v1,
    )


def build_handoff_8897_v2(
    source_image_path: str,
    observation: ImageTriageObservation,
) -> ImageTriageHandoff:
    return _build_handoff_8897(
        source_image_path,
        observation,
        finalizer=finalize_route_8897_v2,
    )


def build_handoff_8897(
    source_image_path: str,
    observation: ImageTriageObservation,
) -> ImageTriageHandoff:
    return _build_handoff_8897(
        source_image_path,
        observation,
        finalizer=finalize_route_8897,
    )

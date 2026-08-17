"""Parse and validate the A3 full-page understanding contract.

This module is intentionally separate from the legacy 8892 CV-block
decomposer. The full-page MVP produces semantic units before any crop exists;
the legacy decomposer produces block-backed crop candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


A3_PAGE_UNDERSTANDING_SCHEMA_VERSION = "a3-page-understanding-v2"
A3_PAGE_DISPOSITIONS = {"has_searchable_candidates", "a1_only", "uncertain"}
A3_UNIT_SEARCHABILITY = {"searchable_candidate", "a1_out_of_scope", "uncertain"}
A3_UNIT_STATUSES = {"clear", "partial", "uncertain"}
A3_DIAGRAM_ROLES = {
    "original_structure",
    "auxiliary_unit_load",
    "internal_force_diagram",
    "deformation_diagram",
    "dimension_or_annotation",
    "irrelevant",
    "unknown",
}
A3_REASON_CODES = {
    "missing_original_structure",
    "incomplete_diagram",
    "ambiguous_binding",
    "auxiliary_only",
    "no_actual_external_load",
    "irrelevant_content",
    "image_unreadable",
    "missing_context_text",
}
A3_PAGE_REASON_CODES = {
    "multi_question_page",
    "mixed_content",
    "truncated_content",
    "uncertain_binding",
    "a1_only_clear",
    "no_searchable_structure",
}

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LABEL_RANGE_RE = re.compile(r"(?:~|～|至|—|–)")


def _reject_extra_keys(value: Mapping[str, Any], allowed: set[str], field_name: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise A3PageParseError(
            f"{field_name} has unsupported fields: {', '.join(sorted(extra))}",
            code="extra_field",
        )


class A3PageParseError(ValueError):
    """Raised when model output cannot safely enter the A3 UI pipeline."""

    def __init__(self, message: str, *, code: str = "invalid_a3_page_output") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class A3PageDiagram:
    diagram_id: str
    role: str
    group_id: str
    unit_ids: tuple[str, ...]
    status: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagram_id": self.diagram_id,
            "role": self.role,
            "group_id": self.group_id,
            "unit_ids": list(self.unit_ids),
            "status": self.status,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class A3PageUnit:
    unit_id: str
    parent_question_label: str
    question_label: str
    title_text: str
    shared_stem_text: str
    visible_text: str
    searchability: str
    reason_codes: tuple[str, ...]
    diagram_ids: tuple[str, ...]
    status: str
    evidence: tuple[str, ...]
    notes: str
    group_id: str

    def to_dict(self, *, display_label: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "unit_id": self.unit_id,
            "parent_question_label": self.parent_question_label,
            "question_label": self.question_label,
            "title_text": self.title_text,
            "shared_stem_text": self.shared_stem_text,
            "visible_text": self.visible_text,
            "searchability": self.searchability,
            "reason_codes": list(self.reason_codes),
            "diagram_ids": list(self.diagram_ids),
            "status": self.status,
            "evidence": list(self.evidence),
            "notes": self.notes,
            "group_id": self.group_id,
        }
        if display_label is not None:
            data["display_label"] = display_label
        return data


@dataclass(frozen=True)
class A3PageGroup:
    group_id: str
    parent_question_label: str
    parent_title_text: str
    shared_stem_text: str
    units: tuple[A3PageUnit, ...]

    def to_dict(self, *, include_derived: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "group_id": self.group_id,
            "parent_question_label": self.parent_question_label,
            "parent_title_text": self.parent_title_text,
            "shared_stem_text": self.shared_stem_text,
            "units": [
                unit.to_dict(
                    display_label=build_display_label(unit, index + 1)
                    if include_derived
                    else None
                )
                for index, unit in enumerate(self.units)
            ],
        }
        if include_derived:
            for unit_data, unit in zip(data["units"], self.units, strict=True):
                unit_data["a2_context_text"] = build_a2_context_text(unit)
        return data


@dataclass(frozen=True)
class A3PageUnderstanding:
    page_disposition: str
    reason_evidence: tuple[tuple[str, str], ...]
    groups: tuple[A3PageGroup, ...]
    diagrams: tuple[A3PageDiagram, ...]
    unassigned_content: tuple[tuple[str, str], ...]
    unknowns: tuple[str, ...]
    schema_version: str = A3_PAGE_UNDERSTANDING_SCHEMA_VERSION
    warnings: tuple[str, ...] = ()

    @property
    def units(self) -> tuple[A3PageUnit, ...]:
        return tuple(unit for group in self.groups for unit in group.units)

    @property
    def searchable_units(self) -> tuple[A3PageUnit, ...]:
        return tuple(unit for unit in self.units if unit.searchability == "searchable_candidate")

    def display_label(self, unit: A3PageUnit) -> str:
        index = next(index for index, item in enumerate(self.units, start=1) if item.unit_id == unit.unit_id)
        return build_display_label(unit, index)

    def to_dict(self, *, include_derived: bool = False) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "page_disposition": self.page_disposition,
            "a3_reason_evidence": [
                {"code": code, "evidence": evidence}
                for code, evidence in self.reason_evidence
            ],
            "groups": [
                group.to_dict(include_derived=include_derived) for group in self.groups
            ],
            "diagrams": [diagram.to_dict() for diagram in self.diagrams],
            "unassigned_content": [
                {"text": text, "reason": reason}
                for text, reason in self.unassigned_content
            ],
            "unknowns": list(self.unknowns),
        }


def _text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise A3PageParseError(f"{field_name} must be a string", code="invalid_field_type")
    return value.strip()


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise A3PageParseError(f"{field_name} must be an array", code="invalid_field_type")
    result: list[str] = []
    for item in value:
        result.append(_text(item, field_name))
    return tuple(result)


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise A3PageParseError(f"{field_name} must be an object", code="invalid_field_type")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise A3PageParseError(f"{field_name} must be an array", code="invalid_field_type")
    return value


def _decode_json(raw: str) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    text = raw.strip()
    warnings: list[str] = []
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) < 3 or lines[0].strip() not in {"```", "```json"}:
            raise A3PageParseError("unsupported Markdown wrapper", code="extra_output")
        text = "\n".join(lines[1:-1]).strip()
        warnings.append("markdown_code_fence_stripped")
    if not text.startswith("{") or not text.endswith("}"):
        raise A3PageParseError("A3 output must contain one JSON object", code="extra_output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise A3PageParseError("A3 output is not valid JSON", code="invalid_json") from exc
    return _object(payload, "A3 output"), tuple(warnings)


def _normalize_label(value: str) -> str:
    return re.sub(r"^题\s*", "", value).strip()


def build_display_label(unit: A3PageUnit, ordinal: int) -> str:
    """Build a user-facing label without allowing the model to invent one."""

    parent = _normalize_label(unit.parent_question_label)
    child = _normalize_label(unit.question_label)
    if child and parent:
        if _LABEL_RANGE_RE.search(parent):
            return unit.question_label
        if child == parent or child.startswith(parent + "-"):
            return unit.question_label
        if child.startswith(parent):
            return unit.question_label
        return f"{parent}-{child}"
    if unit.question_label:
        return unit.question_label
    if unit.parent_question_label:
        return unit.parent_question_label
    return f"未标号题{ordinal}"


def build_a2_context_text(unit: A3PageUnit) -> str:
    """Build the text carried with a manual crop into the single-unit A2 call."""

    parts: list[str] = []
    for value in (unit.shared_stem_text, unit.title_text, unit.visible_text):
        value = value.strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


def parse_a3_page_understanding(
    payload: str | Mapping[str, Any],
) -> A3PageUnderstanding:
    """Decode and validate the full-page A3 model contract."""

    if isinstance(payload, str):
        data, warnings = _decode_json(payload)
    else:
        data = _object(payload, "A3 output")
        warnings = ()
    _reject_extra_keys(
        data,
        {
            "schema_version",
            "page_disposition",
            "a3_reason_evidence",
            "groups",
            "diagrams",
            "unassigned_content",
            "unknowns",
        },
        "A3 output",
    )

    version = _text(data.get("schema_version"), "schema_version")
    if version != A3_PAGE_UNDERSTANDING_SCHEMA_VERSION:
        raise A3PageParseError(
            f"unsupported A3 page schema: {version or 'missing'}",
            code="unsupported_schema",
        )
    disposition = _text(data.get("page_disposition"), "page_disposition")
    if disposition not in A3_PAGE_DISPOSITIONS:
        raise A3PageParseError("unsupported page_disposition", code="invalid_enum")

    reason_evidence: list[tuple[str, str]] = []
    for index, raw_reason in enumerate(_array(data.get("a3_reason_evidence"), "a3_reason_evidence")):
        reason = _object(raw_reason, f"a3_reason_evidence[{index}]")
        _reject_extra_keys(reason, {"code", "evidence"}, f"a3_reason_evidence[{index}]")
        code = _text(reason.get("code"), "a3_reason_evidence.code")
        if code not in A3_PAGE_REASON_CODES:
            raise A3PageParseError(f"unsupported page reason code: {code}", code="invalid_enum")
        reason_evidence.append((code, _text(reason.get("evidence"), "a3_reason_evidence.evidence")))

    raw_groups = _array(data.get("groups"), "groups")
    if not raw_groups:
        raise A3PageParseError("groups cannot be empty", code="empty_groups")
    groups: list[A3PageGroup] = []
    group_ids: set[str] = set()
    unit_ids: set[str] = set()
    units_by_id: dict[str, A3PageUnit] = {}
    for group_index, raw_group in enumerate(raw_groups):
        group = _object(raw_group, f"groups[{group_index}]")
        _reject_extra_keys(
            group,
            {"group_id", "parent_question_label", "parent_title_text", "shared_stem_text", "units"},
            f"groups[{group_index}]",
        )
        group_id = _text(group.get("group_id"), "group_id")
        if not group_id or group_id in group_ids:
            raise A3PageParseError("group ids must be non-empty and unique", code="duplicate_id")
        group_ids.add(group_id)
        raw_units = _array(group.get("units"), f"groups[{group_index}].units")
        if not raw_units:
            raise A3PageParseError("empty groups are not allowed", code="empty_group")
        parent_label = _text(group.get("parent_question_label"), "parent_question_label")
        parent_title = _text(group.get("parent_title_text"), "parent_title_text")
        shared_stem = _text(group.get("shared_stem_text"), "shared_stem_text")
        units: list[A3PageUnit] = []
        for unit_index, raw_unit in enumerate(raw_units):
            unit_data = _object(raw_unit, f"groups[{group_index}].units[{unit_index}]")
            _reject_extra_keys(
                unit_data,
                {
                    "unit_id",
                    "parent_question_label",
                    "question_label",
                    "title_text",
                    "shared_stem_text",
                    "visible_text",
                    "searchability",
                    "reason_codes",
                    "diagram_ids",
                    "status",
                    "evidence",
                    "notes",
                },
                f"groups[{group_index}].units[{unit_index}]",
            )
            unit_id = _text(unit_data.get("unit_id"), "unit_id")
            if not unit_id or not _UNIT_ID_RE.fullmatch(unit_id) or unit_id in unit_ids:
                raise A3PageParseError("unit ids must be valid and unique", code="duplicate_id")
            unit_ids.add(unit_id)
            searchability = _text(unit_data.get("searchability"), "searchability")
            status = _text(unit_data.get("status"), "status")
            if searchability not in A3_UNIT_SEARCHABILITY or status not in A3_UNIT_STATUSES:
                raise A3PageParseError("unsupported unit state", code="invalid_enum")
            reason_codes = _string_list(unit_data.get("reason_codes"), "reason_codes")
            unknown_codes = set(reason_codes) - A3_REASON_CODES
            if unknown_codes:
                raise A3PageParseError("unsupported unit reason code", code="invalid_enum")
            diagram_ids = _string_list(unit_data.get("diagram_ids"), "diagram_ids")
            unit = A3PageUnit(
                unit_id=unit_id,
                parent_question_label=_text(unit_data.get("parent_question_label"), "parent_question_label") or parent_label,
                question_label=_text(unit_data.get("question_label"), "question_label"),
                title_text=_text(unit_data.get("title_text"), "title_text"),
                shared_stem_text=_text(unit_data.get("shared_stem_text"), "shared_stem_text") or shared_stem,
                visible_text=_text(unit_data.get("visible_text"), "visible_text"),
                searchability=searchability,
                reason_codes=reason_codes,
                diagram_ids=diagram_ids,
                status=status,
                evidence=_string_list(unit_data.get("evidence"), "evidence"),
                notes=_text(unit_data.get("notes"), "notes"),
                group_id=group_id,
            )
            raw_unit_stem = _text(unit_data.get("shared_stem_text"), "shared_stem_text")
            if raw_unit_stem and shared_stem and raw_unit_stem != shared_stem:
                raise A3PageParseError(
                    f"unit {unit_id} shared stem differs from its group",
                    code="inconsistent_shared_stem",
                )
            if searchability == "searchable_candidate" and status != "clear":
                raise A3PageParseError(
                    "searchable_candidate must have clear status",
                    code="invalid_state_combination",
                )
            if searchability == "uncertain" and status == "clear":
                raise A3PageParseError(
                    "uncertain unit cannot have clear status",
                    code="invalid_state_combination",
                )
            if "missing_context_text" in reason_codes and searchability != "searchable_candidate":
                raise A3PageParseError(
                    "missing_context_text cannot alone block a complete candidate",
                    code="invalid_state_combination",
                )
            units.append(unit)
            units_by_id[unit_id] = unit
        groups.append(
            A3PageGroup(
                group_id=group_id,
                parent_question_label=parent_label,
                parent_title_text=parent_title,
                shared_stem_text=shared_stem,
                units=tuple(units),
            )
        )

    diagrams: list[A3PageDiagram] = []
    diagram_ids: set[str] = set()
    for index, raw_diagram in enumerate(_array(data.get("diagrams"), "diagrams")):
        diagram = _object(raw_diagram, f"diagrams[{index}]")
        _reject_extra_keys(
            diagram,
            {"diagram_id", "role", "group_id", "unit_ids", "status", "evidence"},
            f"diagrams[{index}]",
        )
        diagram_id = _text(diagram.get("diagram_id"), "diagram_id")
        if not diagram_id or diagram_id in diagram_ids:
            raise A3PageParseError("diagram ids must be non-empty and unique", code="duplicate_id")
        diagram_ids.add(diagram_id)
        role = _text(diagram.get("role"), "diagram.role")
        status = _text(diagram.get("status"), "diagram.status")
        if role not in A3_DIAGRAM_ROLES or status not in A3_UNIT_STATUSES:
            raise A3PageParseError("unsupported diagram state", code="invalid_enum")
        references = _string_list(diagram.get("unit_ids"), "diagram.unit_ids")
        if len(set(references)) != len(references) or any(reference not in unit_ids for reference in references):
            raise A3PageParseError("diagram unit_ids must reference existing units", code="invalid_reference")
        group_id = _text(diagram.get("group_id"), "diagram.group_id")
        if group_id and group_id not in group_ids:
            raise A3PageParseError("diagram group_id must reference an existing group", code="invalid_reference")
        if role == "original_structure" and not references:
            raise A3PageParseError("original_structure requires unit_ids", code="invalid_reference")
        diagrams.append(
            A3PageDiagram(
                diagram_id=diagram_id,
                role=role,
                group_id=group_id,
                unit_ids=references,
                status=status,
                evidence=_text(diagram.get("evidence"), "diagram.evidence"),
            )
        )

    known_diagram_ids = set(diagram_ids)
    diagrams_by_id = {diagram.diagram_id: diagram for diagram in diagrams}
    for diagram in diagrams:
        for unit_id in diagram.unit_ids:
            if diagram.diagram_id not in units_by_id[unit_id].diagram_ids:
                raise A3PageParseError(
                    f"unit {unit_id} does not reference diagram {diagram.diagram_id}",
                    code="inconsistent_reference",
                )
    for unit in units_by_id.values():
        if any(diagram_id not in known_diagram_ids for diagram_id in unit.diagram_ids):
            raise A3PageParseError(
                f"unit {unit.unit_id} references an unknown diagram",
                code="invalid_reference",
            )
        original_diagrams = [
            diagram
            for diagram in diagrams
            if diagram.role == "original_structure" and unit.unit_id in diagram.unit_ids
        ]
        for diagram_id in unit.diagram_ids:
            diagram = diagrams_by_id[diagram_id]
            if unit.unit_id not in diagram.unit_ids:
                raise A3PageParseError(
                    f"diagram {diagram_id} does not reference unit {unit.unit_id}",
                    code="inconsistent_reference",
                )
            if diagram.group_id and diagram.group_id != unit.group_id:
                raise A3PageParseError(
                    f"diagram {diagram_id} group does not match unit {unit.unit_id}",
                    code="inconsistent_reference",
                )
        if unit.searchability == "searchable_candidate" and any(
            diagram.status != "clear" for diagram in original_diagrams
        ):
            raise A3PageParseError(
                f"candidate unit {unit.unit_id} has an unclear original_structure diagram",
                code="invalid_state_combination",
            )
        if unit.searchability == "searchable_candidate" and not original_diagrams:
            raise A3PageParseError(
                f"candidate unit {unit.unit_id} has no original_structure diagram",
                code="invalid_state_combination",
            )
        if unit.searchability == "a1_out_of_scope" and original_diagrams:
            raise A3PageParseError(
                f"A1 unit {unit.unit_id} cannot bind an original_structure diagram",
                code="invalid_state_combination",
            )

    unknowns = _string_list(data.get("unknowns"), "unknowns")
    raw_unassigned = _array(data.get("unassigned_content"), "unassigned_content")
    unassigned: list[tuple[str, str]] = []
    for index, raw_item in enumerate(raw_unassigned):
        item = _object(raw_item, f"unassigned_content[{index}]")
        unassigned.append((_text(item.get("text"), "unassigned_content.text"), _text(item.get("reason"), "unassigned_content.reason")))

    searchable_count = sum(unit.searchability == "searchable_candidate" for unit in units_by_id.values())
    if disposition == "a1_only" and searchable_count:
        raise A3PageParseError("a1_only cannot contain searchable candidates", code="invalid_state_combination")
    if disposition == "has_searchable_candidates" and not searchable_count:
        raise A3PageParseError(
            "has_searchable_candidates requires at least one candidate",
            code="invalid_state_combination",
        )
    return A3PageUnderstanding(
        page_disposition=disposition,
        reason_evidence=tuple(reason_evidence),
        groups=tuple(groups),
        diagrams=tuple(diagrams),
        unassigned_content=tuple(unassigned),
        unknowns=unknowns,
        schema_version=version,
        warnings=warnings,
    )

"""Typed contracts for the isolated 8890 image triage handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


IMAGE_TRIAGE_SCHEMA_VERSION = "8890-image-triage-v1"
A3_DECOMPOSITION_SCHEMA_VERSION = "a3-decomposition-v2"
Route = Literal["A1", "A2", "A3"]
A3Status = Literal["no_unit", "single_ready", "multiple_wait_choice", "uncertain"]
ChapterScope = Literal["page", "question_group", "search_unit", "unknown"]
DiagramRole = Literal[
    "original_structure",
    "auxiliary_unit_load",
    "internal_force_diagram",
    "deformation_diagram",
    "dimension_or_annotation",
    "irrelevant",
    "unknown",
]

A3_CHAPTERS = (
    "2静定结构",
    "3静定结构位移",
    "4力法",
    "5位移法",
    "6力矩分配",
    "7矩阵位移",
    "8影响线",
    "unknown",
)
A3_STATUSES = {"no_unit", "single_ready", "multiple_wait_choice", "uncertain"}
A3_CHAPTER_SCOPES = {"page", "question_group", "search_unit", "unknown"}
A3_DIAGRAM_ROLES = {
    "original_structure",
    "auxiliary_unit_load",
    "internal_force_diagram",
    "deformation_diagram",
    "dimension_or_annotation",
    "irrelevant",
    "unknown",
}


@dataclass(frozen=True)
class DiagramObservation:
    """A model-observed diagram role; unknown is a valid first-pass value."""

    label: str = ""
    role: DiagramRole = "unknown"
    notes: str = ""


@dataclass(frozen=True)
class ImageTriageObservation:
    """Free-form observations plus the minimum facts needed for route safety."""

    route_candidate: Route
    evidence: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    question_count: int | None = None
    original_structure_count: int | None = None
    auxiliary_diagram_count: int | None = None
    has_actual_load_evidence: bool | None = None
    has_structure_content: bool | None = None
    image_recoverable: bool | None = None
    has_ambiguity: bool = False
    diagrams: tuple[DiagramObservation, ...] = ()
    raw_text: str = ""
    schema_version: str = IMAGE_TRIAGE_SCHEMA_VERSION


@dataclass(frozen=True)
class ImageTriageHandoff:
    """What each downstream branch receives after the first-pass route."""

    route: Route
    source_image_path: str
    observation: ImageTriageObservation
    next_action: Literal["stop", "existing_search", "a3_processing"]
    reason: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "source_image_path": self.source_image_path,
            "next_action": self.next_action,
            "reason": list(self.reason),
            "evidence": list(self.observation.evidence),
            "unknowns": list(self.observation.unknowns),
            "raw_text": self.observation.raw_text,
            "schema_version": self.observation.schema_version,
        }


@dataclass(frozen=True)
class ChapterHint:
    """A scoped, evidence-backed hint; it never overrides A2 by itself."""

    value: str = "unknown"
    scope: ChapterScope = "unknown"
    source_text: str = ""
    evidence: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.value not in A3_CHAPTERS:
            raise ValueError(f"unsupported chapter hint: {self.value}")
        if self.scope not in A3_CHAPTER_SCOPES:
            raise ValueError(f"unsupported chapter scope: {self.scope}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("chapter confidence must be between 0 and 1")
        if self.value == "unknown" and self.confidence > 0.5:
            raise ValueError("unknown chapter confidence cannot exceed 0.5")
        if self.value != "unknown" and not self.source_text.strip():
            raise ValueError("known chapter hint requires visible source text")

    @property
    def available(self) -> bool:
        return self.value != "unknown" and self.confidence >= 0.8

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "scope": self.scope,
            "source_text": self.source_text,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ProblemGroup:
    """One question stem and the selectable structures that inherit its context."""

    group_id: str
    parent_question_label: str
    member_labels: tuple[str, ...]
    shared_stem_text: str = ""
    shared_chapter_hint: ChapterHint = field(default_factory=ChapterHint)

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("group_id is required")
        if not self.parent_question_label.strip():
            raise ValueError("parent_question_label is required")
        if not self.member_labels:
            raise ValueError("problem group requires at least one member label")
        if any(not label.strip() for label in self.member_labels):
            raise ValueError("problem group member labels cannot be empty")
        if len(set(self.member_labels)) != len(self.member_labels):
            raise ValueError("problem group member labels must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "parent_question_label": self.parent_question_label,
            "member_labels": list(self.member_labels),
            "shared_stem_text": self.shared_stem_text,
            "shared_chapter_hint": self.shared_chapter_hint.to_dict(),
        }


@dataclass(frozen=True)
class A3DiagramObservation:
    """A semantic role assigned to one deterministic OpenCV candidate block."""

    block_id: int
    role: DiagramRole
    group_id: str = ""
    question_label: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.block_id < 1:
            raise ValueError("block_id must be positive")
        if self.role not in A3_DIAGRAM_ROLES:
            raise ValueError(f"unsupported diagram role: {self.role}")
        if self.role == "original_structure":
            if not self.group_id.strip() or not self.question_label.strip():
                raise ValueError(
                    "original_structure requires group_id and question_label"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "role": self.role,
            "group_id": self.group_id,
            "question_label": self.question_label,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class A3PageObservation:
    """Strict model output for A3 grouping and diagram-role classification."""

    groups: tuple[ProblemGroup, ...]
    diagrams: tuple[A3DiagramObservation, ...]
    unknowns: tuple[str, ...] = ()
    schema_version: str = A3_DECOMPOSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != A3_DECOMPOSITION_SCHEMA_VERSION:
            raise ValueError(f"unsupported A3 schema: {self.schema_version}")
        group_ids = [group.group_id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("problem group ids must be unique")
        block_ids = [diagram.block_id for diagram in self.diagrams]
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("each candidate block must appear at most once")
        groups = {group.group_id: group for group in self.groups}
        for diagram in self.diagrams:
            if diagram.role != "original_structure":
                continue
            group = groups.get(diagram.group_id)
            if group is None:
                raise ValueError("original_structure references an unknown group")
            if diagram.question_label not in group.member_labels:
                raise ValueError("original_structure label is not in its problem group")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "groups": [group.to_dict() for group in self.groups],
            "diagrams": [diagram.to_dict() for diagram in self.diagrams],
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class SearchUnit:
    """One crop that may be handed to A2 after selection."""

    unit_id: str
    question_label: str
    parent_question_label: str
    group_id: str
    stem_text: str
    primary_diagram_path: str
    primary_diagram_bbox: tuple[int, int, int, int]
    source_block_id: int
    chapter_hint: ChapterHint = field(default_factory=ChapterHint)
    quality_flags: tuple[str, ...] = ()
    requires_user_confirmation: bool = False

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.unit_id,
                self.question_label,
                self.parent_question_label,
                self.group_id,
                self.primary_diagram_path,
            )
        ):
            raise ValueError("search unit identity and crop path are required")
        if self.source_block_id < 1:
            raise ValueError("source_block_id must be positive")
        x1, y1, x2, y2 = self.primary_diagram_bbox
        if min(x1, y1) < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("search unit bbox is invalid")

    def to_dict(self, *, include_path: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "unit_id": self.unit_id,
            "question_label": self.question_label,
            "parent_question_label": self.parent_question_label,
            "group_id": self.group_id,
            "stem_text": self.stem_text,
            "primary_diagram_bbox": list(self.primary_diagram_bbox),
            "source_block_id": self.source_block_id,
            "chapter_hint": self.chapter_hint.to_dict(),
            "quality_flags": list(self.quality_flags),
            "requires_user_confirmation": self.requires_user_confirmation,
        }
        if include_path:
            data["primary_diagram_path"] = self.primary_diagram_path
        return data


@dataclass(frozen=True)
class A3DecompositionResult:
    """Mutually exclusive output of the fixed A3 decomposition pipeline."""

    status: A3Status
    search_units: tuple[SearchUnit, ...] = ()
    selected_unit_id: str = ""
    reason_codes: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    schema_version: str = A3_DECOMPOSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != A3_DECOMPOSITION_SCHEMA_VERSION:
            raise ValueError(f"unsupported A3 schema: {self.schema_version}")
        if self.status not in A3_STATUSES:
            raise ValueError(f"unsupported A3 status: {self.status}")
        unit_ids = [unit.unit_id for unit in self.search_units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("search unit ids must be unique")
        if self.status == "no_unit" and self.search_units:
            raise ValueError("no_unit cannot contain search units")
        if self.status == "single_ready" and len(self.search_units) != 1:
            raise ValueError("single_ready requires exactly one search unit")
        if self.status == "multiple_wait_choice" and len(self.search_units) < 2:
            raise ValueError("multiple_wait_choice requires at least two units")
        if self.status == "multiple_wait_choice" and self.selected_unit_id:
            raise ValueError("waiting for choice cannot already have a selected unit")
        if self.selected_unit_id and self.selected_unit_id not in unit_ids:
            raise ValueError("selected_unit_id must reference a search unit")

    def to_dict(self, *, include_paths: bool = True) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "search_units": [
                unit.to_dict(include_path=include_paths) for unit in self.search_units
            ],
            "selected_unit_id": self.selected_unit_id,
            "reason_codes": list(self.reason_codes),
            "unknowns": list(self.unknowns),
        }

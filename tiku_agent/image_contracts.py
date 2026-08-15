"""Typed contracts for the isolated 8890 image triage handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


IMAGE_TRIAGE_SCHEMA_VERSION = "8890-image-triage-v1"
Route = Literal["A1", "A2", "A3"]
DiagramRole = Literal[
    "original_structure",
    "auxiliary_unit_load",
    "internal_force_diagram",
    "deformation_diagram",
    "dimension_or_annotation",
    "irrelevant",
    "unknown",
]


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

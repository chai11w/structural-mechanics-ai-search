"""Executable V1 contract for trace-linked intermediate checkpoints.

Phase 4.1 freezes vocabulary and validation only. It deliberately performs no
I/O, does not mutate Agent state, and does not make a checkpoint authoritative
for user actions. Runtime capture, SQLite persistence, artifact files, query,
and retention jobs belong to later Phase 4 batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from tiku_shared.trace_context import is_valid_trace_id


CHECKPOINT_CONTRACT = "intermediate_checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = 1

SCOPE_WORKFLOW = "workflow"
SCOPE_CHILD_TASK = "child_task"
CHECKPOINT_SCOPES = frozenset({SCOPE_WORKFLOW, SCOPE_CHILD_TASK})

OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_NEEDS_INPUT = "needs_input"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
CHECKPOINT_OUTCOMES = frozenset(
    {
        OUTCOME_SUCCESS,
        OUTCOME_PARTIAL,
        OUTCOME_NO_MATCH,
        OUTCOME_NEEDS_INPUT,
        OUTCOME_SKIPPED,
        OUTCOME_FAILED,
    }
)

STAGE_IMAGE_ACCEPTED = "image_accepted"
STAGE_IMAGE_ROUTED = "image_routed"
STAGE_PAGE_UNDERSTOOD = "page_understood"
STAGE_CROP_PREPARED = "crop_prepared"
STAGE_CROP_VALIDATED = "crop_validated"
STAGE_QUESTION_ANALYZED = "question_analyzed"
STAGE_COARSE_SEARCH_COMPLETED = "coarse_search_completed"
STAGE_RERANK_COMPLETED = "rerank_completed"
STAGE_ANSWER_PREPARED = "answer_prepared"
CHECKPOINT_STAGES = frozenset(
    {
        STAGE_IMAGE_ACCEPTED,
        STAGE_IMAGE_ROUTED,
        STAGE_PAGE_UNDERSTOOD,
        STAGE_CROP_PREPARED,
        STAGE_CROP_VALIDATED,
        STAGE_QUESTION_ANALYZED,
        STAGE_COARSE_SEARCH_COMPLETED,
        STAGE_RERANK_COMPLETED,
        STAGE_ANSWER_PREPARED,
    }
)

SECTION_IMAGE_METADATA = "image_metadata"
SECTION_ROUTE_DECISION = "route_decision"
SECTION_PAGE_SUMMARY = "page_summary"
SECTION_UNIT_RESULTS = "unit_results"
SECTION_CROP_GEOMETRY = "crop_geometry"
SECTION_CROP_GROUNDING = "crop_grounding"
SECTION_CROP_VALIDATION = "crop_validation"
SECTION_QUESTION_CONTEXT = "question_context"
SECTION_CHAPTER_DECISION = "chapter_decision"
SECTION_LOAD_OBSERVATIONS = "load_observations"
SECTION_STRUCTURE_DECISION = "structure_decision"
SECTION_DIMENSION_OBSERVATIONS = "dimension_observations"
SECTION_CANDIDATE_COUNTS = "candidate_counts"
SECTION_FILTER_DECISIONS = "filter_decisions"
SECTION_CANDIDATE_SCORES = "candidate_scores"
SECTION_RERANK_POLICY = "rerank_policy"
SECTION_SELECTION = "selection"
SECTION_DELIVERY = "delivery"
CHECKPOINT_RESULT_SECTIONS = frozenset(
    {
        SECTION_IMAGE_METADATA,
        SECTION_ROUTE_DECISION,
        SECTION_PAGE_SUMMARY,
        SECTION_UNIT_RESULTS,
        SECTION_CROP_GEOMETRY,
        SECTION_CROP_GROUNDING,
        SECTION_CROP_VALIDATION,
        SECTION_QUESTION_CONTEXT,
        SECTION_CHAPTER_DECISION,
        SECTION_LOAD_OBSERVATIONS,
        SECTION_STRUCTURE_DECISION,
        SECTION_DIMENSION_OBSERVATIONS,
        SECTION_CANDIDATE_COUNTS,
        SECTION_FILTER_DECISIONS,
        SECTION_CANDIDATE_SCORES,
        SECTION_RERANK_POLICY,
        SECTION_SELECTION,
        SECTION_DELIVERY,
    }
)

ARTIFACT_ROLE_SOURCE_PAGE = "source_page"
ARTIFACT_ROLE_QUESTION_CROP = "question_crop"
ARTIFACT_ROLE_CROP_OVERLAY = "crop_overlay"
ARTIFACT_ROLE_CANDIDATE_IMAGE = "candidate_image"
ARTIFACT_ROLE_ANSWER_IMAGE = "answer_image"
ARTIFACT_ROLES = frozenset(
    {
        ARTIFACT_ROLE_SOURCE_PAGE,
        ARTIFACT_ROLE_QUESTION_CROP,
        ARTIFACT_ROLE_CROP_OVERLAY,
        ARTIFACT_ROLE_CANDIDATE_IMAGE,
        ARTIFACT_ROLE_ANSWER_IMAGE,
    }
)
SINGLETON_ARTIFACT_ROLES = frozenset(
    {ARTIFACT_ROLE_SOURCE_PAGE, ARTIFACT_ROLE_QUESTION_CROP, ARTIFACT_ROLE_CROP_OVERLAY}
)

RETENTION_NORMAL = "normal"
RETENTION_FAILED = "failed"
RETENTION_FEEDBACK = "feedback"
RETENTION_INVESTIGATION = "investigation"
RETENTION_CLASSES = frozenset(
    {
        RETENTION_NORMAL,
        RETENTION_FAILED,
        RETENTION_FEEDBACK,
        RETENTION_INVESTIGATION,
    }
)

ARTIFACT_AVAILABLE = "available"
ARTIFACT_PURGED = "purged"
ARTIFACT_STATUSES = frozenset({ARTIFACT_AVAILABLE, ARTIFACT_PURGED})
ARTIFACT_PURGE_REASONS = frozenset(
    {"", "expired", "capacity", "user_deleted", "investigation_closed", "missing"}
)

AUDIT_VIEW_CHECKPOINT = "view_checkpoint"
AUDIT_VIEW_ARTIFACT = "view_artifact"
AUDIT_EXTEND_RETENTION = "extend_retention"
AUDIT_DELETE_EVIDENCE = "delete_evidence"
AUDIT_AUTO_PURGE = "auto_purge"
CHECKPOINT_AUDIT_ACTIONS = frozenset(
    {
        AUDIT_VIEW_CHECKPOINT,
        AUDIT_VIEW_ARTIFACT,
        AUDIT_EXTEND_RETENTION,
        AUDIT_DELETE_EVIDENCE,
        AUDIT_AUTO_PURGE,
    }
)

LOAD_TYPES = frozenset({"集中", "均布", "弯矩"})
IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
)
CROP_VALIDATION_CHECKS = frozenset(
    {
        "selected_diagram_match",
        "single_target_diagram",
        "structure_complete",
        "supports_complete",
        "external_loads_complete",
        "image_clear",
    }
)
CROP_EXTERNAL_LOAD_STATUSES = frozenset(
    {"not_run", "not_configured", "yes", "no", "error"}
)

MAX_RESULT_BYTES = 64 * 1024
MAX_RESULT_DEPTH = 7
MAX_COLLECTION_ITEMS = 50
MAX_TEXT_CHARS = 2_000
MAX_EVIDENCE_CHARS = 1_000
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

_CHECKPOINT_ID_RE = re.compile(r"^ckpt_[0-9a-f]{32}$")
_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{32}$")
_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
_SESSION_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|(?:^|[\s\"'])\\\\[^\\/\s]+[\\/]|(?:^|[\s\"'])/(?!/)[^\s\"']+)"
)
_DRIVE_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_URI_RE = re.compile(r"\b[a-z][a-z0-9+.-]{1,31}://", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._-]{4,}|\bsk[-_](?:proj[-_])?[A-Za-z0-9_-]{4,}|"
    r"\b(?:sessionid|session_id|cookie|token|access_token|refresh_token|password|passwd|secret)"
    r"\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)
_FORBIDDEN_KEY_RE = re.compile(
    r"(?:^|_)(?:api_key|access_token|refresh_token|token|password|passwd|cookie|"
    r"sessionid|session_id|secret|invite_code|"
    r"invite_hash|prompt|raw_model_output|reasoning|traceback|stack_trace|"
    r"exception_message|path|filename|file_name|storage_key|url)(?:$|_)",
    re.IGNORECASE,
)


def new_checkpoint_id() -> str:
    return f"ckpt_{uuid4().hex}"


def new_artifact_id() -> str:
    return f"art_{uuid4().hex}"


def is_valid_checkpoint_id(value: object) -> bool:
    return bool(_CHECKPOINT_ID_RE.fullmatch(str(value or "")))


def is_valid_artifact_id(value: object) -> bool:
    return bool(_ARTIFACT_ID_RE.fullmatch(str(value or "")))


@dataclass(frozen=True)
class RetentionPolicyV1:
    checkpoint_default_days: int
    checkpoint_max_days: int
    artifact_default_days: int
    artifact_max_days: int

    def __post_init__(self) -> None:
        values = (
            self.checkpoint_default_days,
            self.checkpoint_max_days,
            self.artifact_default_days,
            self.artifact_max_days,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("retention days must be positive integers")
        if self.checkpoint_default_days > self.checkpoint_max_days:
            raise ValueError("checkpoint default retention exceeds maximum")
        if self.artifact_default_days > self.artifact_max_days:
            raise ValueError("artifact default retention exceeds maximum")


RETENTION_POLICIES: Mapping[str, RetentionPolicyV1] = MappingProxyType(
    {
        RETENTION_NORMAL: RetentionPolicyV1(30, 30, 3, 3),
        RETENTION_FAILED: RetentionPolicyV1(30, 30, 7, 7),
        # Feedback follows the existing configurable feedback ceiling.
        RETENTION_FEEDBACK: RetentionPolicyV1(30, 365, 30, 365),
        RETENTION_INVESTIGATION: RetentionPolicyV1(30, 90, 30, 90),
    }
)


@dataclass(frozen=True)
class EvidenceCapacityPolicyV1:
    """Required bounded-storage settings; deployment values are not guessed in 4.1."""

    max_checkpoint_rows: int
    max_artifact_rows: int
    max_audit_rows: int
    max_trace_rows: int
    max_artifact_bytes: int
    min_free_bytes: int
    max_artifacts_per_checkpoint: int

    def __post_init__(self) -> None:
        for name in (
            "max_checkpoint_rows",
            "max_artifact_rows",
            "max_audit_rows",
            "max_trace_rows",
            "max_artifact_bytes",
            "min_free_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"checkpoint capacity {name} must be positive")
        if (
            type(self.max_artifacts_per_checkpoint) is not int
            or not 1 <= self.max_artifacts_per_checkpoint <= MAX_COLLECTION_ITEMS
        ):
            raise ValueError("invalid max_artifacts_per_checkpoint")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_checkpoint_rows": self.max_checkpoint_rows,
            "max_artifact_rows": self.max_artifact_rows,
            "max_audit_rows": self.max_audit_rows,
            "max_trace_rows": self.max_trace_rows,
            "max_artifact_bytes": self.max_artifact_bytes,
            "min_free_bytes": self.min_free_bytes,
            "max_artifacts_per_checkpoint": self.max_artifacts_per_checkpoint,
        }


@dataclass(frozen=True)
class SectionContractV1:
    container: str
    required_fields: frozenset[str]
    optional_fields: frozenset[str] = frozenset()
    max_items: int = 1

    def __post_init__(self) -> None:
        if self.container not in {"object", "list"}:
            raise ValueError("unknown checkpoint section container")
        if self.required_fields & self.optional_fields:
            raise ValueError("checkpoint section fields overlap")
        if type(self.max_items) is not int or self.max_items < 1:
            raise ValueError("checkpoint section max_items must be positive")
        for name in self.required_fields | self.optional_fields:
            if not _KEY_RE.fullmatch(name):
                raise ValueError("invalid checkpoint section field")


def _section(
    container: str,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    *,
    max_items: int = 1,
) -> SectionContractV1:
    return SectionContractV1(container, frozenset(required), frozenset(optional), max_items)


SECTION_CONTRACTS: Mapping[str, SectionContractV1] = MappingProxyType(
    {
        SECTION_IMAGE_METADATA: _section(
            "object",
            (
                "width_px",
                "height_px",
                "byte_size",
                "mime_type",
                "sha256",
                "orientation_status",
                "applied_rotation_degrees",
            ),
            ("orientation_confidence", "orientation_method"),
        ),
        SECTION_ROUTE_DECISION: _section(
            "object", ("route", "decision_source", "reason_code"), ("confidence",)
        ),
        SECTION_PAGE_SUMMARY: _section(
            "object",
            (
                "source_schema_version",
                "page_disposition",
                "group_count",
                "unit_count",
                "stored_unit_count",
                "units_truncated",
                "searchable_unit_count",
                "diagram_count",
                "unknown_count",
            ),
        ),
        SECTION_UNIT_RESULTS: _section(
            "list",
            (
                "unit_id",
                "group_id",
                "parent_question_label",
                "question_label",
                "display_label",
                "searchability",
                "status",
                "reason_codes",
                "diagram_roles",
                "recognized_text_excerpt",
            ),
            max_items=MAX_COLLECTION_ITEMS,
        ),
        SECTION_CROP_GEOMETRY: _section(
            "object",
            (
                "unit_id",
                "method",
                "model_bbox",
                "expanded_bbox",
                "pixel_bounds",
                "source_width_px",
                "source_height_px",
                "crop_width_px",
                "crop_height_px",
            ),
        ),
        SECTION_CROP_GROUNDING: _section(
            "object",
            (
                "schema_version",
                "page_status",
                "grounding_status",
                "reason_codes",
                "binding_evidence_excerpt",
            ),
        ),
        SECTION_CROP_VALIDATION: _section(
            "object",
            (
                "schema_version",
                "verdict",
                "checks",
                "external_load_status",
                "reason_codes",
            ),
        ),
        SECTION_QUESTION_CONTEXT: _section(
            "object",
            ("analysis_schema_version", "recognized_text_excerpt", "category"),
        ),
        SECTION_CHAPTER_DECISION: _section(
            "object",
            ("chapter", "confidence", "source", "scope_status", "reason_code"),
            ("evidence_excerpt", "topic_id"),
        ),
        SECTION_LOAD_OBSERVATIONS: _section(
            "list", ("type", "raw"), max_items=50
        ),
        SECTION_STRUCTURE_DECISION: _section(
            "object",
            ("structure_type", "source", "filter_applicable", "reason_code"),
            ("confidence",),
        ),
        SECTION_DIMENSION_OBSERVATIONS: _section(
            "list",
            (
                "kind",
                "raw",
                "normalized",
                "unit",
                "source",
                "status",
                "reason_code",
            ),
            max_items=50,
        ),
        SECTION_CANDIDATE_COUNTS: _section(
            "object",
            (
                "chapter_scanned",
                "load_scored",
                "positive_score",
                "rerank_pool",
                "after_dimension_filter",
                "stored_score_count",
                "scores_truncated",
            ),
            ("excluded_previous", "remaining"),
        ),
        SECTION_FILTER_DECISIONS: _section(
            "list",
            ("filter", "status", "before", "after", "reason_code", "policy_version"),
            max_items=20,
        ),
        SECTION_CANDIDATE_SCORES: _section(
            "list",
            (
                "candidate_id",
                "coarse_rank",
                "coarse_score",
                "rerank_rank",
                "rerank_score",
                "final_score",
                "score_status",
                "reason_code",
                "structure_type",
                "long_width",
                "single_side",
            ),
            ("visible",),
            max_items=50,
        ),
        SECTION_RERANK_POLICY: _section(
            "object",
            (
                "reranked",
                "input_count",
                "completed_count",
                "failed_count",
                "threshold",
                "display_all_score",
                "fallback_limit",
                "fallback_used",
                "reason_code",
                "policy_version",
                "visible",
                "stored_score_count",
                "scores_truncated",
            ),
        ),
        SECTION_SELECTION: _section(
            "object",
            ("candidate_id", "selected_rank", "candidate_generation", "selection_source"),
        ),
        SECTION_DELIVERY: _section(
            "object",
            ("answer_artifact_count", "media_status", "delivery_code"),
            ("response_id",),
        ),
    }
)


@dataclass(frozen=True)
class CheckpointStageContractV1:
    stage: str
    scopes: frozenset[str]
    outcomes: frozenset[str]
    required_sections: tuple[str, ...]
    optional_sections: tuple[str, ...] = ()
    required_artifact_role_groups: tuple[frozenset[str], ...] = ()
    optional_artifact_roles: frozenset[str] = frozenset()
    unit_binding: str = "optional"

    def __post_init__(self) -> None:
        if self.stage not in CHECKPOINT_STAGES:
            raise ValueError("unknown checkpoint stage")
        if not self.scopes or not self.scopes <= CHECKPOINT_SCOPES:
            raise ValueError("invalid checkpoint stage scopes")
        if not self.outcomes or not self.outcomes <= CHECKPOINT_OUTCOMES:
            raise ValueError("invalid checkpoint stage outcomes")
        if set(self.required_sections) & set(self.optional_sections):
            raise ValueError("checkpoint stage sections overlap")
        if not set(self.required_sections) | set(self.optional_sections) <= CHECKPOINT_RESULT_SECTIONS:
            raise ValueError("unknown checkpoint result section")
        if self.unit_binding not in {"required", "optional", "forbidden"}:
            raise ValueError("invalid checkpoint unit binding")
        for group in self.required_artifact_role_groups:
            if not group or not group <= ARTIFACT_ROLES:
                raise ValueError("invalid required artifact role group")
        if not self.optional_artifact_roles <= ARTIFACT_ROLES:
            raise ValueError("invalid optional artifact role")


def _stage(
    stage: str,
    scopes: tuple[str, ...],
    outcomes: tuple[str, ...],
    required_sections: tuple[str, ...],
    optional_sections: tuple[str, ...] = (),
    *,
    required_artifact_role_groups: tuple[tuple[str, ...], ...] = (),
    optional_artifact_roles: tuple[str, ...] = (),
    unit_binding: str = "optional",
) -> CheckpointStageContractV1:
    return CheckpointStageContractV1(
        stage=stage,
        scopes=frozenset(scopes),
        outcomes=frozenset(outcomes),
        required_sections=required_sections,
        optional_sections=optional_sections,
        required_artifact_role_groups=tuple(
            frozenset(group) for group in required_artifact_role_groups
        ),
        optional_artifact_roles=frozenset(optional_artifact_roles),
        unit_binding=unit_binding,
    )


STAGE_CONTRACTS: Mapping[str, CheckpointStageContractV1] = MappingProxyType(
    {
        STAGE_IMAGE_ACCEPTED: _stage(
            STAGE_IMAGE_ACCEPTED,
            (SCOPE_WORKFLOW,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL),
            (SECTION_IMAGE_METADATA,),
            required_artifact_role_groups=((ARTIFACT_ROLE_SOURCE_PAGE,),),
            unit_binding="forbidden",
        ),
        STAGE_IMAGE_ROUTED: _stage(
            STAGE_IMAGE_ROUTED,
            (SCOPE_WORKFLOW,),
            (OUTCOME_SUCCESS, OUTCOME_FAILED),
            (SECTION_ROUTE_DECISION,),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE,),
            unit_binding="forbidden",
        ),
        STAGE_PAGE_UNDERSTOOD: _stage(
            STAGE_PAGE_UNDERSTOOD,
            (SCOPE_WORKFLOW,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NO_MATCH, OUTCOME_FAILED),
            (SECTION_PAGE_SUMMARY, SECTION_UNIT_RESULTS),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE,),
            unit_binding="forbidden",
        ),
        STAGE_CROP_PREPARED: _stage(
            STAGE_CROP_PREPARED,
            (SCOPE_WORKFLOW,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_FAILED),
            (SECTION_CROP_GEOMETRY, SECTION_CROP_GROUNDING),
            required_artifact_role_groups=((ARTIFACT_ROLE_QUESTION_CROP,),),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE, ARTIFACT_ROLE_CROP_OVERLAY),
            unit_binding="required",
        ),
        STAGE_CROP_VALIDATED: _stage(
            STAGE_CROP_VALIDATED,
            (SCOPE_WORKFLOW,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NEEDS_INPUT, OUTCOME_FAILED),
            (SECTION_CROP_VALIDATION,),
            required_artifact_role_groups=((ARTIFACT_ROLE_QUESTION_CROP,),),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE, ARTIFACT_ROLE_CROP_OVERLAY),
            unit_binding="required",
        ),
        STAGE_QUESTION_ANALYZED: _stage(
            STAGE_QUESTION_ANALYZED,
            (SCOPE_CHILD_TASK,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NEEDS_INPUT, OUTCOME_FAILED),
            (
                SECTION_QUESTION_CONTEXT,
                SECTION_CHAPTER_DECISION,
                SECTION_LOAD_OBSERVATIONS,
                SECTION_STRUCTURE_DECISION,
            ),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE, ARTIFACT_ROLE_QUESTION_CROP),
        ),
        STAGE_COARSE_SEARCH_COMPLETED: _stage(
            STAGE_COARSE_SEARCH_COMPLETED,
            (SCOPE_CHILD_TASK,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NO_MATCH, OUTCOME_FAILED),
            (
                SECTION_DIMENSION_OBSERVATIONS,
                SECTION_CANDIDATE_COUNTS,
                SECTION_FILTER_DECISIONS,
                SECTION_CANDIDATE_SCORES,
            ),
            optional_artifact_roles=(ARTIFACT_ROLE_SOURCE_PAGE, ARTIFACT_ROLE_QUESTION_CROP),
        ),
        STAGE_RERANK_COMPLETED: _stage(
            STAGE_RERANK_COMPLETED,
            (SCOPE_CHILD_TASK,),
            (
                OUTCOME_SUCCESS,
                OUTCOME_PARTIAL,
                OUTCOME_NO_MATCH,
                OUTCOME_SKIPPED,
                OUTCOME_FAILED,
            ),
            (SECTION_RERANK_POLICY, SECTION_CANDIDATE_SCORES),
            optional_artifact_roles=(
                ARTIFACT_ROLE_SOURCE_PAGE,
                ARTIFACT_ROLE_QUESTION_CROP,
                ARTIFACT_ROLE_CANDIDATE_IMAGE,
            ),
        ),
        STAGE_ANSWER_PREPARED: _stage(
            STAGE_ANSWER_PREPARED,
            (SCOPE_CHILD_TASK,),
            (OUTCOME_SUCCESS, OUTCOME_PARTIAL, OUTCOME_NO_MATCH, OUTCOME_FAILED),
            (SECTION_DELIVERY,),
            (SECTION_SELECTION,),
            required_artifact_role_groups=((ARTIFACT_ROLE_ANSWER_IMAGE,),),
            optional_artifact_roles=(
                ARTIFACT_ROLE_SOURCE_PAGE,
                ARTIFACT_ROLE_QUESTION_CROP,
                ARTIFACT_ROLE_CANDIDATE_IMAGE,
            ),
        ),
    }
)


@dataclass(frozen=True)
class CheckpointOwnerV1:
    scope: str
    session_key: str
    workflow_search_id: str
    workflow_task_revision: int
    task_revision: int
    identity_key: str
    search_id: str = ""
    unit_id: str = ""
    candidate_generation: str = ""

    def __post_init__(self) -> None:
        if self.scope not in CHECKPOINT_SCOPES:
            raise ValueError("invalid checkpoint scope")
        if not _SESSION_KEY_RE.fullmatch(self.session_key):
            raise ValueError("invalid checkpoint session_key")
        _validate_opaque_id(self.identity_key, "identity_key")
        _validate_opaque_id(self.workflow_search_id, "workflow_search_id")
        _validate_optional_opaque_id(self.search_id, "search_id")
        _validate_optional_opaque_id(self.unit_id, "unit_id")
        for value, name in (
            (self.workflow_task_revision, "workflow_task_revision"),
            (self.task_revision, "task_revision"),
        ):
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError(f"invalid checkpoint {name}")
        if not isinstance(self.candidate_generation, str):
            raise ValueError("invalid checkpoint candidate_generation")
        if self.candidate_generation and not re.fullmatch(
            r"[1-9][0-9]{0,6}:[1-9][0-9]{0,6}", self.candidate_generation
        ):
            raise ValueError("invalid checkpoint candidate_generation")
        if self.candidate_generation and int(self.candidate_generation.split(":", 1)[0]) != self.task_revision:
            raise ValueError("checkpoint candidate_generation does not match task revision")
        if self.scope == SCOPE_WORKFLOW and self.search_id:
            raise ValueError("workflow checkpoint must not overload search_id")
        if self.scope == SCOPE_WORKFLOW and self.candidate_generation:
            raise ValueError("workflow checkpoint cannot bind candidate generation")
        if (
            self.scope == SCOPE_WORKFLOW
            and self.workflow_task_revision != self.task_revision
        ):
            raise ValueError("workflow checkpoint revisions must match")
        if self.scope == SCOPE_CHILD_TASK and not self.search_id:
            raise ValueError("child checkpoint requires search_id")
        if (
            self.scope == SCOPE_CHILD_TASK
            and self.unit_id
            and self.workflow_search_id == self.search_id
        ):
            raise ValueError("A3 child checkpoint requires distinct workflow and search ids")
        if (
            self.scope == SCOPE_CHILD_TASK
            and not self.unit_id
            and self.workflow_search_id != self.search_id
        ):
            raise ValueError("standalone child checkpoint must use search_id as workflow id")
        if (
            self.scope == SCOPE_CHILD_TASK
            and not self.unit_id
            and self.workflow_task_revision != self.task_revision
        ):
            raise ValueError("standalone child checkpoint revisions must match")

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "session_key": self.session_key,
            "identity_key": self.identity_key,
            "workflow_search_id": self.workflow_search_id,
            "search_id": self.search_id,
            "unit_id": self.unit_id,
            "workflow_task_revision": self.workflow_task_revision,
            "task_revision": self.task_revision,
            "candidate_generation": self.candidate_generation,
        }


@dataclass(frozen=True)
class ProducerVersionV1:
    code_revision: str
    component: str
    component_version: str
    model_provider: str = ""
    model_name: str = ""
    prompt_sha256: str = ""
    input_schema_version: str = ""
    policy_version: str = ""
    data_version: str = ""

    def __post_init__(self) -> None:
        if not _CODE_REVISION_RE.fullmatch(self.code_revision):
            raise ValueError("invalid checkpoint code revision")
        for name in (
            "component",
            "component_version",
            "model_provider",
            "model_name",
            "input_schema_version",
            "policy_version",
            "data_version",
        ):
            value = getattr(self, name)
            _optional_symbol(value, f"producer {name}")
        if not self.component or not self.component_version:
            raise ValueError("checkpoint producer component and version are required")
        if bool(self.model_provider) != bool(self.model_name):
            raise ValueError("checkpoint model provider and name must be paired")
        if not isinstance(self.prompt_sha256, str) or (
            self.prompt_sha256 and not _SHA256_RE.fullmatch(self.prompt_sha256)
        ):
            raise ValueError("invalid checkpoint prompt hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "code_revision": self.code_revision,
            "component": self.component,
            "component_version": self.component_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "prompt_sha256": self.prompt_sha256,
            "input_schema_version": self.input_schema_version,
            "policy_version": self.policy_version,
            "data_version": self.data_version,
        }


def compute_input_fingerprint_v1(
    *,
    stage: str,
    owner: CheckpointOwnerV1,
    producer: ProducerVersionV1,
    input_digests: Mapping[str, str],
) -> str:
    """Hash canonical V1 input identity without embedding raw input content."""

    if type(stage) is not str or stage not in CHECKPOINT_STAGES:
        raise ValueError("unknown checkpoint fingerprint stage")
    if type(owner) is not CheckpointOwnerV1:
        raise ValueError("fingerprint owner must be CheckpointOwnerV1")
    if type(producer) is not ProducerVersionV1:
        raise ValueError("fingerprint producer must be ProducerVersionV1")
    if not isinstance(input_digests, Mapping) or not input_digests:
        raise ValueError("checkpoint fingerprint requires input digests")
    if len(input_digests) > MAX_COLLECTION_ITEMS:
        raise ValueError("checkpoint fingerprint has too many input digests")

    normalized_digests: list[tuple[str, str]] = []
    for name, digest in input_digests.items():
        if (
            type(name) is not str
            or not _KEY_RE.fullmatch(name)
            or _FORBIDDEN_KEY_RE.search(name)
        ):
            raise ValueError("invalid checkpoint fingerprint input name")
        if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid checkpoint fingerprint input digest")
        normalized_digests.append((name, digest))

    payload = {
        "contract": CHECKPOINT_CONTRACT,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "stage": stage,
        "owner": owner.to_dict(),
        "producer": producer.to_dict(),
        "input_digests": dict(sorted(normalized_digests)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CheckpointFailureV1:
    code: str
    kind: str
    retryable: bool
    fallback: str = ""
    last_successful_checkpoint_id: str = ""

    def __post_init__(self) -> None:
        _code(self.code, "failure code")
        _symbol(self.kind, "failure kind")
        if type(self.retryable) is not bool:
            raise ValueError("checkpoint failure retryable must be boolean")
        _optional_symbol(self.fallback, "failure fallback")
        if self.last_successful_checkpoint_id and not is_valid_checkpoint_id(
            self.last_successful_checkpoint_id
        ):
            raise ValueError("invalid last successful checkpoint id")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "kind": self.kind,
            "retryable": self.retryable,
            "fallback": self.fallback,
            "last_successful_checkpoint_id": self.last_successful_checkpoint_id,
        }


@dataclass(frozen=True)
class ArtifactLinkV1:
    artifact_id: str
    role: str
    ordinal: int = 1

    def __post_init__(self) -> None:
        if not is_valid_artifact_id(self.artifact_id):
            raise ValueError("invalid checkpoint artifact id")
        if self.role not in ARTIFACT_ROLES:
            raise ValueError("invalid checkpoint artifact role")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= MAX_COLLECTION_ITEMS:
            raise ValueError("invalid checkpoint artifact ordinal")
        if self.role in SINGLETON_ARTIFACT_ROLES and self.ordinal != 1:
            raise ValueError("singleton checkpoint artifact role must use ordinal 1")

    def to_dict(self) -> dict[str, object]:
        return {"artifact_id": self.artifact_id, "role": self.role, "ordinal": self.ordinal}


@dataclass(frozen=True)
class ArtifactDescriptorV1:
    artifact_id: str
    owner: CheckpointOwnerV1
    sha256: str
    byte_size: int
    media_type: str
    width_px: int
    height_px: int
    created_at: str
    expires_at: str
    retention_class: str
    status: str = ARTIFACT_AVAILABLE
    purged_at: str = ""
    purge_reason: str = ""
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.owner) is not CheckpointOwnerV1:
            raise ValueError("artifact owner must be CheckpointOwnerV1")
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported artifact schema version")
        if not is_valid_artifact_id(self.artifact_id):
            raise ValueError("invalid artifact id")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("invalid artifact sha256")
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid artifact byte size")
        if self.media_type not in IMAGE_MEDIA_TYPES:
            raise ValueError("unsupported artifact media type")
        for value, name in ((self.width_px, "width"), (self.height_px, "height")):
            if type(value) is not int or not 1 <= value <= 100_000:
                raise ValueError(f"invalid artifact {name}")
        _validate_retention_window(
            self.created_at,
            self.expires_at,
            self.retention_class,
            resource="artifact",
        )
        if self.status not in ARTIFACT_STATUSES:
            raise ValueError("invalid artifact status")
        if self.purge_reason not in ARTIFACT_PURGE_REASONS:
            raise ValueError("invalid artifact purge reason")
        if self.status == ARTIFACT_AVAILABLE and (self.purged_at or self.purge_reason):
            raise ValueError("available artifact cannot have purge metadata")
        if self.status == ARTIFACT_PURGED:
            if not self.purged_at or not self.purge_reason:
                raise ValueError("purged artifact requires purge metadata")
            if _parse_timestamp(self.purged_at, "purged_at") < _parse_timestamp(
                self.created_at, "created_at"
            ):
                raise ValueError("artifact purge cannot precede creation")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "owner": self.owner.to_dict(),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "retention_class": self.retention_class,
            "status": self.status,
            "purged_at": self.purged_at,
            "purge_reason": self.purge_reason,
        }


@dataclass(frozen=True)
class IntermediateCheckpointV1:
    checkpoint_id: str
    trace_id: str
    stage: str
    outcome: str
    occurred_at: str
    expires_at: str
    retention_class: str
    owner: CheckpointOwnerV1
    producer: ProducerVersionV1
    input_fingerprint: str
    result: Mapping[str, Any]
    artifacts: tuple[ArtifactLinkV1, ...] = ()
    request_id: str = ""
    predecessor_checkpoint_id: str = ""
    failure: CheckpointFailureV1 | None = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.owner) is not CheckpointOwnerV1:
            raise ValueError("checkpoint owner must be CheckpointOwnerV1")
        if type(self.producer) is not ProducerVersionV1:
            raise ValueError("checkpoint producer must be ProducerVersionV1")
        if self.failure is not None and type(self.failure) is not CheckpointFailureV1:
            raise ValueError("checkpoint failure must be CheckpointFailureV1")
        if type(self.artifacts) is not tuple:
            raise ValueError("checkpoint artifacts must be a tuple")
        if any(type(link) is not ArtifactLinkV1 for link in self.artifacts):
            raise ValueError("checkpoint artifact links must be ArtifactLinkV1")
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema version")
        if not is_valid_checkpoint_id(self.checkpoint_id):
            raise ValueError("invalid checkpoint id")
        if not is_valid_trace_id(self.trace_id):
            raise ValueError("invalid checkpoint trace id")
        if self.stage not in STAGE_CONTRACTS:
            raise ValueError("unknown checkpoint stage")
        if self.outcome not in CHECKPOINT_OUTCOMES:
            raise ValueError("unknown checkpoint outcome")
        if self.request_id and not _REQUEST_ID_RE.fullmatch(self.request_id):
            raise ValueError("invalid checkpoint request id")
        if self.predecessor_checkpoint_id and not is_valid_checkpoint_id(
            self.predecessor_checkpoint_id
        ):
            raise ValueError("invalid predecessor checkpoint id")
        if self.predecessor_checkpoint_id == self.checkpoint_id:
            raise ValueError("checkpoint cannot be its own predecessor")
        if not isinstance(self.input_fingerprint, str) or not _SHA256_RE.fullmatch(
            self.input_fingerprint
        ):
            raise ValueError("invalid checkpoint input fingerprint")
        _validate_retention_window(
            self.occurred_at,
            self.expires_at,
            self.retention_class,
            resource="checkpoint",
        )
        contract = STAGE_CONTRACTS[self.stage]
        if self.owner.scope not in contract.scopes:
            raise ValueError("checkpoint scope does not match stage")
        if self.outcome not in contract.outcomes:
            raise ValueError("checkpoint outcome does not match stage")
        if contract.unit_binding == "required" and not self.owner.unit_id:
            raise ValueError("checkpoint stage requires unit binding")
        if contract.unit_binding == "forbidden" and self.owner.unit_id:
            raise ValueError("checkpoint stage forbids unit binding")
        if self.outcome == OUTCOME_FAILED and self.failure is None:
            raise ValueError("failed checkpoint requires failure details")
        if self.outcome not in {OUTCOME_FAILED, OUTCOME_PARTIAL} and self.failure is not None:
            raise ValueError("failure details require failed or partial outcome")
        if self.outcome == OUTCOME_FAILED and self.retention_class == RETENTION_NORMAL:
            raise ValueError("failed checkpoint cannot use normal retention")
        if (
            self.failure is not None
            and self.failure.last_successful_checkpoint_id == self.checkpoint_id
        ):
            raise ValueError("failed checkpoint cannot name itself as last successful")

        frozen_result = _freeze_json(self.result)
        _validate_result(contract, self.outcome, frozen_result)
        object.__setattr__(self, "result", frozen_result)

        if len(self.artifacts) > MAX_COLLECTION_ITEMS:
            raise ValueError("checkpoint has too many artifact links")
        links = {(link.role, link.ordinal) for link in self.artifacts}
        if len(links) != len(self.artifacts):
            raise ValueError("duplicate checkpoint artifact role and ordinal")
        _validate_artifact_links(contract, self.outcome, self.artifacts)
        _validate_cross_section_invariants(
            self.stage,
            self.outcome,
            self.owner,
            frozen_result,
            self.artifacts,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": CHECKPOINT_CONTRACT,
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "occurred_at": self.occurred_at,
            "expires_at": self.expires_at,
            "retention_class": self.retention_class,
            "owner": self.owner.to_dict(),
            "producer": self.producer.to_dict(),
            "input_fingerprint": self.input_fingerprint,
            "predecessor_checkpoint_id": self.predecessor_checkpoint_id,
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "result": _thaw_json(self.result),
            "artifacts": [link.to_dict() for link in self.artifacts],
        }


def _validate_result(
    stage: CheckpointStageContractV1,
    outcome: str,
    result: Mapping[str, Any],
) -> None:
    if not isinstance(result, Mapping):
        raise ValueError("checkpoint result must be an object")
    actual = set(result)
    allowed = set(stage.required_sections) | set(stage.optional_sections)
    if not actual <= allowed:
        raise ValueError("checkpoint result has unsupported sections")
    if outcome != OUTCOME_FAILED and not set(stage.required_sections) <= actual:
        raise ValueError("checkpoint result is missing required sections")
    if stage.stage == STAGE_ANSWER_PREPARED:
        if outcome in {OUTCOME_SUCCESS, OUTCOME_PARTIAL} and SECTION_SELECTION not in actual:
            raise ValueError("answer checkpoint is missing selection")
        if outcome == OUTCOME_NO_MATCH and SECTION_SELECTION in actual:
            raise ValueError("no-match answer checkpoint cannot contain selection")
    for name, value in result.items():
        _validate_section(name, value)
    encoded = json.dumps(_thaw_json(result), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("checkpoint result exceeds size limit")


def _validate_section(name: str, value: Any) -> None:
    contract = SECTION_CONTRACTS[name]
    items: tuple[Mapping[str, Any], ...]
    if contract.container == "object":
        if not isinstance(value, Mapping):
            raise ValueError(f"checkpoint section {name} must be an object")
        items = (value,)
    else:
        if not isinstance(value, tuple):
            raise ValueError(f"checkpoint section {name} must be a list")
        if len(value) > contract.max_items:
            raise ValueError(f"checkpoint section {name} exceeds item limit")
        if any(not isinstance(item, Mapping) for item in value):
            raise ValueError(f"checkpoint section {name} items must be objects")
        items = value
    for item in items:
        fields = set(item)
        allowed = contract.required_fields | contract.optional_fields
        if not contract.required_fields <= fields or not fields <= allowed:
            raise ValueError(f"checkpoint section {name} has invalid fields")
    _validate_section_semantics(name, items)


def _validate_section_semantics(name: str, items: tuple[Mapping[str, Any], ...]) -> None:
    if name == SECTION_IMAGE_METADATA:
        item = items[0]
        _positive_int(item["width_px"], "image width")
        _positive_int(item["height_px"], "image height")
        _positive_int(item["byte_size"], "image byte size", maximum=MAX_ARTIFACT_BYTES)
        if item["mime_type"] not in IMAGE_MEDIA_TYPES or not _SHA256_RE.fullmatch(
            str(item["sha256"])
        ):
            raise ValueError("invalid checkpoint image metadata")
        if item["orientation_status"] not in {"not_run", "kept", "rotated", "uncertain"}:
            raise ValueError("invalid checkpoint orientation status")
        if item["applied_rotation_degrees"] not in {0, 90, 180, 270}:
            raise ValueError("invalid checkpoint image rotation")
        if "orientation_confidence" in item:
            _optional_score(item["orientation_confidence"], "orientation confidence")
    elif name == SECTION_ROUTE_DECISION:
        item = items[0]
        if item["route"] not in {"A1", "A2", "A3"}:
            raise ValueError("invalid checkpoint image route")
        _symbol(item["decision_source"], "route decision source")
        _code(item["reason_code"], "route reason code")
        if "confidence" in item:
            _optional_score(item["confidence"], "route confidence")
    elif name == SECTION_PAGE_SUMMARY:
        item = items[0]
        _symbol(item["source_schema_version"], "page schema version")
        _symbol(item["page_disposition"], "page disposition")
        for key in (
            "group_count",
            "unit_count",
            "stored_unit_count",
            "searchable_unit_count",
            "diagram_count",
            "unknown_count",
        ):
            _nonnegative_int(item[key], key)
        if type(item["units_truncated"]) is not bool:
            raise ValueError("units_truncated must be boolean")
        if item["searchable_unit_count"] > item["unit_count"]:
            raise ValueError("searchable unit count exceeds unit count")
        if item["stored_unit_count"] > item["unit_count"]:
            raise ValueError("stored unit count exceeds unit count")
        if item["units_truncated"] != (
            item["stored_unit_count"] < item["unit_count"]
        ):
            raise ValueError("units_truncated does not match stored unit count")
    elif name == SECTION_UNIT_RESULTS:
        unit_ids: list[str] = []
        for item in items:
            _opaque(item["unit_id"], "unit result id")
            unit_ids.append(str(item["unit_id"]))
            _opaque(item["group_id"], "unit group id")
            for key in ("parent_question_label", "question_label", "display_label"):
                _bounded_text(item[key], 200, f"unit {key}")
            _symbol(item["searchability"], "unit searchability")
            _symbol(item["status"], "unit status")
            _bounded_text(item["recognized_text_excerpt"], MAX_EVIDENCE_CHARS, "unit text")
            _string_list(item["reason_codes"], "unit reason codes")
            _string_list(item["diagram_roles"], "unit diagram roles")
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("duplicate checkpoint unit result")
    elif name == SECTION_CROP_GEOMETRY:
        item = items[0]
        _opaque(item["unit_id"], "crop unit id")
        _symbol(item["method"], "crop method")
        _bbox(item["model_bbox"], "model bbox", allow_none=True)
        _bbox(item["expanded_bbox"], "expanded bbox", allow_none=True)
        _bounds(item["pixel_bounds"])
        for key in ("source_width_px", "source_height_px", "crop_width_px", "crop_height_px"):
            _positive_int(item[key], key)
        bounds = item["pixel_bounds"]
        if (
            bounds["right"] > item["source_width_px"]
            or bounds["bottom"] > item["source_height_px"]
        ):
            raise ValueError("checkpoint crop bounds exceed source dimensions")
        if (
            item["crop_width_px"] != bounds["right"] - bounds["left"]
            or item["crop_height_px"] != bounds["bottom"] - bounds["top"]
        ):
            raise ValueError("checkpoint crop dimensions do not match pixel bounds")
    elif name == SECTION_CROP_GROUNDING:
        item = items[0]
        for key in ("schema_version", "page_status", "grounding_status"):
            _symbol(item[key], f"crop grounding {key}")
        _string_list(item["reason_codes"], "crop grounding reason codes")
        _bounded_text(
            item["binding_evidence_excerpt"],
            MAX_EVIDENCE_CHARS,
            "crop binding evidence",
        )
    elif name == SECTION_CROP_VALIDATION:
        item = items[0]
        _symbol(item["schema_version"], "crop validation schema")
        if item["verdict"] not in {"verified", "review_required"}:
            raise ValueError("invalid checkpoint crop validation verdict")
        if item["external_load_status"] not in CROP_EXTERNAL_LOAD_STATUSES:
            raise ValueError("invalid checkpoint external load status")
        checks = item["checks"]
        if not isinstance(checks, Mapping) or set(checks) != CROP_VALIDATION_CHECKS:
            raise ValueError("invalid checkpoint crop validation checks")
        if any(value is not None and type(value) is not bool for value in checks.values()):
            raise ValueError("crop validation checks must be boolean or null")
        all_pass = all(value is True for value in checks.values())
        if (item["verdict"] == "verified") != all_pass:
            raise ValueError("crop validation verdict and checks disagree")
        _string_list(item["reason_codes"], "crop validation reason codes")
    elif name == SECTION_QUESTION_CONTEXT:
        item = items[0]
        _symbol(item["analysis_schema_version"], "question analysis schema")
        _bounded_text(
            item["recognized_text_excerpt"],
            MAX_EVIDENCE_CHARS,
            "question text",
        )
        _symbol(item["category"], "question category")
    elif name == SECTION_CHAPTER_DECISION:
        item = items[0]
        _bounded_text(item["chapter"], 100, "chapter")
        _optional_score(item["confidence"], "chapter confidence")
        _symbol(item["source"], "chapter source")
        if item["scope_status"] not in {"supported", "unsupported", "uncertain"}:
            raise ValueError("invalid checkpoint chapter scope status")
        _code(item["reason_code"], "chapter reason code")
        if "evidence_excerpt" in item:
            _bounded_text(
                item["evidence_excerpt"],
                MAX_EVIDENCE_CHARS,
                "chapter evidence",
            )
        if "topic_id" in item:
            _bounded_text(item["topic_id"], 128, "chapter topic id")
    elif name == SECTION_LOAD_OBSERVATIONS:
        for item in items:
            if item["type"] not in LOAD_TYPES:
                raise ValueError("unsupported checkpoint load type")
            _bounded_text(item["raw"], 200, "load raw text", required=True)
    elif name == SECTION_STRUCTURE_DECISION:
        item = items[0]
        if item["structure_type"] not in {"", "梁", "钢架", "桁架", "拱"}:
            raise ValueError("unsupported checkpoint structure type")
        if type(item["filter_applicable"]) is not bool:
            raise ValueError("structure filter_applicable must be boolean")
        _symbol(item["source"], "structure source")
        _code(item["reason_code"], "structure reason code")
        if "confidence" in item:
            _optional_score(item["confidence"], "structure confidence")
    elif name == SECTION_DIMENSION_OBSERVATIONS:
        for item in items:
            if item["kind"] not in {"long_width", "single_side", "span", "member_length", "other"}:
                raise ValueError("unsupported checkpoint dimension kind")
            if item["status"] not in {"recognized", "missing", "uncertain", "conflict", "not_run"}:
                raise ValueError("unsupported checkpoint dimension status")
            for key in ("raw", "normalized", "unit"):
                _bounded_text(item[key], 200, f"dimension {key}")
            _symbol(item["source"], "dimension source")
            _code(item["reason_code"], "dimension reason code")
    elif name == SECTION_CANDIDATE_COUNTS:
        item = items[0]
        ordered = [
            item["chapter_scanned"],
            item["load_scored"],
            item["positive_score"],
            item["rerank_pool"],
            item["after_dimension_filter"],
        ]
        for value in ordered:
            _nonnegative_int(value, "candidate count")
        if ordered != sorted(ordered, reverse=True):
            raise ValueError("checkpoint candidate counts are not monotonic")
        for key in ("excluded_previous", "remaining"):
            if key in item:
                _nonnegative_int(item[key], key)
        _nonnegative_int(item["stored_score_count"], "stored score count")
        if type(item["scores_truncated"]) is not bool:
            raise ValueError("scores_truncated must be boolean")
        if item["stored_score_count"] > item["positive_score"]:
            raise ValueError("stored score count exceeds positive candidates")
        if item["stored_score_count"] > item["after_dimension_filter"]:
            raise ValueError("stored score count exceeds dimension-filtered candidates")
        if item["scores_truncated"] != (
            item["stored_score_count"] < item["after_dimension_filter"]
        ):
            raise ValueError("scores_truncated does not match stored score count")
    elif name == SECTION_FILTER_DECISIONS:
        filter_names: list[str] = []
        for item in items:
            _symbol(item["filter"], "filter name")
            filter_names.append(str(item["filter"]))
            _nonnegative_int(item["before"], "filter before")
            _nonnegative_int(item["after"], "filter after")
            if item["after"] > item["before"]:
                raise ValueError("checkpoint filter increased candidate count")
            if item["status"] not in {"applied", "skipped", "fallback", "failed"}:
                raise ValueError("invalid checkpoint filter status")
            _code(item["reason_code"], "filter reason code")
            _symbol(item["policy_version"], "filter policy version")
        if len(filter_names) != len(set(filter_names)):
            raise ValueError("duplicate checkpoint filter decision")
    elif name == SECTION_CANDIDATE_SCORES:
        ids: list[str] = []
        coarse_ranks: list[int] = []
        for item in items:
            _opaque(item["candidate_id"], "candidate id")
            ids.append(str(item["candidate_id"]))
            _positive_int(item["coarse_rank"], "coarse rank")
            coarse_ranks.append(int(item["coarse_rank"]))
            _optional_positive_int(item["rerank_rank"], "rerank rank")
            for key in ("coarse_score", "rerank_score", "final_score"):
                _optional_score(item[key], key)
            _symbol(item["score_status"], "candidate score status")
            _code(item["reason_code"], "candidate score reason code")
            if item["structure_type"] not in {"", "梁", "钢架", "桁架", "拱"}:
                raise ValueError("unsupported candidate structure type")
            _bounded_text(item["long_width"], 200, "candidate long_width")
            _bounded_text(item["single_side"], 200, "candidate single_side")
            if "visible" in item and type(item["visible"]) is not bool:
                raise ValueError("candidate visible must be boolean")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate checkpoint candidate id")
        if len(coarse_ranks) != len(set(coarse_ranks)):
            raise ValueError("duplicate checkpoint coarse rank")
    elif name == SECTION_RERANK_POLICY:
        item = items[0]
        for key in ("reranked", "fallback_used", "scores_truncated"):
            if type(item[key]) is not bool:
                raise ValueError(f"checkpoint {key} must be boolean")
        for key in (
            "input_count",
            "completed_count",
            "failed_count",
            "fallback_limit",
            "visible",
            "stored_score_count",
        ):
            _nonnegative_int(item[key], key)
        if item["completed_count"] + item["failed_count"] > item["input_count"]:
            raise ValueError("rerank result count exceeds input count")
        if item["visible"] > item["input_count"]:
            raise ValueError("visible candidate count exceeds rerank input")
        if item["stored_score_count"] > item["input_count"]:
            raise ValueError("stored score count exceeds rerank input")
        if item["stored_score_count"] < item["visible"]:
            raise ValueError("stored score count cannot omit visible candidates")
        if item["scores_truncated"] != (
            item["stored_score_count"] < item["input_count"]
        ):
            raise ValueError("scores_truncated does not match rerank stored score count")
        _required_score(item["threshold"], "rerank threshold")
        _required_score(item["display_all_score"], "display all score")
        _code(item["reason_code"], "rerank reason code")
        _symbol(item["policy_version"], "rerank policy version")
    elif name == SECTION_SELECTION:
        item = items[0]
        _opaque(item["candidate_id"], "selected candidate id")
        _positive_int(item["selected_rank"], "selected rank")
        if not re.fullmatch(r"[1-9][0-9]{0,6}:[1-9][0-9]{0,6}", str(item["candidate_generation"])):
            raise ValueError("invalid selected candidate generation")
        _symbol(item["selection_source"], "selection source")
    elif name == SECTION_DELIVERY:
        item = items[0]
        _nonnegative_int(item["answer_artifact_count"], "answer artifact count")
        _symbol(item["media_status"], "delivery media status")
        _code(item["delivery_code"], "delivery code")
        if "response_id" in item and item["response_id"] and not re.fullmatch(
            r"resp_[0-9a-f]{32}", str(item["response_id"])
        ):
            raise ValueError("invalid checkpoint response id")


def _validate_cross_section_invariants(
    stage: str,
    outcome: str,
    owner: CheckpointOwnerV1,
    result: Mapping[str, Any],
    artifacts: tuple[ArtifactLinkV1, ...],
) -> None:
    if SECTION_PAGE_SUMMARY in result and SECTION_UNIT_RESULTS in result:
        if result[SECTION_PAGE_SUMMARY]["stored_unit_count"] != len(
            result[SECTION_UNIT_RESULTS]
        ):
            raise ValueError("stored unit count does not match unit results")
    if SECTION_CANDIDATE_COUNTS in result and SECTION_CANDIDATE_SCORES in result:
        if result[SECTION_CANDIDATE_COUNTS]["stored_score_count"] != len(
            result[SECTION_CANDIDATE_SCORES]
        ):
            raise ValueError("stored score count does not match candidate scores")
    if SECTION_RERANK_POLICY in result and SECTION_CANDIDATE_SCORES in result:
        if result[SECTION_RERANK_POLICY]["stored_score_count"] != len(
            result[SECTION_CANDIDATE_SCORES]
        ):
            raise ValueError("rerank stored score count does not match candidate scores")
    if stage == STAGE_COARSE_SEARCH_COMPLETED:
        if outcome != OUTCOME_FAILED and not result[SECTION_DIMENSION_OBSERVATIONS]:
            raise ValueError("coarse checkpoint requires a dimension observation")
        if any(
            "visible" in item
            for item in result.get(SECTION_CANDIDATE_SCORES, ())
        ):
            raise ValueError("coarse candidate scores cannot contain visibility")
        if outcome != OUTCOME_FAILED:
            counts = result[SECTION_CANDIDATE_COUNTS]
            if outcome == OUTCOME_NO_MATCH and counts["after_dimension_filter"] != 0:
                raise ValueError("coarse no-match checkpoint has remaining candidates")
            if outcome in {OUTCOME_SUCCESS, OUTCOME_PARTIAL} and (
                counts["after_dimension_filter"] == 0
                or counts["stored_score_count"] == 0
            ):
                raise ValueError("coarse matched checkpoint requires candidates")
    if stage == STAGE_RERANK_COMPLETED and SECTION_CANDIDATE_SCORES in result:
        scores = result[SECTION_CANDIDATE_SCORES]
        if any("visible" not in item for item in scores):
            raise ValueError("rerank candidate scores require visibility")
        if SECTION_RERANK_POLICY in result and sum(
            item["visible"] is True for item in scores
        ) != result[SECTION_RERANK_POLICY]["visible"]:
            raise ValueError("rerank visible count does not match candidate scores")
        if outcome != OUTCOME_FAILED:
            policy = result[SECTION_RERANK_POLICY]
            if outcome == OUTCOME_SUCCESS and (
                policy["visible"] == 0
                or not policy["reranked"]
                or policy["fallback_used"]
            ):
                raise ValueError("successful rerank checkpoint has invalid result policy")
            if outcome == OUTCOME_SUCCESS and (
                policy["completed_count"] != policy["input_count"]
                or policy["failed_count"] != 0
            ):
                raise ValueError("successful rerank checkpoint is not complete")
            if outcome == OUTCOME_NO_MATCH and (
                policy["visible"] != 0 or policy["fallback_used"]
            ):
                raise ValueError("rerank no-match checkpoint has visible candidates")
            if outcome == OUTCOME_NO_MATCH and policy["reranked"] and (
                policy["completed_count"] != policy["input_count"]
                or policy["failed_count"] != 0
            ):
                raise ValueError("rerank no-match checkpoint is not complete")
            if outcome in {OUTCOME_PARTIAL, OUTCOME_SKIPPED} and (
                policy["visible"] == 0
                or policy["reranked"]
                or not policy["fallback_used"]
            ):
                raise ValueError("rerank fallback checkpoint has invalid result policy")
            if outcome == OUTCOME_SKIPPED and (
                policy["completed_count"] != 0 or policy["failed_count"] != 0
            ):
                raise ValueError("skipped rerank checkpoint cannot contain rerank results")
    if stage == STAGE_CROP_VALIDATED and SECTION_CROP_VALIDATION in result:
        validation = result[SECTION_CROP_VALIDATION]
        verdict = validation["verdict"]
        external_status = validation["external_load_status"]
        ready = verdict == "verified" and external_status in {"yes", "not_configured"}
        needs_input = (
            verdict == "review_required" and external_status == "not_run"
        ) or (verdict == "verified" and external_status in {"no", "error"})
        if not ready and not needs_input:
            raise ValueError("crop verdict and external load status disagree")
        if outcome in {OUTCOME_SUCCESS, OUTCOME_PARTIAL} and not ready:
            raise ValueError("successful crop validation is not ready")
        if outcome == OUTCOME_NEEDS_INPUT and not needs_input:
            raise ValueError("crop validation needs-input outcome is inconsistent")
    if SECTION_CROP_GEOMETRY in result:
        if result[SECTION_CROP_GEOMETRY]["unit_id"] != owner.unit_id:
            raise ValueError("crop geometry unit does not match checkpoint owner")
    if SECTION_SELECTION in result:
        generation = result[SECTION_SELECTION]["candidate_generation"]
        if not owner.candidate_generation or generation != owner.candidate_generation:
            raise ValueError("selection generation does not match checkpoint owner")
    if SECTION_DELIVERY in result:
        linked_answers = sum(
            link.role == ARTIFACT_ROLE_ANSWER_IMAGE for link in artifacts
        )
        if result[SECTION_DELIVERY]["answer_artifact_count"] != linked_answers:
            raise ValueError("delivery artifact count does not match checkpoint links")
    if stage == STAGE_ANSWER_PREPARED and outcome == OUTCOME_NO_MATCH:
        delivery = result[SECTION_DELIVERY]
        if (
            delivery["answer_artifact_count"] != 0
            or delivery["media_status"] != "not_available"
            or delivery["delivery_code"] != "NO_MATCH"
        ):
            raise ValueError("no-match answer checkpoint has invalid delivery")
        if any(link.role == ARTIFACT_ROLE_ANSWER_IMAGE for link in artifacts):
            raise ValueError("no-match answer checkpoint cannot link an answer artifact")


def _validate_artifact_links(
    contract: CheckpointStageContractV1,
    outcome: str,
    artifacts: tuple[ArtifactLinkV1, ...],
) -> None:
    roles = {link.role for link in artifacts}
    allowed = set(contract.optional_artifact_roles)
    for group in contract.required_artifact_role_groups:
        allowed.update(group)
    if not roles <= allowed:
        raise ValueError("checkpoint has artifact role not allowed for stage")
    if outcome == OUTCOME_SUCCESS:
        for group in contract.required_artifact_role_groups:
            if not roles & group:
                raise ValueError("checkpoint is missing required artifact role")


def _freeze_json(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_RESULT_DEPTH:
        raise ValueError("checkpoint result nesting is too deep")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("checkpoint result contains non-finite number")
        return value
    if isinstance(value, str):
        _bounded_text(value, MAX_TEXT_CHARS, "checkpoint text")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValueError("checkpoint result object is too large")
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise ValueError("checkpoint result has invalid key")
            if _FORBIDDEN_KEY_RE.search(key):
                raise ValueError("checkpoint result has forbidden key")
            frozen[key] = _freeze_json(item, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ValueError("checkpoint result collection is too large")
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    raise ValueError("checkpoint result must contain JSON-compatible values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_retention_window(
    created_at: str,
    expires_at: str,
    retention_class: str,
    *,
    resource: str,
) -> None:
    if retention_class not in RETENTION_POLICIES:
        raise ValueError("unknown checkpoint retention class")
    created = _parse_timestamp(created_at, "created_at")
    expires = _parse_timestamp(expires_at, "expires_at")
    if expires <= created:
        raise ValueError("checkpoint retention expiry must follow creation")
    policy = RETENTION_POLICIES[retention_class]
    maximum_days = (
        policy.checkpoint_max_days if resource == "checkpoint" else policy.artifact_max_days
    )
    if (expires - created).total_seconds() > maximum_days * 86_400:
        raise ValueError(f"{resource} retention exceeds class maximum")


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid checkpoint {name}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid checkpoint {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"checkpoint {name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _validate_opaque_id(value: object, name: str) -> None:
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise ValueError(f"invalid checkpoint {name}")
    _reject_sensitive_text(value, name)


def _validate_optional_opaque_id(value: object, name: str) -> None:
    if not isinstance(value, str) or (value and not _OPAQUE_ID_RE.fullmatch(value)):
        raise ValueError(f"invalid checkpoint {name}")
    if value:
        _reject_sensitive_text(value, name)


def _opaque(value: object, name: str) -> None:
    _validate_opaque_id(value, name)


def _symbol(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"invalid checkpoint {name}")
    _reject_sensitive_text(value, name)
    if not _SYMBOL_RE.fullmatch(value):
        raise ValueError(f"invalid checkpoint {name}")


def _optional_symbol(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"invalid checkpoint {name}")
    if value:
        _reject_sensitive_text(value, name)
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError(f"invalid checkpoint {name}")


def _code(value: object, name: str) -> None:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ValueError(f"invalid checkpoint {name}")
    _reject_sensitive_text(value, name)


def _bounded_text(value: object, maximum: int, name: str, *, required: bool = False) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or _CONTROL_RE.search(value)
        or (required and not value.strip())
    ):
        raise ValueError(f"invalid checkpoint {name}")
    _reject_sensitive_text(value, name)


def _string_list(value: object, name: str) -> None:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not _SYMBOL_RE.fullmatch(item) for item in value
    ):
        raise ValueError(f"invalid checkpoint {name}")
    for item in value:
        _reject_sensitive_text(item, name)


def _reject_sensitive_text(value: str, name: str) -> None:
    if (
        _ABSOLUTE_PATH_RE.search(value)
        or _DRIVE_RELATIVE_PATH_RE.search(value)
        or _URI_RE.search(value)
        or _SECRET_VALUE_RE.search(value)
    ):
        raise ValueError(
            f"checkpoint {name} contains path, URL, or secret-shaped text"
        )


def _nonnegative_int(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError(f"invalid checkpoint {name}")


def _positive_int(value: object, name: str, *, maximum: int = 1_000_000) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid checkpoint {name}")


def _optional_positive_int(value: object, name: str) -> None:
    if value is not None:
        _positive_int(value, name)


def _optional_score(value: object, name: str) -> None:
    if value is None:
        return
    if type(value) not in {int, float} or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise ValueError(f"invalid checkpoint {name}")


def _required_score(value: object, name: str) -> None:
    if value is None:
        raise ValueError(f"invalid checkpoint {name}")
    _optional_score(value, name)


def _bbox(value: object, name: str, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if not isinstance(value, tuple) or len(value) != 4 or any(type(item) is not int for item in value):
        raise ValueError(f"invalid checkpoint {name}")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= 1_000 and 0 <= y1 < y2 <= 1_000):
        raise ValueError(f"invalid checkpoint {name}")


def _bounds(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {"left", "top", "right", "bottom"}:
        raise ValueError("invalid checkpoint pixel bounds")
    coords = [value[key] for key in ("left", "top", "right", "bottom")]
    if any(type(item) is not int or item < 0 for item in coords):
        raise ValueError("invalid checkpoint pixel bounds")
    if coords[0] >= coords[2] or coords[1] >= coords[3]:
        raise ValueError("unordered checkpoint pixel bounds")


__all__ = [
    "ARTIFACT_AVAILABLE",
    "ARTIFACT_PURGED",
    "ARTIFACT_ROLES",
    "ARTIFACT_SCHEMA_VERSION",
    "CHECKPOINT_AUDIT_ACTIONS",
    "CHECKPOINT_CONTRACT",
    "CHECKPOINT_OUTCOMES",
    "CHECKPOINT_RESULT_SECTIONS",
    "CHECKPOINT_SCHEMA_VERSION",
    "CHECKPOINT_SCOPES",
    "CHECKPOINT_STAGES",
    "RETENTION_CLASSES",
    "RETENTION_POLICIES",
    "SECTION_CONTRACTS",
    "STAGE_CONTRACTS",
    "ArtifactDescriptorV1",
    "ArtifactLinkV1",
    "CheckpointFailureV1",
    "CheckpointOwnerV1",
    "CheckpointStageContractV1",
    "CROP_EXTERNAL_LOAD_STATUSES",
    "EvidenceCapacityPolicyV1",
    "IntermediateCheckpointV1",
    "ProducerVersionV1",
    "RetentionPolicyV1",
    "SectionContractV1",
    "compute_input_fingerprint_v1",
    "is_valid_artifact_id",
    "is_valid_checkpoint_id",
    "new_artifact_id",
    "new_checkpoint_id",
]

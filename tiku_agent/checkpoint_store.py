"""Bounded SQLite persistence for Intermediate Checkpoint V1 evidence.

The store is intentionally independent from the A2/A3 runtimes.  It owns the
trusted clock, artifact paths, retention enforcement, capacity checks and
evidence audit needed before runtime capture can be enabled.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
from queue import Empty, Full, Queue
import re
import shutil
import sqlite3
import stat
from threading import Condition, Lock, RLock, Thread, current_thread, local
from time import monotonic, sleep
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from tiku_agent.checkpoint_contract import (
    ARTIFACT_AVAILABLE,
    ARTIFACT_PURGED,
    ARTIFACT_ROLE_ANSWER_IMAGE,
    ARTIFACT_ROLE_CANDIDATE_IMAGE,
    ARTIFACT_ROLE_CROP_OVERLAY,
    ARTIFACT_ROLE_QUESTION_CROP,
    ARTIFACT_ROLE_SOURCE_PAGE,
    ARTIFACT_SCHEMA_VERSION,
    AUDIT_AUTO_PURGE,
    AUDIT_DELETE_EVIDENCE,
    AUDIT_EXTEND_RETENTION,
    AUDIT_VIEW_ARTIFACT,
    AUDIT_VIEW_CHECKPOINT,
    CHECKPOINT_CONTRACT,
    CHECKPOINT_SCHEMA_VERSION,
    IMAGE_MEDIA_TYPES,
    MAX_ARTIFACT_BYTES,
    RETENTION_POLICIES,
    SCOPE_CHILD_TASK,
    SCOPE_WORKFLOW,
    STAGE_CROP_PREPARED,
    STAGE_CROP_VALIDATED,
    ArtifactDescriptorV1,
    ArtifactLinkV1,
    CheckpointFailureV1,
    CheckpointOwnerV1,
    EvidenceCapacityPolicyV1,
    IntermediateCheckpointV1,
    ProducerVersionV1,
    is_valid_artifact_id,
    is_valid_checkpoint_id,
    new_artifact_id,
)


CHECKPOINT_STORE_SCHEMA_VERSION = 1
EVIDENCE_AUDIT_RETENTION_DAYS = 90
DEFAULT_CHECKPOINT_QUEUE_CAPACITY = 256
MAX_HEALTH_COUNTER = 2_147_483_647

_AUDIT_ID_RE = re.compile(r"^audit_[0-9a-f]{32}$")
_PLAN_ID_RE = re.compile(r"^rplan_[0-9a-f]{32}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_RESULT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_KEY_RE = re.compile(r"^blobs/[0-9a-f]{2}/[0-9a-f]{64}\.bin$")
_MEDIA_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class CheckpointStoreError(RuntimeError):
    """Base class for safe, stable store failures."""


class EvidenceValidationError(CheckpointStoreError, ValueError):
    pass


class EvidenceStoreUnavailable(CheckpointStoreError):
    pass


class EvidenceConflictError(CheckpointStoreError):
    pass


class EvidenceNotFoundError(CheckpointStoreError):
    pass


class EvidenceOwnershipError(CheckpointStoreError):
    pass


class EvidenceExpiredError(CheckpointStoreError):
    pass


class EvidenceUnavailableError(CheckpointStoreError):
    pass


class EvidenceAuditError(CheckpointStoreError):
    pass


class EvidenceCapacityError(CheckpointStoreError):
    def __init__(self, gate: str) -> None:
        self.gate = gate
        super().__init__(f"evidence capacity rejected by {gate}")


class EvidenceMaintenanceError(CheckpointStoreError):
    pass


class CheckpointRecorderClosed(CheckpointStoreError):
    pass


class CheckpointQueueFull(CheckpointStoreError):
    pass


@dataclass(frozen=True)
class StoredArtifactV1:
    descriptor: ArtifactDescriptorV1
    content: bytes

    def __post_init__(self) -> None:
        if type(self.descriptor) is not ArtifactDescriptorV1:
            raise EvidenceValidationError("invalid stored artifact descriptor")
        if type(self.content) is not bytes:
            raise EvidenceValidationError("invalid stored artifact content")


@dataclass(frozen=True)
class EvidenceAuditRecordV1:
    audit_id: str
    action: str
    actor_key: str
    owner_identity_key: str
    target_id: str
    result_code: str
    occurred_at: str
    expires_at: str
    new_expires_at: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "audit_id": self.audit_id,
            "action": self.action,
            "actor_key": self.actor_key,
            "owner_identity_key": self.owner_identity_key,
            "target_id": self.target_id,
            "result_code": self.result_code,
            "occurred_at": self.occurred_at,
            "expires_at": self.expires_at,
            "new_expires_at": self.new_expires_at,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class CheckpointRetentionCandidateV1:
    checkpoint_id: str
    expires_at: str
    semantic_sha256: str

    def __post_init__(self) -> None:
        if not is_valid_checkpoint_id(self.checkpoint_id):
            raise EvidenceValidationError("invalid retention checkpoint candidate")
        _parse_time(self.expires_at, "candidate expires_at")
        if type(self.semantic_sha256) is not str or not _SHA256_RE.fullmatch(self.semantic_sha256):
            raise EvidenceValidationError("invalid retention checkpoint hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "expires_at": self.expires_at,
            "semantic_sha256": self.semantic_sha256,
        }


@dataclass(frozen=True)
class ArtifactRetentionCandidateV1:
    artifact_id: str
    expires_at: str
    status: str
    storage_key: str
    sha256: str
    byte_size: int
    reason: str = "expired"

    def __post_init__(self) -> None:
        if not is_valid_artifact_id(self.artifact_id):
            raise EvidenceValidationError("invalid retention artifact candidate")
        _parse_time(self.expires_at, "candidate expires_at")
        if self.status not in {ARTIFACT_AVAILABLE, ARTIFACT_PURGED}:
            raise EvidenceValidationError("invalid retention artifact status")
        if type(self.storage_key) is not str or not _STORAGE_KEY_RE.fullmatch(self.storage_key):
            raise EvidenceValidationError("invalid retention artifact storage key")
        if type(self.sha256) is not str or not _SHA256_RE.fullmatch(self.sha256):
            raise EvidenceValidationError("invalid retention artifact hash")
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= MAX_ARTIFACT_BYTES:
            raise EvidenceValidationError("invalid retention artifact size")
        if self.reason not in {"expired", "missing", "tombstone"}:
            raise EvidenceValidationError("invalid retention artifact reason")
        expected_status = ARTIFACT_PURGED if self.reason == "tombstone" else ARTIFACT_AVAILABLE
        if self.status != expected_status or self.storage_key != _storage_key(self.sha256):
            raise EvidenceValidationError("inconsistent retention artifact candidate")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "expires_at": self.expires_at,
            "status": self.status,
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AuditRetentionCandidateV1:
    audit_id: str
    expires_at: str

    def __post_init__(self) -> None:
        if type(self.audit_id) is not str or not _AUDIT_ID_RE.fullmatch(self.audit_id):
            raise EvidenceValidationError("invalid retention audit candidate")
        _parse_time(self.expires_at, "candidate expires_at")

    def to_dict(self) -> dict[str, object]:
        return {"audit_id": self.audit_id, "expires_at": self.expires_at}


@dataclass(frozen=True)
class OrphanRetentionCandidateV1:
    storage_key: str
    sha256: str
    byte_size: int
    reason: str
    file_present: bool = True

    def __post_init__(self) -> None:
        if type(self.storage_key) is not str or not _STORAGE_KEY_RE.fullmatch(self.storage_key):
            raise EvidenceValidationError("invalid retention orphan storage key")
        if type(self.sha256) is not str or not _SHA256_RE.fullmatch(self.sha256):
            raise EvidenceValidationError("invalid retention orphan hash")
        if type(self.byte_size) is not int or not 1 <= self.byte_size <= MAX_ARTIFACT_BYTES:
            raise EvidenceValidationError("invalid retention orphan size")
        if self.reason not in {"refcount_mismatch", "zero_ref_blob", "unindexed_file"}:
            raise EvidenceValidationError("invalid retention orphan reason")
        if type(self.file_present) is not bool:
            raise EvidenceValidationError("invalid retention orphan file state")
        if self.reason == "unindexed_file" and not self.file_present:
            raise EvidenceValidationError("unindexed orphan file must be present")
        if self.storage_key != _storage_key(self.sha256):
            raise EvidenceValidationError("inconsistent retention orphan candidate")

    def to_dict(self) -> dict[str, object]:
        return {
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "reason": self.reason,
            "file_present": self.file_present,
        }


@dataclass(frozen=True)
class EvidenceRetentionPlanV1:
    schema_version: int
    store_id: str
    plan_id: str
    created_at: str
    as_of: str
    checkpoint_candidates: tuple[CheckpointRetentionCandidateV1, ...]
    artifact_candidates: tuple[ArtifactRetentionCandidateV1, ...]
    audit_candidates: tuple[AuditRetentionCandidateV1, ...]
    orphan_candidates: tuple[OrphanRetentionCandidateV1, ...]
    snapshot_sha256: str
    plan_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "store_id": self.store_id,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "as_of": self.as_of,
            "checkpoint_candidates": [item.to_dict() for item in self.checkpoint_candidates],
            "artifact_candidates": [item.to_dict() for item in self.artifact_candidates],
            "audit_candidates": [item.to_dict() for item in self.audit_candidates],
            "orphan_candidates": [item.to_dict() for item in self.orphan_candidates],
            "snapshot_sha256": self.snapshot_sha256,
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "EvidenceRetentionPlanV1":
        expected = {
            "schema_version", "store_id", "plan_id", "created_at", "as_of",
            "checkpoint_candidates", "artifact_candidates", "audit_candidates",
            "orphan_candidates", "snapshot_sha256", "plan_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise EvidenceValidationError("invalid retention plan shape")
        try:
            plan = cls(
                schema_version=value["schema_version"],
                store_id=value["store_id"],
                plan_id=value["plan_id"],
                created_at=value["created_at"],
                as_of=value["as_of"],
                checkpoint_candidates=tuple(
                    CheckpointRetentionCandidateV1(**dict(item))
                    for item in value["checkpoint_candidates"]
                ),
                artifact_candidates=tuple(
                    ArtifactRetentionCandidateV1(**dict(item))
                    for item in value["artifact_candidates"]
                ),
                audit_candidates=tuple(
                    AuditRetentionCandidateV1(**dict(item))
                    for item in value["audit_candidates"]
                ),
                orphan_candidates=tuple(
                    OrphanRetentionCandidateV1(**dict(item))
                    for item in value["orphan_candidates"]
                ),
                snapshot_sha256=value["snapshot_sha256"],
                plan_sha256=value["plan_sha256"],
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise EvidenceValidationError("invalid retention plan shape") from exc
        plan._validate()
        return plan

    def _validate(self) -> None:
        if self.schema_version != CHECKPOINT_STORE_SCHEMA_VERSION:
            raise EvidenceValidationError("unsupported retention plan schema")
        if self.store_id and not _SAFE_ID_RE.fullmatch(self.store_id):
            raise EvidenceValidationError("invalid retention store id")
        needs_store_identity = bool(
            self.checkpoint_candidates
            or self.artifact_candidates
            or self.audit_candidates
            or any(item.reason != "unindexed_file" for item in self.orphan_candidates)
        )
        if not self.store_id and needs_store_identity:
            raise EvidenceValidationError("retention plan is not bound to a store")
        if not isinstance(self.plan_id, str) or not _PLAN_ID_RE.fullmatch(self.plan_id):
            raise EvidenceValidationError("invalid retention plan id")
        created = _parse_time(self.created_at, "plan created_at")
        cutoff = _parse_time(self.as_of, "plan as_of")
        if cutoff > created:
            raise EvidenceValidationError("retention plan cutoff follows creation")
        if (
            type(self.snapshot_sha256) is not str
            or not _SHA256_RE.fullmatch(self.snapshot_sha256)
            or type(self.plan_sha256) is not str
            or not _SHA256_RE.fullmatch(self.plan_sha256)
        ):
            raise EvidenceValidationError("invalid retention plan hash")
        collections = (
            (self.checkpoint_candidates, lambda item: item.checkpoint_id),
            (self.artifact_candidates, lambda item: item.artifact_id),
            (self.audit_candidates, lambda item: item.audit_id),
            (self.orphan_candidates, lambda item: item.storage_key),
        )
        for items, key in collections:
            keys = [key(item) for item in items]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise EvidenceValidationError("retention candidates must be sorted and unique")
        snapshot = {
            "checkpoint_candidates": [item.to_dict() for item in self.checkpoint_candidates],
            "artifact_candidates": [item.to_dict() for item in self.artifact_candidates],
            "audit_candidates": [item.to_dict() for item in self.audit_candidates],
            "orphan_candidates": [item.to_dict() for item in self.orphan_candidates],
        }
        if _digest_json(snapshot) != self.snapshot_sha256:
            raise EvidenceValidationError("retention snapshot hash mismatch")
        payload = self.to_dict()
        supplied = payload.pop("plan_sha256")
        if _digest_json(payload) != supplied:
            raise EvidenceValidationError("retention plan hash mismatch")


@dataclass(frozen=True)
class EvidencePurgeResultV1:
    plan_id: str
    applied_at: str
    checkpoints_deleted: int
    artifacts_purged: int
    audits_deleted: int
    orphan_files_deleted: int
    physical_files_deleted: int
    physical_bytes_released: int
    already_satisfied: int
    audit_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "applied_at": self.applied_at,
            "checkpoints_deleted": self.checkpoints_deleted,
            "artifacts_purged": self.artifacts_purged,
            "audits_deleted": self.audits_deleted,
            "orphan_files_deleted": self.orphan_files_deleted,
            "physical_files_deleted": self.physical_files_deleted,
            "physical_bytes_released": self.physical_bytes_released,
            "already_satisfied": self.already_satisfied,
            "audit_id": self.audit_id,
        }


@dataclass(frozen=True)
class EvidenceDeleteResultV1:
    target_id: str
    target_kind: str
    deleted: bool
    physical_bytes_released: int = 0


@dataclass(frozen=True)
class EvidenceCapacitySnapshotV1:
    checkpoint_rows: int
    artifact_rows: int
    audit_rows: int
    trace_rows: int
    artifact_blob_rows: int
    artifact_files: int
    artifact_bytes: int
    artifact_file_bytes: int
    disk_free_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "checkpoint_rows": self.checkpoint_rows,
            "artifact_rows": self.artifact_rows,
            "audit_rows": self.audit_rows,
            "trace_rows": self.trace_rows,
            "artifact_blob_rows": self.artifact_blob_rows,
            "artifact_files": self.artifact_files,
            "artifact_bytes": self.artifact_bytes,
            "artifact_file_bytes": self.artifact_file_bytes,
            "disk_free_bytes": self.disk_free_bytes,
        }


def artifact_owner_allows_checkpoint(
    artifact_owner: CheckpointOwnerV1,
    checkpoint_owner: CheckpointOwnerV1,
    *,
    role: str,
    checkpoint_stage: str,
) -> bool:
    """Apply the frozen owner/role matrix without granting access by ID alone."""

    if type(artifact_owner) is not CheckpointOwnerV1 or type(checkpoint_owner) is not CheckpointOwnerV1:
        return False
    if (
        artifact_owner.session_key != checkpoint_owner.session_key
        or artifact_owner.identity_key != checkpoint_owner.identity_key
        or artifact_owner.workflow_search_id != checkpoint_owner.workflow_search_id
        or artifact_owner.workflow_task_revision != checkpoint_owner.workflow_task_revision
    ):
        return False
    if role == ARTIFACT_ROLE_SOURCE_PAGE:
        return (
            artifact_owner.scope == SCOPE_WORKFLOW
            and not artifact_owner.search_id
            and not artifact_owner.unit_id
            and not artifact_owner.candidate_generation
            and artifact_owner.task_revision == artifact_owner.workflow_task_revision
        )
    if role in {ARTIFACT_ROLE_QUESTION_CROP, ARTIFACT_ROLE_CROP_OVERLAY}:
        if (
            artifact_owner.scope != SCOPE_WORKFLOW
            or not artifact_owner.unit_id
            or artifact_owner.search_id
            or artifact_owner.candidate_generation
            or artifact_owner.task_revision != artifact_owner.workflow_task_revision
            or artifact_owner.unit_id != checkpoint_owner.unit_id
        ):
            return False
        return checkpoint_owner.scope == SCOPE_CHILD_TASK or checkpoint_stage in {
            STAGE_CROP_PREPARED,
            STAGE_CROP_VALIDATED,
        }
    if role in {ARTIFACT_ROLE_CANDIDATE_IMAGE, ARTIFACT_ROLE_ANSWER_IMAGE}:
        return artifact_owner.scope == SCOPE_CHILD_TASK and artifact_owner == checkpoint_owner
    return False


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceValidationError(f"invalid {name}")
    try:
        return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise EvidenceValidationError(f"invalid {name}") from exc


def _iso(value: datetime) -> str:
    return _aware_utc(value, "time").isoformat()


def _safe_id(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EvidenceValidationError(f"invalid {name}")
    clean = value.strip()
    if allow_empty and not clean:
        return ""
    if not _SAFE_ID_RE.fullmatch(clean):
        raise EvidenceValidationError(f"invalid {name}")
    return clean


def _reason_code(value: object, name: str = "reason_code") -> str:
    if not isinstance(value, str) or not _RESULT_CODE_RE.fullmatch(value):
        raise EvidenceValidationError(f"invalid {name}")
    return value


def _retention_days(retention_class: str, requested: int | None, *, resource: str) -> int:
    try:
        policy = RETENTION_POLICIES[retention_class]
    except KeyError as exc:
        raise EvidenceValidationError("unknown retention class") from exc
    default = policy.checkpoint_default_days if resource == "checkpoint" else policy.artifact_default_days
    maximum = policy.checkpoint_max_days if resource == "checkpoint" else policy.artifact_max_days
    days = default if requested is None else requested
    if type(days) is not int or not 1 <= days <= maximum:
        raise EvidenceValidationError(f"invalid {resource} retention days")
    return days


def _owner_from_dict(value: object) -> CheckpointOwnerV1:
    if not isinstance(value, Mapping) or set(value) != {
        "scope",
        "session_key",
        "identity_key",
        "workflow_search_id",
        "search_id",
        "unit_id",
        "workflow_task_revision",
        "task_revision",
        "candidate_generation",
    }:
        raise EvidenceValidationError("invalid stored evidence owner")
    try:
        return CheckpointOwnerV1(**dict(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("invalid stored evidence owner") from exc


def _producer_from_dict(value: object) -> ProducerVersionV1:
    if not isinstance(value, Mapping):
        raise EvidenceValidationError("invalid stored checkpoint producer")
    try:
        return ProducerVersionV1(**dict(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("invalid stored checkpoint producer") from exc


def _checkpoint_from_payload(value: object) -> IntermediateCheckpointV1:
    if not isinstance(value, Mapping) or value.get("contract") != CHECKPOINT_CONTRACT:
        raise EvidenceValidationError("invalid stored checkpoint payload")
    expected = {
        "contract",
        "schema_version",
        "checkpoint_id",
        "trace_id",
        "request_id",
        "stage",
        "outcome",
        "occurred_at",
        "expires_at",
        "retention_class",
        "owner",
        "producer",
        "input_fingerprint",
        "predecessor_checkpoint_id",
        "failure",
        "result",
        "artifacts",
    }
    if set(value) != expected:
        raise EvidenceValidationError("invalid stored checkpoint payload")
    failure_value = value["failure"]
    links_value = value["artifacts"]
    try:
        failure = None if failure_value is None else CheckpointFailureV1(**dict(failure_value))
        if not isinstance(links_value, list):
            raise TypeError
        links = tuple(ArtifactLinkV1(**dict(item)) for item in links_value)
        return IntermediateCheckpointV1(
            schema_version=value["schema_version"],
            checkpoint_id=value["checkpoint_id"],
            trace_id=value["trace_id"],
            request_id=value["request_id"],
            stage=value["stage"],
            outcome=value["outcome"],
            occurred_at=value["occurred_at"],
            expires_at=value["expires_at"],
            retention_class=value["retention_class"],
            owner=_owner_from_dict(value["owner"]),
            producer=_producer_from_dict(value["producer"]),
            input_fingerprint=value["input_fingerprint"],
            predecessor_checkpoint_id=value["predecessor_checkpoint_id"],
            failure=failure,
            result=value["result"],
            artifacts=links,
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise EvidenceValidationError("invalid stored checkpoint payload") from exc


def _descriptor_from_row(row: sqlite3.Row) -> ArtifactDescriptorV1:
    try:
        return ArtifactDescriptorV1(
            schema_version=row["schema_version"],
            artifact_id=row["artifact_id"],
            owner=_owner_from_dict(json.loads(row["owner_json"])),
            sha256=row["sha256"],
            byte_size=row["byte_size"],
            media_type=row["media_type"],
            width_px=row["width_px"],
            height_px=row["height_px"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            retention_class=row["retention_class"],
            status=row["status"],
            purged_at=row["purged_at"],
            purge_reason=row["purge_reason"],
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError("invalid stored artifact descriptor") from exc


def _checkpoint_semantic_payload(checkpoint: IntermediateCheckpointV1, retention_days: int) -> dict[str, object]:
    payload = checkpoint.to_dict()
    payload.pop("occurred_at")
    payload.pop("expires_at")
    payload["retention_days"] = retention_days
    return payload


def _storage_key(digest: str) -> str:
    return f"blobs/{digest[:2]}/{digest}.bin"


def _absolute_path(value: str | Path) -> Path:
    """Normalize a path without resolving symlinks or reparse points."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _reject_linked_path(path: Path, *, stop: Path | None = None) -> None:
    """Reject links in a lexical path and all existing parent components."""

    current = _absolute_path(path)
    boundary = _absolute_path(stop) if stop is not None else None
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            details = None
        except OSError as exc:
            raise EvidenceMaintenanceError("evidence path metadata is unavailable") from exc
        if details is not None and (
            current.is_symlink()
            or bool(
                getattr(details, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        ):
            raise EvidenceMaintenanceError("evidence path contains a link")
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


def _is_reparse_or_symlink(path: Path) -> bool:
    """Return whether an existing path is a link or Windows reparse point."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _lifecycle_hash(
    *, target_id: str, created_at: str, expires_at: str,
    retention_class: str, retention_days: int, status: str = "",
    purged_at: str = "", purge_reason: str = "",
) -> str:
    return _digest_json({
        "target_id": target_id, "created_at": created_at,
        "expires_at": expires_at, "retention_class": retention_class,
        "retention_days": retention_days, "status": status,
        "purged_at": purged_at, "purge_reason": purge_reason,
    })


def _increment(value: int) -> int:
    return min(MAX_HEALTH_COUNTER, value + 1)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS checkpoint_store_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            trace_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            outcome TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            retention_class TEXT NOT NULL,
            retention_days INTEGER NOT NULL,
            owner_json TEXT NOT NULL,
            session_key TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            workflow_search_id TEXT NOT NULL,
            search_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            producer_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            semantic_sha256 TEXT NOT NULL,
            lifecycle_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS checkpoints_expiry_idx ON checkpoints(expires_at);
        CREATE INDEX IF NOT EXISTS checkpoints_reuse_idx
            ON checkpoints(identity_key, session_key, workflow_search_id, stage, input_fingerprint);
        CREATE INDEX IF NOT EXISTS checkpoints_predecessor_idx
            ON checkpoints(identity_key, session_key, workflow_search_id, search_id, unit_id, outcome, occurred_at);

        CREATE TABLE IF NOT EXISTS artifact_blobs (
            sha256 TEXT PRIMARY KEY,
            byte_size INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            width_px INTEGER NOT NULL,
            height_px INTEGER NOT NULL,
            storage_key TEXT NOT NULL UNIQUE,
            ref_count INTEGER NOT NULL CHECK(ref_count >= 0)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            owner_json TEXT NOT NULL,
            session_key TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            workflow_search_id TEXT NOT NULL,
            search_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            width_px INTEGER NOT NULL,
            height_px INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            retention_class TEXT NOT NULL,
            retention_days INTEGER NOT NULL,
            status TEXT NOT NULL,
            purged_at TEXT NOT NULL,
            purge_reason TEXT NOT NULL,
            logical_key TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            lifecycle_sha256 TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS artifacts_available_logical_idx
            ON artifacts(logical_key) WHERE status = 'available';
        CREATE INDEX IF NOT EXISTS artifacts_expiry_idx ON artifacts(status, expires_at);
        CREATE INDEX IF NOT EXISTS artifacts_blob_idx ON artifacts(status, sha256);

        CREATE TABLE IF NOT EXISTS checkpoint_artifacts (
            checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
            role TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            PRIMARY KEY(checkpoint_id, role, ordinal),
            UNIQUE(checkpoint_id, artifact_id, role, ordinal)
        );
        CREATE INDEX IF NOT EXISTS checkpoint_artifacts_artifact_idx
            ON checkpoint_artifacts(artifact_id);

        CREATE TABLE IF NOT EXISTS evidence_audit (
            audit_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            actor_key TEXT NOT NULL,
            owner_identity_key TEXT NOT NULL,
            target_id TEXT NOT NULL,
            result_code TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            new_expires_at TEXT NOT NULL,
            reason_code TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evidence_audit_expiry_idx ON evidence_audit(expires_at);
        CREATE INDEX IF NOT EXISTS evidence_audit_target_idx ON evidence_audit(target_id, occurred_at);
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO checkpoint_store_meta(key, value) VALUES ('schema_version', ?)",
        (str(CHECKPOINT_STORE_SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO checkpoint_store_meta(key, value) VALUES ('store_id', ?)",
        (f"store_{uuid4().hex}",),
    )
    version = connection.execute(
        "SELECT value FROM checkpoint_store_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None or version[0] != str(CHECKPOINT_STORE_SCHEMA_VERSION):
        raise EvidenceStoreUnavailable("unsupported checkpoint store schema")


def _verify_read_schema(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not tables:
        return False
    required = {
        "checkpoint_store_meta",
        "checkpoints",
        "artifact_blobs",
        "artifacts",
        "checkpoint_artifacts",
        "evidence_audit",
    }
    if not required <= tables:
        raise EvidenceStoreUnavailable("invalid checkpoint store schema")
    version = connection.execute(
        "SELECT value FROM checkpoint_store_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None or version[0] != str(CHECKPOINT_STORE_SCHEMA_VERSION):
        raise EvidenceStoreUnavailable("unsupported checkpoint store schema")
    store_id = connection.execute(
        "SELECT value FROM checkpoint_store_meta WHERE key = 'store_id'"
    ).fetchone()
    if store_id is None or not isinstance(store_id[0], str) or not _SAFE_ID_RE.fullmatch(store_id[0]):
        raise EvidenceStoreUnavailable("checkpoint store identity is invalid")
    required_columns = {
        "checkpoints": {"checkpoint_id", "retention_days", "semantic_sha256", "lifecycle_sha256", "payload_json"},
        "artifacts": {"artifact_id", "retention_days", "logical_key", "storage_key", "lifecycle_sha256", "status"},
        "artifact_blobs": {"sha256", "storage_key", "ref_count", "byte_size"},
        "checkpoint_artifacts": {"checkpoint_id", "artifact_id", "role", "ordinal"},
        "evidence_audit": {"audit_id", "action", "target_id", "expires_at"},
    }
    for table, columns in required_columns.items():
        actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns <= actual:
            raise EvidenceStoreUnavailable("invalid checkpoint store schema")
    return True


def _inspect_image(content: bytes) -> tuple[str, int, int]:
    if not content or len(content) > MAX_ARTIFACT_BYTES:
        raise EvidenceValidationError("invalid artifact byte size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as opened:
                image_format = str(opened.format or "").upper()
                width, height = opened.size
                opened.verify()
            with Image.open(BytesIO(content)) as decoded:
                decoded.seek(0)
                decoded.load()
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise EvidenceValidationError("artifact is not a valid supported image") from exc
    media_type = _MEDIA_BY_FORMAT.get(image_format, "")
    if media_type not in IMAGE_MEDIA_TYPES:
        raise EvidenceValidationError("artifact media type is not supported")
    if not 1 <= width <= 100_000 or not 1 <= height <= 100_000:
        raise EvidenceValidationError("invalid artifact dimensions")
    return media_type, width, height


class SQLiteCheckpointStore:
    """Independent, bounded Checkpoint/Artifact evidence authority."""

    def __init__(
        self,
        path: str | Path,
        *,
        artifact_root: str | Path,
        capacity: EvidenceCapacityPolicyV1,
        trace_db_path: str | Path,
        clock: Callable[[], datetime] = utc_now,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        trace_row_counter: Callable[[Path], int] | None = None,
        sqlite_timeout_seconds: float = 5.0,
    ) -> None:
        if type(capacity) is not EvidenceCapacityPolicyV1:
            raise EvidenceValidationError("capacity must be EvidenceCapacityPolicyV1")
        if not callable(clock) or not callable(disk_usage):
            raise EvidenceValidationError("clock and disk_usage must be callable")
        if trace_row_counter is not None and not callable(trace_row_counter):
            raise EvidenceValidationError("trace_row_counter must be callable")
        if isinstance(sqlite_timeout_seconds, bool) or not isinstance(
            sqlite_timeout_seconds, (int, float)
        ):
            raise EvidenceValidationError("sqlite timeout must be numeric")
        if not 0.05 <= float(sqlite_timeout_seconds) <= 30:
            raise EvidenceValidationError(
                "sqlite timeout must be between 0.05 and 30 seconds"
            )

        # Keep the lexical path.  Resolving here would erase a symlink/reparse
        # point before the store has a chance to reject it.
        self.path = _absolute_path(path)
        self.artifact_root = _absolute_path(artifact_root)
        self.trace_db_path = _absolute_path(trace_db_path)
        try:
            for controlled_path in (self.path, self.artifact_root, self.trace_db_path):
                _reject_linked_path(controlled_path)
        except EvidenceMaintenanceError as exc:
            raise EvidenceValidationError("evidence path contains a link") from exc
        if self.path == self.artifact_root or self.path.is_relative_to(self.artifact_root):
            raise EvidenceValidationError(
                "checkpoint database cannot be inside artifact root"
            )
        if self.trace_db_path == self.artifact_root or self.trace_db_path.is_relative_to(
            self.artifact_root
        ):
            raise EvidenceValidationError("trace database cannot be inside artifact root")
        self.capacity = capacity
        self._clock = clock
        self._disk_usage = disk_usage
        self._trace_row_counter = trace_row_counter
        self._sqlite_timeout_seconds = float(sqlite_timeout_seconds)
        self._lock = RLock()
        self._health_lock = Lock()
        self._health_counts = {
            "capacity_rejections": 0,
            "write_failures": 0,
            "read_rejections": 0,
            "audit_failures": 0,
            "maintenance_failures": 0,
            "physical_delete_failures": 0,
        }
        self._last_failure_kind = ""
        self._last_failure_at = ""
        self._store_id: str | None = None
        # Expiry cleanup changes SQLite state transactionally.  Physical files
        # are removed only after that transaction commits, so a failed write
        # cannot leave a live row pointing at a deleted blob.
        self._pending_physical_cleanup: dict[str, tuple[str, int]] = {}

    def _now(self) -> datetime:
        try:
            return _aware_utc(self._clock(), "trusted clock")
        except EvidenceValidationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EvidenceValidationError("trusted clock failed") from exc

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        _reject_linked_path(self.path)
        existed = _lexists(self.path)
        if existed and not self.path.is_file():
            raise EvidenceStoreUnavailable("checkpoint database is not a regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _reject_linked_path(self.path)
        with closing(
            sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if existed:
                _verify_read_schema(connection)
            else:
                _create_schema(connection)
            meta = connection.execute(
                "SELECT value FROM checkpoint_store_meta WHERE key = 'store_id'"
            ).fetchone()
            if meta is None or not _SAFE_ID_RE.fullmatch(str(meta[0])):
                raise EvidenceStoreUnavailable("checkpoint store identity is invalid")
            self._store_id = str(meta[0])
            connection.execute("PRAGMA journal_mode = WAL")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                self._pending_physical_cleanup.clear()
                raise
            else:
                connection.commit()
                self._flush_pending_physical_cleanup()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection | None]:
        _reject_linked_path(self.path)
        if not _lexists(self.path):
            self._store_id = None
            yield None
            return
        if not self.path.is_file():
            raise EvidenceStoreUnavailable("checkpoint database is not a regular file")
        with closing(
            sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                timeout=self._sqlite_timeout_seconds,
            )
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if not _verify_read_schema(connection):
                # An absent file is represented by ``None`` above.  Once a
                # file exists, an empty or partial database is corruption and
                # must never be treated as an empty store.
                raise EvidenceStoreUnavailable("invalid checkpoint store schema")
            meta = connection.execute(
                "SELECT value FROM checkpoint_store_meta WHERE key = 'store_id'"
            ).fetchone()
            if meta is None or not _SAFE_ID_RE.fullmatch(str(meta[0])):
                raise EvidenceStoreUnavailable("checkpoint store identity is invalid")
            self._store_id = str(meta[0])
            yield connection

    def store_id(self) -> str:
        """Return the persistent database identity without creating the store."""

        with self._lock, self._read_connection() as connection:
            if connection is None:
                return ""
            if not self._store_id:
                raise EvidenceStoreUnavailable("checkpoint store identity is unavailable")
            return self._store_id

    def _storage_path(self, storage_key: str) -> Path:
        if not isinstance(storage_key, str) or not _STORAGE_KEY_RE.fullmatch(storage_key):
            raise EvidenceUnavailableError("invalid artifact storage mapping")
        pure = PurePosixPath(storage_key)
        if pure.is_absolute() or ".." in pure.parts:
            raise EvidenceUnavailableError("invalid artifact storage mapping")
        lexical = self.artifact_root.joinpath(*pure.parts)
        # Check the lexical path before resolving it.  Resolving first would
        # hide an in-root symlink and could make a delete or read operate on a
        # different file than the indexed storage key.
        try:
            _reject_linked_path(lexical, stop=self.artifact_root)
        except EvidenceMaintenanceError as exc:
            raise EvidenceUnavailableError("artifact storage contains a link") from exc
        target = _absolute_path(lexical).resolve(strict=False)
        try:
            _reject_linked_path(lexical, stop=self.artifact_root)
        except EvidenceMaintenanceError as exc:
            raise EvidenceUnavailableError("artifact storage contains a link") from exc
        if not target.is_relative_to(self.artifact_root):
            raise EvidenceUnavailableError("artifact storage escaped controlled root")
        return target

    def _disk_free_bytes(self) -> int:
        try:
            probe = self.artifact_root
            while not probe.exists() and probe.parent != probe:
                probe = probe.parent
            usage = self._disk_usage(probe)
            free = getattr(usage, "free", None)
            if free is None and isinstance(usage, Sequence) and len(usage) >= 3:
                free = usage[2]
        except Exception as exc:  # noqa: BLE001
            raise EvidenceCapacityError("min_free_bytes") from exc
        if type(free) is not int or free < 0:
            raise EvidenceCapacityError("min_free_bytes")
        return free

    def _schedule_physical_cleanup(
        self, digest: str, storage_key: str, byte_size: int
    ) -> None:
        """Remember a zero-reference file for post-commit removal."""

        previous = self._pending_physical_cleanup.get(digest)
        if previous is None:
            self._pending_physical_cleanup[digest] = (storage_key, max(0, byte_size))
        elif previous[0] != storage_key:
            raise EvidenceMaintenanceError("artifact blob storage mapping disagrees")

    def _pending_cleanup_bytes(self) -> int:
        return sum(item[1] for item in self._pending_physical_cleanup.values())

    def _flush_pending_physical_cleanup(self) -> None:
        pending = self._pending_physical_cleanup
        self._pending_physical_cleanup = {}
        if not pending:
            return

        # A new artifact may have reused a digest in the transaction that
        # scheduled the cleanup.  Re-read the committed index before unlinking
        # anything so that such a file is retained.
        live_keys: set[str] = set()
        try:
            with self._read_connection() as connection:
                if connection is not None:
                    live_keys = {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT storage_key FROM artifact_blobs WHERE ref_count > 0"
                        ).fetchall()
                    }
        except Exception as exc:  # noqa: BLE001 - the DB commit already won.
            self._note_failure("physical_delete_failures", type(exc).__name__)
            return

        for _digest, (storage_key, _scheduled_size) in sorted(pending.items()):
            if storage_key in live_keys:
                continue
            try:
                path = self._storage_path(storage_key)
                if not path.exists():
                    continue
                if not path.is_file() or _is_reparse_or_symlink(path):
                    raise EvidenceMaintenanceError(
                        "artifact blob path is not a regular file"
                    )
                path.unlink()
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                self._note_failure("physical_delete_failures", type(exc).__name__)

    def _remove_uncommitted_blob_file(self, path: Path, digest: str) -> None:
        """Remove a blob file written by a transaction that did not commit.

        A failed SQLite transaction cannot roll back the filesystem rename used
        for content-addressed blobs.  Recheck the committed index before
        unlinking so a concurrent writer that adopted the same digest keeps
        the file.  Any cleanup failure remains observable through health and
        is recoverable as an ``unindexed_file`` retention candidate.
        """

        try:
            with self._read_connection() as connection:
                if connection is not None:
                    row = connection.execute(
                        "SELECT ref_count FROM artifact_blobs WHERE sha256 = ?",
                        (digest,),
                    ).fetchone()
                    if row is not None and type(row["ref_count"]) is int and row["ref_count"] > 0:
                        return
            if not path.exists():
                return
            if not path.is_file() or _is_reparse_or_symlink(path):
                raise EvidenceMaintenanceError(
                    "artifact blob path is not a regular file"
                )
            if sha256(path.read_bytes()).hexdigest() != digest:
                return
            path.unlink()
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
            self._note_failure("physical_delete_failures", type(exc).__name__)

    def _trace_rows(self) -> int:
        try:
            if self._trace_row_counter is not None:
                count = self._trace_row_counter(self.trace_db_path)
            else:
                from tiku_shared.trace_events import SQLiteTraceEventStore

                snapshot = SQLiteTraceEventStore(
                    self.trace_db_path,
                    max_rows=self.capacity.max_trace_rows,
                    write_timeout_seconds=min(self._sqlite_timeout_seconds, 5.0),
                ).capacity_snapshot()
                if not isinstance(snapshot, Mapping) or snapshot.get("available") is not True:
                    raise EvidenceCapacityError("max_trace_rows")
                count = snapshot.get("current_rows")
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceCapacityError("max_trace_rows") from exc
        if type(count) is not int or count < 0:
            raise EvidenceCapacityError("max_trace_rows")
        return count

    @staticmethod
    def _counts_locked(connection: sqlite3.Connection) -> dict[str, int]:
        return {
            "checkpoint_rows": int(connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]),
            "artifact_rows": int(
                connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            ),
            "audit_rows": int(connection.execute("SELECT COUNT(*) FROM evidence_audit").fetchone()[0]),
            "artifact_blob_rows": int(connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0]),
            "artifact_bytes": int(connection.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM artifact_blobs"
            ).fetchone()[0]),
        }

    def _note_failure(self, counter: str, kind: str) -> None:
        with self._health_lock:
            if counter in self._health_counts:
                self._health_counts[counter] = _increment(self._health_counts[counter])
            self._last_failure_kind = re.sub(r"[^A-Za-z0-9_]", "_", str(kind)).lower()[:64]
            try:
                value = self._clock()
                self._last_failure_at = _iso(value) if isinstance(value, datetime) else ""
            except Exception:  # noqa: BLE001
                self._last_failure_at = ""

    def health(self) -> dict[str, object]:
        with self._health_lock:
            counters = dict(self._health_counts)
            reasons = tuple(name for name, count in counters.items() if count)
            return {
                "status": "degraded" if reasons else "ok",
                "current_reasons": reasons,
                "counters": counters,
                "pending": 0,
                "queue_capacity": 0,
                "accepting": True,
                "last_failure_code": self._last_failure_kind,
                "last_failure_at": self._last_failure_at,
                **counters,
                "last_failure_kind": self._last_failure_kind,
            }

    def _preflight_locked(
        self,
        connection: sqlite3.Connection,
        *,
        checkpoint_delta: int = 0,
        artifact_delta: int = 0,
        artifact_byte_delta: int = 0,
        artifact_link_count: int = 0,
        audit_kind: str = "evidence",
        now: datetime | None = None,
    ) -> None:
        trusted_now = self._now() if now is None else _aware_utc(now, "trusted clock")

        def evaluate() -> tuple[dict[str, int], tuple[tuple[bool, str], ...]]:
            counts = self._counts_locked(connection)
            free = self._disk_free_bytes() + self._pending_cleanup_bytes()
            gates = (
                (
                    counts["checkpoint_rows"] + checkpoint_delta
                    > self.capacity.max_checkpoint_rows,
                    "max_checkpoint_rows",
                ),
                (
                    counts["artifact_rows"] + artifact_delta
                    > self.capacity.max_artifact_rows,
                    "max_artifact_rows",
                ),
                (
                    counts["artifact_bytes"] + artifact_byte_delta
                    > self.capacity.max_artifact_bytes,
                    "max_artifact_bytes",
                ),
                (
                    artifact_link_count > self.capacity.max_artifacts_per_checkpoint,
                    "max_artifacts_per_checkpoint",
                ),
                (self._trace_rows() >= self.capacity.max_trace_rows, "max_trace_rows"),
                (free < self.capacity.min_free_bytes, "min_free_bytes"),
            )
            if audit_kind == "normal":
                audit_rejected = (
                    counts["audit_rows"] + 1 > self.capacity.max_audit_rows - 1
                )
            elif audit_kind == "auto_purge":
                audit_rejected = (
                    counts["audit_rows"] + 1 > self.capacity.max_audit_rows
                )
            else:
                audit_rejected = counts["audit_rows"] > self.capacity.max_audit_rows - 2
            return counts, gates + ((audit_rejected, "max_audit_rows"),)

        counts, gates = evaluate()
        rejected = [gate for is_rejected, gate in gates if is_rejected]
        # The per-checkpoint link limit is a request invariant; deleting old
        # records cannot make an oversized incoming checkpoint valid.
        cleanup_eligible = [
            gate for gate in rejected if gate != "max_artifacts_per_checkpoint"
        ]
        if cleanup_eligible:
            self._purge_expired_locked(connection, now=trusted_now)
            counts, gates = evaluate()
            rejected = [gate for is_rejected, gate in gates if is_rejected]
        if rejected:
            gate = rejected[0]
            self._note_failure("capacity_rejections", gate)
            raise EvidenceCapacityError(gate)

    def _purge_expired_locked(
        self, connection: sqlite3.Connection, *, now: datetime
    ) -> bool:
        """Delete expired rows and tombstone expired artifacts in the write transaction."""

        cutoff = _iso(now)
        changed = False

        expired_checkpoints = connection.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE expires_at <= ? "
            "ORDER BY checkpoint_id",
            (cutoff,),
        ).fetchall()
        for row in expired_checkpoints:
            connection.execute(
                "DELETE FROM checkpoints WHERE checkpoint_id = ?", (row["checkpoint_id"],)
            )
            changed = True

        expired_artifacts = connection.execute(
            "SELECT * FROM artifacts WHERE status = ? AND expires_at <= ? "
            "ORDER BY artifact_id",
            (ARTIFACT_AVAILABLE, cutoff),
        ).fetchall()
        for row in expired_artifacts:
            purged_at = cutoff
            reason = "expired"
            connection.execute(
                "UPDATE artifacts SET status = ?, purged_at = ?, purge_reason = ?, "
                "lifecycle_sha256 = ? WHERE artifact_id = ?",
                (
                    ARTIFACT_PURGED,
                    purged_at,
                    reason,
                    _lifecycle_hash(
                        target_id=row["artifact_id"],
                        created_at=row["created_at"],
                        expires_at=row["expires_at"],
                        retention_class=row["retention_class"],
                        retention_days=row["retention_days"],
                        status=ARTIFACT_PURGED,
                        purged_at=purged_at,
                        purge_reason=reason,
                    ),
                    row["artifact_id"],
                ),
            )
            blob = connection.execute(
                "SELECT ref_count FROM artifact_blobs WHERE sha256 = ?",
                (row["sha256"],),
            ).fetchone()
            if blob is not None:
                if type(blob["ref_count"]) is not int or blob["ref_count"] < 1:
                    raise EvidenceMaintenanceError("artifact blob reference count disagrees")
                updated = connection.execute(
                    "UPDATE artifact_blobs SET ref_count = ref_count - 1 "
                    "WHERE sha256 = ? AND ref_count > 0",
                    (row["sha256"],),
                )
                if updated.rowcount != 1:
                    raise EvidenceMaintenanceError("artifact blob reference count disagrees")
            changed = True

        # Tombstones with no checkpoint references and zero-reference blobs no
        # longer carry useful evidence.  Remove their index rows now and defer
        # the physical unlink until the transaction has committed.
        tombstones = connection.execute(
            "SELECT a.artifact_id, a.sha256, a.storage_key "
            "FROM artifacts a WHERE a.status = ? "
            "AND NOT EXISTS (SELECT 1 FROM checkpoint_artifacts ca "
            "WHERE ca.artifact_id = a.artifact_id) ORDER BY a.artifact_id",
            (ARTIFACT_PURGED,),
        ).fetchall()
        for row in tombstones:
            connection.execute(
                "DELETE FROM artifacts WHERE artifact_id = ?", (row["artifact_id"],)
            )
            changed = True

        zero_ref_blobs = connection.execute(
            "SELECT * FROM artifact_blobs WHERE ref_count = 0 ORDER BY sha256"
        ).fetchall()
        for row in zero_ref_blobs:
            expected = int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE sha256 = ? AND status = ?",
                    (row["sha256"], ARTIFACT_AVAILABLE),
                ).fetchone()[0]
            )
            if expected != 0:
                raise EvidenceMaintenanceError("artifact blob reference count disagrees")
            self._schedule_blob_cleanup_locked(row)
            connection.execute(
                "DELETE FROM artifact_blobs WHERE sha256 = ? AND ref_count = 0",
                (row["sha256"],),
            )
            changed = True

        deleted_audits = connection.execute(
            "DELETE FROM evidence_audit WHERE expires_at <= ?", (cutoff,)
        )
        if deleted_audits.rowcount:
            changed = True

        if changed:
            self._insert_audit_locked(
                connection,
                action=AUDIT_AUTO_PURGE,
                actor_key="checkpoint_auto_purge",
                owner_identity_key="",
                target_id="capacity_auto_purge",
                result_code="SUCCEEDED",
                now=now,
                reason_code="EXPIRED_CLEANUP",
            )
        return changed

    def _schedule_blob_cleanup_locked(self, row: sqlite3.Row) -> None:
        path = self._storage_path(row["storage_key"])
        if not path.exists():
            self._schedule_physical_cleanup(
                row["sha256"], row["storage_key"], 0
            )
            return
        if not path.is_file() or _is_reparse_or_symlink(path):
            raise EvidenceMaintenanceError("artifact blob path is not a regular file")
        content = path.read_bytes()
        if sha256(content).hexdigest() != row["sha256"]:
            raise EvidenceMaintenanceError("artifact blob content disagrees")
        self._schedule_physical_cleanup(
            row["sha256"], row["storage_key"], path.stat().st_size
        )

    def _insert_audit_locked(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        actor_key: str,
        owner_identity_key: str,
        target_id: str,
        result_code: str,
        now: datetime,
        new_expires_at: str = "",
        reason_code: str = "",
    ) -> EvidenceAuditRecordV1:
        if action not in {
            AUDIT_VIEW_CHECKPOINT, AUDIT_VIEW_ARTIFACT, AUDIT_EXTEND_RETENTION,
            AUDIT_DELETE_EVIDENCE, AUDIT_AUTO_PURGE,
        }:
            raise EvidenceValidationError("invalid audit action")
        actor = _safe_id(actor_key, "actor_key")
        owner_id = _safe_id(owner_identity_key, "owner_identity_key", allow_empty=True)
        target = _safe_id(target_id, "target_id")
        result = _reason_code(result_code, "result_code")
        reason = "" if not reason_code else _reason_code(reason_code)
        if new_expires_at:
            _parse_time(new_expires_at, "new_expires_at")
        if action == AUDIT_AUTO_PURGE:
            count = int(connection.execute("SELECT COUNT(*) FROM evidence_audit").fetchone()[0])
            if count >= self.capacity.max_audit_rows:
                oldest = connection.execute(
                    "SELECT audit_id FROM evidence_audit WHERE action = ? "
                    "ORDER BY occurred_at, audit_id LIMIT 1", (AUDIT_AUTO_PURGE,),
                ).fetchone()
                if oldest is not None:
                    connection.execute("DELETE FROM evidence_audit WHERE audit_id = ?", (oldest[0],))
            count = int(connection.execute("SELECT COUNT(*) FROM evidence_audit").fetchone()[0])
            if count >= self.capacity.max_audit_rows:
                raise EvidenceAuditError("auto-purge audit capacity is unavailable")
        else:
            count = int(connection.execute("SELECT COUNT(*) FROM evidence_audit").fetchone()[0])
            if count >= self.capacity.max_audit_rows - 1:
                raise EvidenceAuditError("evidence audit capacity is unavailable")
        record = EvidenceAuditRecordV1(
            audit_id=f"audit_{uuid4().hex}", action=action, actor_key=actor,
            owner_identity_key=owner_id, target_id=target, result_code=result,
            occurred_at=_iso(now),
            expires_at=_iso(now + timedelta(days=EVIDENCE_AUDIT_RETENTION_DAYS)),
            new_expires_at=new_expires_at, reason_code=reason,
        )
        fields = record.to_dict()
        connection.execute(
            "INSERT INTO evidence_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(fields.values()),
        )
        return record

    @staticmethod
    def _validate_retention_columns(
        connection: sqlite3.Connection,
        *,
        target_id: str,
        created_at: str,
        expires_at: str,
        retention_class: str,
        retention_days: object,
        resource: str,
    ) -> None:
        if type(retention_days) is not int:
            raise EvidenceUnavailableError("stored retention days are invalid")
        policy = RETENTION_POLICIES.get(retention_class)
        if policy is None:
            raise EvidenceUnavailableError("stored retention class is invalid")
        maximum = policy.checkpoint_max_days if resource == "checkpoint" else policy.artifact_max_days
        if not 1 <= retention_days <= maximum:
            raise EvidenceUnavailableError("stored retention days are invalid")
        created = _parse_time(created_at, "created_at")
        expires = _parse_time(expires_at, "expires_at")
        if expires <= created or expires > created + timedelta(days=maximum):
            raise EvidenceUnavailableError("stored retention window is invalid")

    def _descriptor_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ArtifactDescriptorV1:
        descriptor = _descriptor_from_row(row)
        owner_json = _canonical_json(descriptor.owner.to_dict())
        if (
            descriptor.schema_version != row["schema_version"]
            or owner_json != row["owner_json"]
            or descriptor.owner.session_key != row["session_key"]
            or descriptor.owner.identity_key != row["identity_key"]
            or descriptor.owner.workflow_search_id != row["workflow_search_id"]
            or descriptor.owner.search_id != row["search_id"]
            or descriptor.owner.unit_id != row["unit_id"]
            or row["storage_key"] != _storage_key(descriptor.sha256)
            or row["lifecycle_sha256"] != _lifecycle_hash(
                target_id=descriptor.artifact_id,
                created_at=descriptor.created_at,
                expires_at=descriptor.expires_at,
                retention_class=descriptor.retention_class,
                retention_days=row["retention_days"],
                status=descriptor.status,
                purged_at=descriptor.purged_at,
                purge_reason=descriptor.purge_reason,
            )
        ):
            raise EvidenceUnavailableError("stored artifact columns disagree")
        self._validate_retention_columns(
            connection, target_id=descriptor.artifact_id,
            created_at=descriptor.created_at, expires_at=descriptor.expires_at,
            retention_class=descriptor.retention_class,
            retention_days=row["retention_days"], resource="artifact",
        )
        expected_key = _digest_json({
            "owner": descriptor.owner.to_dict(), "sha256": descriptor.sha256,
            "retention_class": descriptor.retention_class,
            "retention_days": row["retention_days"],
        })
        if row["logical_key"] != expected_key:
            raise EvidenceUnavailableError("stored artifact logical key disagrees")
        return descriptor

    @staticmethod
    def _checkpoint_row(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> IntermediateCheckpointV1:
        try:
            checkpoint = _checkpoint_from_payload(json.loads(row["payload_json"]))
        except (json.JSONDecodeError, EvidenceValidationError) as exc:
            raise EvidenceUnavailableError("stored checkpoint cannot be decoded") from exc
        if (
            checkpoint.checkpoint_id != row["checkpoint_id"]
            or checkpoint.schema_version != row["schema_version"]
            or checkpoint.trace_id != row["trace_id"]
            or checkpoint.stage != row["stage"]
            or checkpoint.outcome != row["outcome"]
            or checkpoint.occurred_at != row["occurred_at"]
            or checkpoint.expires_at != row["expires_at"]
            or checkpoint.retention_class != row["retention_class"]
            or checkpoint.input_fingerprint != row["input_fingerprint"]
            or _canonical_json(checkpoint.owner.to_dict()) != row["owner_json"]
            or _canonical_json(checkpoint.producer.to_dict()) != row["producer_json"]
            or row["lifecycle_sha256"] != _lifecycle_hash(
                target_id=checkpoint.checkpoint_id,
                created_at=checkpoint.occurred_at,
                expires_at=checkpoint.expires_at,
                retention_class=checkpoint.retention_class,
                retention_days=row["retention_days"],
            )
        ):
            raise EvidenceUnavailableError("stored checkpoint columns disagree")
        expected = _digest_json(
            _checkpoint_semantic_payload(checkpoint, row["retention_days"])
        )
        if expected != row["semantic_sha256"]:
            raise EvidenceUnavailableError("stored checkpoint semantic hash disagrees")
        SQLiteCheckpointStore._validate_retention_columns(
            connection, target_id=checkpoint.checkpoint_id,
            created_at=checkpoint.occurred_at, expires_at=checkpoint.expires_at,
            retention_class=checkpoint.retention_class,
            retention_days=row["retention_days"], resource="checkpoint",
        )
        linked = {
            (item["artifact_id"], item["role"], item["ordinal"])
            for item in connection.execute(
                "SELECT artifact_id, role, ordinal FROM checkpoint_artifacts "
                "WHERE checkpoint_id = ?",
                (checkpoint.checkpoint_id,),
            ).fetchall()
        }
        payload_links = {
            (item.artifact_id, item.role, item.ordinal)
            for item in checkpoint.artifacts
        }
        if linked != payload_links:
            raise EvidenceUnavailableError("checkpoint artifact links disagree")
        return checkpoint

    def put_checkpoint(
        self,
        checkpoint: IntermediateCheckpointV1,
        *,
        retention_days: int | None = None,
    ) -> IntermediateCheckpointV1:
        if type(checkpoint) is not IntermediateCheckpointV1:
            raise EvidenceValidationError("checkpoint must be IntermediateCheckpointV1")
        days = _retention_days(
            checkpoint.retention_class, retention_days, resource="checkpoint"
        )
        semantic_sha = _digest_json(_checkpoint_semantic_payload(checkpoint, days))
        now = self._now()
        stored = replace(
            checkpoint,
            occurred_at=_iso(now),
            expires_at=_iso(now + timedelta(days=days)),
        )
        owner_json = _canonical_json(stored.owner.to_dict())
        producer_json = _canonical_json(stored.producer.to_dict())
        payload_json = _canonical_json(stored.to_dict())
        try:
            with self._lock, self._write_connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                    (stored.checkpoint_id,),
                ).fetchone()
                if existing is not None:
                    if existing["semantic_sha256"] != semantic_sha:
                        raise EvidenceConflictError(
                            "checkpoint id has different semantics"
                        )
                    committed = self._checkpoint_row(connection, existing)
                    if now >= _parse_time(committed.expires_at, "expires_at"):
                        raise EvidenceExpiredError("checkpoint has expired")
                    return committed
                self._preflight_locked(
                    connection,
                    checkpoint_delta=1,
                    artifact_link_count=len(stored.artifacts),
                    now=now,
                )
                for link in stored.artifacts:
                    artifact_row = connection.execute(
                        "SELECT * FROM artifacts WHERE artifact_id = ?",
                        (link.artifact_id,),
                    ).fetchone()
                    if artifact_row is None:
                        raise EvidenceUnavailableError(
                            "checkpoint artifact does not exist"
                        )
                    descriptor = self._descriptor_row(connection, artifact_row)
                    if (
                        descriptor.status != ARTIFACT_AVAILABLE
                        or now >= _parse_time(descriptor.expires_at, "expires_at")
                    ):
                        raise EvidenceUnavailableError(
                            "checkpoint artifact is unavailable"
                        )
                    if not artifact_owner_allows_checkpoint(
                        descriptor.owner,
                        stored.owner,
                        role=link.role,
                        checkpoint_stage=stored.stage,
                    ):
                        raise EvidenceOwnershipError(
                            "checkpoint artifact owner is incompatible"
                        )
                connection.execute(
                    """
                    INSERT INTO checkpoints(
                        checkpoint_id, schema_version, trace_id, stage, outcome,
                        occurred_at, expires_at, retention_class, retention_days, owner_json,
                        session_key, identity_key, workflow_search_id, search_id,
                        unit_id, producer_json, input_fingerprint, semantic_sha256,
                        lifecycle_sha256, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.checkpoint_id, stored.schema_version, stored.trace_id,
                        stored.stage, stored.outcome, stored.occurred_at,
                        stored.expires_at, stored.retention_class, days, owner_json,
                        stored.owner.session_key, stored.owner.identity_key,
                        stored.owner.workflow_search_id, stored.owner.search_id,
                        stored.owner.unit_id, producer_json, stored.input_fingerprint,
                        semantic_sha,
                        _lifecycle_hash(
                            target_id=stored.checkpoint_id,
                            created_at=stored.occurred_at,
                            expires_at=stored.expires_at,
                            retention_class=stored.retention_class,
                            retention_days=days,
                        ),
                        payload_json,
                    ),
                )
                connection.executemany(
                    "INSERT INTO checkpoint_artifacts(checkpoint_id, artifact_id, role, ordinal) "
                    "VALUES (?, ?, ?, ?)",
                    [(stored.checkpoint_id, link.artifact_id, link.role, link.ordinal)
                     for link in stored.artifacts],
                )
            return stored
        except CheckpointStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("write_failures", type(exc).__name__)
            raise EvidenceStoreUnavailable(
                "checkpoint database is unavailable"
            ) from exc

    def put_artifact(
        self,
        owner: CheckpointOwnerV1,
        content: bytes,
        *,
        retention_class: str,
        retention_days: int | None = None,
        expected_media_type: str = "",
    ) -> ArtifactDescriptorV1:
        if type(owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError(
                "artifact owner must be CheckpointOwnerV1"
            )
        if type(content) is not bytes:
            raise EvidenceValidationError("artifact content must be bytes")
        media_type, width, height = _inspect_image(content)
        if expected_media_type and expected_media_type != media_type:
            raise EvidenceValidationError(
                "artifact media type does not match content"
            )
        days = _retention_days(
            retention_class, retention_days, resource="artifact"
        )
        digest = sha256(content).hexdigest()
        owner_json = _canonical_json(owner.to_dict())
        logical_key = _digest_json(
            {
                "owner": owner.to_dict(),
                "sha256": digest,
                "retention_class": retention_class,
                "retention_days": days,
            }
        )
        storage_key = _storage_key(digest)
        now = self._now()
        created_blob_path: Path | None = None
        try:
            with self._lock, self._write_connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE logical_key = ? AND status = ?",
                    (logical_key, ARTIFACT_AVAILABLE),
                ).fetchone()
                if existing is not None:
                    descriptor = self._descriptor_row(connection, existing)
                    if now >= _parse_time(descriptor.expires_at, "expires_at"):
                        raise EvidenceExpiredError("artifact has expired")
                    path = self._storage_path(existing["storage_key"])
                    if (
                        not path.is_file()
                        or path.stat().st_size != len(content)
                        or sha256(path.read_bytes()).hexdigest() != digest
                    ):
                        raise EvidenceUnavailableError(
                            "artifact bytes are unavailable"
                        )
                    return descriptor
                blob = connection.execute(
                    "SELECT * FROM artifact_blobs WHERE sha256 = ?", (digest,)
                ).fetchone()
                new_blob_bytes = len(content) if blob is None else 0
                self._preflight_locked(
                    connection,
                    artifact_delta=1,
                    artifact_byte_delta=new_blob_bytes,
                    now=now,
                )
                # The preflight may purge an expired last reference to this
                # digest.  Refresh the row and run the byte gate again so a
                # stale preflight result cannot create an Artifact without a
                # corresponding Blob row.
                refreshed_blob = connection.execute(
                    "SELECT * FROM artifact_blobs WHERE sha256 = ?", (digest,)
                ).fetchone()
                refreshed_new_blob_bytes = len(content) if refreshed_blob is None else 0
                if (
                    (blob is None) != (refreshed_blob is None)
                    or refreshed_new_blob_bytes != new_blob_bytes
                ):
                    self._preflight_locked(
                        connection,
                        artifact_delta=1,
                        artifact_byte_delta=refreshed_new_blob_bytes,
                        now=now,
                    )
                blob = refreshed_blob
                if blob is not None and (
                    blob["byte_size"] != len(content)
                    or blob["media_type"] != media_type
                    or blob["width_px"] != width
                    or blob["height_px"] != height
                    or blob["storage_key"] != storage_key
                ):
                    raise EvidenceConflictError("artifact blob metadata conflict")
                if blob is not None:
                    expected_refs = int(connection.execute(
                        "SELECT COUNT(*) FROM artifacts WHERE sha256 = ? AND status = ?",
                        (digest, ARTIFACT_AVAILABLE),
                    ).fetchone()[0])
                    if type(blob["ref_count"]) is not int or blob["ref_count"] != expected_refs:
                        raise EvidenceUnavailableError(
                            "artifact blob reference count disagrees"
                        )
                blob_path = self._storage_path(storage_key)
                existed_before_write = blob_path.exists()
                self._write_blob_file(storage_key, content)
                if not existed_before_write:
                    created_blob_path = blob_path
                if blob is None:
                    connection.execute(
                        "INSERT INTO artifact_blobs(sha256, byte_size, media_type, "
                        "width_px, height_px, storage_key, ref_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (digest, len(content), media_type, width, height, storage_key),
                    )
                else:
                    connection.execute(
                        "UPDATE artifact_blobs SET ref_count = ref_count + 1 "
                        "WHERE sha256 = ?",
                        (digest,),
                    )
                if self._disk_free_bytes() + self._pending_cleanup_bytes() < self.capacity.min_free_bytes:
                    raise EvidenceCapacityError("min_free_bytes")
                descriptor = ArtifactDescriptorV1(
                    artifact_id=new_artifact_id(),
                    owner=owner,
                    sha256=digest,
                    byte_size=len(content),
                    media_type=media_type,
                    width_px=width,
                    height_px=height,
                    created_at=_iso(now),
                    expires_at=_iso(now + timedelta(days=days)),
                    retention_class=retention_class,
                )
                connection.execute(
                    """
                        INSERT INTO artifacts(
                        artifact_id, schema_version, owner_json, session_key,
                        identity_key, workflow_search_id, search_id, unit_id,
                        sha256, byte_size, media_type, width_px, height_px,
                        created_at, expires_at, retention_class, retention_days,
                        status, purged_at, purge_reason, logical_key, storage_key,
                        lifecycle_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        descriptor.artifact_id, descriptor.schema_version,
                        owner_json, owner.session_key, owner.identity_key,
                        owner.workflow_search_id, owner.search_id, owner.unit_id,
                        descriptor.sha256, descriptor.byte_size,
                        descriptor.media_type, descriptor.width_px,
                        descriptor.height_px, descriptor.created_at,
                        descriptor.expires_at, descriptor.retention_class, days,
                        descriptor.status, descriptor.purged_at,
                        descriptor.purge_reason, logical_key, storage_key,
                        _lifecycle_hash(
                            target_id=descriptor.artifact_id,
                            created_at=descriptor.created_at,
                            expires_at=descriptor.expires_at,
                            retention_class=descriptor.retention_class,
                            retention_days=days,
                            status=descriptor.status,
                            purged_at=descriptor.purged_at,
                            purge_reason=descriptor.purge_reason,
                        ),
                    ),
                )
            return descriptor
        except CheckpointStoreError:
            if created_blob_path is not None:
                self._remove_uncommitted_blob_file(created_blob_path, digest)
            raise
        except (OSError, sqlite3.Error) as exc:
            if created_blob_path is not None:
                self._remove_uncommitted_blob_file(created_blob_path, digest)
            self._note_failure("write_failures", type(exc).__name__)
            raise EvidenceStoreUnavailable("artifact store is unavailable") from exc

    def latest_successful_checkpoint(
        self, owner: CheckpointOwnerV1, *, actor_key: str,
    ) -> IntermediateCheckpointV1 | None:
        """Find a bounded, audited predecessor within the same logical task revision."""
        if type(owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError("owner must be CheckpointOwnerV1")
        _safe_id(actor_key, "actor_key")
        now = self._now()
        selected = None
        with self._lock, self._read_connection() as connection:
            if connection is None:
                return None
            rows = connection.execute(
                "SELECT * FROM checkpoints WHERE identity_key = ? AND session_key = ? "
                "AND workflow_search_id = ? AND search_id = ? AND unit_id = ? "
                "AND outcome = 'success' ORDER BY occurred_at DESC, rowid DESC LIMIT 100",
                (owner.identity_key, owner.session_key, owner.workflow_search_id, owner.search_id, owner.unit_id),
            ).fetchall()
            for row in rows:
                candidate = self._checkpoint_row(connection, row)
                if (
                    candidate.owner.task_revision == owner.task_revision
                    and replace(candidate.owner, candidate_generation=owner.candidate_generation) == owner
                    and now < _parse_time(candidate.expires_at, "expires_at")
                ):
                    selected = candidate
                    break
        if selected is None:
            return None
        return self.read_checkpoint(selected.checkpoint_id, actor_key=actor_key, expected_owner=selected.owner)

    def read_checkpoint(
        self,
        checkpoint_id: str,
        *,
        actor_key: str,
        expected_owner: CheckpointOwnerV1,
    ) -> IntermediateCheckpointV1:
        if not is_valid_checkpoint_id(checkpoint_id):
            raise EvidenceValidationError("invalid checkpoint id")
        if type(expected_owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError("expected_owner must be CheckpointOwnerV1")
        actor = _safe_id(actor_key, "actor_key")
        now = self._now()
        result: IntermediateCheckpointV1 | None = None
        failure: CheckpointStoreError | None = None
        result_code = "NOT_FOUND"
        owner_identity = expected_owner.identity_key
        try:
            with self._lock, self._write_connection() as connection:
                row = connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                ).fetchone()
                if row is None:
                    failure = EvidenceNotFoundError("checkpoint was not found")
                else:
                    try:
                        candidate = self._checkpoint_row(connection, row)
                    except EvidenceUnavailableError as exc:
                        failure = exc
                        result_code = "DECODE_REJECTED"
                    else:
                        owner_identity = candidate.owner.identity_key
                        if candidate.owner != expected_owner:
                            failure = EvidenceOwnershipError(
                                "checkpoint owner does not match"
                            )
                            result_code = "OWNER_REJECTED"
                        elif now >= _parse_time(candidate.expires_at, "expires_at"):
                            failure = EvidenceExpiredError("checkpoint has expired")
                            result_code = "EXPIRED"
                        else:
                            result = candidate
                            result_code = "SUCCEEDED"
                try:
                    self._insert_audit_locked(
                        connection,
                        action=AUDIT_VIEW_CHECKPOINT,
                        actor_key=actor,
                        owner_identity_key=owner_identity,
                        target_id=checkpoint_id,
                        result_code=result_code,
                        now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._note_failure("audit_failures", type(exc).__name__)
                    raise EvidenceAuditError(
                        "checkpoint view audit is unavailable"
                    ) from exc
        except CheckpointStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("audit_failures", type(exc).__name__)
            raise EvidenceAuditError("checkpoint view audit is unavailable") from exc
        if failure is not None:
            self._note_failure("read_rejections", type(failure).__name__)
            raise failure
        if result is None:
            raise EvidenceUnavailableError("checkpoint read failed")
        return result

    get_checkpoint = read_checkpoint

    def _available_artifact_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> tuple[ArtifactDescriptorV1, bytes]:
        descriptor = self._descriptor_row(connection, row)
        if descriptor.status != ARTIFACT_AVAILABLE:
            raise EvidenceUnavailableError("artifact is not available")
        if now >= _parse_time(descriptor.expires_at, "expires_at"):
            raise EvidenceExpiredError("artifact has expired")
        blob = connection.execute(
            "SELECT * FROM artifact_blobs WHERE sha256 = ?", (descriptor.sha256,)
        ).fetchone()
        if blob is None or any(
            blob[name] != row[name]
            for name in ("byte_size", "media_type", "width_px", "height_px", "storage_key")
        ):
            raise EvidenceUnavailableError("artifact blob metadata disagrees")
        expected_refs = int(connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE sha256 = ? AND status = ?",
            (descriptor.sha256, ARTIFACT_AVAILABLE),
        ).fetchone()[0])
        if blob["ref_count"] != expected_refs or expected_refs < 1:
            raise EvidenceUnavailableError("artifact blob reference count disagrees")
        path = self._storage_path(row["storage_key"])
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise EvidenceUnavailableError("artifact bytes are unavailable") from exc
        if len(content) != descriptor.byte_size or sha256(content).hexdigest() != descriptor.sha256:
            raise EvidenceUnavailableError("artifact content hash disagrees")
        media_type, width, height = _inspect_image(content)
        if (media_type, width, height) != (
            descriptor.media_type, descriptor.width_px, descriptor.height_px
        ):
            raise EvidenceUnavailableError("artifact decoded metadata disagrees")
        return descriptor, content

    def read_artifact(
        self,
        artifact_id: str,
        *,
        checkpoint_id: str,
        actor_key: str,
        expected_checkpoint_owner: CheckpointOwnerV1,
    ) -> StoredArtifactV1:
        if not is_valid_artifact_id(artifact_id) or not is_valid_checkpoint_id(checkpoint_id):
            raise EvidenceValidationError("invalid evidence id")
        if type(expected_checkpoint_owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError(
                "expected_checkpoint_owner must be CheckpointOwnerV1"
            )
        actor = _safe_id(actor_key, "actor_key")
        now = self._now()
        result: StoredArtifactV1 | None = None
        failure: CheckpointStoreError | None = None
        result_code = "NOT_FOUND"
        owner_identity = expected_checkpoint_owner.identity_key
        try:
            with self._lock, self._write_connection() as connection:
                checkpoint_row = connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
                ).fetchone()
                artifact_row = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()
                link = connection.execute(
                    "SELECT role, ordinal FROM checkpoint_artifacts "
                    "WHERE checkpoint_id = ? AND artifact_id = ?",
                    (checkpoint_id, artifact_id),
                ).fetchone()
                if checkpoint_row is None or artifact_row is None or link is None:
                    failure = EvidenceNotFoundError("artifact link was not found")
                else:
                    try:
                        checkpoint = self._checkpoint_row(connection, checkpoint_row)
                        descriptor, content = self._available_artifact_from_row(
                            connection, artifact_row, now
                        )
                    except EvidenceExpiredError as exc:
                        failure, result_code = exc, "EXPIRED"
                    except (EvidenceUnavailableError, EvidenceValidationError) as exc:
                        failure = EvidenceUnavailableError("artifact evidence is unavailable")
                        result_code = "DECODE_REJECTED"
                    else:
                        owner_identity = descriptor.owner.identity_key
                        if checkpoint.owner != expected_checkpoint_owner:
                            failure = EvidenceOwnershipError("checkpoint owner does not match")
                            result_code = "OWNER_REJECTED"
                        elif now >= _parse_time(checkpoint.expires_at, "expires_at"):
                            failure = EvidenceExpiredError("checkpoint has expired")
                            result_code = "EXPIRED"
                        elif not artifact_owner_allows_checkpoint(
                            descriptor.owner, checkpoint.owner,
                            role=link["role"], checkpoint_stage=checkpoint.stage,
                        ):
                            failure = EvidenceOwnershipError("artifact owner is incompatible")
                            result_code = "OWNER_REJECTED"
                        else:
                            result = StoredArtifactV1(descriptor=descriptor, content=content)
                            result_code = "SUCCEEDED"
                try:
                    self._insert_audit_locked(
                        connection, action=AUDIT_VIEW_ARTIFACT, actor_key=actor,
                        owner_identity_key=owner_identity, target_id=artifact_id,
                        result_code=result_code, now=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._note_failure("audit_failures", type(exc).__name__)
                    raise EvidenceAuditError("artifact view audit is unavailable") from exc
        except CheckpointStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("audit_failures", type(exc).__name__)
            raise EvidenceAuditError("artifact view audit is unavailable") from exc
        if failure is not None:
            self._note_failure("read_rejections", type(failure).__name__)
            raise failure
        if result is None:
            raise EvidenceUnavailableError("artifact read failed")
        return result

    get_artifact = read_artifact

    def extend_retention(
        self,
        target_id: str,
        *,
        actor_key: str,
        expected_owner: CheckpointOwnerV1,
        new_expires_at: datetime,
        reason_code: str,
    ) -> IntermediateCheckpointV1 | ArtifactDescriptorV1:
        actor = _safe_id(actor_key, "actor_key")
        reason = _reason_code(reason_code)
        if type(expected_owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError("expected_owner must be CheckpointOwnerV1")
        new_expiry = _aware_utc(new_expires_at, "new_expires_at")
        now = self._now()
        if new_expiry <= now:
            raise EvidenceValidationError("new expiry must be in the future")
        result: IntermediateCheckpointV1 | ArtifactDescriptorV1
        try:
            with self._lock, self._write_connection() as connection:
                if is_valid_checkpoint_id(target_id):
                    row = connection.execute(
                        "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise EvidenceNotFoundError("checkpoint was not found")
                    current = self._checkpoint_row(connection, row)
                    resource = "checkpoint"
                    retention_days = row["retention_days"]
                elif is_valid_artifact_id(target_id):
                    row = connection.execute(
                        "SELECT * FROM artifacts WHERE artifact_id = ?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise EvidenceNotFoundError("artifact was not found")
                    current, _ = self._available_artifact_from_row(connection, row, now)
                    resource = "artifact"
                    retention_days = row["retention_days"]
                else:
                    raise EvidenceValidationError("invalid evidence id")
                if current.owner != expected_owner:
                    raise EvidenceOwnershipError("evidence owner does not match")
                current_expiry = _parse_time(current.expires_at, "expires_at")
                if now >= current_expiry:
                    raise EvidenceExpiredError("evidence has expired")
                if new_expiry <= current_expiry:
                    raise EvidenceValidationError("new expiry must extend retention")
                policy = RETENTION_POLICIES[current.retention_class]
                maximum_days = (
                    policy.checkpoint_max_days
                    if resource == "checkpoint"
                    else policy.artifact_max_days
                )
                created = _parse_time(current.occurred_at, "occurred_at") if resource == "checkpoint" else _parse_time(current.created_at, "created_at")
                if new_expiry > created + timedelta(days=maximum_days):
                    raise EvidenceValidationError("new expiry exceeds retention maximum")
                updated = replace(current, expires_at=_iso(new_expiry))
                self._insert_audit_locked(
                    connection, action=AUDIT_EXTEND_RETENTION, actor_key=actor,
                    owner_identity_key=current.owner.identity_key,
                    target_id=target_id, result_code="SUCCEEDED", now=now,
                    new_expires_at=_iso(new_expiry), reason_code=reason,
                )
                if resource == "checkpoint":
                    updated_payload = _canonical_json(updated.to_dict())
                    connection.execute(
                        "UPDATE checkpoints SET expires_at = ?, lifecycle_sha256 = ?, "
                        "payload_json = ? "
                        "WHERE checkpoint_id = ?",
                        (
                            _iso(new_expiry),
                            _lifecycle_hash(
                                target_id=updated.checkpoint_id,
                                created_at=updated.occurred_at,
                                expires_at=updated.expires_at,
                                retention_class=updated.retention_class,
                                retention_days=retention_days,
                            ),
                            updated_payload,
                            target_id,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE artifacts SET expires_at = ?, lifecycle_sha256 = ? "
                        "WHERE artifact_id = ?",
                        (
                            _iso(new_expiry),
                            _lifecycle_hash(
                                target_id=current.artifact_id,
                                created_at=current.created_at,
                                expires_at=updated.expires_at,
                                retention_class=current.retention_class,
                                retention_days=retention_days,
                                status=current.status,
                                purged_at=current.purged_at,
                                purge_reason=current.purge_reason,
                            ),
                            target_id,
                        ),
                    )
                result = updated
            return result
        except EvidenceAuditError:
            self._note_failure("audit_failures", "evidence_audit_error")
            raise
        except CheckpointStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("audit_failures", type(exc).__name__)
            raise EvidenceAuditError("retention audit is unavailable") from exc

    def _release_zero_ref_blob(self, digest: str) -> int:
        storage_key = ""
        expected_size = 0
        blob_metadata: dict[str, object] = {}
        index_deleted = False
        try:
            with self._lock:
                # Remove the index row in its own committed transaction.  A
                # filesystem unlink before this commit could leave a live
                # index pointing at missing bytes when SQLite rolls back.
                with self._write_connection() as connection:
                    blob = connection.execute(
                        "SELECT * FROM artifact_blobs WHERE sha256 = ?", (digest,)
                    ).fetchone()
                    if blob is None or blob["ref_count"] != 0:
                        return 0
                    storage_key = str(blob["storage_key"])
                    expected_size = int(blob["byte_size"])
                    blob_metadata = {
                        "sha256": str(blob["sha256"]),
                        "byte_size": int(blob["byte_size"]),
                        "media_type": str(blob["media_type"]),
                        "width_px": int(blob["width_px"]),
                        "height_px": int(blob["height_px"]),
                        "storage_key": storage_key,
                    }
                    deleted = connection.execute(
                        "DELETE FROM artifact_blobs WHERE sha256 = ? AND ref_count = 0",
                        (digest,),
                    )
                    if deleted.rowcount != 1:
                        raise EvidenceMaintenanceError(
                            "artifact blob reference count disagrees"
                        )
                    index_deleted = True

                path = self._storage_path(storage_key)
                if not _lexists(path):
                    return 0
                if not path.is_file() or _is_reparse_or_symlink(path):
                    raise EvidenceMaintenanceError(
                        "artifact blob path is not a regular file"
                    )
                content = path.read_bytes()
                if len(content) != expected_size or sha256(content).hexdigest() != digest:
                    raise EvidenceMaintenanceError("artifact blob content disagrees")
                actual_size = path.stat().st_size

                # A writer may have adopted the digest after the index delete.
                # Keep the file if it is live again; the committed index is the
                # authority for this decision.
                with self._read_connection() as connection:
                    if connection is not None:
                        live = connection.execute(
                            "SELECT ref_count FROM artifact_blobs WHERE sha256 = ?",
                            (digest,),
                        ).fetchone()
                        if live is not None and live["ref_count"] > 0:
                            return 0
                try:
                    path.unlink()
                except FileNotFoundError:
                    return 0
                return actual_size
        except (OSError, sqlite3.Error, CheckpointStoreError) as exc:
            if index_deleted and blob_metadata:
                # Preserve a zero-ref index when the physical unlink failed.
                # A later retry of the same frozen retention plan can then
                # find and release the blob instead of publishing a terminal
                # result with an untracked file left behind.
                try:
                    with self._lock, self._write_connection() as connection:
                        live = connection.execute(
                            "SELECT ref_count FROM artifact_blobs WHERE sha256 = ?",
                            (digest,),
                        ).fetchone()
                        if live is None:
                            refs = int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM artifacts "
                                    "WHERE sha256 = ? AND status = ?",
                                    (digest, ARTIFACT_AVAILABLE),
                                ).fetchone()[0]
                            )
                            connection.execute(
                                "INSERT INTO artifact_blobs "
                                "(sha256, byte_size, media_type, width_px, height_px, storage_key, ref_count) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (
                                    blob_metadata["sha256"],
                                    blob_metadata["byte_size"],
                                    blob_metadata["media_type"],
                                    blob_metadata["width_px"],
                                    blob_metadata["height_px"],
                                    blob_metadata["storage_key"],
                                    refs,
                                ),
                            )
                except Exception as restore_exc:  # noqa: BLE001
                    self._note_failure(
                        "physical_delete_failures", type(restore_exc).__name__
                    )
            self._note_failure("physical_delete_failures", type(exc).__name__)
            if isinstance(exc, CheckpointStoreError):
                raise
            raise EvidenceMaintenanceError("artifact blob release failed") from exc

    def delete_evidence(
        self,
        target_id: str,
        *,
        actor_key: str,
        expected_owner: CheckpointOwnerV1,
        reason_code: str,
    ) -> EvidenceDeleteResultV1:
        actor = _safe_id(actor_key, "actor_key")
        reason = _reason_code(reason_code)
        if type(expected_owner) is not CheckpointOwnerV1:
            raise EvidenceValidationError("expected_owner must be CheckpointOwnerV1")
        now = self._now()
        digest_to_release = ""
        result: EvidenceDeleteResultV1
        try:
            with self._lock, self._write_connection() as connection:
                if is_valid_checkpoint_id(target_id):
                    row = connection.execute(
                        "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise EvidenceNotFoundError("checkpoint was not found")
                    checkpoint = self._checkpoint_row(connection, row)
                    if checkpoint.owner != expected_owner:
                        raise EvidenceOwnershipError("evidence owner does not match")
                    self._insert_audit_locked(
                        connection, action=AUDIT_DELETE_EVIDENCE, actor_key=actor,
                        owner_identity_key=checkpoint.owner.identity_key,
                        target_id=target_id, result_code="SUCCEEDED", now=now,
                        reason_code=reason,
                    )
                    connection.execute(
                        "DELETE FROM checkpoints WHERE checkpoint_id = ?", (target_id,)
                    )
                    result = EvidenceDeleteResultV1(target_id, "checkpoint", True)
                elif is_valid_artifact_id(target_id):
                    row = connection.execute(
                        "SELECT * FROM artifacts WHERE artifact_id = ?", (target_id,)
                    ).fetchone()
                    if row is None:
                        raise EvidenceNotFoundError("artifact was not found")
                    descriptor = self._descriptor_row(connection, row)
                    if descriptor.owner != expected_owner:
                        raise EvidenceOwnershipError("evidence owner does not match")
                    audit_result = (
                        "ALREADY_PURGED"
                        if descriptor.status == ARTIFACT_PURGED
                        else "SUCCEEDED"
                    )
                    self._insert_audit_locked(
                        connection, action=AUDIT_DELETE_EVIDENCE, actor_key=actor,
                        owner_identity_key=descriptor.owner.identity_key,
                        target_id=target_id, result_code=audit_result, now=now,
                        reason_code=reason,
                    )
                    if descriptor.status == ARTIFACT_PURGED:
                        result = EvidenceDeleteResultV1(target_id, "artifact", False)
                    else:
                        digest_to_release = descriptor.sha256
                        purged_at = _iso(now)
                        purge_reason = "user_deleted"
                        connection.execute(
                            "UPDATE artifacts SET status = ?, purged_at = ?, purge_reason = ?, "
                            "lifecycle_sha256 = ? "
                            "WHERE artifact_id = ?",
                            (
                                ARTIFACT_PURGED,
                                purged_at,
                                purge_reason,
                                _lifecycle_hash(
                                    target_id=descriptor.artifact_id,
                                    created_at=descriptor.created_at,
                                    expires_at=descriptor.expires_at,
                                    retention_class=descriptor.retention_class,
                                    retention_days=row["retention_days"],
                                    status=ARTIFACT_PURGED,
                                    purged_at=purged_at,
                                    purge_reason=purge_reason,
                                ),
                                target_id,
                            ),
                        )
                        updated = connection.execute(
                            "UPDATE artifact_blobs SET ref_count = ref_count - 1 "
                            "WHERE sha256 = ? AND ref_count > 0",
                            (digest_to_release,),
                        )
                        if updated.rowcount != 1:
                            raise EvidenceUnavailableError(
                                "artifact blob reference count disagrees"
                            )
                        result = EvidenceDeleteResultV1(target_id, "artifact", True)
                else:
                    raise EvidenceValidationError("invalid evidence id")
            if digest_to_release:
                released = self._release_zero_ref_blob(digest_to_release)
                result = replace(result, physical_bytes_released=released)
            return result
        except EvidenceAuditError:
            self._note_failure("audit_failures", "evidence_audit_error")
            raise
        except CheckpointStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("write_failures", type(exc).__name__)
            raise EvidenceStoreUnavailable("evidence delete is unavailable") from exc

    def _managed_file_rows(self) -> list[tuple[str, Path, int, str]]:
        _reject_linked_path(self.artifact_root)
        if not _lexists(self.artifact_root):
            return []
        if (
            not self.artifact_root.is_dir()
            or _is_reparse_or_symlink(self.artifact_root)
        ):
            raise EvidenceMaintenanceError("artifact root is not a controlled directory")
        rows: list[tuple[str, Path, int, str]] = []
        try:
            entries = self.artifact_root.rglob("*")
            for path in entries:
                if _is_reparse_or_symlink(path):
                    raise EvidenceMaintenanceError(
                        "artifact root contains an unsafe entry"
                    )
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise EvidenceMaintenanceError(
                        "artifact root contains an unsafe entry"
                    )
                # ``path`` has already passed the lexical link check.  Keep
                # that check in place and only then resolve for the read.
                _reject_linked_path(path, stop=self.artifact_root)
                resolved = _absolute_path(path).resolve(strict=True)
                if not resolved.is_relative_to(self.artifact_root):
                    raise EvidenceMaintenanceError(
                        "artifact file escaped controlled root"
                    )
                key = resolved.relative_to(self.artifact_root).as_posix()
                if not _STORAGE_KEY_RE.fullmatch(key):
                    raise EvidenceMaintenanceError(
                        "artifact root contains an unknown file"
                    )
                content = resolved.read_bytes()
                rows.append((key, resolved, len(content), sha256(content).hexdigest()))
        except EvidenceMaintenanceError:
            raise
        except (OSError, RuntimeError) as exc:
            raise EvidenceMaintenanceError(
                "artifact root cannot be inspected"
            ) from exc
        return sorted(rows, key=lambda item: item[0])

    def capacity_snapshot(self) -> EvidenceCapacitySnapshotV1:
        with self._lock:
            try:
                with self._read_connection() as connection:
                    counts = (
                        {"checkpoint_rows": 0, "artifact_rows": 0, "audit_rows": 0,
                         "artifact_blob_rows": 0, "artifact_bytes": 0}
                        if connection is None else self._counts_locked(connection)
                    )
                files = self._managed_file_rows()
                return EvidenceCapacitySnapshotV1(
                    checkpoint_rows=counts["checkpoint_rows"],
                    artifact_rows=counts["artifact_rows"],
                    audit_rows=counts["audit_rows"],
                    trace_rows=self._trace_rows(),
                    artifact_blob_rows=counts["artifact_blob_rows"],
                    artifact_files=len(files),
                    artifact_bytes=counts["artifact_bytes"],
                    artifact_file_bytes=sum(item[2] for item in files),
                    disk_free_bytes=self._disk_free_bytes(),
                )
            except CheckpointStoreError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise EvidenceStoreUnavailable("capacity snapshot is unavailable") from exc

    def plan_retention(
        self, *, as_of: datetime | None = None
    ) -> EvidenceRetentionPlanV1:
        now = self._now()
        cutoff = now if as_of is None else _aware_utc(as_of, "as_of")
        if cutoff > now:
            raise EvidenceValidationError("retention as_of cannot be in the future")
        cutoff_iso = _iso(cutoff)
        checkpoints: list[CheckpointRetentionCandidateV1] = []
        artifacts: dict[str, ArtifactRetentionCandidateV1] = {}
        audits: list[AuditRetentionCandidateV1] = []
        orphans: dict[str, OrphanRetentionCandidateV1] = {}
        with self._lock:
            try:
                files = {item[0]: item for item in self._managed_file_rows()}
                blob_rows: dict[str, sqlite3.Row] = {}
                with self._read_connection() as connection:
                    if connection is not None:
                        checkpoints = [
                            CheckpointRetentionCandidateV1(
                                row["checkpoint_id"], row["expires_at"], row["semantic_sha256"]
                            )
                            for row in connection.execute(
                                "SELECT checkpoint_id, expires_at, semantic_sha256 FROM checkpoints "
                                "WHERE expires_at <= ? ORDER BY checkpoint_id", (cutoff_iso,)
                            )
                        ]
                        artifact_rows = connection.execute(
                            "SELECT a.*, (SELECT COUNT(*) FROM checkpoint_artifacts ca "
                            "WHERE ca.artifact_id = a.artifact_id) AS checkpoint_refs "
                            "FROM artifacts a ORDER BY a.artifact_id"
                        ).fetchall()
                        blob_rows = {
                            row["storage_key"]: row
                            for row in connection.execute("SELECT * FROM artifact_blobs").fetchall()
                        }
                        for row in artifact_rows:
                            reason = ""
                            # Physical absence takes precedence over TTL: a
                            # missing blob cannot be backed up and must be
                            # reported as the logical ``missing`` candidate.
                            if row["status"] == ARTIFACT_AVAILABLE and (
                                row["storage_key"] not in blob_rows or row["storage_key"] not in files
                            ):
                                reason = "missing"
                            elif row["status"] == ARTIFACT_PURGED and row["checkpoint_refs"] == 0:
                                reason = "tombstone"
                            elif row["status"] == ARTIFACT_AVAILABLE and row["expires_at"] <= cutoff_iso:
                                reason = "expired"
                            if reason:
                                artifacts[row["artifact_id"]] = ArtifactRetentionCandidateV1(
                                    artifact_id=row["artifact_id"], expires_at=row["expires_at"],
                                    status=row["status"], storage_key=row["storage_key"],
                                    sha256=row["sha256"], byte_size=row["byte_size"], reason=reason,
                                )
                        audits = [
                            AuditRetentionCandidateV1(row["audit_id"], row["expires_at"])
                            for row in connection.execute(
                                "SELECT audit_id, expires_at FROM evidence_audit "
                                "WHERE expires_at <= ? ORDER BY audit_id", (cutoff_iso,)
                            )
                        ]
                        for key, row in blob_rows.items():
                            expected_refs = int(connection.execute(
                                "SELECT COUNT(*) FROM artifacts WHERE sha256 = ? AND status = ?",
                                (row["sha256"], ARTIFACT_AVAILABLE),
                            ).fetchone()[0])
                            if row["ref_count"] != expected_refs:
                                orphans[key] = OrphanRetentionCandidateV1(
                                    key, row["sha256"], row["byte_size"],
                                    "refcount_mismatch", file_present=key in files,
                                )
                            elif expected_refs == 0:
                                orphans[key] = OrphanRetentionCandidateV1(
                                    key, row["sha256"], row["byte_size"],
                                    "zero_ref_blob", file_present=key in files,
                                )
                # Physical files remain in scope even when the SQLite store is
                # absent.  This lets a retention run clean a failed blob write
                # without first creating a new evidence database.
                for key, (_, _path, size, digest) in files.items():
                    if key not in blob_rows:
                        orphans[key] = OrphanRetentionCandidateV1(
                            key, digest, size, "unindexed_file"
                        )
            except CheckpointStoreError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise EvidenceMaintenanceError("retention plan is unavailable") from exc
        snapshot = {
            "checkpoint_candidates": [item.to_dict() for item in checkpoints],
            "artifact_candidates": [item.to_dict() for item in artifacts.values()],
            "audit_candidates": [item.to_dict() for item in audits],
            "orphan_candidates": [item.to_dict() for item in orphans.values()],
        }
        base: dict[str, object] = {
            "schema_version": CHECKPOINT_STORE_SCHEMA_VERSION,
            "store_id": self._store_id or "",
            "plan_id": f"rplan_{uuid4().hex}", "created_at": _iso(now),
            "as_of": cutoff_iso,
            **snapshot,
            "snapshot_sha256": _digest_json(snapshot),
        }
        base["plan_sha256"] = _digest_json(base)
        return EvidenceRetentionPlanV1.from_dict(base)

    def retention_plan_satisfied(self, plan: EvidenceRetentionPlanV1) -> bool:
        """Return whether every database/physical target in a plan is gone."""

        if type(plan) is not EvidenceRetentionPlanV1:
            raise EvidenceValidationError("plan must be EvidenceRetentionPlanV1")
        plan._validate()
        with self._lock:
            try:
                _reject_linked_path(self.path)
                orphan_blob_present: dict[str, bool] = {}
                has_database = _lexists(self.path)
                with self._read_connection() as connection:
                    if connection is None:
                        # A plan captured without a database is valid only for
                        # physical unindexed files.  Keep checking those files
                        # below; an absent database alone is not sufficient.
                        if (
                            has_database
                            or plan.store_id
                            or plan.checkpoint_candidates
                            or plan.artifact_candidates
                            or plan.audit_candidates
                            or any(
                                item.reason != "unindexed_file"
                                for item in plan.orphan_candidates
                            )
                        ):
                            return False
                    else:
                        # If the plan was captured while the database was
                        # absent, a newly appearing database is drift even if
                        # the plan contains only physical candidates.
                        if not plan.store_id:
                            return False
                        if plan.store_id and self._store_id != plan.store_id:
                            return False
                        for candidate in plan.checkpoint_candidates:
                            if connection.execute(
                                "SELECT 1 FROM checkpoints WHERE checkpoint_id = ?",
                                (candidate.checkpoint_id,),
                            ).fetchone() is not None:
                                return False
                        for candidate in plan.artifact_candidates:
                            row = connection.execute(
                                "SELECT * FROM artifacts WHERE artifact_id = ?",
                                (candidate.artifact_id,),
                            ).fetchone()
                            if row is None:
                                # The logical row may have been removed by a
                                # concurrent/manual cleanup after the plan was
                                # frozen.  That is only satisfied when the
                                # candidate's blob is also gone (or is still
                                # validly shared by another live artifact).
                                if not self._artifact_candidate_physical_state_satisfied_locked(
                                    connection, candidate
                                ):
                                    return False
                                continue
                            # An expired or physically missing artifact is
                            # intentionally retained as an inaccessible
                            # tombstone after logical purge.  Tombstone
                            # candidates themselves must be fully removed.
                            if candidate.reason == "tombstone":
                                return False
                            try:
                                descriptor = self._descriptor_row(connection, row)
                            except CheckpointStoreError:
                                return False
                            if (
                                candidate.reason not in {"expired", "missing"}
                                or candidate.status != ARTIFACT_AVAILABLE
                                or descriptor.status != ARTIFACT_PURGED
                                or descriptor.purge_reason != candidate.reason
                                or descriptor.expires_at != candidate.expires_at
                                or descriptor.sha256 != candidate.sha256
                                or descriptor.byte_size != candidate.byte_size
                                or row["storage_key"] != candidate.storage_key
                            ):
                                return False
                            # A logical purge is complete only when a blob
                            # with no remaining available references has also
                            # been physically removed.  This catches an
                            # unlink failure even when the original plan did
                            # not contain a separate zero-ref orphan entry.
                            if not self._artifact_candidate_physical_state_satisfied_locked(
                                connection, candidate
                            ):
                                return False
                        for candidate in plan.audit_candidates:
                            if connection.execute(
                                "SELECT 1 FROM evidence_audit WHERE audit_id = ?",
                                (candidate.audit_id,),
                            ).fetchone() is not None:
                                return False
                        for candidate in plan.orphan_candidates:
                            blob = connection.execute(
                                "SELECT * FROM artifact_blobs WHERE storage_key = ?",
                                (candidate.storage_key,),
                            ).fetchone()
                            expected_refs = int(
                                connection.execute(
                                    "SELECT COUNT(*) FROM artifacts "
                                    "WHERE sha256 = ? AND status = ?",
                                    (candidate.sha256, ARTIFACT_AVAILABLE),
                                ).fetchone()[0]
                            )
                            if candidate.reason == "unindexed_file":
                                orphan_blob_present[candidate.storage_key] = blob is not None
                                if blob is not None or expected_refs:
                                    return False
                                continue
                            orphan_blob_present[candidate.storage_key] = blob is not None
                            if blob is None:
                                # A zero-reference or repaired blob may
                                # already have been removed by a prior attempt.
                                # An available artifact still pointing at the
                                # missing index/file is not a completed repair.
                                if expected_refs:
                                    return False
                                continue
                            if (blob["sha256"], blob["byte_size"]) != (
                                candidate.sha256,
                                candidate.byte_size,
                            ):
                                return False
                            if (
                                type(blob["ref_count"]) is not int
                                or blob["ref_count"] != expected_refs
                            ):
                                return False
                            if expected_refs == 0:
                                # apply_retention_plan removes a zero-reference
                                # index row; retaining it means the phase is
                                # not complete yet.
                                return False
                for candidate in plan.orphan_candidates:
                    try:
                        path = self._storage_path(candidate.storage_key)
                        present = _lexists(path)
                        if candidate.reason == "unindexed_file":
                            if present:
                                return False
                        elif candidate.reason == "zero_ref_blob":
                            # A zero-reference candidate is complete only
                            # after both its index row and physical file are
                            # gone.  A newly referenced blob is drift.
                            if present:
                                return False
                        else:  # refcount_mismatch
                            # A repaired blob may remain live when another
                            # available artifact references it.  If the blob
                            # was removed, the physical file must be absent;
                            # otherwise it must still be a valid regular file.
                            blob_present = orphan_blob_present.get(
                                candidate.storage_key, False
                            )
                            if blob_present:
                                if not present or not path.is_file() or _is_reparse_or_symlink(path):
                                    return False
                                if path.stat().st_size != candidate.byte_size:
                                    return False
                                if sha256(path.read_bytes()).hexdigest() != candidate.sha256:
                                    return False
                            elif present:
                                return False
                    except (OSError, CheckpointStoreError):
                        return False
                return True
            except (OSError, sqlite3.Error, CheckpointStoreError):
                return False

    def _artifact_candidate_physical_state_satisfied_locked(
        self,
        connection: sqlite3.Connection,
        candidate: ArtifactRetentionCandidateV1,
    ) -> bool:
        """Check the blob/index postcondition for one frozen Artifact target.

        A missing Artifact row is not enough to declare a candidate complete:
        a failed manual delete can leave its blob index or physical bytes
        behind.  A remaining blob is acceptable only when it is a consistent
        live shared blob for another available Artifact.
        """

        try:
            blob = connection.execute(
                "SELECT * FROM artifact_blobs WHERE sha256 = ?", (candidate.sha256,)
            ).fetchone()
            expected_refs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifacts "
                    "WHERE sha256 = ? AND status = ?",
                    (candidate.sha256, ARTIFACT_AVAILABLE),
                ).fetchone()[0]
            )
            path = self._storage_path(candidate.storage_key)
            present = _lexists(path)
            if expected_refs == 0:
                return blob is None and not present
            if blob is None or (
                blob["sha256"] != candidate.sha256
                or blob["byte_size"] != candidate.byte_size
                or blob["storage_key"] != candidate.storage_key
                or type(blob["ref_count"]) is not int
                or blob["ref_count"] != expected_refs
            ):
                return False
            if (
                not present
                or not path.is_file()
                or _is_reparse_or_symlink(path)
                or path.stat().st_size != candidate.byte_size
                or sha256(path.read_bytes()).hexdigest() != candidate.sha256
            ):
                return False
            return True
        except (OSError, CheckpointStoreError, sqlite3.Error):
            return False

    def _assert_retention_store_identity_locked(
        self, plan: EvidenceRetentionPlanV1
    ) -> None:
        """Reject a missing or replaced database before an apply can mutate it."""

        expected = plan.store_id
        if expected:
            _reject_linked_path(self.path)
            if not _lexists(self.path) or not self.path.is_file():
                raise EvidenceMaintenanceError("retention store is missing")
            with self._read_connection() as connection:
                if connection is None or self._store_id != expected:
                    raise EvidenceMaintenanceError("retention store identity changed")
            return

        # A plan with no identity is valid only for a physical unindexed-file
        # cleanup captured while the SQLite store was absent.  If a database
        # appeared before apply, it is a different state and must not be
        # silently treated as an idempotent empty store.
        _reject_linked_path(self.path)
        if _lexists(self.path):
            if not self.path.is_file():
                raise EvidenceMaintenanceError("retention store path is invalid")
            raise EvidenceMaintenanceError("retention store appeared after planning")

    def _apply_unindexed_only_retention_plan_locked(
        self, plan: EvidenceRetentionPlanV1, *, now: datetime
    ) -> EvidencePurgeResultV1:
        """Remove orphan files from a plan captured without a SQLite store.

        This path deliberately does not open ``_write_connection``.  A failed
        blob write can leave a file behind after its transaction rolls back;
        cleaning that file must not manufacture a new evidence database or an
        audit row for a store that did not exist when the plan was frozen.
        """

        if plan.store_id or plan.checkpoint_candidates or plan.artifact_candidates or plan.audit_candidates:
            raise EvidenceMaintenanceError("physical-only retention plan is invalid")
        if any(item.reason != "unindexed_file" for item in plan.orphan_candidates):
            raise EvidenceMaintenanceError("physical-only retention plan is invalid")
        deleted_files = 0
        released_bytes = 0
        already_satisfied = 0
        for candidate in plan.orphan_candidates:
            try:
                removed, actual_size = self._remove_unindexed_file(candidate)
                if removed:
                    deleted_files += 1
                    released_bytes += actual_size
                else:
                    already_satisfied += 1
            except FileNotFoundError:
                already_satisfied += 1
        # Recheck the database path after physical work.  A caller using this
        # low-level API may race another writer that creates the store; report
        # drift instead of claiming a clean absent-store apply.
        _reject_linked_path(self.path)
        if _lexists(self.path):
            raise EvidenceMaintenanceError("retention store appeared during apply")
        return EvidencePurgeResultV1(
            plan_id=plan.plan_id,
            applied_at=_iso(now),
            checkpoints_deleted=0,
            artifacts_purged=0,
            audits_deleted=0,
            orphan_files_deleted=deleted_files,
            physical_files_deleted=deleted_files,
            physical_bytes_released=released_bytes,
            already_satisfied=already_satisfied,
            audit_id="",
        )

    def _remove_unindexed_file(
        self, candidate: OrphanRetentionCandidateV1
    ) -> tuple[bool, int]:
        """Verify and remove one physical-only orphan file.

        ``False`` means the file was already absent.  Any content, path or
        unlink mismatch is a maintenance error so the caller can persist a
        failed retention attempt and retry with a fresh plan.
        """

        path = self._storage_path(candidate.storage_key)
        if not _lexists(path):
            return False, 0
        if not path.is_file() or _is_reparse_or_symlink(path):
            raise EvidenceMaintenanceError(
                "retention orphan path is not a regular file"
            )
        content = path.read_bytes()
        if (
            len(content) != candidate.byte_size
            or sha256(content).hexdigest() != candidate.sha256
        ):
            raise EvidenceMaintenanceError("retention orphan drift")
        actual_size = path.stat().st_size
        try:
            path.unlink()
        except FileNotFoundError:
            return False, 0
        return True, actual_size

    def apply_retention_plan(
        self,
        plan: EvidenceRetentionPlanV1,
        *,
        actor_key: str = "checkpoint_retention",
    ) -> EvidencePurgeResultV1:
        if type(plan) is not EvidenceRetentionPlanV1:
            raise EvidenceValidationError("plan must be EvidenceRetentionPlanV1")
        plan._validate()
        actor = _safe_id(actor_key, "actor_key")
        now = self._now()
        if _parse_time(plan.as_of, "plan as_of") > now:
            raise EvidenceMaintenanceError("retention plan is from the future")
        deleted_checkpoints = 0
        purged_artifacts = 0
        deleted_audits = 0
        satisfied = 0
        release_digests: set[str] = set()
        audit_id = ""
        try:
            with self._lock:
                self._assert_retention_store_identity_locked(plan)
                if not plan.store_id and not (
                    plan.checkpoint_candidates
                    or plan.artifact_candidates
                    or plan.audit_candidates
                    or any(
                        item.reason != "unindexed_file"
                        for item in plan.orphan_candidates
                    )
                ):
                    return self._apply_unindexed_only_retention_plan_locked(
                        plan, now=now
                    )
                with self._write_connection() as connection:
                    for candidate in plan.checkpoint_candidates:
                        row = connection.execute(
                            "SELECT * FROM checkpoints "
                            "WHERE checkpoint_id = ?", (candidate.checkpoint_id,),
                        ).fetchone()
                        if row is None:
                            satisfied += 1
                            continue
                        checkpoint = self._checkpoint_row(connection, row)
                        if (checkpoint.expires_at, row["semantic_sha256"]) != (
                            candidate.expires_at, candidate.semantic_sha256,
                        ):
                            raise EvidenceMaintenanceError("retention checkpoint drift")
                        connection.execute(
                            "DELETE FROM checkpoints WHERE checkpoint_id = ?",
                            (candidate.checkpoint_id,),
                        )
                        deleted_checkpoints += 1

                    for candidate in plan.artifact_candidates:
                        row = connection.execute(
                            "SELECT * FROM artifacts WHERE artifact_id = ?",
                            (candidate.artifact_id,),
                        ).fetchone()
                        if row is None:
                            if not self._artifact_candidate_physical_state_satisfied_locked(
                                connection, candidate
                            ):
                                raise EvidenceMaintenanceError(
                                    "retention artifact physical target remains"
                                )
                            satisfied += 1
                            continue
                        # Decode and verify the complete descriptor, including
                        # its lifecycle hash, before acting on a saved row.
                        descriptor = self._descriptor_row(connection, row)
                        exact = (
                            descriptor.expires_at == candidate.expires_at
                            and row["storage_key"] == candidate.storage_key
                            and descriptor.sha256 == candidate.sha256
                            and descriptor.byte_size == candidate.byte_size
                        )
                        if not exact:
                            raise EvidenceMaintenanceError("retention artifact drift")
                        if candidate.reason == "tombstone":
                            refs = int(connection.execute(
                                "SELECT COUNT(*) FROM checkpoint_artifacts WHERE artifact_id = ?",
                                (candidate.artifact_id,),
                            ).fetchone()[0])
                            if row["status"] != ARTIFACT_PURGED or refs:
                                raise EvidenceMaintenanceError("retention artifact drift")
                            connection.execute(
                                "DELETE FROM artifacts WHERE artifact_id = ?",
                                (candidate.artifact_id,),
                            )
                            satisfied += 1
                            continue
                        if row["status"] == ARTIFACT_PURGED:
                            satisfied += 1
                            release_digests.add(candidate.sha256)
                            continue
                        if row["status"] != candidate.status or candidate.status != ARTIFACT_AVAILABLE:
                            raise EvidenceMaintenanceError("retention artifact drift")
                        reason = "missing" if candidate.reason == "missing" else "expired"
                        purged_at = _iso(now)
                        connection.execute(
                            "UPDATE artifacts SET status = ?, purged_at = ?, purge_reason = ?, "
                            "lifecycle_sha256 = ? WHERE artifact_id = ?",
                            (
                                ARTIFACT_PURGED,
                                purged_at,
                                reason,
                                _lifecycle_hash(
                                    target_id=row["artifact_id"],
                                    created_at=row["created_at"],
                                    expires_at=row["expires_at"],
                                    retention_class=row["retention_class"],
                                    retention_days=row["retention_days"],
                                    status=ARTIFACT_PURGED,
                                    purged_at=purged_at,
                                    purge_reason=reason,
                                ),
                                candidate.artifact_id,
                            ),
                        )
                        blob = connection.execute(
                            "UPDATE artifact_blobs SET ref_count = ref_count - 1 "
                            "WHERE sha256 = ? AND ref_count > 0", (candidate.sha256,),
                        )
                        if blob.rowcount not in {0, 1}:
                            raise EvidenceMaintenanceError("retention blob drift")
                        release_digests.add(candidate.sha256)
                        purged_artifacts += 1

                    for candidate in plan.audit_candidates:
                        row = connection.execute(
                            "SELECT expires_at FROM evidence_audit WHERE audit_id = ?",
                            (candidate.audit_id,),
                        ).fetchone()
                        if row is None:
                            satisfied += 1
                            continue
                        if row["expires_at"] != candidate.expires_at:
                            raise EvidenceMaintenanceError("retention audit drift")
                        connection.execute(
                            "DELETE FROM evidence_audit WHERE audit_id = ?",
                            (candidate.audit_id,),
                        )
                        deleted_audits += 1

                    for candidate in plan.orphan_candidates:
                        if candidate.reason == "unindexed_file":
                            exists = connection.execute(
                                "SELECT 1 FROM artifact_blobs WHERE storage_key = ?",
                                (candidate.storage_key,),
                            ).fetchone()
                            if exists is not None:
                                raise EvidenceMaintenanceError("retention orphan drift")
                            continue
                        blob = connection.execute(
                            "SELECT * FROM artifact_blobs WHERE storage_key = ?",
                            (candidate.storage_key,),
                        ).fetchone()
                        if blob is None:
                            expected_refs = int(connection.execute(
                                "SELECT COUNT(*) FROM artifacts "
                                "WHERE sha256 = ? AND status = ?",
                                (candidate.sha256, ARTIFACT_AVAILABLE),
                            ).fetchone()[0])
                            if expected_refs:
                                raise EvidenceMaintenanceError("retention orphan drift")
                            satisfied += 1
                            continue
                        if (blob["sha256"], blob["byte_size"]) != (
                            candidate.sha256, candidate.byte_size,
                        ):
                            raise EvidenceMaintenanceError("retention orphan drift")
                        expected_refs = int(connection.execute(
                            "SELECT COUNT(*) FROM artifacts WHERE sha256 = ? AND status = ?",
                            (candidate.sha256, ARTIFACT_AVAILABLE),
                        ).fetchone()[0])
                        if candidate.reason == "zero_ref_blob" and expected_refs != 0:
                            raise EvidenceMaintenanceError("retention orphan drift")
                        connection.execute(
                            "UPDATE artifact_blobs SET ref_count = ? WHERE sha256 = ?",
                            (expected_refs, candidate.sha256),
                        )
                        if expected_refs == 0:
                            release_digests.add(candidate.sha256)

                    audit = self._insert_audit_locked(
                        connection, action=AUDIT_AUTO_PURGE, actor_key=actor,
                        owner_identity_key="", target_id=plan.plan_id,
                        result_code="SUCCEEDED", now=now,
                    )
                    audit_id = audit.audit_id
        except EvidenceAuditError:
            self._note_failure("audit_failures", "evidence_audit_error")
            raise
        except CheckpointStoreError:
            self._note_failure("maintenance_failures", "retention_apply_rejected")
            raise
        except (OSError, sqlite3.Error) as exc:
            self._note_failure("maintenance_failures", type(exc).__name__)
            raise EvidenceMaintenanceError("retention apply is unavailable") from exc

        physical_files = 0
        physical_bytes = 0
        for digest in sorted(release_digests):
            released = self._release_zero_ref_blob(digest)
            if released:
                physical_files += 1
                physical_bytes += released
        orphan_files = 0
        for candidate in plan.orphan_candidates:
            if candidate.reason != "unindexed_file":
                continue
            try:
                removed, actual_size = self._remove_unindexed_file(candidate)
                if removed:
                    orphan_files += 1
                    physical_files += 1
                    physical_bytes += actual_size
                else:
                    satisfied += 1
            except (OSError, CheckpointStoreError):
                # Physical cleanup is part of a retention apply contract.  A
                # failed verification or unlink must fail the run closed so a
                # terminal result cannot claim that all targets were removed.
                raise
        return EvidencePurgeResultV1(
            plan_id=plan.plan_id, applied_at=_iso(now),
            checkpoints_deleted=deleted_checkpoints,
            artifacts_purged=purged_artifacts,
            audits_deleted=deleted_audits,
            orphan_files_deleted=orphan_files,
            physical_files_deleted=physical_files,
            physical_bytes_released=physical_bytes,
            already_satisfied=satisfied, audit_id=audit_id,
        )

    def _write_blob_file(self, storage_key: str, content: bytes) -> Path:
        target = self._storage_path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = sha256(content).hexdigest()
        if target.exists():
            if not target.is_file() or sha256(target.read_bytes()).hexdigest() != digest:
                raise EvidenceConflictError("artifact storage content conflict")
            return target
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return target

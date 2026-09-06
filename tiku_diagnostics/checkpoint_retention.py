"""Coordinate bounded Checkpoint/Artifact and Trace retention maintenance.

Planning is read-only. Applying requires the exact unified plan hash, an
explicitly allowed runtime, and verified backups outside the checkout.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
from threading import Lock
from typing import Callable, Mapping, Sequence

from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1
from tiku_agent.checkpoint_store import (
    EvidenceMaintenanceError,
    EvidenceRetentionPlanV1,
    SQLiteCheckpointStore,
)
from tiku_shared.trace_events import (
    SQLiteTraceEventStore,
    TraceCleanupDriftError,
    TraceCleanupSnapshot,
)


CHECKPOINT_RETENTION_PLAN_SCHEMA_VERSION = 1
CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION = 1
TRACE_RETENTION_DAYS = 30
RUNTIME_NAME_8790 = "tiku_agent_8790"
CHECKPOINT_DATABASE = "checkpoint_evidence.sqlite3"
CHECKPOINT_ARTIFACT_ROOT = "checkpoint_artifacts"
TRACE_DATABASE = "trace_events.sqlite3"

RETENTION_PLAN_INVALID = "RETENTION_PLAN_INVALID"
RETENTION_HASH_MISMATCH = "RETENTION_HASH_MISMATCH"
RETENTION_PATH_REJECTED = "RETENTION_PATH_REJECTED"
RETENTION_DRIFT_DETECTED = "RETENTION_DRIFT_DETECTED"
RETENTION_BACKUP_FAILED = "RETENTION_BACKUP_FAILED"
RETENTION_APPLY_FAILED = "RETENTION_APPLY_FAILED"
RETENTION_RUNTIME_NOT_CONFIRMED = "RETENTION_RUNTIME_NOT_CONFIRMED"
RETENTION_ALREADY_RUNNING = "RETENTION_ALREADY_RUNNING"
RETENTION_CAPACITY_INVALID = "RETENTION_CAPACITY_INVALID"
RETENTION_IO_FAILED = "RETENTION_IO_FAILED"

_RUNTIME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUDIT_ID_RE = re.compile(r"^audit_[0-9a-f]{32}$")
_CAPACITY_KEYS = (
    "max_checkpoint_rows",
    "max_artifact_rows",
    "max_audit_rows",
    "max_trace_rows",
    "max_artifact_bytes",
    "min_free_bytes",
    "max_artifacts_per_checkpoint",
)
_METRIC_KEYS = (
    "checkpoint_rows",
    "artifact_rows",
    "audit_rows",
    "trace_rows",
    "artifact_files",
    "artifact_bytes",
    "managed_artifact_bytes",
    "disk_free_bytes",
)
_RELEASED_METRIC_KEYS = (
    "checkpoint_rows",
    "artifact_rows",
    "audit_rows",
    "trace_rows",
    "artifact_files",
    "artifact_bytes",
    "managed_artifact_bytes",
    "disk_free_delta",
)
_PHASE_NAMES = ("checkpoint_store", "trace_store")
_RESULT_CORE_KEYS = {
    "schema_version",
    "status",
    "failure_code",
    "plan_hash",
    "runtime_name",
    "backup_dir",
    "applied_at",
    "completed_phases",
    "metrics_before",
    "metrics_after",
    "released",
}
_RESULT_FINAL_KEYS = _RESULT_CORE_KEYS | {
    "backup_pruned_count",
    "backup_rotation_failure_code",
}
_PLAN_KEYS = {
    "schema_version",
    "mode",
    "runtime_name",
    "runtime_root",
    "repository_root",
    "created_at",
    "as_of",
    "policy",
    "checkpoint_store",
    "trace_store",
    "metrics_before",
    "summary",
    "plan_hash",
}
_RUN_LOCK_GUARD = Lock()
_RUN_LOCKS: dict[str, Lock] = {}


class CheckpointRetentionError(RuntimeError):
    """A stable, non-sensitive maintenance rejection."""

    def __init__(self, message: str, *, code: str = RETENTION_PLAN_INVALID) -> None:
        self.code = code
        super().__init__(message)


def build_checkpoint_retention_plan(
    runtime_root: str | Path,
    *,
    runtime_name: str,
    repository_root: str | Path,
    capacity: EvidenceCapacityPolicyV1,
    backup_keep_runs: int,
    as_of: datetime | str | None = None,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Build the one canonical plan consumed by both component stores."""

    _require_capacity(capacity)
    keep_runs = _positive_setting(backup_keep_runs, "backup_keep_runs")
    name = _validated_runtime_name(runtime_name)
    repository = _resolved_directory(repository_root, "repository root")
    runtime = _resolved_directory(runtime_root, "runtime root")
    if not _is_relative_to(runtime, repository):
        raise CheckpointRetentionError(
            "runtime root must stay inside the repository",
            code=RETENTION_PATH_REJECTED,
        )
    _reject_linked_path(runtime, stop=repository)

    current = _timestamp(now, "now") if now is not None else datetime.now(UTC)
    anchor = _timestamp(as_of, "as_of") if as_of is not None else current
    if anchor > current:
        raise CheckpointRetentionError("future as_of cannot create an applyable plan")

    checkpoint_store, trace_store = _open_stores(runtime, capacity, current)
    try:
        checkpoint_plan = checkpoint_store.plan_retention(as_of=anchor)
        trace_plan = trace_store.cleanup_candidates(
            cutoff=(anchor - timedelta(days=TRACE_RETENTION_DAYS)).isoformat()
        )
        metrics = _combined_metrics(checkpoint_store, trace_store)
    except (EvidenceMaintenanceError, TraceCleanupDriftError, ValueError, OSError, sqlite3.Error) as exc:
        raise CheckpointRetentionError("retention planning failed") from exc

    checkpoint_payload = _object_dict(checkpoint_plan, "checkpoint retention plan")
    trace_payload = _object_dict(trace_plan, "trace cleanup snapshot")
    checkpoint_counts = {
        key: len(_required_list(checkpoint_payload, key))
        for key in (
            "checkpoint_candidates",
            "artifact_candidates",
            "audit_candidates",
            "orphan_candidates",
        )
    }
    plan: dict[str, object] = {
        "schema_version": CHECKPOINT_RETENTION_PLAN_SCHEMA_VERSION,
        "mode": "plan",
        "runtime_name": name,
        "runtime_root": str(runtime),
        "repository_root": str(repository),
        "created_at": current.isoformat(),
        "as_of": anchor.isoformat(),
        "policy": {
            "trace_retention_days": TRACE_RETENTION_DAYS,
            "trace_capacity_eviction": False,
            "backup_keep_runs": keep_runs,
            "capacity": capacity.to_dict(),
        },
        "checkpoint_store": {
            "database": CHECKPOINT_DATABASE,
            "artifact_root": CHECKPOINT_ARTIFACT_ROOT,
            "retention_plan": checkpoint_payload,
        },
        "trace_store": {
            "database": TRACE_DATABASE,
            "whole_trace_only": True,
            "fresh_over_capacity_eviction": False,
            "cleanup_snapshot": trace_payload,
        },
        "metrics_before": metrics,
        "summary": {
            **checkpoint_counts,
            "trace_candidates": len(_required_list(trace_payload, "candidates")),
            "trace_event_candidates": _required_nonnegative_int(
                trace_payload, "event_count"
            ),
            "artifact_candidate_bytes": _candidate_artifact_bytes(checkpoint_payload),
        },
    }
    plan["plan_hash"] = checkpoint_retention_plan_hash(plan)
    _validated_plan(plan, expected_plan_hash=str(plan["plan_hash"]), now=current)
    return plan


def checkpoint_retention_plan_hash(plan: Mapping[str, object]) -> str:
    """Hash canonical JSON while excluding the plan_hash field itself."""

    if not isinstance(plan, Mapping):
        raise CheckpointRetentionError("retention plan must be an object")
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CheckpointRetentionError("retention plan is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def write_checkpoint_retention_plan(
    path: str | Path,
    plan: Mapping[str, object],
    *,
    now: datetime | str | None = None,
) -> None:
    validated = _validated_plan(
        plan,
        expected_plan_hash=str(plan.get("plan_hash") or ""),
        now=now,
    )
    target = _absolute_path(path)
    _reject_linked_path(target)
    repository = _absolute_path(str(validated["repository_root"]))
    if target == repository or _is_relative_to(target, repository):
        raise CheckpointRetentionError(
            "plan output must stay outside the repository",
            code=RETENTION_PATH_REJECTED,
        )
    if _lexists(target):
        raise CheckpointRetentionError("retention plan output already exists")
    _reject_linked_path(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(target)
    _write_json_exclusive(target, validated)


def load_checkpoint_retention_plan(
    path: str | Path,
    *,
    now: datetime | str | None = None,
) -> dict[str, object]:
    target = _absolute_path(path)
    _reject_linked_path(target)
    value = _read_json_object(target, "retention plan")
    return _validated_plan(
        value,
        expected_plan_hash=str(value.get("plan_hash") or ""),
        now=now,
    )


def apply_checkpoint_retention_plan(
    plan: Mapping[str, object],
    *,
    expected_plan_hash: str,
    repository_root: str | Path,
    backup_root: str | Path,
    allowed_runtime_roots: Sequence[str | Path],
    capacity: EvidenceCapacityPolicyV1,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Acquire the shared runtime lock, then apply an exact saved plan online."""

    _require_capacity(capacity)
    preview = _validated_plan(
        plan,
        expected_plan_hash=expected_plan_hash,
        now=now,
    )
    runtime = _resolved_directory(str(preview["runtime_root"]), "runtime root")
    allowed = {
        _resolved_directory(item, "allowed runtime root")
        for item in allowed_runtime_roots
    }
    if runtime not in allowed:
        raise CheckpointRetentionError(
            "plan runtime root is not explicitly allowed",
            code=RETENTION_PATH_REJECTED,
        )
    with _execution_lock(runtime):
        return _apply_checkpoint_retention_plan_unlocked(
            preview,
            expected_plan_hash=expected_plan_hash,
            repository_root=repository_root,
            backup_root=backup_root,
            allowed_runtime_roots=allowed_runtime_roots,
            capacity=capacity,
            now=now,
        )


def _apply_checkpoint_retention_plan_unlocked(
    plan: Mapping[str, object],
    *,
    expected_plan_hash: str,
    repository_root: str | Path,
    backup_root: str | Path,
    allowed_runtime_roots: Sequence[str | Path],
    capacity: EvidenceCapacityPolicyV1,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Apply one exact plan while the caller holds the runtime lock."""

    _require_capacity(capacity)
    current = _timestamp(now, "now") if now is not None else datetime.now(UTC)
    validated = _validated_plan(
        plan,
        expected_plan_hash=expected_plan_hash,
        now=current,
    )
    if _capacity_from_plan(validated).to_dict() != capacity.to_dict():
        raise CheckpointRetentionError(
            "capacity settings do not match the saved plan",
            code=RETENTION_CAPACITY_INVALID,
        )

    repository = _resolved_directory(repository_root, "repository root")
    runtime = _resolved_directory(str(validated["runtime_root"]), "runtime root")
    if _absolute_path(str(validated["repository_root"])) != repository:
        raise CheckpointRetentionError(
            "plan repository root does not match this checkout",
            code=RETENTION_PATH_REJECTED,
        )
    if not allowed_runtime_roots:
        raise CheckpointRetentionError(
            "an explicit allowed runtime root is required",
            code=RETENTION_PATH_REJECTED,
        )
    allowed = {
        _resolved_directory(item, "allowed runtime root")
        for item in allowed_runtime_roots
    }
    if runtime not in allowed or not _is_relative_to(runtime, repository):
        raise CheckpointRetentionError(
            "plan runtime root is not explicitly allowed",
            code=RETENTION_PATH_REJECTED,
        )
    _reject_linked_path(runtime, stop=repository)
    _validate_fixed_layout(validated, runtime)

    backup_base = _absolute_path(backup_root)
    _reject_linked_path(backup_base)
    if (
        backup_base == repository
        or _is_relative_to(backup_base, repository)
        or _is_relative_to(repository, backup_base)
    ):
        raise CheckpointRetentionError(
            "backup root must be outside the repository",
            code=RETENTION_PATH_REJECTED,
        )
    nearest = _nearest_existing_parent(backup_base)
    _reject_linked_path(nearest)

    plan_hash = str(validated["plan_hash"])
    target = _absolute_path(
        backup_base
        / str(validated["as_of"])[:10]
        / f"checkpoint_retention_{validated['runtime_name']}_{plan_hash[:16]}"
    )
    _reject_linked_path(target, stop=backup_base)
    if target == backup_base or not _is_relative_to(target, backup_base):
        raise CheckpointRetentionError(
            "backup target escaped the approved backup root",
            code=RETENTION_PATH_REJECTED,
        )

    checkpoint_store, trace_store = _open_stores(runtime, capacity, current)
    result_path = target / "result.json"
    resuming = _lexists(target)
    state: dict[str, object] = {
        "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
        "plan_hash": plan_hash,
        "status": "prepared",
        "completed_phases": {},
    }
    if _control_file_present(
        result_path, "retention result", required=False
    ):
        previous = _read_json_object(result_path, "retention result")
        if previous.get("plan_hash") != plan_hash or previous.get("status") != "applied":
            raise CheckpointRetentionError(
                "backup target has a conflicting result",
                code=RETENTION_DRIFT_DETECTED,
            )
        # A terminal-looking result is untrusted.  Do not repair its backup:
        # first revalidate the exact plan, the complete manifest and every
        # copied file, then fence the live stores by their persistent IDs.
        state = _load_completed_backup(target, validated)
        _assert_plan_store_identities(checkpoint_store, trace_store, validated)
        _assert_live_cleanup_satisfied(checkpoint_store, trace_store, validated)
        final = _validated_retention_result(
            previous,
            plan=validated,
            state=state,
            target=target,
            now=current,
        )
        _remove_control_file(target / "failure.json")
        if final:
            if state.get("status") != "applied":
                raise CheckpointRetentionError(
                    "retention result progress is invalid",
                    code=RETENTION_DRIFT_DETECTED,
                )
            pruned, prune_code = _safe_prune_backup_runs(
                backup_base,
                runtime_name=str(validated["runtime_name"]),
                keep_runs=_backup_keep_runs(validated),
                preserve=target,
            )
            return {
                **previous,
                "status": "already_applied",
                "backup_pruned_count": pruned,
                "backup_rotation_failure_code": prune_code,
            }
    else:
        # A partial backup may only be completed while the source stores still
        # have the identities frozen into the plan.
        # Backup creation is part of the resumable apply state machine.  Keep
        # it inside a failure boundary so a partial manifest cannot be left
        # without the same progress/failure controls used by cleanup phases.
        try:
            _assert_plan_store_identities(checkpoint_store, trace_store, validated)
            state = _prepare_verified_backup(
                validated,
                runtime=runtime,
                target=target,
                nearest_backup_parent=nearest,
            )
            state = _validated_progress_state(state, validated)
            _assert_plan_store_identities(checkpoint_store, trace_store, validated)
        except Exception as exc:
            if target.is_dir():
                _persist_retention_failure(
                    target,
                    backup_base=backup_base,
                    plan=validated,
                    state=state,
                    phases={},
                    current=current,
                    exc=exc,
                )
            if isinstance(exc, CheckpointRetentionError):
                raise
            if isinstance(exc, (EvidenceMaintenanceError, TraceCleanupDriftError)):
                raise CheckpointRetentionError(
                    "retention component drift or apply failure",
                    code=RETENTION_DRIFT_DETECTED,
                ) from exc
            raise CheckpointRetentionError(
                "retention apply failed closed",
                code=RETENTION_APPLY_FAILED,
            ) from exc

    phases = state.get("completed_phases")
    assert isinstance(phases, dict)
    try:
        if "checkpoint_store" in phases:
            if not checkpoint_store.retention_plan_satisfied(
                _checkpoint_plan_object(validated)
            ):
                raise CheckpointRetentionError(
                    "checkpoint cleanup phase is no longer satisfied",
                    code=RETENTION_DRIFT_DETECTED,
                )
        if "trace_store" in phases:
            if not _trace_candidates_already_satisfied(
                trace_store, _trace_snapshot_object(validated)
            ):
                raise CheckpointRetentionError(
                    "trace cleanup phase is no longer satisfied",
                    code=RETENTION_DRIFT_DETECTED,
                )
        if "checkpoint_store" not in phases:
            _assert_plan_store_identities(checkpoint_store, trace_store, validated)
            store_result = checkpoint_store.apply_retention_plan(
                _checkpoint_plan_object(validated),
                actor_key="checkpoint_retention",
            )
            phases["checkpoint_store"] = _object_dict(
                store_result, "checkpoint purge result"
            )
            state["status"] = "partial"
            _atomic_write_json(target / "state.json", state)

        if "trace_store" not in phases:
            trace_snapshot = _trace_snapshot_object(validated)
            _assert_plan_store_identities(checkpoint_store, trace_store, validated)
            recovered = resuming and _trace_candidates_already_satisfied(
                trace_store, trace_snapshot
            )
            if recovered:
                trace_result = {
                    "status": "already_satisfied",
                    "snapshot_hash": trace_snapshot.snapshot_hash,
                    "cutoff": trace_snapshot.cutoff,
                    "candidate_count": len(trace_snapshot.candidates),
                    "event_count": trace_snapshot.event_count,
                    "deleted_trace_count": len(trace_snapshot.candidates),
                    "deleted_event_count": trace_snapshot.event_count,
                }
            else:
                trace_result = trace_store.apply_cleanup(trace_snapshot)
            phases["trace_store"] = _object_dict(trace_result, "trace cleanup result")
            state["status"] = "partial"
            _atomic_write_json(target / "state.json", state)

        # A component can report a logically applied repair while a physical
        # target remains unusable (for example, a refcount-mismatch blob with
        # missing bytes).  Fence publication of the terminal result on a full
        # live-store satisfaction check so such a run remains retryable.
        _assert_live_cleanup_satisfied(checkpoint_store, trace_store, validated)

        metrics_before = _validated_metrics(validated["metrics_before"])
        metrics_after = _combined_metrics(checkpoint_store, trace_store)
        result: dict[str, object] = {
            "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
            "status": "applied",
            "failure_code": "",
            "plan_hash": plan_hash,
            "runtime_name": validated["runtime_name"],
            "backup_dir": str(target),
            "applied_at": current.isoformat(),
            "completed_phases": dict(phases),
            "metrics_before": metrics_before,
            "metrics_after": metrics_after,
            "released": _released_metrics(metrics_before, metrics_after),
        }
        # Commit phase progress before publishing the terminal result.  A
        # failure on either write leaves a state that the same plan can resume.
        state["status"] = "applied"
        _atomic_write_json(target / "state.json", state)
        pruned, prune_code = _safe_prune_backup_runs(
            backup_base,
            runtime_name=str(validated["runtime_name"]),
            keep_runs=_backup_keep_runs(validated),
            preserve=target,
        )
        result["backup_pruned_count"] = pruned
        result["backup_rotation_failure_code"] = prune_code
        _validated_retention_result(
            result,
            plan=validated,
            state=state,
            target=target,
            now=current,
            live_metrics=metrics_after,
        )
        _remove_control_file(target / "failure.json")
        _atomic_write_json(result_path, result)
        return result
    except Exception as exc:
        _persist_retention_failure(
            target,
            backup_base=backup_base,
            plan=validated,
            state=state,
            phases=phases,
            current=current,
            exc=exc,
        )
        if isinstance(exc, CheckpointRetentionError):
            raise
        if isinstance(exc, (EvidenceMaintenanceError, TraceCleanupDriftError)):
            raise CheckpointRetentionError(
                "retention component drift or apply failure",
                code=RETENTION_DRIFT_DETECTED,
            ) from exc
        raise CheckpointRetentionError(
            "retention apply failed closed",
            code=RETENTION_APPLY_FAILED,
        ) from exc


def run_checkpoint_retention_once(
    runtime_root: str | Path,
    *,
    runtime_name: str,
    repository_root: str | Path,
    backup_root: str | Path,
    allowed_runtime_roots: Sequence[str | Path],
    capacity: EvidenceCapacityPolicyV1,
    backup_keep_runs: int,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Plan and apply online while excluding another run for this runtime."""

    runtime = _absolute_path(runtime_root)
    with _execution_lock(runtime):
        plan = build_checkpoint_retention_plan(
            runtime,
            runtime_name=runtime_name,
            repository_root=repository_root,
            capacity=capacity,
            backup_keep_runs=backup_keep_runs,
            now=now,
        )
        return _apply_checkpoint_retention_plan_unlocked(
            plan,
            expected_plan_hash=str(plan["plan_hash"]),
            repository_root=repository_root,
            backup_root=backup_root,
            allowed_runtime_roots=allowed_runtime_roots,
            capacity=capacity,
            now=now,
        )


class CheckpointRetentionRunner:
    """Small online periodic-runner surface for runtime assembly."""

    def __init__(
        self,
        *,
        runtime_root: str | Path,
        runtime_name: str,
        repository_root: str | Path,
        backup_root: str | Path,
        capacity: EvidenceCapacityPolicyV1,
        backup_keep_runs: int,
        clock: Callable[[], datetime | str] = lambda: datetime.now(UTC),
    ) -> None:
        _require_capacity(capacity)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.runtime_root = _absolute_path(runtime_root)
        self.runtime_name = _validated_runtime_name(runtime_name)
        self.repository_root = _absolute_path(repository_root)
        self.backup_root = _absolute_path(backup_root)
        self.capacity = capacity
        self.backup_keep_runs = _positive_setting(backup_keep_runs, "backup_keep_runs")
        self._clock = clock
        self._health_lock = Lock()
        self._runs = 0
        self._failures = 0
        self._overlap_rejections = 0
        self._last_plan_hash = ""
        self._last_failure_code = ""
        self._last_failure_at = ""

    def run_once(self) -> dict[str, object]:
        current: datetime | str | None = None
        try:
            current = self._clock()
            result = run_checkpoint_retention_once(
                self.runtime_root,
                runtime_name=self.runtime_name,
                repository_root=self.repository_root,
                backup_root=self.backup_root,
                allowed_runtime_roots=(self.runtime_root,),
                capacity=self.capacity,
                backup_keep_runs=self.backup_keep_runs,
                now=current,
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, CheckpointRetentionError)
                else RETENTION_APPLY_FAILED
            )
            with self._health_lock:
                self._failures = _bounded_counter(self._failures + 1)
                if code == RETENTION_ALREADY_RUNNING:
                    self._overlap_rejections = _bounded_counter(
                        self._overlap_rejections + 1
                    )
                self._last_failure_code = code.lower()
                try:
                    self._last_failure_at = (
                        _timestamp(current, "clock").isoformat()
                        if current is not None
                        else datetime.now(UTC).isoformat()
                    )
                except Exception:
                    self._last_failure_at = ""
            if isinstance(exc, CheckpointRetentionError):
                raise
            raise CheckpointRetentionError(
                "periodic retention run failed", code=code
            ) from exc
        with self._health_lock:
            self._runs = _bounded_counter(self._runs + 1)
            self._last_plan_hash = str(result.get("plan_hash") or "")
            self._last_failure_code = ""
            self._last_failure_at = ""
        return result

    def health(self) -> dict[str, object]:
        with self._health_lock:
            reasons = ["retention_failure"] if self._last_failure_code else []
            return {
                "status": "degraded" if reasons else "ok",
                "current_reasons": reasons,
                "counters": {
                    "runs": self._runs,
                    "failures": self._failures,
                    "overlap_rejections": self._overlap_rejections,
                },
                "pending": 0,
                "queue_capacity": 0,
                "accepting": True,
                "last_failure_code": self._last_failure_code,
                "last_failure_at": self._last_failure_at,
                "last_plan_hash": self._last_plan_hash,
            }


def checkpoint_retention_plan_report(
    plan: Mapping[str, object],
    *,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Return counts and capacity without paths or candidate identifiers."""

    validated = _validated_plan(
        plan,
        expected_plan_hash=str(plan.get("plan_hash") or ""),
        now=now,
    )
    return {
        "schema_version": validated["schema_version"],
        "mode": validated["mode"],
        "runtime_name": validated["runtime_name"],
        "created_at": validated["created_at"],
        "as_of": validated["as_of"],
        "plan_hash": validated["plan_hash"],
        "policy": validated["policy"],
        "metrics_before": validated["metrics_before"],
        "summary": validated["summary"],
    }


def format_checkpoint_retention_plan(
    plan: Mapping[str, object],
    *,
    now: datetime | str | None = None,
) -> str:
    report = checkpoint_retention_plan_report(plan, now=now)
    summary = report["summary"]
    metrics = report["metrics_before"]
    assert isinstance(summary, Mapping) and isinstance(metrics, Mapping)
    return "\n".join(
        (
            f"Checkpoint retention plan: runtime={report['runtime_name']} as_of={report['as_of']}",
            f"Plan hash: {report['plan_hash']}",
            "Candidates: "
            f"checkpoints={summary['checkpoint_candidates']} "
            f"artifacts={summary['artifact_candidates']} "
            f"audits={summary['audit_candidates']} "
            f"orphans={summary['orphan_candidates']} "
            f"traces={summary['trace_candidates']} "
            f"trace_events={summary['trace_event_candidates']}",
            "Before: "
            f"rows={metrics['checkpoint_rows']}/{metrics['artifact_rows']}/"
            f"{metrics['audit_rows']}/{metrics['trace_rows']} "
            f"artifact_files={metrics['artifact_files']} "
            f"artifact_bytes={metrics['artifact_bytes']} "
            f"disk_free={metrics['disk_free_bytes']}",
        )
    )


def _open_stores(
    runtime: Path,
    capacity: EvidenceCapacityPolicyV1,
    current: datetime,
) -> tuple[SQLiteCheckpointStore, SQLiteTraceEventStore]:
    checkpoint_path = _fixed_source_path(runtime, CHECKPOINT_DATABASE)
    artifact_root = _fixed_source_path(runtime, CHECKPOINT_ARTIFACT_ROOT)
    trace_path = _fixed_source_path(runtime, TRACE_DATABASE)
    trace_store = SQLiteTraceEventStore(trace_path, max_rows=capacity.max_trace_rows)

    def trace_rows(_path: Path) -> int:
        snapshot = _object_dict(trace_store.capacity_snapshot(), "trace capacity")
        return _required_nonnegative_int(snapshot, "current_rows")

    checkpoint_store = SQLiteCheckpointStore(
        checkpoint_path,
        artifact_root=artifact_root,
        capacity=capacity,
        trace_db_path=trace_path,
        trace_row_counter=trace_rows,
        clock=lambda: current,
    )
    return checkpoint_store, trace_store


def _combined_metrics(
    checkpoint_store: SQLiteCheckpointStore,
    trace_store: SQLiteTraceEventStore,
) -> dict[str, int]:
    store = _object_dict(checkpoint_store.capacity_snapshot(), "evidence capacity")
    trace = _object_dict(trace_store.capacity_snapshot(), "trace capacity")
    return {
        "checkpoint_rows": _required_nonnegative_int(store, "checkpoint_rows"),
        "artifact_rows": _required_nonnegative_int(store, "artifact_rows"),
        "audit_rows": _required_nonnegative_int(store, "audit_rows"),
        "trace_rows": _required_nonnegative_int(trace, "current_rows"),
        "artifact_files": _required_nonnegative_int(store, "artifact_files"),
        "artifact_bytes": _required_nonnegative_int(store, "artifact_file_bytes"),
        "managed_artifact_bytes": _required_nonnegative_int(store, "artifact_bytes"),
        "disk_free_bytes": _required_nonnegative_int(store, "disk_free_bytes"),
    }


def _validated_plan(
    plan: Mapping[str, object],
    *,
    expected_plan_hash: str,
    now: datetime | str | None = None,
) -> dict[str, object]:
    if not isinstance(plan, Mapping) or set(plan) != _PLAN_KEYS:
        raise CheckpointRetentionError("retention plan shape is invalid")
    value = _json_clone(plan, "retention plan")
    if value.get("schema_version") != CHECKPOINT_RETENTION_PLAN_SCHEMA_VERSION:
        raise CheckpointRetentionError("unsupported retention plan schema")
    if value.get("mode") != "plan":
        raise CheckpointRetentionError("retention plan is not applyable")
    _validated_runtime_name(value.get("runtime_name"))
    embedded_hash = str(value.get("plan_hash") or "")
    if expected_plan_hash != embedded_hash:
        raise CheckpointRetentionError(
            "confirmed plan hash does not match", code=RETENTION_HASH_MISMATCH
        )
    if not _SHA256_RE.fullmatch(embedded_hash) or checkpoint_retention_plan_hash(value) != embedded_hash:
        raise CheckpointRetentionError(
            "retention plan hash verification failed", code=RETENTION_HASH_MISMATCH
        )
    created_at = _timestamp(value.get("created_at"), "created_at")
    as_of = _timestamp(value.get("as_of"), "as_of")
    current = _timestamp(now, "now") if now is not None else datetime.now(UTC)
    if created_at > current or as_of > current or as_of > created_at:
        raise CheckpointRetentionError("retention plan timestamps are inconsistent")

    policy = value.get("policy")
    if not isinstance(policy, dict) or set(policy) != {
        "trace_retention_days",
        "trace_capacity_eviction",
        "backup_keep_runs",
        "capacity",
    }:
        raise CheckpointRetentionError("retention policy shape is invalid")
    if policy.get("trace_retention_days") != TRACE_RETENTION_DAYS or policy.get("trace_capacity_eviction") is not False:
        raise CheckpointRetentionError("trace retention policy is invalid")
    _positive_setting(policy.get("backup_keep_runs"), "backup_keep_runs")
    capacity = _capacity_from_plan(value)

    checkpoint = value.get("checkpoint_store")
    trace = value.get("trace_store")
    if not isinstance(checkpoint, dict) or set(checkpoint) != {
        "database", "artifact_root", "retention_plan"
    }:
        raise CheckpointRetentionError("checkpoint store plan shape is invalid")
    if not isinstance(trace, dict) or set(trace) != {
        "database", "whole_trace_only", "fresh_over_capacity_eviction", "cleanup_snapshot"
    }:
        raise CheckpointRetentionError("trace store plan shape is invalid")
    if (
        checkpoint.get("database") != CHECKPOINT_DATABASE
        or checkpoint.get("artifact_root") != CHECKPOINT_ARTIFACT_ROOT
        or trace.get("database") != TRACE_DATABASE
        or trace.get("whole_trace_only") is not True
        or trace.get("fresh_over_capacity_eviction") is not False
    ):
        raise CheckpointRetentionError("retention plan layout or policy is invalid")
    checkpoint_plan = _checkpoint_plan_object(value)
    trace_plan = _trace_snapshot_object(value)
    checkpoint_payload = _object_dict(checkpoint_plan, "checkpoint retention plan")
    trace_payload = _object_dict(trace_plan, "trace cleanup snapshot")
    _validate_plan_store_binding(
        checkpoint_payload,
        candidate_count=_checkpoint_database_candidate_total(checkpoint_payload),
        name="checkpoint",
    )
    _validate_plan_store_binding(
        trace_payload,
        candidate_count=_required_nonnegative_int(trace_payload, "candidate_count"),
        name="trace",
    )
    if _timestamp(checkpoint_payload.get("as_of"), "checkpoint as_of") != as_of:
        raise CheckpointRetentionError("checkpoint cutoff does not match unified plan")
    if _timestamp(trace_payload.get("cutoff"), "trace cutoff") != as_of - timedelta(days=TRACE_RETENTION_DAYS):
        raise CheckpointRetentionError("trace cutoff does not match 30-day policy")

    metrics = _validated_metrics(value.get("metrics_before"))
    summary = value.get("summary")
    expected_summary = {
        key: len(_required_list(checkpoint_payload, key))
        for key in (
            "checkpoint_candidates", "artifact_candidates", "audit_candidates", "orphan_candidates"
        )
    }
    expected_summary.update({
        "trace_candidates": len(_required_list(trace_payload, "candidates")),
        "trace_event_candidates": _required_nonnegative_int(trace_payload, "event_count"),
        "artifact_candidate_bytes": _candidate_artifact_bytes(checkpoint_payload),
    })
    if not isinstance(summary, dict) or summary != expected_summary:
        raise CheckpointRetentionError("retention plan summary does not match candidates")
    value["metrics_before"] = metrics
    value["policy"]["capacity"] = capacity.to_dict()  # type: ignore[index]
    return value


def _checkpoint_plan_object(plan: Mapping[str, object]) -> EvidenceRetentionPlanV1:
    payload = _checkpoint_payload(plan)
    loader = getattr(EvidenceRetentionPlanV1, "from_dict", None)
    if not callable(loader):
        raise CheckpointRetentionError(
            "checkpoint store plan adapter is unavailable", code=RETENTION_APPLY_FAILED
        )
    try:
        return loader(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointRetentionError("checkpoint retention plan is invalid") from exc


def _validate_plan_store_binding(
    payload: Mapping[str, object], *, candidate_count: int, name: str
) -> str:
    """Require a persistent store identity whenever a plan can delete rows."""

    raw = payload.get("store_id", "")
    if not isinstance(raw, str):
        raise CheckpointRetentionError(f"{name} store identity is invalid", code=RETENTION_DRIFT_DETECTED)
    store_id = raw.strip()
    if candidate_count and not store_id:
        raise CheckpointRetentionError(
            f"{name} plan is not bound to a store", code=RETENTION_DRIFT_DETECTED
        )
    if store_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", store_id):
        raise CheckpointRetentionError(
            f"{name} store identity is invalid", code=RETENTION_DRIFT_DETECTED
        )
    return store_id


def _assert_store_identity(
    store: object, payload: Mapping[str, object], *, name: str, candidate_count: int
) -> None:
    """Fence apply/recovery against a missing or replaced SQLite database."""

    expected = _validate_plan_store_binding(
        payload, candidate_count=candidate_count, name=name
    )
    if not expected:
        return
    getter = getattr(store, "store_id", None)
    if not callable(getter):
        raise CheckpointRetentionError(
            f"{name} store identity is unavailable", code=RETENTION_DRIFT_DETECTED
        )
    try:
        actual = getter()
    except Exception as exc:  # noqa: BLE001
        raise CheckpointRetentionError(
            f"{name} store identity is unavailable", code=RETENTION_DRIFT_DETECTED
        ) from exc
    if not isinstance(actual, str) or actual != expected:
        raise CheckpointRetentionError(
            f"{name} store identity changed", code=RETENTION_DRIFT_DETECTED
        )


def _assert_plan_store_identities(
    checkpoint_store: SQLiteCheckpointStore,
    trace_store: SQLiteTraceEventStore,
    plan: Mapping[str, object],
) -> None:
    checkpoint_payload = _checkpoint_payload(plan)
    trace_payload = _trace_payload(plan)
    _assert_store_identity(
        checkpoint_store,
        checkpoint_payload,
        name="checkpoint",
        candidate_count=_checkpoint_database_candidate_total(checkpoint_payload),
    )
    _assert_store_identity(
        trace_store,
        trace_payload,
        name="trace",
        candidate_count=_required_nonnegative_int(trace_payload, "candidate_count"),
    )


def _assert_live_cleanup_satisfied(
    checkpoint_store: SQLiteCheckpointStore,
    trace_store: SQLiteTraceEventStore,
    plan: Mapping[str, object],
) -> None:
    """Ensure a terminal/marked phase still reflects the live stores."""

    checkpoint_plan = _checkpoint_plan_object(plan)
    try:
        satisfied = checkpoint_store.retention_plan_satisfied(checkpoint_plan)
    except Exception as exc:  # noqa: BLE001 - expose only stable drift.
        raise CheckpointRetentionError(
            "checkpoint cleanup state is unavailable", code=RETENTION_DRIFT_DETECTED
        ) from exc
    if not satisfied:
        raise CheckpointRetentionError(
            "checkpoint cleanup targets remain", code=RETENTION_DRIFT_DETECTED
        )

    trace_snapshot = _trace_snapshot_object(plan)
    if not _trace_candidates_already_satisfied(trace_store, trace_snapshot):
        raise CheckpointRetentionError(
            "trace cleanup targets remain", code=RETENTION_DRIFT_DETECTED
        )


def _trace_snapshot_object(plan: Mapping[str, object]) -> TraceCleanupSnapshot:
    try:
        return TraceCleanupSnapshot.from_dict(_trace_payload(plan))
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointRetentionError("trace cleanup snapshot is invalid") from exc


def _capacity_from_plan(plan: Mapping[str, object]) -> EvidenceCapacityPolicyV1:
    policy = plan.get("policy")
    capacity = policy.get("capacity") if isinstance(policy, Mapping) else None
    if not isinstance(capacity, Mapping) or set(capacity) != set(_CAPACITY_KEYS):
        raise CheckpointRetentionError(
            "all seven capacity settings are required", code=RETENTION_CAPACITY_INVALID
        )
    try:
        return EvidenceCapacityPolicyV1(**{key: capacity[key] for key in _CAPACITY_KEYS})  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CheckpointRetentionError(
            "capacity settings are invalid", code=RETENTION_CAPACITY_INVALID
        ) from exc


def _expected_backup_specs(
    plan: Mapping[str, object], runtime: Path
) -> list[dict[str, object]]:
    """Freeze the physical files needed by this plan from its metadata."""

    checkpoint_payload = _checkpoint_payload(plan)
    trace_payload = _trace_payload(plan)
    specs: list[dict[str, object]] = []
    if _checkpoint_database_candidate_total(checkpoint_payload):
        specs.append({
            "kind": "sqlite",
            "source": CHECKPOINT_DATABASE,
            "backup": f"sqlite/{CHECKPOINT_DATABASE}",
            "source_path": _fixed_source_path(runtime, CHECKPOINT_DATABASE),
        })
    if _required_nonnegative_int(trace_payload, "event_count"):
        specs.append({
            "kind": "sqlite",
            "source": TRACE_DATABASE,
            "backup": f"sqlite/{TRACE_DATABASE}",
            "source_path": _fixed_source_path(runtime, TRACE_DATABASE),
        })
    root = _fixed_source_path(runtime, CHECKPOINT_ARTIFACT_ROOT)
    for metadata in _candidate_artifact_metadata(checkpoint_payload):
        if not metadata["backup_required"]:
            continue
        relative = _safe_relative_path(str(metadata["storage_key"]), "artifact storage key")
        source_path = _absolute_path(root / Path(*relative.parts))
        _reject_linked_path(source_path, stop=root)
        specs.append({
            "kind": "artifact",
            "source": str(metadata["storage_key"]),
            "backup": f"artifacts/{relative.as_posix()}",
            "source_path": source_path,
            "byte_size": _required_positive_int(metadata, "byte_size"),
            "sha256": str(metadata["sha256"]),
        })
    specs.sort(key=lambda item: (str(item["kind"]), str(item["source"])))
    return specs


def _backup_record_from_file(
    target: Path, spec: Mapping[str, object]
) -> dict[str, object]:
    relative = _safe_relative_path(str(spec["backup"]), "backup path")
    backup = _absolute_path(target / Path(*relative.parts))
    if not _is_relative_to(backup, target) or not _control_file_present(
        backup, "backup file", required=False
    ):
        raise CheckpointRetentionError(
            "backup file is missing", code=RETENTION_BACKUP_FAILED
        )
    _reject_linked_path(backup, stop=target)
    size = backup.stat().st_size
    digest = _sha256_file(backup)
    if spec["kind"] == "artifact":
        if size != spec["byte_size"] or digest != spec["sha256"]:
            raise CheckpointRetentionError(
                "backup artifact verification failed", code=RETENTION_BACKUP_FAILED
            )
    else:
        _verify_sqlite_integrity(backup)
    return {
        "kind": spec["kind"],
        "source": spec["source"],
        "backup": relative.as_posix(),
        "bytes": size,
        "sha256": digest,
    }


def _write_backup_manifest(
    target: Path, plan: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> None:
    _atomic_write_json(target / "backup_manifest.json", {
        "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "files": sorted(
            [dict(item) for item in records],
            key=lambda item: (str(item["kind"]), str(item["source"])),
        ),
    })


def _complete_verified_backup(
    plan: Mapping[str, object], *, runtime: Path, target: Path,
    nearest_backup_parent: Path,
) -> dict[str, object]:
    """Create or resume a verified backup, retaining completed files on retry."""

    _reject_linked_path(target, stop=nearest_backup_parent)
    target.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(target, stop=target.parent)
    if not target.is_dir():
        raise CheckpointRetentionError(
            "backup target is not a directory", code=RETENTION_PATH_REJECTED
        )
    plan_path = target / "plan.json"
    _reject_linked_path(plan_path, stop=target)
    if _control_file_present(plan_path, "backed-up retention plan", required=False):
        if _read_json_object(plan_path, "backed-up retention plan") != plan:
            raise CheckpointRetentionError(
                "backup target plan does not match", code=RETENTION_DRIFT_DETECTED
            )
    else:
        # Only the deterministic coordinator target may be initialized here.
        if any(target.iterdir()):
            raise CheckpointRetentionError(
                "backup target is incomplete or conflicting", code=RETENTION_DRIFT_DETECTED
            )
        _write_json_exclusive(plan_path, plan)

    specs = _expected_backup_specs(plan, runtime)
    records: list[dict[str, object]] = []
    manifest_path = target / "backup_manifest.json"
    if _control_file_present(manifest_path, "backup manifest", required=False):
        manifest = _read_json_object(manifest_path, "backup manifest")
        _verify_backup_manifest(target, manifest, plan, allow_partial=True)
        raw_files = manifest.get("files")
        assert isinstance(raw_files, list)
        records = [dict(item) for item in raw_files if isinstance(item, Mapping)]
    by_key = {(str(item["kind"]), str(item["source"])): item for item in records}

    missing_bytes = 1024 * 1024
    for spec in specs:
        key = (str(spec["kind"]), str(spec["source"]))
        existing = by_key.get(key)
        if existing is not None:
            # Revalidate the frozen record; source files may already be gone.
            checked = _backup_record_from_file(target, spec)
            if checked["bytes"] != existing.get("bytes") or checked["sha256"] != existing.get("sha256"):
                raise CheckpointRetentionError(
                    "backup manifest entry changed", code=RETENTION_BACKUP_FAILED
                )
            by_key[key] = checked
            continue
        source = spec["source_path"]
        assert isinstance(source, Path)
        _reject_linked_path(source, stop=runtime)
        if not _lexists(source) or not source.is_file():
            raise CheckpointRetentionError(
                "planned backup source is missing", code=RETENTION_DRIFT_DETECTED
            )
        _reject_linked_path(source, stop=runtime)
        missing_bytes += source.stat().st_size if spec["kind"] == "sqlite" else int(spec["byte_size"])

    if shutil.disk_usage(nearest_backup_parent).free < missing_bytes:
        raise CheckpointRetentionError(
            "insufficient free space for verified backups", code=RETENTION_BACKUP_FAILED
        )

    for spec in specs:
        key = (str(spec["kind"]), str(spec["source"]))
        if key in by_key:
            continue
        source = spec["source_path"]
        assert isinstance(source, Path)
        relative = _safe_relative_path(str(spec["backup"]), "backup path")
        destination = _absolute_path(target / Path(*relative.parts))
        _reject_linked_path(destination, stop=target)
        if not _is_relative_to(destination, target):
            raise CheckpointRetentionError(
                "backup path escaped target", code=RETENTION_PATH_REJECTED
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if spec["kind"] == "sqlite":
            _backup_sqlite(source, destination)
        else:
            _copy_verified_artifact(source, destination, spec)
        by_key[key] = _backup_record_from_file(target, spec)
        _write_backup_manifest(target, plan, list(by_key.values()))

    records = list(by_key.values())
    _write_backup_manifest(target, plan, records)
    _verify_backup_manifest(target, _read_json_object(manifest_path, "backup manifest"), plan)
    state_path = target / "state.json"
    _reject_linked_path(state_path, stop=target)
    if _control_file_present(state_path, "retention progress state", required=False):
        state = _read_json_object(state_path, "retention progress state")
    else:
        state = {
            "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
            "plan_hash": plan["plan_hash"],
            "status": "prepared",
            "completed_phases": {},
        }
        _atomic_write_json(state_path, state)
    return state


def _prepare_verified_backup(
    plan: Mapping[str, object],
    *,
    runtime: Path,
    target: Path,
    nearest_backup_parent: Path,
) -> dict[str, object]:
    try:
        return _complete_verified_backup(
            plan, runtime=runtime, target=target,
            nearest_backup_parent=nearest_backup_parent,
        )
    except CheckpointRetentionError:
        raise
    except OSError as exc:
        raise CheckpointRetentionError(
            "verified backup failed", code=RETENTION_BACKUP_FAILED
        ) from exc


def _load_prepared_backup(
    target: Path, plan: Mapping[str, object]
) -> dict[str, object]:
    saved = _read_json_object(target / "plan.json", "backed-up retention plan")
    if saved != plan:
        raise CheckpointRetentionError(
            "backup target plan does not match", code=RETENTION_DRIFT_DETECTED
        )
    # A failed first pass may have left only plan.json and a partial manifest.
    # Complete and verify the frozen file set before reading progress state.
    runtime = _resolved_directory(str(plan["runtime_root"]), "runtime root")
    nearest = _nearest_existing_parent(target.parent.parent.parent)
    _complete_verified_backup(
        plan, runtime=runtime, target=target, nearest_backup_parent=nearest
    )
    manifest = _read_json_object(target / "backup_manifest.json", "backup manifest")
    _verify_backup_manifest(target, manifest, plan)
    state = _read_json_object(target / "state.json", "retention progress state")
    return _validated_progress_state(state, plan)


def _load_completed_backup(
    target: Path, plan: Mapping[str, object]
) -> dict[str, object]:
    """Read a terminal backup and verify every persisted component.

    A terminal result is never trusted merely because ``result.json`` exists.
    Replaying it must validate the frozen plan, the complete manifest and the
    progress state without trying to infer anything from the live stores.
    """

    saved_plan = _read_json_object(target / "plan.json", "backed-up retention plan")
    if saved_plan != plan:
        raise CheckpointRetentionError(
            "backup target plan does not match", code=RETENTION_DRIFT_DETECTED
        )
    manifest = _read_json_object(target / "backup_manifest.json", "backup manifest")
    _verify_backup_manifest(target, manifest, plan)
    state = _read_json_object(target / "state.json", "retention progress state")
    validated = _validated_progress_state(state, plan)
    if validated["status"] != "applied" or set(validated["completed_phases"]) != set(
        _PHASE_NAMES
    ):
        raise CheckpointRetentionError(
            "completed retention progress is incomplete", code=RETENTION_DRIFT_DETECTED
        )
    return validated


def _validated_progress_state(
    value: Mapping[str, object], plan: Mapping[str, object]
) -> dict[str, object]:
    """Validate resumable phase state before it can authorize a mutation."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "plan_hash",
        "status",
        "completed_phases",
    }:
        raise CheckpointRetentionError(
            "retention progress state is invalid", code=RETENTION_DRIFT_DETECTED
        )
    state = _json_clone(value, "retention progress state")
    if (
        state.get("schema_version") != CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION
        or state.get("plan_hash") != plan.get("plan_hash")
        or state.get("status") not in {"prepared", "partial", "applied"}
    ):
        raise CheckpointRetentionError(
            "retention progress state is invalid", code=RETENTION_DRIFT_DETECTED
        )
    phases = state.get("completed_phases")
    if not isinstance(phases, dict) or not set(phases).issubset(set(_PHASE_NAMES)):
        raise CheckpointRetentionError(
            "retention progress phases are invalid", code=RETENTION_DRIFT_DETECTED
        )
    if state["status"] == "prepared" and phases:
        raise CheckpointRetentionError(
            "prepared retention state has completed phases", code=RETENTION_DRIFT_DETECTED
        )
    if state["status"] == "partial" and len(phases) != 1:
        raise CheckpointRetentionError(
            "partial retention state has invalid phases", code=RETENTION_DRIFT_DETECTED
        )
    if state["status"] == "applied" and set(phases) != set(_PHASE_NAMES):
        raise CheckpointRetentionError(
            "applied retention state is incomplete", code=RETENTION_DRIFT_DETECTED
        )

    checkpoint_payload = _checkpoint_payload(plan)
    trace_payload = _trace_payload(plan)
    for phase_name, raw_result in phases.items():
        if not isinstance(raw_result, Mapping):
            raise CheckpointRetentionError(
                "retention phase result is invalid", code=RETENTION_DRIFT_DETECTED
            )
        result = _json_clone(raw_result, "retention phase result")
        if phase_name == "checkpoint_store":
            expected = {
                "plan_id",
                "applied_at",
                "checkpoints_deleted",
                "artifacts_purged",
                "audits_deleted",
                "orphan_files_deleted",
                "physical_files_deleted",
                "physical_bytes_released",
                "already_satisfied",
                "audit_id",
            }
            if set(result) != expected:
                raise CheckpointRetentionError(
                    "checkpoint phase result is invalid", code=RETENTION_DRIFT_DETECTED
                )
            checkpoint_plan = _checkpoint_payload(plan)
            if result.get("plan_id") != checkpoint_plan.get("plan_id"):
                raise CheckpointRetentionError(
                    "checkpoint phase plan identity changed", code=RETENTION_DRIFT_DETECTED
                )
            _timestamp(result.get("applied_at"), "checkpoint phase applied_at")
            for key in expected - {"plan_id", "applied_at", "audit_id"}:
                _required_nonnegative_int(result, key)
            checkpoint_count = len(
                _required_list(checkpoint_payload, "checkpoint_candidates")
            )
            artifact_count = len(
                _required_list(checkpoint_payload, "artifact_candidates")
            )
            audit_count = len(_required_list(checkpoint_payload, "audit_candidates"))
            orphan_candidates = _required_list(checkpoint_payload, "orphan_candidates")
            unindexed_count = sum(
                1
                for item in orphan_candidates
                if isinstance(item, Mapping) and item.get("reason") == "unindexed_file"
            )
            total_candidates = checkpoint_count + artifact_count + audit_count + len(
                orphan_candidates
            )
            physical_metadata = _candidate_artifact_metadata(checkpoint_payload)
            physical_file_limit = len(physical_metadata)
            physical_byte_limit = sum(
                _required_positive_int(item, "byte_size") for item in physical_metadata
            )
            limits = {
                "checkpoints_deleted": checkpoint_count,
                "artifacts_purged": artifact_count,
                "audits_deleted": audit_count,
                "orphan_files_deleted": unindexed_count,
                "physical_files_deleted": physical_file_limit,
                "physical_bytes_released": physical_byte_limit,
                "already_satisfied": total_candidates,
            }
            for key, limit in limits.items():
                if result[key] > limit:
                    raise CheckpointRetentionError(
                        "checkpoint phase count exceeds its plan",
                        code=RETENTION_DRIFT_DETECTED,
                    )
            if result["physical_files_deleted"] < result["orphan_files_deleted"]:
                raise CheckpointRetentionError(
                    "checkpoint phase physical count is inconsistent",
                    code=RETENTION_DRIFT_DETECTED,
                )
            if result["physical_bytes_released"] < 0:
                raise CheckpointRetentionError(
                    "checkpoint phase physical bytes are invalid",
                    code=RETENTION_DRIFT_DETECTED,
                )
            audit_id = result.get("audit_id")
            if not isinstance(audit_id, str):
                raise CheckpointRetentionError(
                    "checkpoint phase audit identity is invalid", code=RETENTION_DRIFT_DETECTED
                )
            has_database_candidates = _checkpoint_database_candidate_total(
                checkpoint_payload
            ) > 0 or bool(checkpoint_payload.get("store_id"))
            if has_database_candidates:
                if not _AUDIT_ID_RE.fullmatch(audit_id):
                    raise CheckpointRetentionError(
                        "checkpoint phase audit identity is invalid",
                        code=RETENTION_DRIFT_DETECTED,
                    )
            elif audit_id:
                raise CheckpointRetentionError(
                    "physical-only checkpoint phase has an audit identity",
                    code=RETENTION_DRIFT_DETECTED,
                )
        else:
            expected = {
                "status",
                "snapshot_hash",
                "cutoff",
                "candidate_count",
                "event_count",
                "deleted_trace_count",
                "deleted_event_count",
            }
            if set(result) != expected:
                raise CheckpointRetentionError(
                    "trace phase result is invalid", code=RETENTION_DRIFT_DETECTED
                )
            if result.get("status") not in {"applied", "already_satisfied"}:
                raise CheckpointRetentionError(
                    "trace phase status is invalid", code=RETENTION_DRIFT_DETECTED
                )
            if result.get("snapshot_hash") != trace_payload.get("snapshot_hash"):
                raise CheckpointRetentionError(
                    "trace phase snapshot identity changed", code=RETENTION_DRIFT_DETECTED
                )
            if _timestamp(result.get("cutoff"), "trace phase cutoff") != _timestamp(
                trace_payload.get("cutoff"), "trace cutoff"
            ):
                raise CheckpointRetentionError(
                    "trace phase cutoff changed", code=RETENTION_DRIFT_DETECTED
                )
            if result.get("candidate_count") != trace_payload.get("candidate_count"):
                raise CheckpointRetentionError(
                    "trace phase candidate count changed", code=RETENTION_DRIFT_DETECTED
                )
            if result.get("event_count") != trace_payload.get("event_count"):
                raise CheckpointRetentionError(
                    "trace phase event count changed", code=RETENTION_DRIFT_DETECTED
                )
            for key in (
                "candidate_count",
                "event_count",
                "deleted_trace_count",
                "deleted_event_count",
            ):
                _required_nonnegative_int(result, key)
            if result["deleted_trace_count"] != result["candidate_count"]:
                raise CheckpointRetentionError(
                    "trace phase deletion count is invalid", code=RETENTION_DRIFT_DETECTED
                )
            if result["deleted_event_count"] != result["event_count"]:
                raise CheckpointRetentionError(
                    "trace phase event deletion count is invalid", code=RETENTION_DRIFT_DETECTED
                )
        phases[phase_name] = result
    state["completed_phases"] = phases
    return state


def _validated_retention_result(
    value: Mapping[str, object],
    *,
    plan: Mapping[str, object],
    state: Mapping[str, object],
    target: Path,
    now: datetime,
    live_metrics: Mapping[str, object] | None = None,
) -> bool:
    """Validate a terminal result and its progress before replay/publication."""

    if not isinstance(value, Mapping) or set(value) != _RESULT_FINAL_KEYS:
        raise CheckpointRetentionError(
            "retention result shape is invalid", code=RETENTION_DRIFT_DETECTED
        )
    result = _json_clone(value, "retention result")
    if (
        result.get("schema_version") != CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION
        or result.get("status") != "applied"
        or result.get("failure_code") != ""
        or result.get("plan_hash") != plan.get("plan_hash")
        or result.get("runtime_name") != plan.get("runtime_name")
        or result.get("backup_dir") != str(target)
    ):
        raise CheckpointRetentionError(
            "retention result identity is invalid", code=RETENTION_DRIFT_DETECTED
        )
    applied_at = _timestamp(result.get("applied_at"), "applied_at")
    if applied_at > now:
        raise CheckpointRetentionError(
            "retention result is from the future", code=RETENTION_DRIFT_DETECTED
        )
    validated_state = _validated_progress_state(state, plan)
    if validated_state["status"] != "applied":
        raise CheckpointRetentionError(
            "retention result progress is incomplete", code=RETENTION_DRIFT_DETECTED
        )
    if result.get("completed_phases") != validated_state.get("completed_phases"):
        raise CheckpointRetentionError(
            "retention result phases disagree", code=RETENTION_DRIFT_DETECTED
        )
    metrics_before = _validated_metrics(result.get("metrics_before"))
    plan_metrics = _validated_metrics(plan.get("metrics_before"))
    if metrics_before != plan_metrics:
        raise CheckpointRetentionError(
            "retention result baseline changed", code=RETENTION_DRIFT_DETECTED
        )
    metrics_after = _validated_metrics(result.get("metrics_after"))
    released = result.get("released")
    if not isinstance(released, Mapping) or set(released) != set(_RELEASED_METRIC_KEYS):
        raise CheckpointRetentionError(
            "retention result release metrics are invalid", code=RETENTION_DRIFT_DETECTED
        )
    released_values: dict[str, int] = {}
    for key in _RELEASED_METRIC_KEYS:
        raw = released.get(key)
        if type(raw) is not int:
            raise CheckpointRetentionError(
                "retention result release metrics are invalid", code=RETENTION_DRIFT_DETECTED
            )
        if key != "disk_free_delta" and raw < 0:
            raise CheckpointRetentionError(
                "retention result release metrics are invalid", code=RETENTION_DRIFT_DETECTED
            )
        released_values[key] = raw
    expected_released = _released_metrics(metrics_before, metrics_after)
    if released_values != expected_released:
        raise CheckpointRetentionError(
            "retention result release metrics changed", code=RETENTION_DRIFT_DETECTED
        )
    # An in-progress apply can validate its freshly measured metrics here.  A
    # replay deliberately omits ``live_metrics`` because unrelated retention
    # work may have lowered counts after this result was published.
    if live_metrics is not None:
        live = _validated_metrics(live_metrics)
        for key in _METRIC_KEYS:
            if key == "disk_free_bytes":
                continue
            if live[key] < metrics_after[key]:
                raise CheckpointRetentionError(
                    "retention live metrics regressed",
                    code=RETENTION_DRIFT_DETECTED,
                )
    if type(result.get("backup_pruned_count")) is not int or result["backup_pruned_count"] < 0:
        raise CheckpointRetentionError(
            "retention backup rotation count is invalid", code=RETENTION_DRIFT_DETECTED
        )
    rotation_code = result.get("backup_rotation_failure_code")
    if rotation_code not in {"", RETENTION_BACKUP_FAILED.lower()}:
        raise CheckpointRetentionError(
            "retention backup rotation status is invalid", code=RETENTION_DRIFT_DETECTED
        )
    return True


def _verify_backup_manifest(
    target: Path,
    manifest: Mapping[str, object],
    plan: Mapping[str, object],
    *,
    allow_partial: bool = False,
) -> None:
    if (
        set(manifest) != {"schema_version", "plan_hash", "files"}
        or manifest.get("schema_version") != CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION
        or manifest.get("plan_hash") != plan["plan_hash"]
        or not isinstance(manifest.get("files"), list)
    ):
        raise CheckpointRetentionError("backup manifest is invalid")
    checkpoint_payload = _checkpoint_payload(plan)
    trace_payload = _trace_payload(plan)
    expected_layout: dict[tuple[str, str], str] = {}
    if _checkpoint_database_candidate_total(checkpoint_payload):
        expected_layout[("sqlite", CHECKPOINT_DATABASE)] = f"sqlite/{CHECKPOINT_DATABASE}"
    if _required_nonnegative_int(trace_payload, "event_count"):
        expected_layout[("sqlite", TRACE_DATABASE)] = f"sqlite/{TRACE_DATABASE}"
    expected_artifacts: dict[str, tuple[int, str]] = {}
    for item in _candidate_artifact_metadata(checkpoint_payload):
        if not item["backup_required"]:
            continue
        source = item["storage_key"]
        if not isinstance(source, str):
            raise CheckpointRetentionError("backup artifact source is invalid")
        expected_layout[("artifact", source)] = f"artifacts/{source}"
        expected_artifacts[source] = (
            _required_positive_int(item, "byte_size"),
            _required_sha256(item, "sha256"),
        )
    expected_keys = set(expected_layout)
    seen_keys: set[tuple[str, str]] = set()
    for raw in manifest["files"]:  # type: ignore[index]
        if not isinstance(raw, dict) or set(raw) != {
            "kind", "source", "backup", "bytes", "sha256"
        } or any(type(raw.get(key)) is not str for key in ("kind", "source", "backup", "sha256")):
            raise CheckpointRetentionError("backup manifest file entry is invalid")
        if type(raw.get("bytes")) is not int or raw["bytes"] < 0:
            raise CheckpointRetentionError("backup manifest file entry is invalid")
        if not _SHA256_RE.fullmatch(raw["sha256"]):
            raise CheckpointRetentionError("backup manifest file entry is invalid")
        key = (raw["kind"], raw["source"])
        expected_backup = expected_layout.get(key)
        if expected_backup is None or raw["backup"] != expected_backup or key in seen_keys:
            raise CheckpointRetentionError("backup manifest scope or path is invalid")
        relative = _safe_relative_path(raw["backup"], "backup path")
        backup = _absolute_path(target / Path(*relative.parts))
        _reject_linked_path(backup, stop=target)
        byte_size = raw["bytes"]
        digest = raw["sha256"]
        if (
            not _is_relative_to(backup, target)
            or not backup.is_file()
            or backup.stat().st_size != byte_size
            or _sha256_file(backup) != digest
        ):
            raise CheckpointRetentionError(
                "backup file verification failed", code=RETENTION_BACKUP_FAILED
            )
        _reject_linked_path(backup, stop=target)
        if raw["kind"] == "sqlite":
            _verify_sqlite_integrity(backup)
        elif raw["kind"] == "artifact":
            if expected_artifacts.get(raw["source"]) != (byte_size, digest):
                raise CheckpointRetentionError("backup artifact scope is invalid")
        else:
            raise CheckpointRetentionError("backup manifest kind is invalid")
        seen_keys.add(key)

    # The manifest is also a directory boundary.  A validly hashed file that
    # is omitted from the manifest must not be smuggled into a backup target.
    # During a resumable partial backup, deterministic data paths are allowed
    # before their manifest entry is committed; a complete terminal manifest
    # must enumerate every one of them.
    allowed_data = set(expected_layout.values()) if allow_partial else {
        expected_layout[key] for key in seen_keys
    }
    _verify_backup_directory_entries(
        target,
        allowed_data=allowed_data,
        allow_partial=allow_partial,
    )
    if not allow_partial and seen_keys != expected_keys:
        raise CheckpointRetentionError(
            "backup manifest is incomplete", code=RETENTION_BACKUP_FAILED
        )


def _verify_backup_directory_entries(
    target: Path, *, allowed_data: set[str], allow_partial: bool
) -> None:
    """Reject files, directories, links and reparse points outside the target layout."""

    controls = {"plan.json", "backup_manifest.json", "state.json", "result.json", "failure.json"}
    allowed_files = controls | set(allowed_data)
    allowed_dirs = {PurePosixPath(".")}
    for relative in allowed_data:
        parsed = PurePosixPath(relative)
        for index in range(1, len(parsed.parts)):
            allowed_dirs.add(PurePosixPath(*parsed.parts[:index]))
    try:
        if not target.is_dir() or _is_reparse_or_symlink(target):
            raise CheckpointRetentionError("backup target directory is invalid")
        for entry in target.rglob("*"):
            if _is_reparse_or_symlink(entry):
                raise CheckpointRetentionError("backup target contains a link")
            _reject_linked_path(entry, stop=target)
            resolved = _absolute_path(entry).resolve(strict=True)
            if not _is_relative_to(resolved, target):
                raise CheckpointRetentionError("backup target escaped its root")
            relative = PurePosixPath(resolved.relative_to(target).as_posix())
            if entry.is_dir():
                if relative not in allowed_dirs:
                    raise CheckpointRetentionError("backup target contains an unknown directory")
            elif entry.is_file():
                if relative.as_posix() not in allowed_files:
                    raise CheckpointRetentionError("backup target contains an extra file")
            else:
                raise CheckpointRetentionError("backup target contains an unsafe entry")
    except CheckpointRetentionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CheckpointRetentionError("backup target cannot be inspected", code=RETENTION_IO_FAILED) from exc


def _artifact_backup_sources(
    checkpoint_payload: Mapping[str, object], runtime: Path
) -> list[dict[str, object]]:
    root = _fixed_source_path(runtime, CHECKPOINT_ARTIFACT_ROOT)
    values: list[dict[str, object]] = []
    for metadata in _candidate_artifact_metadata(checkpoint_payload):
        if not metadata["backup_required"]:
            continue
        relative = _safe_relative_path(
            str(metadata["storage_key"]), "artifact storage key"
        )
        path = _absolute_path(root / Path(*relative.parts))
        _reject_linked_path(path, stop=root)
        byte_size = _required_positive_int(metadata, "byte_size")
        digest = str(metadata["sha256"])
        if (
            not _is_relative_to(path, root)
            or not path.is_file()
            or not _SHA256_RE.fullmatch(digest)
            or path.stat().st_size != byte_size
            or _sha256_file(path) != digest
        ):
            raise CheckpointRetentionError(
                "candidate artifact file drift detected",
                code=RETENTION_DRIFT_DETECTED,
            )
        _reject_linked_path(path, stop=root)
        values.append({**metadata, "path": path})
    return values


def _candidate_artifact_metadata(
    checkpoint_payload: Mapping[str, object],
) -> list[dict[str, object]]:
    by_key: dict[str, dict[str, object]] = {}
    for group in ("artifact_candidates", "orphan_candidates"):
        for raw in _required_list(checkpoint_payload, group):
            if not isinstance(raw, dict):
                raise CheckpointRetentionError("artifact retention candidate is invalid")
            key = raw.get("storage_key")
            digest = raw.get("sha256")
            byte_size = raw.get("byte_size")
            if (
                type(key) is not str
                or not key
                or not re.fullmatch(r"blobs/[0-9a-f]{2}/[0-9a-f]{64}\.bin", key)
                or type(digest) is not str
                or not _SHA256_RE.fullmatch(digest)
                or type(byte_size) is not int
                or byte_size <= 0
            ):
                raise CheckpointRetentionError("artifact retention candidate is invalid")
            if key != f"blobs/{digest[:2]}/{digest}.bin":
                raise CheckpointRetentionError("artifact retention candidate is inconsistent")
            # A missing Artifact is already a logical cleanup candidate whose
            # blob/file is absent.  Requiring a physical backup for it would
            # make a valid plan impossible to apply.  Orphans still need a
            # backup while they are present so cleanup remains reversible.
            if group == "orphan_candidates":
                reason = raw.get("reason")
                file_present = raw.get("file_present")
                if reason not in {"refcount_mismatch", "zero_ref_blob", "unindexed_file"}:
                    raise CheckpointRetentionError("artifact retention candidate is invalid")
                if type(file_present) is not bool:
                    raise CheckpointRetentionError("artifact retention candidate is invalid")
                backup_required = file_present
            else:
                status = raw.get("status")
                reason = raw.get("reason")
                if status not in {"available", "purged"} or reason not in {
                    "expired", "missing", "tombstone"
                }:
                    raise CheckpointRetentionError("artifact retention candidate is invalid")
                backup_required = status == "available" and reason != "missing"
            value = {
                "storage_key": key,
                "sha256": digest,
                "byte_size": byte_size,
                "backup_required": backup_required,
            }
            previous = by_key.get(key)
            if previous is not None:
                if (
                    previous["sha256"] != value["sha256"]
                    or previous["byte_size"] != value["byte_size"]
                ):
                    raise CheckpointRetentionError("artifact storage candidate is ambiguous")
                previous["backup_required"] = bool(
                    previous["backup_required"] or backup_required
                )
            else:
                by_key[key] = value
    return [by_key[key] for key in sorted(by_key)]


def _candidate_artifact_bytes(checkpoint_payload: Mapping[str, object]) -> int:
    return sum(
        _required_positive_int(item, "byte_size")
        for item in _candidate_artifact_metadata(checkpoint_payload)
        if item["backup_required"]
    )


def _checkpoint_candidate_total(checkpoint_payload: Mapping[str, object]) -> int:
    return sum(
        len(_required_list(checkpoint_payload, key))
        for key in (
            "checkpoint_candidates", "artifact_candidates", "audit_candidates", "orphan_candidates"
        )
    )


def _checkpoint_database_candidate_total(
    checkpoint_payload: Mapping[str, object],
) -> int:
    """Count candidates that mutate the checkpoint SQLite store.

    An ``unindexed_file`` orphan is a physical cleanup only; it does not bind
    the plan to a database identity.  Every other candidate requires the
    database to still be the store from which the plan was read.
    """

    total = sum(
        len(_required_list(checkpoint_payload, key))
        for key in ("checkpoint_candidates", "artifact_candidates", "audit_candidates")
    )
    total += sum(
        1
        for item in _required_list(checkpoint_payload, "orphan_candidates")
        if not isinstance(item, Mapping) or item.get("reason") != "unindexed_file"
    )
    return total


def _checkpoint_payload(plan: Mapping[str, object]) -> dict[str, object]:
    outer = plan.get("checkpoint_store")
    payload = outer.get("retention_plan") if isinstance(outer, Mapping) else None
    if not isinstance(payload, dict):
        raise CheckpointRetentionError("checkpoint retention plan is invalid")
    return payload


def _trace_payload(plan: Mapping[str, object]) -> dict[str, object]:
    outer = plan.get("trace_store")
    payload = outer.get("cleanup_snapshot") if isinstance(outer, Mapping) else None
    if not isinstance(payload, dict):
        raise CheckpointRetentionError("trace cleanup snapshot is invalid")
    return payload


def _copy_verified_artifact(
    source: Path, destination: Path, metadata: Mapping[str, object]
) -> None:
    _reject_linked_path(source)
    _reject_linked_path(destination, stop=destination.parent)
    if not _lexists(source) or not source.is_file():
        raise CheckpointRetentionError(
            "artifact backup source is unavailable", code=RETENTION_BACKUP_FAILED
        )
    expected_bytes = int(metadata["byte_size"])
    expected_hash = str(metadata["sha256"])
    before = _sha256_file(source)
    shutil.copy2(source, destination)
    if (
        before != expected_hash
        or source.stat().st_size != expected_bytes
        or destination.stat().st_size != expected_bytes
        or _sha256_file(destination) != expected_hash
        or _sha256_file(source) != expected_hash
    ):
        raise CheckpointRetentionError(
            "artifact backup verification failed", code=RETENTION_BACKUP_FAILED
        )


def _backup_sqlite(source: Path, destination: Path) -> None:
    _reject_linked_path(source)
    _reject_linked_path(destination, stop=destination.parent)
    if not _lexists(source) or not source.is_file():
        raise CheckpointRetentionError(
            "SQLite backup source is unavailable", code=RETENTION_BACKUP_FAILED
        )
    uri = f"file:{_absolute_path(source).resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(destination, timeout=5.0)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.commit()
        # The source stores use WAL.  The backup API can leave destination
        # sidecars beside the copied main file; the retention artifact is a
        # single verified SQLite file, so remove those transient companions
        # before publishing the manifest.
        _verify_sqlite_integrity(destination)
        _remove_sqlite_sidecars(destination)
    except sqlite3.Error as exc:
        raise CheckpointRetentionError(
            "SQLite online backup failed", code=RETENTION_BACKUP_FAILED
        ) from exc


def _remove_sqlite_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if _lexists(sidecar):
            _reject_linked_path(sidecar, stop=database.parent)
            if not sidecar.is_file():
                raise CheckpointRetentionError(
                    "SQLite backup sidecar is unsafe", code=RETENTION_BACKUP_FAILED
                )
            sidecar.unlink()


def _verify_sqlite_integrity(path: Path) -> None:
    # ``immutable`` keeps a read-only integrity check from creating WAL/SHM
    # sidecars next to the verified backup file.
    _reject_linked_path(path)
    if not _lexists(path) or not path.is_file():
        raise CheckpointRetentionError(
            "SQLite backup path is unavailable", code=RETENTION_BACKUP_FAILED
        )
    uri = f"file:{_absolute_path(path).resolve().as_posix()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise CheckpointRetentionError(
            "SQLite backup integrity check failed", code=RETENTION_BACKUP_FAILED
        ) from exc
    if row is None or str(row[0]).lower() != "ok":
        raise CheckpointRetentionError(
            "SQLite backup integrity check failed", code=RETENTION_BACKUP_FAILED
        )


def _trace_candidates_already_satisfied(
    store: SQLiteTraceEventStore, snapshot: TraceCleanupSnapshot
) -> bool:
    try:
        actual_store_id = store.store_id()
    except (TraceCleanupDriftError, OSError, sqlite3.Error) as exc:
        raise CheckpointRetentionError(
            "trace store identity is unavailable", code=RETENTION_DRIFT_DETECTED
        ) from exc
    if actual_store_id != snapshot.store_id:
        raise CheckpointRetentionError(
            "trace store identity changed", code=RETENTION_DRIFT_DETECTED
        )
    if not snapshot.candidates:
        return True
    try:
        missing = [
            not store.events_for_trace(candidate.trace_id, limit=1)
            for candidate in snapshot.candidates
        ]
    except (TraceCleanupDriftError, OSError, sqlite3.Error) as exc:
        raise CheckpointRetentionError(
            "trace cleanup state is unavailable", code=RETENTION_DRIFT_DETECTED
        ) from exc
    if all(missing):
        return True
    if any(missing):
        raise CheckpointRetentionError(
            "trace cleanup has an ambiguous partial result",
            code=RETENTION_DRIFT_DETECTED,
        )
    return False


def _released_metrics(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    return {
        "checkpoint_rows": max(0, before["checkpoint_rows"] - after["checkpoint_rows"]),
        "artifact_rows": max(0, before["artifact_rows"] - after["artifact_rows"]),
        "audit_rows": max(0, before["audit_rows"] - after["audit_rows"]),
        "trace_rows": max(0, before["trace_rows"] - after["trace_rows"]),
        "artifact_files": max(0, before["artifact_files"] - after["artifact_files"]),
        "artifact_bytes": max(0, before["artifact_bytes"] - after["artifact_bytes"]),
        "managed_artifact_bytes": max(
            0, before["managed_artifact_bytes"] - after["managed_artifact_bytes"]
        ),
        "disk_free_delta": after["disk_free_bytes"] - before["disk_free_bytes"],
    }


def _prune_completed_backups(
    backup_root: Path,
    *,
    runtime_name: str,
    keep_runs: int,
    preserve: Path,
) -> int:
    """Remove only older terminal runs created by this coordinator."""

    _reject_linked_path(backup_root)
    if not _lexists(backup_root):
        return 0
    if not backup_root.is_dir():
        raise CheckpointRetentionError(
            "backup rotation root is not a directory", code=RETENTION_PATH_REJECTED
        )
    pattern = f"checkpoint_retention_{runtime_name}_"
    terminal: list[Path] = []
    for date_dir in backup_root.iterdir():
        _reject_linked_path(date_dir, stop=backup_root)
        if not date_dir.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name):
            continue
        for candidate in date_dir.iterdir():
            _reject_linked_path(candidate, stop=backup_root)
            if (
                candidate.is_dir()
                and candidate.name.startswith(pattern)
                and re.fullmatch(re.escape(pattern) + r"[0-9a-f]{16}", candidate.name)
                and (
                    _control_file_present(
                        candidate / "result.json", "retention result", required=False
                    )
                    or _control_file_present(
                        candidate / "failure.json", "retention failure", required=False
                    )
                )
            ):
                terminal.append(_absolute_path(candidate))
    terminal.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    keep = {_absolute_path(preserve), *terminal[:keep_runs]}
    removed = 0
    for candidate in terminal:
        if candidate in keep:
            continue
        if not _is_relative_to(candidate, backup_root) or candidate == backup_root:
            raise CheckpointRetentionError(
                "backup rotation target escaped root", code=RETENTION_PATH_REJECTED
            )
        _reject_linked_path(candidate, stop=backup_root)
        shutil.rmtree(candidate)
        removed += 1
    return removed


def _safe_prune_backup_runs(
    backup_root: Path,
    *,
    runtime_name: str,
    keep_runs: int,
    preserve: Path,
) -> tuple[int, str]:
    try:
        return (
            _prune_completed_backups(
                backup_root,
                runtime_name=runtime_name,
                keep_runs=keep_runs,
                preserve=preserve,
            ),
            "",
        )
    except (CheckpointRetentionError, OSError):
        return 0, RETENTION_BACKUP_FAILED.lower()


def _validate_fixed_layout(plan: Mapping[str, object], runtime: Path) -> None:
    checkpoint = plan["checkpoint_store"]
    trace = plan["trace_store"]
    assert isinstance(checkpoint, Mapping) and isinstance(trace, Mapping)
    for relative in (
        str(checkpoint["database"]),
        str(checkpoint["artifact_root"]),
        str(trace["database"]),
    ):
        path = _fixed_source_path(runtime, relative)
        if path.exists():
            _reject_linked_path(path, stop=runtime)


def _validated_metrics(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(_METRIC_KEYS):
        raise CheckpointRetentionError("retention metrics are invalid")
    return {key: _required_nonnegative_int(value, key) for key in _METRIC_KEYS}


def _backup_keep_runs(plan: Mapping[str, object]) -> int:
    policy = plan.get("policy")
    if not isinstance(policy, Mapping):
        raise CheckpointRetentionError("retention policy is invalid")
    return _positive_setting(policy.get("backup_keep_runs"), "backup_keep_runs")


def _require_capacity(value: object) -> EvidenceCapacityPolicyV1:
    if not isinstance(value, EvidenceCapacityPolicyV1):
        raise CheckpointRetentionError(
            "all seven capacity settings are required",
            code=RETENTION_CAPACITY_INVALID,
        )
    return value


def _positive_setting(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise CheckpointRetentionError(
            f"{name} must be a positive integer", code=RETENTION_CAPACITY_INVALID
        )
    return value


def _fixed_source_path(runtime: Path, relative: str) -> Path:
    parsed = _safe_relative_path(relative, "runtime source path")
    path = _absolute_path(runtime / Path(*parsed.parts))
    if path == runtime or not _is_relative_to(path, runtime):
        raise CheckpointRetentionError(
            "runtime source path escaped its root", code=RETENTION_PATH_REJECTED
        )
    _reject_linked_path(path, stop=runtime)
    return path


def _safe_relative_path(value: str, name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise CheckpointRetentionError(f"{name} is invalid", code=RETENTION_PATH_REJECTED)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CheckpointRetentionError(f"{name} is invalid", code=RETENTION_PATH_REJECTED)
    return path


def _resolved_directory(value: str | Path, name: str) -> Path:
    path = _absolute_path(value)
    _reject_linked_path(path)
    if not path.is_dir() or not path.exists():
        raise CheckpointRetentionError(
            f"{name} is missing or not a directory", code=RETENTION_PATH_REJECTED
        )
    return path


def _nearest_existing_parent(path: Path) -> Path:
    current = _absolute_path(path)
    while not _lexists(current):
        parent = current.parent
        if parent == current:
            raise CheckpointRetentionError(
                "backup root has no existing parent", code=RETENTION_PATH_REJECTED
            )
        current = parent
    _reject_linked_path(current)
    if not current.is_dir():
        raise CheckpointRetentionError(
            "backup root parent is not a directory", code=RETENTION_PATH_REJECTED
        )
    return current


def _reject_linked_path(path: Path, *, stop: Path | None = None) -> None:
    current = _absolute_path(path)
    boundary = _absolute_path(stop) if stop is not None else None
    while True:
        if _lexists(current) and _is_reparse_or_symlink(current):
            raise CheckpointRetentionError(
                "maintenance path cannot contain links or reparse points",
                code=RETENTION_PATH_REJECTED,
            )
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CheckpointRetentionError(
            "maintenance path metadata is unavailable", code=RETENTION_PATH_REJECTED
        ) from exc
    return path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _absolute_path(value: str | Path) -> Path:
    """Normalize a path without resolving links that must be rejected."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validated_runtime_name(value: object) -> str:
    if type(value) is not str:
        raise CheckpointRetentionError("runtime name is invalid")
    name = value.strip()
    if not _RUNTIME_NAME_RE.fullmatch(name):
        raise CheckpointRetentionError("runtime name is invalid")
    return name


def _timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CheckpointRetentionError(f"{name} must be an aware timestamp") from exc
    else:
        raise CheckpointRetentionError(f"{name} must be an aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CheckpointRetentionError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _runtime_lock(runtime: Path) -> Lock:
    key = os.path.normcase(str(runtime))
    with _RUN_LOCK_GUARD:
        return _RUN_LOCKS.setdefault(key, Lock())


@contextmanager
def _execution_lock(runtime: Path):
    process_lock = _runtime_lock(runtime)
    if not process_lock.acquire(blocking=False):
        raise CheckpointRetentionError(
            "checkpoint retention run is already active",
            code=RETENTION_ALREADY_RUNNING,
        )
    stream = None
    os_locked = False
    try:
        lock_path = runtime / ".checkpoint_retention.lock"
        # Use lstat semantics before opening.  ``Path.exists()`` is false for
        # a dangling link, which would otherwise let ``open`` follow it.
        _reject_linked_path(lock_path, stop=runtime)
        if _lexists(lock_path):
            try:
                details = lock_path.lstat()
            except FileNotFoundError:
                details = None
            except OSError as exc:
                raise CheckpointRetentionError(
                    "retention lock metadata is unavailable",
                    code=RETENTION_PATH_REJECTED,
                ) from exc
            if details is not None and not stat.S_ISREG(details.st_mode):
                raise CheckpointRetentionError(
                    "retention lock must be a regular file",
                    code=RETENTION_PATH_REJECTED,
                )
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            os_locked = True
        except (OSError, BlockingIOError) as exc:
            raise CheckpointRetentionError(
                "checkpoint retention run is already active",
                code=RETENTION_ALREADY_RUNNING,
            ) from exc
        yield
    finally:
        if stream is not None:
            if os_locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()
        process_lock.release()


def _bounded_counter(value: int) -> int:
    return min(2_147_483_647, max(0, int(value)))


def _exception_code(exc: BaseException) -> str:
    if isinstance(exc, CheckpointRetentionError):
        return exc.code
    if isinstance(exc, (EvidenceMaintenanceError, TraceCleanupDriftError)):
        return RETENTION_DRIFT_DETECTED
    return RETENTION_APPLY_FAILED


def _persist_retention_failure(
    target: Path,
    *,
    backup_base: Path,
    plan: Mapping[str, object],
    state: Mapping[str, object],
    phases: Mapping[str, object],
    current: datetime,
    exc: BaseException,
) -> None:
    """Persist a bounded failure marker for a resumable apply attempt.

    Backup creation can fail before the normal phase loop has initialized its
    progress file.  When a deterministic target directory exists, publish a
    valid prepared/partial/applied state first, then publish the redacted
    failure marker.  No exception text is persisted.
    """

    if not _lexists(target):
        return
    _reject_linked_path(target, stop=backup_base)
    if not target.is_dir():
        raise CheckpointRetentionError(
            "retention backup target is not a directory",
            code=RETENTION_PATH_REJECTED,
        )
    _reject_linked_path(target, stop=target)

    state_path = target / "state.json"
    _reject_linked_path(state_path, stop=target)
    state_payload: dict[str, object] | None = None

    # Prefer the last durable state on disk.  The phase loop mutates its
    # in-memory dictionary before each atomic write; if that write fails, the
    # in-memory value can describe work that was never committed to state.json.
    if _control_file_present(state_path, "retention progress state", required=False):
        try:
            durable = _read_json_object(state_path, "retention progress state")
            checked = _validated_progress_state(durable, plan)
            state_payload = {
                "schema_version": checked["schema_version"],
                "plan_hash": checked["plan_hash"],
                "status": checked["status"],
                "completed_phases": dict(checked["completed_phases"]),
            }
        except Exception:
            state_payload = None

    if state_payload is None:
        completed = dict(phases)
        candidate = {
            "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
            "plan_hash": plan["plan_hash"],
            "status": (
                "prepared"
                if not completed
                else "applied"
                if set(completed) == set(_PHASE_NAMES)
                else "partial"
            ),
            "completed_phases": completed,
        }
        try:
            checked = _validated_progress_state(candidate, plan)
        except Exception:
            # A failure marker must itself remain resumable.  If the
            # speculative phase result cannot be validated, retain only the
            # conservative prepared state and let the next attempt replay the
            # plan from its frozen inputs.
            checked = {
                "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
                "plan_hash": plan["plan_hash"],
                "status": "prepared",
                "completed_phases": {},
            }
        state_payload = {
            "schema_version": checked["schema_version"],
            "plan_hash": checked["plan_hash"],
            "status": checked["status"],
            "completed_phases": dict(checked["completed_phases"]),
        }
    _atomic_write_json(state_path, state_payload)

    failure: dict[str, object] = {
        "schema_version": CHECKPOINT_RETENTION_STATE_SCHEMA_VERSION,
        "plan_hash": plan["plan_hash"],
        "status": "failed_closed",
        "failure_code": _exception_code(exc),
        "failed_at": current.isoformat(),
        "completed_phase_names": sorted(
            str(name) for name in state_payload["completed_phases"]
        ),
    }
    _atomic_write_json(target / "failure.json", failure)
    pruned, prune_code = _safe_prune_backup_runs(
        backup_base,
        runtime_name=str(plan["runtime_name"]),
        keep_runs=_backup_keep_runs(plan),
        preserve=target,
    )
    failure["backup_pruned_count"] = pruned
    failure["backup_rotation_failure_code"] = prune_code
    _atomic_write_json(target / "failure.json", failure)


def _object_dict(value: object, name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        payload = value
    else:
        method = getattr(value, "to_dict", None)
        if not callable(method):
            raise CheckpointRetentionError(f"{name} is not serializable")
        payload = method()
    if not isinstance(payload, Mapping):
        raise CheckpointRetentionError(f"{name} is not an object")
    return _json_clone(payload, name)


def _json_clone(value: object, name: str) -> dict[str, object]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckpointRetentionError(f"{name} is not canonical JSON") from exc
    if not isinstance(decoded, dict):
        raise CheckpointRetentionError(f"{name} must be an object")
    return decoded


def _required_list(value: Mapping[str, object], key: str) -> list[object]:
    result = value.get(key)
    if not isinstance(result, list):
        raise CheckpointRetentionError(f"retention field {key} must be a list")
    return result


def _required_nonnegative_int(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if type(result) is not int or result < 0:
        raise CheckpointRetentionError(f"retention field {key} must be nonnegative")
    return result


def _required_positive_int(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if type(result) is not int or result <= 0:
        raise CheckpointRetentionError(f"retention field {key} must be positive")
    return result


def _required_sha256(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if type(result) is not str or not _SHA256_RE.fullmatch(result):
        raise CheckpointRetentionError(f"retention field {key} must be a SHA-256 digest")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise CheckpointRetentionError(
            "maintenance file could not be hashed", code=RETENTION_IO_FAILED
        ) from exc
    return digest.hexdigest()


def _control_file_present(
    path: Path, name: str, *, required: bool
) -> bool:
    """Check a retention control path without following links or devices."""

    _reject_linked_path(path)
    if not _lexists(path):
        if required:
            raise CheckpointRetentionError(
                f"{name} is unreadable", code=RETENTION_IO_FAILED
            )
        return False
    try:
        details = path.lstat()
    except FileNotFoundError:
        if required:
            raise CheckpointRetentionError(
                f"{name} is unreadable", code=RETENTION_IO_FAILED
            )
        return False
    except OSError as exc:
        raise CheckpointRetentionError(
            f"{name} metadata is unavailable", code=RETENTION_IO_FAILED
        ) from exc
    if path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise CheckpointRetentionError(
            f"{name} cannot be a link", code=RETENTION_PATH_REJECTED
        )
    if not stat.S_ISREG(details.st_mode):
        raise CheckpointRetentionError(
            f"{name} is not a regular file", code=RETENTION_PATH_REJECTED
        )
    return True


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    _reject_linked_path(path.parent)
    _control_file_present(path, "maintenance state", required=False)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CheckpointRetentionError(
            "maintenance state could not be written", code=RETENTION_IO_FAILED
        ) from exc


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    _reject_linked_path(path.parent)
    _control_file_present(path, "maintenance state", required=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_linked_path(path.parent)
    descriptor: int | None = None
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
        raise CheckpointRetentionError(
            "maintenance progress could not be persisted", code=RETENTION_IO_FAILED
        ) from exc


def _remove_control_file(path: Path) -> None:
    """Remove a coordinator control file without following links."""

    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CheckpointRetentionError(
            "maintenance control file metadata is unavailable",
            code=RETENTION_IO_FAILED,
        ) from exc

    if path.is_symlink() or bool(
        getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise CheckpointRetentionError(
            "maintenance control file cannot be a link",
            code=RETENTION_PATH_REJECTED,
        )
    if not stat.S_ISREG(details.st_mode):
        raise CheckpointRetentionError(
            "maintenance control file is not a regular file",
            code=RETENTION_PATH_REJECTED,
        )
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CheckpointRetentionError(
            "maintenance control file could not be removed",
            code=RETENTION_IO_FAILED,
        ) from exc


def _read_json_object(path: Path, name: str) -> dict[str, object]:
    # Control files are part of the retention trust boundary.  Reject links,
    # reparse points, and non-regular files before opening them.
    _control_file_present(path, name, required=True)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointRetentionError(f"{name} is unreadable", code=RETENTION_IO_FAILED) from exc
    if not isinstance(value, dict):
        raise CheckpointRetentionError(f"{name} must be an object")
    return value

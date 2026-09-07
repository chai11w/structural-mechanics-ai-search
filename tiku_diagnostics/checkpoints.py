"""Audited local evidence diagnostics and explicitly confirmed management."""

from dataclasses import asdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3

from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1, RETENTION_POLICIES
from tiku_agent.checkpoint_store import (
    CheckpointQueryScopeV1, SQLiteCheckpointStore, EvidenceConflictError,
    EvidenceValidationError, EvidenceNotFoundError, EvidenceExpiredError,
    EvidenceUnavailableError, utc_now,
)
from . import checkpoint_retention as maintenance


PLAN_KEYS = frozenset({
    "schema_version", "operation", "created_at", "expires_at", "actor_key", "scope",
    "store_id", "runtime_binding", "capacity", "checkpoint_id", "artifact_id",
    "target_fingerprint", "new_expires_at", "retention_class", "reason_code", "plan_hash",
})


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("invalid evidence timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise EvidenceValidationError("evidence timestamp requires a timezone")
    return result.astimezone(UTC)


def summary(checkpoint) -> dict:
    return {key: value for key, value in checkpoint.to_dict().items() if key in {
        "checkpoint_id", "trace_id", "stage", "outcome", "occurred_at", "expires_at",
        "owner", "predecessor_checkpoint_id", "artifacts", "retention_class", "failure",
    }}


class CheckpointDiagnosticService:
    """Local operators must supply a scope; no browser or remote authentication is implied."""

    def __init__(self, runtime_root, *, capacity: EvidenceCapacityPolicyV1,
                 actor_key: str, scope: CheckpointQueryScopeV1, clock=utc_now):
        if not isinstance(actor_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}", actor_key):
            raise EvidenceValidationError("invalid diagnostic actor")
        if actor_key.upper().startswith("TIKU-") or type(scope) is not CheckpointQueryScopeV1:
            raise EvidenceValidationError("diagnostics requires safe actor and exact scope")
        self.runtime = maintenance._absolute_path(runtime_root)
        maintenance._reject_linked_path(self.runtime)
        if not self.runtime.is_dir():
            raise EvidenceNotFoundError("diagnostic runtime is unavailable")
        self.actor_key = actor_key
        self.scope = scope
        self.store = SQLiteCheckpointStore(
            self.runtime / maintenance.CHECKPOINT_DATABASE,
            artifact_root=self.runtime / maintenance.CHECKPOINT_ARTIFACT_ROOT,
            trace_db_path=self.runtime / maintenance.TRACE_DATABASE,
            capacity=capacity, clock=clock,
        )

    def query(self, *, trace_id="", limit=50, after_checkpoint_id="") -> dict:
        records, cursor = self.store.query_checkpoints(self.scope, actor_key=self.actor_key,
            trace_id=trace_id, limit=limit, after_checkpoint_id=after_checkpoint_id)
        return {"schema_version": 1, "checkpoints": [summary(row) for row in records],
                "count": len(records), "truncated": bool(cursor), "next_checkpoint_id": cursor}

    def checkpoint(self, checkpoint_id):
        records, _ = self.store.query_checkpoints(self.scope, actor_key=self.actor_key,
                                                 checkpoint_id=checkpoint_id, limit=1)
        if not records:
            raise EvidenceNotFoundError("checkpoint is unavailable in the requested scope")
        return records[0]

    def artifact(self, checkpoint_id, artifact_id):
        checkpoint = self.checkpoint(checkpoint_id)
        return self.store.read_artifact(artifact_id, checkpoint_id=checkpoint_id,
            actor_key=self.actor_key, expected_checkpoint_owner=checkpoint.owner)

    def chain(self, checkpoint_id, *, limit=20) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise EvidenceValidationError("chain limit must be between 1 and 100")
        checkpoint = self.checkpoint(checkpoint_id)
        anchor = checkpoint.owner
        scope = CheckpointQueryScopeV1(anchor.identity_key, anchor.session_key,
                                      anchor.workflow_search_id, anchor.workflow_task_revision)
        records, seen, stop = [], set(), "complete"
        while checkpoint is not None:
            if checkpoint.checkpoint_id in seen:
                raise EvidenceConflictError("checkpoint predecessor cycle")
            if checkpoint.owner.unit_id and anchor.unit_id and checkpoint.owner.unit_id != anchor.unit_id:
                raise EvidenceConflictError("checkpoint predecessor crosses units")
            if checkpoint.owner.scope == "child_task" and (
                checkpoint.owner.search_id != anchor.search_id or checkpoint.owner.task_revision != anchor.task_revision
            ):
                raise EvidenceConflictError("checkpoint predecessor crosses child versions")
            seen.add(checkpoint.checkpoint_id)
            records.append(summary(checkpoint))
            predecessor = checkpoint.predecessor_checkpoint_id
            if not predecessor:
                break
            if len(records) >= limit:
                stop = "limit"
                break
            previous, _ = self.store.query_checkpoints(scope, actor_key=self.actor_key,
                                                       checkpoint_id=predecessor, limit=1)
            if not previous:
                stop = "unavailable_predecessor"
                break
            checkpoint = previous[0]
        return {"schema_version": 1, "checkpoints": records, "stop_reason": stop,
                "next_checkpoint_id": predecessor if stop != "complete" else ""}

    def _target(self, checkpoint_id, artifact_id):
        checkpoint = self.checkpoint(checkpoint_id)
        if not artifact_id:
            return checkpoint, checkpoint
        artifact = self.store.read_artifact(artifact_id, checkpoint_id=checkpoint_id,
            actor_key=self.actor_key, expected_checkpoint_owner=checkpoint.owner)
        return checkpoint, artifact.descriptor

    def plan(self, operation, *, checkpoint_id, artifact_id="", new_expires_at="",
             retention_class="", reason_code):
        if operation not in {"extend", "delete"}:
            raise EvidenceValidationError("invalid management operation")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason_code):
            raise EvidenceValidationError("management requires a stable reason code")
        checkpoint, target = self._target(checkpoint_id, artifact_id)
        now = self.store._now()
        if operation == "extend":
            expiry = timestamp(new_expires_at)
            selected_class = retention_class or target.retention_class
            if selected_class != target.retention_class and selected_class not in {"investigation", "feedback"}:
                raise EvidenceValidationError("retention change requires investigation or feedback")
            policy = RETENTION_POLICIES[selected_class]
            days = policy.artifact_max_days if artifact_id else policy.checkpoint_max_days
            created = timestamp(target.created_at if artifact_id else target.occurred_at)
            if expiry <= timestamp(target.expires_at) or expiry > created + timedelta(days=days):
                raise EvidenceValidationError("requested extension exceeds the finite retention window")
            new_expires_at, retention_class = expiry.isoformat(), selected_class
        elif new_expires_at or retention_class:
            raise EvidenceValidationError("delete cannot change retention")
        plan = {
            "schema_version": 1, "operation": operation,
            "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=15)).isoformat(),
            "actor_key": self.actor_key, "scope": asdict(self.scope), "store_id": self.store.store_id(),
            "runtime_binding": digest(str(self.store.path)), "capacity": self.store.capacity.to_dict(),
            "checkpoint_id": checkpoint.checkpoint_id, "artifact_id": artifact_id,
            "target_fingerprint": digest(target.to_dict()), "new_expires_at": new_expires_at,
            "retention_class": retention_class, "reason_code": reason_code,
        }
        plan["plan_hash"] = digest(plan)
        return plan

    def _validate_plan(self, plan, expected_hash):
        if type(plan) is not dict or set(plan) != PLAN_KEYS or plan.get("schema_version") != 1:
            raise EvidenceValidationError("invalid evidence management plan")
        actual_hash = digest({key: value for key, value in plan.items() if key != "plan_hash"})
        if not expected_hash or actual_hash != expected_hash or plan["plan_hash"] != actual_hash:
            raise EvidenceConflictError("evidence plan hash does not match")
        if (plan["actor_key"] != self.actor_key or plan["scope"] != asdict(self.scope)
                or plan["capacity"] != self.store.capacity.to_dict()
                or plan["runtime_binding"] != digest(str(self.store.path))
                or plan["store_id"] != self.store.store_id()):
            raise EvidenceConflictError("evidence plan binding changed")
        created, expiry, now = timestamp(plan["created_at"]), timestamp(plan["expires_at"]), self.store._now()
        if not created <= now < expiry or expiry - created > timedelta(minutes=15):
            raise EvidenceConflictError("evidence plan is expired or not yet valid")
        if plan["operation"] not in {"extend", "delete"}:
            raise EvidenceValidationError("invalid management operation")
        if not isinstance(plan["reason_code"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", plan["reason_code"]):
            raise EvidenceValidationError("invalid management reason")

    def apply(self, plan, *, expected_hash, backup_root, repository_root, max_backup_runs):
        self._validate_plan(plan, expected_hash)
        with maintenance._execution_lock(self.runtime):
            self._validate_plan(plan, expected_hash)
            checkpoint, target = self._target(plan["checkpoint_id"], plan["artifact_id"])
            if digest(target.to_dict()) != plan["target_fingerprint"]:
                raise EvidenceConflictError("evidence changed after planning")
            backup = self._backup(plan, checkpoint, backup_root, repository_root, max_backup_runs)
            self._validate_plan(plan, expected_hash)
            target_id = plan["artifact_id"] or plan["checkpoint_id"]
            arguments = dict(actor_key=self.actor_key, expected_owner=target.owner,
                reason_code=plan["reason_code"], expected_fingerprint=plan["target_fingerprint"],
                expected_store_id=plan["store_id"], operation_deadline=timestamp(plan["expires_at"]))
            if plan["operation"] == "extend":
                updated = self.store.extend_retention(target_id, new_expires_at=timestamp(plan["new_expires_at"]),
                    new_retention_class=plan["retention_class"] or None, **arguments)
                result = {"target_id": target_id, "expires_at": updated.expires_at,
                          "retention_class": updated.retention_class}
            else:
                result = asdict(self.store.delete_evidence(target_id, **arguments))
            result.update(status="applied", operation=plan["operation"], plan_hash=plan["plan_hash"],
                          backup_id=backup.name)
            try:
                maintenance._write_json_exclusive(backup / "result.json", result)
                result["receipt_saved"] = True
            except Exception:
                result["receipt_saved"] = False
                result["warning_code"] = "EVIDENCE_RECEIPT_UNAVAILABLE"
            return result

    def _backup(self, plan, checkpoint, backup_root, repository_root, max_backup_runs):
        if type(max_backup_runs) is not int or not 1 <= max_backup_runs <= 10_000:
            raise EvidenceValidationError("invalid management backup capacity")
        if not Path(backup_root).is_absolute():
            raise EvidenceValidationError("backup root must be absolute")
        root = maintenance._absolute_path(backup_root)
        repository = maintenance._absolute_path(repository_root)
        maintenance._reject_linked_path(root)
        maintenance._reject_linked_path(repository)
        if (root == repository or root.is_relative_to(repository) or repository.is_relative_to(root)
                or root == self.runtime or root.is_relative_to(self.runtime) or self.runtime.is_relative_to(root)):
            raise EvidenceValidationError("backup root must be outside the repository and runtime")
        bucket = root / "checkpoint-management"
        maintenance._reject_linked_path(bucket)
        bucket.mkdir(parents=True, exist_ok=True)
        entries = list(bucket.iterdir())
        if len(entries) >= max_backup_runs:
            raise EvidenceUnavailableError("management backup capacity reached")
        target = bucket / plan["plan_hash"]
        target.mkdir(exist_ok=False)
        sources = [self.store.path, Path(str(self.store.path) + "-wal")]
        required = sum(path.stat().st_size for path in sources if path.is_file())
        self._backup_space(target, required)
        database = target / maintenance.CHECKPOINT_DATABASE
        maintenance._backup_sqlite(self.store.path, database)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            store_id = connection.execute("SELECT value FROM checkpoint_store_meta WHERE key = 'store_id'").fetchone()
            if store_id is None or store_id[0] != plan["store_id"]:
                raise EvidenceConflictError("backup store identity does not match the plan")
            table, column = ("artifacts", "artifact_id") if plan["artifact_id"] else ("checkpoints", "checkpoint_id")
            row = connection.execute("SELECT * FROM " + table + " WHERE " + column + " = ?",
                                     (plan["artifact_id"] or plan["checkpoint_id"],)).fetchone()
            if row is None:
                raise EvidenceConflictError("backup target is unavailable")
            backed_up = self.store._descriptor_row(connection, row) if plan["artifact_id"] else self.store._checkpoint_row(connection, row)
            if digest(backed_up.to_dict()) != plan["target_fingerprint"]:
                raise EvidenceConflictError("backup target changed after planning")
        files = {database.name: maintenance._sha256_file(database)}
        unavailable = []
        links = [link for link in checkpoint.artifacts
                 if not plan["artifact_id"] or link.artifact_id == plan["artifact_id"]]
        for link in links:
            try:
                artifact = self.store.read_artifact(link.artifact_id, checkpoint_id=checkpoint.checkpoint_id,
                    actor_key=self.actor_key, expected_checkpoint_owner=checkpoint.owner)
            except (EvidenceExpiredError, EvidenceNotFoundError):
                unavailable.append(link.artifact_id)
                continue
            self._backup_space(target, len(artifact.content))
            filename = link.artifact_id + ".bin"
            with (target / filename).open("xb") as stream:
                stream.write(artifact.content)
            files[filename] = maintenance._sha256_file(target / filename)
            if files[filename] != artifact.descriptor.sha256:
                raise EvidenceUnavailableError("artifact backup hash mismatch")
        self._backup_space(target, 0)
        manifest = {"schema_version": 1, "plan_hash": plan["plan_hash"], "store_id": plan["store_id"],
                    "files": files, "unavailable_artifact_ids": unavailable}
        maintenance._write_json_exclusive(target / "manifest.json", manifest)
        maintenance._write_json_exclusive(target / "plan.json", plan)
        for filename, expected in files.items():
            if maintenance._sha256_file(target / filename) != expected:
                raise EvidenceUnavailableError("management backup verification failed")
        return target

    def _backup_space(self, directory, additional):
        if shutil.disk_usage(directory).free - additional < self.store.capacity.min_free_bytes:
            raise EvidenceUnavailableError("management backup disk capacity reached")

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
import json
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image

from tiku_agent.checkpoint_contract import (
    ARTIFACT_ROLE_SOURCE_PAGE,
    RETENTION_FEEDBACK,
    RETENTION_NORMAL,
    SCOPE_CHILD_TASK,
    SCOPE_WORKFLOW,
    ArtifactLinkV1,
    CheckpointOwnerV1,
    EvidenceCapacityPolicyV1,
    IntermediateCheckpointV1,
    ProducerVersionV1,
    new_checkpoint_id,
)
from tiku_agent.checkpoint_store import (
    EvidenceAuditError,
    EvidenceCapacityError,
    EvidenceConflictError,
    EvidenceExpiredError,
    EvidenceMaintenanceError,
    EvidenceOwnershipError,
    EvidenceUnavailableError,
    EvidenceValidationError,
    SQLiteCheckpointStore,
)


NOW = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def owner(
    *,
    identity: str = "invite_1",
    session: str = "1" * 64,
    workflow: str = "search_parent",
    unit: str = "",
    scope: str = SCOPE_WORKFLOW,
    search: str = "",
) -> CheckpointOwnerV1:
    return CheckpointOwnerV1(
        scope=scope,
        session_key=session,
        identity_key=identity,
        workflow_search_id=workflow,
        search_id=search,
        unit_id=unit,
        workflow_task_revision=2,
        task_revision=2,
    )


def producer() -> ProducerVersionV1:
    return ProducerVersionV1(
        code_revision="2" * 40,
        component="checkpoint_store_test",
        component_version="v1",
    )


def checkpoint(
    *,
    checkpoint_owner: CheckpointOwnerV1 | None = None,
    checkpoint_id: str | None = None,
    artifacts: tuple[ArtifactLinkV1, ...] = (),
    retention_class: str = RETENTION_NORMAL,
) -> IntermediateCheckpointV1:
    return IntermediateCheckpointV1(
        checkpoint_id=checkpoint_id or new_checkpoint_id(),
        trace_id="trace_" + "3" * 32,
        request_id="req_" + "4" * 32,
        stage="image_routed",
        outcome="success",
        occurred_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=30)).isoformat(),
        retention_class=retention_class,
        owner=checkpoint_owner or owner(),
        producer=producer(),
        input_fingerprint="5" * 64,
        result={
            "route_decision": {
                "route": "A3",
                "decision_source": "authority_v1",
                "reason_code": "MULTI_QUESTION_PAGE",
            }
        },
        artifacts=artifacts,
    )


def png_bytes(color: str = "red") -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 3), color).save(stream, format="PNG")
    return stream.getvalue()


class CheckpointStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = MutableClock()

    def policy(self, **changes: int) -> EvidenceCapacityPolicyV1:
        values = {
            "max_checkpoint_rows": 20,
            "max_artifact_rows": 20,
            "max_audit_rows": 30,
            "max_trace_rows": 30,
            "max_artifact_bytes": 1_000_000,
            "min_free_bytes": 1,
            "max_artifacts_per_checkpoint": 5,
        }
        values.update(changes)
        return EvidenceCapacityPolicyV1(**values)

    def store(self, **changes: object) -> SQLiteCheckpointStore:
        values = {
            "artifact_root": self.root / "artifacts",
            "capacity": self.policy(),
            "trace_db_path": self.root / "trace.sqlite3",
            "clock": self.clock,
            "disk_usage": lambda _path: (10_000_000, 0, 10_000_000),
        }
        values.update(changes)
        return SQLiteCheckpointStore(self.root / "evidence.sqlite3", **values)

    def test_read_only_plan_and_snapshot_do_not_create_missing_paths(self) -> None:
        store = self.store()
        self.assertEqual(store.plan_retention().checkpoint_candidates, ())
        self.assertEqual(store.capacity_snapshot().checkpoint_rows, 0)
        self.assertFalse(store.path.exists())
        self.assertFalse(store.artifact_root.exists())

    def test_checkpoint_roundtrip_uses_server_time_and_idempotency_is_strict(self) -> None:
        store = self.store()
        incoming = checkpoint()
        committed = store.put_checkpoint(incoming)
        self.assertEqual(committed.occurred_at, NOW.isoformat())
        self.assertEqual(
            committed.expires_at, (NOW + timedelta(days=30)).isoformat()
        )
        self.clock.value += timedelta(days=1)
        self.assertEqual(store.put_checkpoint(incoming), committed)
        self.assertEqual(
            store.read_checkpoint(
                committed.checkpoint_id,
                actor_key="diagnostics",
                expected_owner=committed.owner,
            ),
            committed,
        )
        changed = replace(incoming, input_fingerprint="6" * 64)
        with self.assertRaises(EvidenceConflictError):
            store.put_checkpoint(changed)

    def test_same_id_retry_at_exact_expiry_is_rejected(self) -> None:
        store = self.store()
        incoming = checkpoint()
        committed = store.put_checkpoint(incoming)
        self.clock.value = datetime.fromisoformat(committed.expires_at)
        with self.assertRaises(EvidenceExpiredError):
            store.put_checkpoint(incoming)
        with self.assertRaises(EvidenceExpiredError):
            store.read_checkpoint(
                committed.checkpoint_id,
                actor_key="diagnostics",
                expected_owner=committed.owner,
            )

    def test_artifact_real_parse_physical_dedup_and_owner_isolation(self) -> None:
        store = self.store()
        content = png_bytes()
        first = store.put_artifact(owner(), content, retention_class=RETENTION_NORMAL)
        second = store.put_artifact(
            owner(identity="invite_2"), content, retention_class=RETENTION_NORMAL
        )
        repeated = store.put_artifact(owner(), content, retention_class=RETENTION_NORMAL)
        self.assertEqual(first.artifact_id, repeated.artifact_id)
        self.assertNotEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.media_type, "image/png")
        self.assertEqual((first.width_px, first.height_px), (4, 3))
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT ref_count FROM artifact_blobs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 2)
        self.assertEqual(len(list(store.artifact_root.rglob("*.bin"))), 1)
        with self.assertRaises(EvidenceValidationError):
            store.put_artifact(owner(), b"not-image", retention_class=RETENTION_NORMAL)
        with self.assertRaises(EvidenceValidationError):
            store.put_artifact(
                owner(), content, retention_class=RETENTION_NORMAL,
                expected_media_type="image/jpeg",
            )

    def test_artifact_read_requires_checkpoint_link_and_exact_owner(self) -> None:
        store = self.store()
        descriptor = store.put_artifact(
            owner(), png_bytes(), retention_class=RETENTION_NORMAL
        )
        linked = checkpoint(
            artifacts=(ArtifactLinkV1(descriptor.artifact_id, ARTIFACT_ROLE_SOURCE_PAGE),)
        )
        committed = store.put_checkpoint(linked)
        result = store.read_artifact(
            descriptor.artifact_id,
            checkpoint_id=committed.checkpoint_id,
            actor_key="diagnostics",
            expected_checkpoint_owner=committed.owner,
        )
        self.assertEqual(result.content, png_bytes())
        with self.assertRaises(EvidenceOwnershipError):
            store.read_artifact(
                descriptor.artifact_id,
                checkpoint_id=committed.checkpoint_id,
                actor_key="diagnostics",
                expected_checkpoint_owner=owner(identity="invite_2"),
            )

    def test_checkpoint_read_rejects_relationship_and_payload_drift(self) -> None:
        store = self.store()
        descriptor = store.put_artifact(owner(), png_bytes(), retention_class=RETENTION_NORMAL)
        committed = store.put_checkpoint(checkpoint(artifacts=(
            ArtifactLinkV1(descriptor.artifact_id, ARTIFACT_ROLE_SOURCE_PAGE),
        )))
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute(
                "UPDATE checkpoint_artifacts SET role = 'question_crop' WHERE checkpoint_id = ?",
                (committed.checkpoint_id,),
            )
            connection.commit()
        with self.assertRaises(EvidenceUnavailableError):
            store.read_checkpoint(
                committed.checkpoint_id,
                actor_key="diagnostics",
                expected_owner=committed.owner,
            )

from __future__ import annotations

from dataclasses import replace
import json
import unittest
from unittest import mock

from tiku_agent import task_state_contract as contract
from tiku_agent.task_state_public import (
    PUBLIC_TASK_STATE_FIELD,
    PublicTaskStateError,
    empty_public_task_state_snapshot,
    public_task_state_json,
    public_task_state_snapshot,
    with_public_task_state,
)


class TaskStatePublicTests(unittest.TestCase):
    @staticmethod
    def _snapshot() -> contract.TaskStateSnapshotV1:
        workflow = contract.WorkflowStateView(
            exists=True,
            workflow_id="search_workflow_12345678",
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route="A3",
            task_revision=7,
            phase="A2_ACTIVE",
            status=contract.STATUS_RUNNING,
            completed_steps=(
                contract.WORKFLOW_STEP_IMAGE_ACCEPTED,
                contract.WORKFLOW_STEP_ROUTE_DECIDED,
                contract.WORKFLOW_STEP_PAGE_UNDERSTOOD,
                contract.WORKFLOW_STEP_UNIT_CATALOG_READY,
                contract.WORKFLOW_STEP_UNIT_SELECTED,
                contract.WORKFLOW_STEP_CHILD_STARTED,
            ),
            allowed_actions=(contract.ACTION_CANCEL_CURRENT_UNIT,),
            next_stage=contract.NEXT_FOLLOW_CHILD,
        )
        unit = contract.UnitStateView(
            unit_id="g1-u1",
            page_index=1,
            display_label="四-1",
            status=contract.UNIT_ACTIVE,
        )
        child = contract.ChildTaskStateView(
            task_id="search_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id=unit.unit_id,
            task_revision=3,
            phase="WAIT_CANDIDATE_CHOICE",
            status=contract.STATUS_WAITING_USER,
            completed_steps=(
                contract.CHILD_STEP_QUESTION_ACCEPTED,
                contract.CHILD_STEP_QUESTION_ANALYZED,
                contract.CHILD_STEP_CHAPTER_RESOLVED,
                contract.CHILD_STEP_ROUTE_SELECTED,
                contract.CHILD_STEP_SEARCH_COMPLETED,
                contract.CHILD_STEP_CANDIDATES_READY,
            ),
            allowed_actions=(contract.ACTION_SELECT_CANDIDATE,),
            next_stage=contract.NEXT_SELECT_CANDIDATE,
            chapter="2静定结构",
            candidate_count=2,
            candidate_generation="3:1",
        )
        return contract.TaskStateSnapshotV1(
            workflow=workflow,
            active_child_task=child,
            current_unit=unit,
            units=(unit,),
        )

    def test_empty_projection_is_explicit_and_exact(self):
        public = empty_public_task_state_snapshot()
        self.assertEqual(public, contract.empty_task_state_snapshot().to_dict())
        self.assertEqual(set(public), {
            "schema_version",
            "workflow",
            "active_child_task",
            "current_unit",
            "units",
            "consistency",
        })

    def test_projection_is_exact_json_safe_and_detached(self):
        snapshot = self._snapshot()
        public = public_task_state_snapshot(snapshot)
        encoded = public_task_state_json(snapshot)

        self.assertEqual(json.loads(encoded), public)
        self.assertEqual(public["schema_version"], 1)
        self.assertEqual(public["workflow"]["task_revision"], 7)
        self.assertEqual(public["active_child_task"]["task_revision"], 3)
        self.assertEqual(public["current_unit"]["unit_id"], "g1-u1")
        self.assertEqual(public["units"][0]["display_label"], "四-1")
        self.assertEqual(set(public["workflow"]), {
            "exists",
            "workflow_id",
            "kind",
            "route",
            "task_revision",
            "phase",
            "status",
            "completed_steps",
            "allowed_actions",
            "next_stage",
        })
        self.assertEqual(set(public["active_child_task"]), {
            "task_id",
            "kind",
            "unit_id",
            "task_revision",
            "phase",
            "status",
            "completed_steps",
            "allowed_actions",
            "next_stage",
            "chapter",
            "candidate_count",
            "candidate_generation",
        })
        self.assertEqual(
            set(public["current_unit"]),
            {"unit_id", "page_index", "display_label", "status"},
        )
        self.assertEqual(
            set(public["consistency"]),
            {"status", "codes"},
        )

        public["workflow"]["completed_steps"].append("MUTATED")
        public["active_child_task"]["completed_steps"].clear()
        public["units"].clear()
        self.assertEqual(snapshot.workflow.completed_steps[-1], "CHILD_TASK_STARTED")
        self.assertEqual(
            snapshot.active_child_task.completed_steps[-1],
            "CANDIDATES_READY",
        )
        self.assertEqual(len(snapshot.units), 1)

    def test_payload_attachment_is_one_canonical_field_and_does_not_mutate_input(self):
        snapshot = self._snapshot()
        original = {
            "text": "已找到候选题。",
            "session": {"phase": "WAIT_CANDIDATE_CHOICE"},
            PUBLIC_TASK_STATE_FIELD: {"client_supplied": True},
        }
        attached = with_public_task_state(original, snapshot)

        self.assertEqual(attached[PUBLIC_TASK_STATE_FIELD], public_task_state_snapshot(snapshot))
        self.assertNotIn("client_supplied", json.dumps(attached, ensure_ascii=False))
        self.assertEqual(original[PUBLIC_TASK_STATE_FIELD], {"client_supplied": True})
        self.assertEqual(attached["session"], original["session"])

    def test_json_and_stream_result_envelopes_share_exact_state_object(self):
        snapshot = self._snapshot()
        json_payload = with_public_task_state({"text": "ok"}, snapshot)
        stream_payload = {
            "type": "result",
            "data": with_public_task_state({"text": "ok"}, snapshot),
        }
        stream_error = with_public_task_state(
            {"type": "error", "detail": "controlled"}, snapshot
        )

        self.assertEqual(
            json_payload[PUBLIC_TASK_STATE_FIELD],
            stream_payload["data"][PUBLIC_TASK_STATE_FIELD],
        )
        self.assertEqual(
            json_payload[PUBLIC_TASK_STATE_FIELD],
            stream_error[PUBLIC_TASK_STATE_FIELD],
        )
        self.assertEqual(stream_payload["type"], "result")
        self.assertEqual(stream_error["type"], "error")

    def test_mapping_none_subclass_and_unknown_shape_are_rejected(self):
        with self.assertRaises(PublicTaskStateError):
            public_task_state_snapshot({})  # type: ignore[arg-type]
        with self.assertRaises(PublicTaskStateError):
            public_task_state_snapshot(None)  # type: ignore[arg-type]

        snapshot = self._snapshot()
        canonical = snapshot.to_dict()

        class ForgedSnapshot(contract.TaskStateSnapshotV1):
            def to_dict(self) -> dict[str, object]:
                forged = super().to_dict()
                forged["workflow"]["phase"] = "FORGED"
                return forged

        forged = ForgedSnapshot(
            workflow=snapshot.workflow,
            active_child_task=snapshot.active_child_task,
            current_unit=snapshot.current_unit,
            units=snapshot.units,
        )
        with self.assertRaises(PublicTaskStateError):
            public_task_state_snapshot(forged)

        with mock.patch.object(
            contract.TaskStateSnapshotV1,
            "to_dict",
            return_value={**canonical, "private_path": "C:\\private\\state.json"},
        ):
            with self.assertRaises(PublicTaskStateError):
                public_task_state_snapshot(snapshot)

        sensitive = json.loads(json.dumps(canonical, ensure_ascii=False))
        sensitive["active_child_task"]["chapter"] = "token=abcd1234"
        with mock.patch.object(
            contract.TaskStateSnapshotV1,
            "to_dict",
            return_value=sensitive,
        ):
            with self.assertRaises(PublicTaskStateError):
                public_task_state_snapshot(snapshot)

        non_json = json.loads(json.dumps(canonical, ensure_ascii=False))
        non_json["workflow"]["task_revision"] = float("nan")
        with mock.patch.object(
            contract.TaskStateSnapshotV1,
            "to_dict",
            return_value=non_json,
        ):
            with self.assertRaises(PublicTaskStateError):
                public_task_state_snapshot(snapshot)

    def test_typed_contract_rejects_sensitive_text_before_publication(self):
        unsafe_values = (
            "https://private.example/state",
            "s3://private-bucket/state.json",
            "x://private/state.json",
            r"C:\private\state.json",
            r"\\server\share\state.json",
            "/mnt/data/state.json",
            "/custom/state.json",
            "../private/state.json",
            "~/private/state.json",
            "ValueError: provider detail",
            "token=abcd1234",
            "client_secret=abcd1234",
            "sk-proj-abcdefghijklmnop",
            "identity_hash=abc123",
            "invite_code=abc123",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    contract.UnitStateView(
                        unit_id="g1-u1",
                        page_index=1,
                        display_label=value,
                        status=contract.UNIT_AVAILABLE,
                    )

        snapshot = self._snapshot()
        with self.assertRaises(ValueError):
            replace(snapshot.workflow, workflow_id="sk-proj-abcdefghijklmnop")

        for value in ("力法/位移法", "ValueError 方法", "令牌 token", "四-1"):
            with self.subTest(safe=value):
                unit = contract.UnitStateView(
                    unit_id="g1-u1",
                    page_index=1,
                    display_label=value,
                    status=contract.UNIT_AVAILABLE,
                )
                self.assertEqual(unit.display_label, value)


if __name__ == "__main__":
    unittest.main()

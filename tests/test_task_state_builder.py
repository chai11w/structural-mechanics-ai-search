import copy
import json
import unittest
from unittest import mock

from tiku_agent import task_state_builder as builder
from tiku_agent import task_state_contract as contract
from tiku_agent.a3_runtime import A3SessionState
from tiku_agent.state import AgentState


SESSION_ID = "session_task_state_builder"
WORKFLOW_ID = "search_workflow_12345678"
CHILD_ID = "search_child_12345678"

WORKFLOW_STEPS = (
    "IMAGE_ACCEPTED",
    "ROUTE_DECIDED",
    "PAGE_UNDERSTOOD",
    "UNIT_CATALOG_READY",
    "UNIT_SELECTED",
    "CHILD_TASK_STARTED",
    "WORKFLOW_COMPLETED",
)
CHILD_STEPS = (
    "QUESTION_ACCEPTED",
    "QUESTION_ANALYZED",
    "CHAPTER_RESOLVED",
    "SEARCH_ROUTE_SELECTED",
    "SEARCH_COMPLETED",
    "CANDIDATES_READY",
    "ANSWER_PREPARED",
)
EXPECTED_CONSISTENCY_CODES = frozenset(
    {
        "WORKFLOW_ID_MISSING",
        "CHILD_TASK_ID_MISSING",
        "ACTIVE_CHILD_TASK_MISSING",
        "ACTIVE_UNIT_MISSING",
        "ACTIVE_UNIT_CLOSED",
        "UNIT_STATE_OVERLAP",
        "DUPLICATE_UNIT_ID",
        "UNKNOWN_WORKFLOW_PHASE",
        "UNKNOWN_CHILD_PHASE",
        "PARENT_CHILD_ID_COLLISION",
        "ORPHAN_CHILD_TASK",
        "WORKFLOW_STATE_UNREADABLE",
        "CHILD_STATE_UNREADABLE",
        "WORKFLOW_ROUTE_PHASE_MISMATCH",
        "WORKFLOW_ROUTE_UNIT_MISMATCH",
        "WORKFLOW_COMPLETE_UNIT_OPEN",
        "CHILD_CANDIDATE_GENERATION_MISMATCH",
    }
)


def _unit(
    unit_id: str,
    page_index: int,
    display_label: str | None = None,
    *,
    searchability: str = "searchable_candidate",
) -> dict[str, object]:
    return {
        "unit_id": unit_id,
        "page_index": page_index,
        "display_label": display_label or f"四-{page_index}",
        "searchability": searchability,
    }


class TaskStateBuilderTests(unittest.TestCase):
    def _workflow(
        self,
        *,
        route: str = "A3",
        phase: str = "WAIT_UNIT_SELECTION",
        units: list[dict[str, object]] | None = None,
        **overrides,
    ) -> A3SessionState:
        values = {
            "session_id": SESSION_ID,
            "entry_route": "" if route == "PENDING" else route,
            "phase": phase,
            "source_page_path": r"C:\runtime\uploads\page.jpg",
            "page_understanding": {"page_disposition": "has_searchable_candidates"}
            if route == "A3"
            else {},
            "units": list(units or []),
            "task_revision": 7,
            # This deliberately differs from workflow_search_id.  It catches
            # producers that accidentally reuse the legacy mixed-meaning ID.
            "current_search_id": CHILD_ID,
            "workflow_search_id": WORKFLOW_ID,
        }
        values.update(overrides)
        return A3SessionState(**values)

    def _child(self, *, phase: str = "WAIT_CHAPTER", **overrides) -> AgentState:
        values = {
            "session_id": SESSION_ID,
            "phase": phase,
            "current_image_path": r"C:\runtime\uploads\question.jpg",
            "current_search_id": CHILD_ID,
            "task_revision": 3,
        }
        values.update(overrides)
        return AgentState(**values)

    def _read_set(
        self,
        *,
        workflow: A3SessionState | None = None,
        child: AgentState | None = None,
        topology: str = builder.TOPOLOGY_A3_WRAPPER,
        workflow_read_status: str | None = None,
        child_read_status: str | None = None,
        child_observation: str = builder.CHILD_OBSERVATION_LIVE,
    ) -> builder.TaskStateReadSet:
        return builder.TaskStateReadSet(
            session_id=SESSION_ID,
            topology=topology,
            workflow_state=workflow,
            child_state=child,
            workflow_read_status=(
                workflow_read_status
                if workflow_read_status is not None
                else builder.READ_OK if workflow is not None else builder.READ_MISSING
            ),
            child_read_status=(
                child_read_status
                if child_read_status is not None
                else builder.READ_OK if child is not None else builder.READ_MISSING
            ),
            child_observation=child_observation,
        )

    def _build(
        self,
        *,
        workflow: A3SessionState | None = None,
        child: AgentState | None = None,
        topology: str = builder.TOPOLOGY_A3_WRAPPER,
        evidence: builder.TaskStateBuildEvidence | None = None,
        workflow_read_status: str | None = None,
        child_read_status: str | None = None,
        child_observation: str = builder.CHILD_OBSERVATION_LIVE,
    ):
        return builder.build_task_state_snapshot_v1(
            self._read_set(
                workflow=workflow,
                child=child,
                topology=topology,
                workflow_read_status=workflow_read_status,
                child_read_status=child_read_status,
                child_observation=child_observation,
            ),
            evidence,
        )

    def assertFailClosed(self, snapshot, expected_codes):
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(snapshot.consistency.codes, tuple(expected_codes))
        self.assertEqual(snapshot.units, ())
        self.assertIsNone(snapshot.current_unit)
        if snapshot.workflow.exists:
            self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
            self.assertEqual(snapshot.workflow.allowed_actions, ())
            self.assertEqual(snapshot.workflow.next_stage, "RETRY")
        else:
            self.assertEqual(snapshot.workflow.allowed_actions, ())
        if snapshot.active_child_task is not None:
            self.assertEqual(snapshot.active_child_task.status, "INCONSISTENT")
            self.assertEqual(snapshot.active_child_task.allowed_actions, ())
            self.assertEqual(snapshot.active_child_task.next_stage, "RETRY")
        public = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        self.assertNotIn("secret-marker", public)
        self.assertNotIn("question.jpg", public)
        self.assertNotIn("page.jpg", public)

    def test_normal_topology_matrix_uses_authoritative_ids_and_is_pure(self):
        empty = self._build()
        self.assertFalse(empty.workflow.exists)
        self.assertIsNone(empty.active_child_task)
        self.assertEqual(empty.consistency.status, "OK")

        standalone_child = self._child(
            phase="WAIT_CHAPTER",
            current_loads=[{"type": "集中"}],
        )
        standalone = self._build(
            child=standalone_child,
            topology=builder.TOPOLOGY_STANDALONE_A2,
        )
        self.assertFalse(standalone.workflow.exists)
        self.assertEqual(standalone.active_child_task.task_id, CHILD_ID)
        self.assertEqual(standalone.active_child_task.unit_id, "")

        a1 = self._build(workflow=self._workflow(route="A1", phase="COMPLETE"))
        self.assertEqual((a1.workflow.route, a1.workflow.phase), ("A1", "COMPLETE"))
        self.assertIsNone(a1.active_child_task)
        self.assertEqual(a1.units, ())

        direct_parent = self._workflow(route="A2", phase="A2_ACTIVE")
        direct_child = self._child(phase="WAIT_CHAPTER")
        direct = self._build(workflow=direct_parent, child=direct_child)
        self.assertEqual(direct.workflow.workflow_id, WORKFLOW_ID)
        self.assertEqual(direct.workflow.task_revision, 7)
        self.assertEqual(direct.active_child_task.task_id, CHILD_ID)
        self.assertEqual(direct.active_child_task.task_revision, 3)
        self.assertEqual(direct.active_child_task.unit_id, "")
        self.assertIsNone(direct.current_unit)
        self.assertEqual(direct.units, ())

        units = [
            _unit("g1-u2", 2),
            _unit("note-u3", 3, searchability="context_only"),
            _unit("g1-u1", 1),
        ]
        residual = self._child(phase="ANSWERED")
        waiting_parent = self._workflow(
            route="A3",
            phase="WAIT_UNIT_SELECTION",
            units=units,
            selected_unit_id="g1-u2",
        )
        waiting = self._build(workflow=waiting_parent, child=residual)
        self.assertEqual([unit.unit_id for unit in waiting.units], ["g1-u1", "g1-u2"])
        self.assertIsNone(waiting.current_unit)
        self.assertIsNone(waiting.active_child_task)

        crop_parent = self._workflow(
            route="A3",
            phase="CROP_REQUIRED",
            units=[_unit("g1-u1", 1)],
            selected_unit_id="g1-u1",
        )
        crop = self._build(workflow=crop_parent)
        self.assertEqual(crop.current_unit.unit_id, "g1-u1")
        self.assertEqual(crop.current_unit.status, "ACTIVE")
        self.assertIsNone(crop.active_child_task)

        active_parent = self._workflow(
            route="A3",
            phase="A2_ACTIVE",
            units=[_unit("g1-u1", 1)],
            selected_unit_id="g1-u1",
        )
        active_child = self._child(phase="WAIT_CHAPTER")
        parent_before = copy.deepcopy(active_parent.to_dict())
        child_before = copy.deepcopy(active_child.to_dict())
        active_first = self._build(workflow=active_parent, child=active_child)
        active_second = self._build(workflow=active_parent, child=active_child)
        self.assertEqual(active_first, active_second)
        self.assertEqual(active_parent.to_dict(), parent_before)
        self.assertEqual(active_child.to_dict(), child_before)
        self.assertEqual(active_first.active_child_task.unit_id, "g1-u1")
        self.assertEqual(active_first.current_unit.unit_id, "g1-u1")

        complete_parent = self._workflow(
            route="A3",
            phase="COMPLETE",
            units=[_unit("g1-u1", 1), _unit("g1-u2", 2)],
            completed_unit_ids=["g1-u1"],
            searched_unit_ids=["g1-u2"],
        )
        complete = self._build(workflow=complete_parent, child=residual)
        self.assertEqual([unit.status for unit in complete.units], ["COMPLETED", "CLOSED"])
        self.assertIsNone(complete.current_unit)
        self.assertIsNone(complete.active_child_task)

    def test_builder_projects_the_complete_workflow_route_phase_matrix(self):
        phase_views = {
            "IDLE": ("IDLE", "UPLOAD_IMAGE"),
            "UNDERSTANDING_PAGE": ("RUNNING", "SYSTEM_CONTINUE"),
            "AUTO_GROUNDING_PAGE": ("RUNNING", "SYSTEM_CONTINUE"),
            "AUTO_VALIDATING_CROPS": ("RUNNING", "SYSTEM_CONTINUE"),
            "WAIT_UNIT_SELECTION": ("WAITING_USER", "SELECT_UNIT"),
            "CROP_REQUIRED": ("WAITING_USER", "SUBMIT_CROP"),
            "VERIFYING_CROP": ("RUNNING", "SYSTEM_CONTINUE"),
            "A2_ACTIVE": ("RUNNING", "FOLLOW_CHILD_TASK"),
            "COMPLETE": ("COMPLETED", "DONE"),
            "ERROR": ("FAILED", "RETRY"),
        }
        legal_phases = {
            "PENDING": {"UNDERSTANDING_PAGE", "ERROR"},
            "A1": {"COMPLETE"},
            "A2": {"A2_ACTIVE"},
            "A3": {
                "UNDERSTANDING_PAGE",
                "AUTO_GROUNDING_PAGE",
                "AUTO_VALIDATING_CROPS",
                "WAIT_UNIT_SELECTION",
                "CROP_REQUIRED",
                "VERIFYING_CROP",
                "A2_ACTIVE",
                "COMPLETE",
                "ERROR",
            },
        }
        current_unit_phases = {"CROP_REQUIRED", "VERIFYING_CROP", "A2_ACTIVE"}

        self.assertEqual(
            set(phase_views),
            set(contract.WORKFLOW_PHASE_CONTRACTS) - {contract.PHASE_UNKNOWN},
        )
        self.assertEqual(
            set(legal_phases),
            set(contract.WORKFLOW_PHASES_BY_ROUTE)
            - {contract.WORKFLOW_ROUTE_NONE},
        )
        for route, phases in legal_phases.items():
            self.assertEqual(phases, set(contract.WORKFLOW_PHASES_BY_ROUTE[route]))

        empty = self._build()
        self.assertEqual(empty.consistency.status, "OK")
        self.assertEqual(empty.workflow.route, contract.WORKFLOW_ROUTE_NONE)
        self.assertEqual(empty.workflow.phase, "IDLE")
        self.assertEqual(empty.workflow.status, "IDLE")
        self.assertEqual(empty.workflow.next_stage, "UPLOAD_IMAGE")

        for route, allowed in legal_phases.items():
            for phase, (expected_status, expected_next_stage) in phase_views.items():
                with self.subTest(route=route, phase=phase):
                    workflow_kwargs = {}
                    child = None
                    if route == "A3" and phase in current_unit_phases:
                        workflow_kwargs = {
                            "units": [_unit("g1-u1", 1)],
                            "selected_unit_id": "g1-u1",
                        }
                    if phase == "A2_ACTIVE" and route in {"A2", "A3"}:
                        child = self._child(phase="WAIT_CHAPTER")

                    snapshot = self._build(
                        workflow=self._workflow(
                            route=route,
                            phase=phase,
                            **workflow_kwargs,
                        ),
                        child=child,
                    )

                    if phase not in allowed:
                        self.assertFailClosed(
                            snapshot,
                            ("WORKFLOW_ROUTE_PHASE_MISMATCH",),
                        )
                        continue

                    self.assertEqual(snapshot.consistency.status, "OK")
                    self.assertEqual(snapshot.workflow.route, route)
                    self.assertEqual(snapshot.workflow.phase, phase)
                    self.assertEqual(snapshot.workflow.status, expected_status)
                    self.assertEqual(
                        snapshot.workflow.next_stage,
                        expected_next_stage,
                    )
                    if route == "A3" and phase in current_unit_phases:
                        self.assertEqual(snapshot.current_unit.unit_id, "g1-u1")
                    else:
                        self.assertIsNone(snapshot.current_unit)

    def test_builder_projects_the_complete_child_phase_topology_matrix(self):
        active_phase_views = {
            "PROCESSING": ("RUNNING", "SYSTEM_CONTINUE"),
            "WAIT_CHAPTER": ("WAITING_USER", "SET_CHAPTER"),
            "WAIT_QUESTION_CHOICE": ("WAITING_USER", "SELECT_QUESTION"),
            "WAIT_CANDIDATE_CHOICE": ("WAITING_USER", "SELECT_CANDIDATE"),
            "READY_TO_ROUTE": ("RUNNING", "SYSTEM_CONTINUE"),
            "READY_FOR_SEARCH": ("RUNNING", "SYSTEM_CONTINUE"),
            "ANSWERED": ("COMPLETED", "DONE"),
            "ERROR": ("FAILED", "RETRY"),
            "NO_MATCH": ("NO_MATCH", "DONE"),
        }
        self.assertEqual(
            set(active_phase_views),
            set(contract.CHILD_PHASE_CONTRACTS)
            - {"IDLE", "CANCELLED", contract.PHASE_UNKNOWN},
        )

        for topology in ("standalone", "direct-a2", "a3-active"):
            for phase, (expected_status, expected_next_stage) in active_phase_views.items():
                with self.subTest(topology=topology, phase=phase):
                    workflow = None
                    builder_topology = builder.TOPOLOGY_STANDALONE_A2
                    expected_unit_id = ""
                    if topology == "direct-a2":
                        workflow = self._workflow(route="A2", phase="A2_ACTIVE")
                        builder_topology = builder.TOPOLOGY_A3_WRAPPER
                    elif topology == "a3-active":
                        workflow = self._workflow(
                            route="A3",
                            phase="A2_ACTIVE",
                            units=[_unit("g1-u1", 1)],
                            selected_unit_id="g1-u1",
                        )
                        builder_topology = builder.TOPOLOGY_A3_WRAPPER
                        expected_unit_id = "g1-u1"

                    snapshot = self._build(
                        workflow=workflow,
                        child=self._child(phase=phase),
                        topology=builder_topology,
                    )

                    self.assertEqual(snapshot.consistency.status, "OK")
                    self.assertEqual(snapshot.active_child_task.phase, phase)
                    self.assertEqual(
                        snapshot.active_child_task.status,
                        expected_status,
                    )
                    self.assertEqual(
                        snapshot.active_child_task.next_stage,
                        expected_next_stage,
                    )
                    self.assertEqual(
                        snapshot.active_child_task.unit_id,
                        expected_unit_id,
                    )
                    if workflow is not None:
                        self.assertEqual(snapshot.workflow.status, "RUNNING")
                        self.assertEqual(
                            snapshot.workflow.next_stage,
                            "FOLLOW_CHILD_TASK",
                        )

        idle = self._child(phase="IDLE")
        standalone_idle = self._build(
            child=idle,
            topology=builder.TOPOLOGY_STANDALONE_A2,
        )
        self.assertEqual(standalone_idle.consistency.status, "OK")
        self.assertIsNone(standalone_idle.active_child_task)

        direct_parent = self._workflow(route="A2", phase="A2_ACTIVE")
        self.assertFailClosed(
            self._build(workflow=direct_parent, child=idle),
            ("ACTIVE_CHILD_TASK_MISSING",),
        )

        a3_parent = self._workflow(
            route="A3",
            phase="A2_ACTIVE",
            units=[_unit("g1-u1", 1)],
            selected_unit_id="g1-u1",
        )
        self.assertFailClosed(
            self._build(workflow=a3_parent, child=idle),
            ("ACTIVE_CHILD_TASK_MISSING",),
        )

        cancelled = self._child(phase="CANCELLED")
        self.assertFailClosed(
            self._build(workflow=a3_parent, child=cancelled),
            ("ACTIVE_CHILD_TASK_MISSING",),
        )
        frozen_cancelled = self._build(
            workflow=a3_parent,
            child=cancelled,
            child_observation=builder.CHILD_OBSERVATION_RESPONSE_FROZEN,
        )
        self.assertEqual(frozen_cancelled.consistency.status, "OK")
        self.assertEqual(frozen_cancelled.active_child_task.phase, "CANCELLED")
        self.assertEqual(frozen_cancelled.active_child_task.status, "CANCELLED")
        self.assertEqual(frozen_cancelled.active_child_task.next_stage, "DONE")
        self.assertEqual(frozen_cancelled.active_child_task.unit_id, "g1-u1")
        self.assertEqual(frozen_cancelled.workflow.status, "RUNNING")
        self.assertEqual(
            frozen_cancelled.workflow.next_stage,
            "FOLLOW_CHILD_TASK",
        )

        for topology, parent in (
            ("direct-a2", direct_parent),
            ("a3-active", a3_parent),
        ):
            with self.subTest(topology=topology, phase="UNKNOWN"):
                unknown_child = self._child(phase="WAIT_CHAPTER")
                unknown_child.phase = contract.PHASE_UNKNOWN
                self.assertFailClosed(
                    self._build(workflow=parent, child=unknown_child),
                    ("UNKNOWN_CHILD_PHASE",),
                )

        a1_residual = self._build(
            workflow=self._workflow(route="A1", phase="COMPLETE"),
            child=self._child(phase="WAIT_CHAPTER"),
        )
        self.assertEqual(a1_residual.consistency.status, "OK")
        self.assertIsNone(a1_residual.active_child_task)

    def test_workflow_completed_steps_are_derived_from_current_fields(self):
        unit1 = _unit("g1-u1", 1)
        unit2 = _unit("g1-u2", 2)
        cases = []

        cases.append((
            "pending_only_image",
            self._workflow(route="PENDING", phase="UNDERSTANDING_PAGE"),
            None,
            ("IMAGE_ACCEPTED",),
        ))
        cases.append((
            "a3_without_understanding",
            self._workflow(
                route="A3",
                phase="WAIT_UNIT_SELECTION",
                units=[unit1],
                page_understanding={},
            ),
            None,
            ("IMAGE_ACCEPTED", "ROUTE_DECIDED"),
        ))
        cases.append((
            "catalog_ready_without_selection",
            self._workflow(route="A3", phase="WAIT_UNIT_SELECTION", units=[unit1]),
            None,
            WORKFLOW_STEPS[:4],
        ))
        cases.append((
            "crop_draft_proves_selection",
            self._workflow(
                route="A3",
                phase="WAIT_UNIT_SELECTION",
                units=[unit1],
                crop_drafts={"g1-u1": {"path": "private-crop-path"}},
            ),
            None,
            WORKFLOW_STEPS[:5],
        ))
        cases.append((
            "completed_unit_proves_selection_and_child_history",
            self._workflow(
                route="A3",
                phase="WAIT_UNIT_SELECTION",
                units=[unit1, unit2],
                completed_unit_ids=["g1-u1"],
            ),
            None,
            WORKFLOW_STEPS[:6],
        ))
        cases.append((
            "a3_active_child",
            self._workflow(
                route="A3",
                phase="A2_ACTIVE",
                units=[unit1],
                selected_unit_id="g1-u1",
            ),
            self._child(),
            WORKFLOW_STEPS[:6],
        ))
        cases.append((
            "direct_a2_does_not_invent_page_steps",
            self._workflow(route="A2", phase="A2_ACTIVE"),
            self._child(),
            ("IMAGE_ACCEPTED", "ROUTE_DECIDED", "CHILD_TASK_STARTED"),
        ))
        cases.append((
            "a1_complete",
            self._workflow(route="A1", phase="COMPLETE"),
            None,
            ("IMAGE_ACCEPTED", "ROUTE_DECIDED", "WORKFLOW_COMPLETED"),
        ))
        cases.append((
            "a3_complete",
            self._workflow(
                route="A3",
                phase="COMPLETE",
                units=[unit1],
                completed_unit_ids=["g1-u1"],
            ),
            None,
            WORKFLOW_STEPS,
        ))

        for label, workflow, child, expected in cases:
            with self.subTest(label=label):
                snapshot = self._build(workflow=workflow, child=child)
                self.assertEqual(snapshot.consistency.status, "OK")
                self.assertEqual(snapshot.workflow.completed_steps, expected)

    def test_child_completed_steps_are_derived_from_current_fields(self):
        candidate = {"rank": 1, "candidate_key": "candidate-1"}
        cases = [
            (
                "processing",
                self._child(phase="PROCESSING"),
                CHILD_STEPS[:1],
            ),
            (
                "analyzed",
                self._child(phase="WAIT_CHAPTER", current_loads=[{"type": "集中"}]),
                CHILD_STEPS[:2],
            ),
            (
                "chapter_resolved",
                self._child(
                    phase="READY_TO_ROUTE",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                ),
                CHILD_STEPS[:3],
            ),
            (
                "route_selected",
                self._child(
                    phase="READY_FOR_SEARCH",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="main",
                ),
                CHILD_STEPS[:4],
            ),
            (
                "empty_search_completed",
                self._child(
                    phase="NO_MATCH",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="symbolic",
                    candidate_revision=1,
                    candidate_generation="",
                ),
                CHILD_STEPS[:5],
            ),
            (
                "candidates_ready",
                self._child(
                    phase="WAIT_CANDIDATE_CHOICE",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="main",
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                ),
                CHILD_STEPS[:6],
            ),
            (
                "answer_prepared",
                self._child(
                    phase="ANSWERED",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="main",
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                    selected_rank=1,
                    last_answer_paths=[r"C:\private\answer.jpg"],
                ),
                CHILD_STEPS,
            ),
            (
                "image_route_is_not_search_route",
                self._child(
                    phase="NO_MATCH",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="A3",
                    candidate_revision=1,
                ),
                CHILD_STEPS[:3],
            ),
            (
                "invalid_selected_rank_does_not_prepare_answer",
                self._child(
                    phase="ANSWERED",
                    current_loads=[{"type": "集中"}],
                    current_chapter="2静定结构",
                    current_route="main",
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                    selected_rank=2,
                    last_answer_paths=[r"C:\private\answer.jpg"],
                ),
                CHILD_STEPS[:6],
            ),
        ]

        for label, child, expected in cases:
            with self.subTest(label=label):
                snapshot = self._build(
                    child=child,
                    topology=builder.TOPOLOGY_STANDALONE_A2,
                    child_observation=(
                        builder.CHILD_OBSERVATION_RESPONSE_FROZEN
                        if child.phase == "CANCELLED"
                        else builder.CHILD_OBSERVATION_LIVE
                    ),
                )
                self.assertEqual(snapshot.consistency.status, "OK")
                self.assertEqual(snapshot.active_child_task.completed_steps, expected)

    def test_workflow_actions_are_phase_candidates_filtered_by_evidence(self):
        unit1 = _unit("g1-u1", 1)
        unit2 = _unit("g1-u2", 2)
        full_evidence = builder.TaskStateBuildEvidence(
            trusted_image_event=True,
            reset_session_available=True,
            verified_source_page_path=r"C:\runtime\uploads\page.jpg",
            workflow_retry_available=True,
        )

        waiting = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1],
            auto_crop_enabled=True,
        )
        self.assertEqual(
            set(self._build(workflow=waiting, evidence=full_evidence).workflow.allowed_actions),
            {"select_unit", "prepare_units", "finish_page", "upload_image", "reset_session"},
        )

        crop = self._workflow(
            phase="CROP_REQUIRED",
            units=[unit1, unit2],
            selected_unit_id="g1-u1",
            auto_crop_enabled=True,
        )
        self.assertEqual(
            set(self._build(workflow=crop, evidence=full_evidence).workflow.allowed_actions),
            {
                "submit_crop",
                "select_unit",
                "prepare_units",
                "cancel_current_unit",
                "finish_page",
                "upload_image",
                "reset_session",
            },
        )

        active = self._workflow(
            phase="A2_ACTIVE",
            units=[unit1, unit2],
            selected_unit_id="g1-u1",
        )
        active_snapshot = self._build(
            workflow=active,
            child=self._child(),
            evidence=full_evidence,
        )
        self.assertEqual(
            set(active_snapshot.workflow.allowed_actions),
            {"select_unit", "cancel_current_unit", "finish_page", "upload_image", "reset_session"},
        )

        error = self._workflow(route="A3", phase="ERROR")
        self.assertEqual(
            set(self._build(workflow=error, evidence=full_evidence).workflow.allowed_actions),
            {"retry_current_stage", "upload_image", "reset_session"},
        )

        no_entry_evidence = self._build(workflow=waiting)
        self.assertEqual(
            set(no_entry_evidence.workflow.allowed_actions),
            {"select_unit", "prepare_units", "finish_page"},
        )

        no_remaining = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1],
            auto_crop_enabled=True,
            page_finished=True,
        )
        self.assertEqual(self._build(workflow=no_remaining).workflow.allowed_actions, ())

        no_auto = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1],
            auto_crop_enabled=False,
        )
        self.assertEqual(
            set(self._build(workflow=no_auto).workflow.allowed_actions),
            {"select_unit", "finish_page"},
        )

        wrong_source = builder.TaskStateBuildEvidence(
            verified_source_page_path=r"C:\runtime\uploads\other.jpg",
            workflow_retry_available=True,
        )
        self.assertEqual(self._build(workflow=error, evidence=wrong_source).workflow.allowed_actions, ())

    def test_child_actions_follow_field_predicates_not_extra_image_guards(self):
        question = {"question_image_path": r"C:\private\crop.jpg"}
        candidate = {"rank": 1, "candidate_key": "candidate-1"}
        retry_evidence = builder.TaskStateBuildEvidence(
            retryable_child_task=(CHILD_ID, 3),
        )
        cases = [
            (
                "wait_chapter",
                self._child(
                    phase="WAIT_CHAPTER",
                    questions=[question],
                    global_search_offered=True,
                    last_error="safe failure",
                ),
                None,
                {"set_chapter", "global_search", "select_question", "explain_failure", "cancel"},
            ),
            (
                "wait_question",
                self._child(
                    phase="WAIT_QUESTION_CHOICE",
                    questions=[question],
                    last_error="safe failure",
                ),
                None,
                {"select_question", "explain_failure", "cancel"},
            ),
            (
                "wait_candidate",
                self._child(
                    phase="WAIT_CANDIDATE_CHOICE",
                    questions=[question],
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                    last_error="safe failure",
                ),
                None,
                {
                    "set_chapter",
                    "select_question",
                    "select_candidate",
                    "reject_candidates",
                    "show_candidates",
                    "explain_failure",
                    "cancel",
                },
            ),
            (
                "answered",
                self._child(
                    phase="ANSWERED",
                    questions=[question],
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                    last_answer_paths=[r"C:\private\answer.jpg"],
                    last_error="safe failure",
                ),
                None,
                {
                    "set_chapter",
                    "select_question",
                    "select_candidate",
                    "reject_candidates",
                    "show_candidates",
                    "report_answer_mismatch",
                    "resend_answer",
                    "explain_failure",
                    "cancel",
                },
            ),
            (
                "no_match",
                self._child(
                    phase="NO_MATCH",
                    questions=[question],
                    last_error="safe failure",
                ),
                None,
                {"set_chapter", "select_question", "explain_failure", "cancel"},
            ),
            (
                "error",
                self._child(
                    phase="ERROR",
                    questions=[question],
                    candidates=[candidate],
                    candidate_revision=1,
                    candidate_generation="3:1",
                    last_error="safe failure",
                ),
                retry_evidence,
                {
                    "set_chapter",
                    "select_question",
                    "select_candidate",
                    "explain_failure",
                    "retry_search",
                    "cancel",
                },
            ),
        ]
        for label, child, evidence, expected in cases:
            with self.subTest(label=label):
                snapshot = self._build(
                    child=child,
                    topology=builder.TOPOLOGY_STANDALONE_A2,
                    evidence=evidence,
                )
                self.assertEqual(set(snapshot.active_child_task.allowed_actions), expected)

        # Selection is authorized by the relevant non-empty namespace, not by
        # the presence of an image path.
        no_image = self._child(
            phase="WAIT_CANDIDATE_CHOICE",
            current_image_path="",
            questions=[question],
            candidates=[candidate],
            candidate_revision=1,
            candidate_generation="3:1",
        )
        no_image_actions = set(
            self._build(
                child=no_image,
                topology=builder.TOPOLOGY_STANDALONE_A2,
            ).active_child_task.allowed_actions
        )
        self.assertEqual(
            no_image_actions,
            {"select_question", "select_candidate", "reject_candidates", "show_candidates", "cancel"},
        )

        # active_image_path is question crop first, then the source image.  A
        # trusted retry identity must therefore work for a question-path-only
        # state as well.
        question_path_only = self._child(
            phase="ERROR",
            current_image_path="",
            current_question_image_path=r"C:\runtime\crops\question.jpg",
            last_error="safe failure",
        )
        question_path_actions = set(
            self._build(
                child=question_path_only,
                topology=builder.TOPOLOGY_STANDALONE_A2,
                evidence=retry_evidence,
            ).active_child_task.allowed_actions
        )
        self.assertEqual(
            question_path_actions,
            {"set_chapter", "explain_failure", "retry_search", "cancel"},
        )

        no_retry = self._build(
            child=self._child(phase="ERROR", last_error="safe failure"),
            topology=builder.TOPOLOGY_STANDALONE_A2,
            evidence=builder.TaskStateBuildEvidence(
                retryable_child_task=("search_other_12345678", 3),
            ),
        )
        self.assertNotIn("retry_search", no_retry.active_child_task.allowed_actions)

        stale_retry_revision = self._build(
            child=self._child(phase="ERROR", last_error="safe failure"),
            topology=builder.TOPOLOGY_STANDALONE_A2,
            evidence=builder.TaskStateBuildEvidence(
                retryable_child_task=(CHILD_ID, 2),
            ),
        )
        self.assertNotIn(
            "retry_search",
            stale_retry_revision.active_child_task.allowed_actions,
        )

        for phase in ("PROCESSING", "READY_TO_ROUTE", "READY_FOR_SEARCH"):
            with self.subTest(internal_phase=phase):
                internal = self._build(
                    child=self._child(phase=phase),
                    topology=builder.TOPOLOGY_STANDALONE_A2,
                )
                self.assertEqual(internal.active_child_task.allowed_actions, ())

    def test_unit_projection_filters_sorts_and_requires_verified_crop_evidence(self):
        units = [
            _unit("g1-u6", 6),
            _unit("g1-u3", 3),
            _unit("g1-u1", 1),
            _unit("context-u7", 7, searchability="context_only"),
            _unit("g1-u5", 5),
            _unit("g1-u2", 2),
            _unit("g1-u4", 4),
        ]
        auto_crops = {
            unit_id: {
                "validation_status": "auto_ready",
                "path": rf"C:\controlled\crops\{unit_id}.jpg",
            }
            for unit_id in ("g1-u1", "g1-u2", "g1-u3", "g1-u4", "g1-u5")
        }
        parent = self._workflow(
            phase="CROP_REQUIRED",
            units=units,
            selected_unit_id="g1-u3",
            completed_unit_ids=["g1-u1"],
            searched_unit_ids=["g1-u2"],
            auto_crop_enabled=True,
            auto_crops=auto_crops,
        )
        evidence = builder.TaskStateBuildEvidence(
            verified_controlled_crop_paths=(
                ("g1-u1", r"C:\controlled\crops\g1-u1.jpg"),
                ("g1-u2", r"C:\controlled\crops\g1-u2.jpg"),
                ("g1-u3", r"C:\controlled\crops\g1-u3.jpg"),
                ("g1-u4", r"C:\controlled\crops\g1-u4.jpg"),
                # Existing but different evidence must not validate u5.
                ("g1-u5", r"C:\outside\g1-u5.jpg"),
            )
        )
        snapshot = self._build(workflow=parent, evidence=evidence)
        self.assertEqual(
            [(unit.unit_id, unit.status) for unit in snapshot.units],
            [
                ("g1-u1", "COMPLETED"),
                ("g1-u2", "CLOSED"),
                ("g1-u3", "ACTIVE"),
                ("g1-u4", "PREPARED"),
                ("g1-u5", "AVAILABLE"),
                ("g1-u6", "AVAILABLE"),
            ],
        )
        self.assertEqual(snapshot.current_unit.unit_id, "g1-u3")

        auto_disabled = copy.deepcopy(parent)
        auto_disabled.auto_crop_enabled = False
        disabled = self._build(workflow=auto_disabled, evidence=evidence)
        self.assertEqual(disabled.units[3].status, "AVAILABLE")

        residual = copy.deepcopy(parent)
        residual.phase = "WAIT_UNIT_SELECTION"
        residual.selected_unit_id = "g1-u4"
        residual_view = self._build(workflow=residual, evidence=evidence)
        self.assertIsNone(residual_view.current_unit)
        self.assertEqual(residual_view.units[3].status, "PREPARED")

        errored = copy.deepcopy(parent)
        errored.phase = "ERROR"
        errored.selected_unit_id = ""
        errored_view = self._build(workflow=errored, evidence=evidence)
        self.assertIsNone(errored_view.current_unit)
        self.assertEqual(errored_view.units[3].status, "AVAILABLE")

        finished = copy.deepcopy(residual)
        finished.page_finished = True
        finished_view = self._build(workflow=finished, evidence=evidence)
        self.assertIsNone(finished_view.current_unit)
        self.assertEqual(
            [unit.status for unit in finished_view.units],
            ["COMPLETED", "CLOSED", "CLOSED", "CLOSED", "CLOSED", "CLOSED"],
        )

    def test_candidate_generation_is_checked_against_both_private_revisions(self):
        candidate = {"rank": 1, "candidate_key": "candidate-1"}
        valid = self._child(
            phase="WAIT_CANDIDATE_CHOICE",
            candidates=[candidate],
            candidate_revision=2,
            candidate_generation="3:2",
        )
        valid_snapshot = self._build(
            child=valid,
            topology=builder.TOPOLOGY_STANDALONE_A2,
        )
        self.assertEqual(valid_snapshot.consistency.status, "OK")
        self.assertEqual(valid_snapshot.active_child_task.candidate_generation, "3:2")

        empty_result = self._child(
            phase="NO_MATCH",
            current_route="main",
            candidate_revision=4,
            candidate_generation="",
        )
        empty_snapshot = self._build(
            child=empty_result,
            topology=builder.TOPOLOGY_STANDALONE_A2,
        )
        self.assertEqual(empty_snapshot.consistency.status, "OK")
        self.assertEqual(empty_snapshot.active_child_task.candidate_count, 0)
        self.assertEqual(empty_snapshot.active_child_task.candidate_generation, "")
        self.assertIn("SEARCH_COMPLETED", empty_snapshot.active_child_task.completed_steps)
        self.assertNotIn("CANDIDATES_READY", empty_snapshot.active_child_task.completed_steps)

        invalid = {
            "task_prefix": self._child(
                phase="WAIT_CANDIDATE_CHOICE",
                candidates=[candidate],
                candidate_revision=2,
                candidate_generation="4:2",
            ),
            "candidate_suffix": self._child(
                phase="WAIT_CANDIDATE_CHOICE",
                candidates=[candidate],
                candidate_revision=2,
                candidate_generation="3:1",
            ),
            "zero_authoritative_candidate_revision": self._child(
                phase="WAIT_CANDIDATE_CHOICE",
                candidates=[candidate],
                candidate_revision=0,
                candidate_generation="3:1",
            ),
            "empty_candidates_with_generation": self._child(
                phase="NO_MATCH",
                candidates=[],
                candidate_revision=1,
                candidate_generation="3:1",
            ),
        }
        for label, child in invalid.items():
            with self.subTest(label=label):
                snapshot = self._build(
                    child=child,
                    topology=builder.TOPOLOGY_STANDALONE_A2,
                )
                self.assertFailClosed(
                    snapshot,
                    ("CHILD_CANDIDATE_GENERATION_MISMATCH",),
                )

    def test_cancelled_live_residual_and_response_frozen_are_distinct(self):
        cancelled = self._child(phase="CANCELLED", last_error="secret-marker")

        standalone_live = self._build(
            child=cancelled,
            topology=builder.TOPOLOGY_STANDALONE_A2,
        )
        self.assertEqual(standalone_live.consistency.status, "OK")
        self.assertIsNone(standalone_live.active_child_task)

        standalone_frozen = self._build(
            child=cancelled,
            topology=builder.TOPOLOGY_STANDALONE_A2,
            child_observation=builder.CHILD_OBSERVATION_RESPONSE_FROZEN,
        )
        self.assertEqual(standalone_frozen.active_child_task.phase, "CANCELLED")
        self.assertEqual(standalone_frozen.active_child_task.status, "CANCELLED")
        self.assertEqual(standalone_frozen.active_child_task.allowed_actions, ())
        self.assertEqual(standalone_frozen.active_child_task.next_stage, "DONE")

        direct_parent = self._workflow(route="A2", phase="A2_ACTIVE")
        direct_live = self._build(workflow=direct_parent, child=cancelled)
        self.assertFailClosed(direct_live, ("ACTIVE_CHILD_TASK_MISSING",))

        direct_frozen = self._build(
            workflow=direct_parent,
            child=cancelled,
            child_observation=builder.CHILD_OBSERVATION_RESPONSE_FROZEN,
        )
        self.assertEqual(direct_frozen.consistency.status, "OK")
        self.assertEqual(direct_frozen.active_child_task.phase, "CANCELLED")

        waiting_parent = self._workflow(
            route="A3",
            phase="WAIT_UNIT_SELECTION",
            units=[_unit("g1-u1", 1)],
        )
        residual = self._build(workflow=waiting_parent, child=cancelled)
        self.assertEqual(residual.consistency.status, "OK")
        self.assertIsNone(residual.active_child_task)

    def test_all_17_consistency_codes_are_emitted_and_fail_closed(self):
        unit1 = _unit("g1-u1", 1)
        unit2 = _unit("g1-u2", 2)
        candidate = {"rank": 1, "candidate_key": "candidate-1"}

        missing_workflow_id = self._workflow(route="A1", phase="COMPLETE")
        missing_workflow_id.workflow_search_id = ""
        missing_workflow_id.current_search_id = "search_not_authoritative_12345678"

        missing_child_id = self._child(phase="WAIT_CHAPTER")
        missing_child_id.current_search_id = ""

        active_unit_missing = self._workflow(
            phase="CROP_REQUIRED",
            units=[unit1],
            selected_unit_id="",
        )
        active_unit_closed = self._workflow(
            phase="CROP_REQUIRED",
            units=[unit1],
            selected_unit_id="g1-u1",
            completed_unit_ids=["g1-u1"],
        )
        overlap = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1, unit2],
            completed_unit_ids=["g1-u1"],
            searched_unit_ids=["g1-u1"],
        )
        duplicate = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1, _unit("g1-u1", 2)],
        )
        unknown_workflow = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            units=[unit1],
        )
        unknown_workflow.phase = "ALIEN_WORKFLOW_PHASE"
        unknown_child = self._child()
        unknown_child.phase = "ALIEN_CHILD_PHASE"

        collision_child = self._child()
        collision_parent = self._workflow(route="A2", phase="A2_ACTIVE")
        collision_parent.workflow_search_id = collision_child.current_search_id

        route_phase = self._workflow(route="A1", phase="WAIT_UNIT_SELECTION")
        route_unit = self._workflow(
            route="A2",
            phase="A2_ACTIVE",
            units=[unit1],
            selected_unit_id="g1-u1",
        )
        complete_open = self._workflow(
            route="A3",
            phase="COMPLETE",
            units=[unit1],
        )
        stale_candidates = self._child(
            phase="WAIT_CANDIDATE_CHOICE",
            candidates=[candidate],
            candidate_revision=2,
            candidate_generation="3:1",
            last_error="secret-marker",
        )

        cases = {
            "WORKFLOW_ID_MISSING": self._read_set(workflow=missing_workflow_id),
            "CHILD_TASK_ID_MISSING": self._read_set(
                child=missing_child_id,
                topology=builder.TOPOLOGY_STANDALONE_A2,
            ),
            "ACTIVE_CHILD_TASK_MISSING": self._read_set(
                workflow=self._workflow(route="A2", phase="A2_ACTIVE")
            ),
            "ACTIVE_UNIT_MISSING": self._read_set(workflow=active_unit_missing),
            "ACTIVE_UNIT_CLOSED": self._read_set(workflow=active_unit_closed),
            "UNIT_STATE_OVERLAP": self._read_set(workflow=overlap),
            "DUPLICATE_UNIT_ID": self._read_set(workflow=duplicate),
            "UNKNOWN_WORKFLOW_PHASE": self._read_set(workflow=unknown_workflow),
            "UNKNOWN_CHILD_PHASE": self._read_set(
                child=unknown_child,
                topology=builder.TOPOLOGY_STANDALONE_A2,
            ),
            "PARENT_CHILD_ID_COLLISION": self._read_set(
                workflow=collision_parent,
                child=collision_child,
            ),
            "ORPHAN_CHILD_TASK": self._read_set(child=self._child()),
            "WORKFLOW_STATE_UNREADABLE": self._read_set(
                workflow_read_status=builder.READ_UNREADABLE,
            ),
            "CHILD_STATE_UNREADABLE": self._read_set(
                topology=builder.TOPOLOGY_STANDALONE_A2,
                child_read_status=builder.READ_UNREADABLE,
            ),
            "WORKFLOW_ROUTE_PHASE_MISMATCH": self._read_set(workflow=route_phase),
            "WORKFLOW_ROUTE_UNIT_MISMATCH": self._read_set(
                workflow=route_unit,
                child=self._child(),
            ),
            "WORKFLOW_COMPLETE_UNIT_OPEN": self._read_set(workflow=complete_open),
            "CHILD_CANDIDATE_GENERATION_MISMATCH": self._read_set(
                child=stale_candidates,
                topology=builder.TOPOLOGY_STANDALONE_A2,
            ),
        }
        self.assertEqual(frozenset(cases), EXPECTED_CONSISTENCY_CODES)

        for expected, read_set in cases.items():
            with self.subTest(code=expected):
                snapshot = builder.build_task_state_snapshot_v1(read_set)
                self.assertFailClosed(snapshot, (expected,))

        multiple = self._workflow(
            route="A2",
            phase="A2_ACTIVE",
            units=[unit1],
            selected_unit_id="g1-u1",
        )
        multiple.workflow_search_id = ""
        combined = self._build(workflow=multiple)
        self.assertFailClosed(
            combined,
            (
                "WORKFLOW_ID_MISSING",
                "ACTIVE_CHILD_TASK_MISSING",
                "WORKFLOW_ROUTE_UNIT_MISMATCH",
            ),
        )

        route_history_only = self._workflow(
            route="A2",
            phase="A2_ACTIVE",
            units=[unit1],
            completed_unit_ids=["g1-u1"],
        )
        self.assertEqual(
            self._build(workflow=route_history_only).consistency.codes,
            (
                "ACTIVE_CHILD_TASK_MISSING",
                "WORKFLOW_ROUTE_UNIT_MISMATCH",
            ),
        )

    def test_read_failures_keep_unknown_retry_placeholders(self):
        workflow_unreadable = self._build(
            workflow_read_status=builder.READ_UNREADABLE,
        )
        self.assertEqual(
            workflow_unreadable.consistency.codes,
            ("WORKFLOW_STATE_UNREADABLE",),
        )
        self.assertTrue(workflow_unreadable.workflow.exists)
        self.assertEqual(workflow_unreadable.workflow.phase, "UNKNOWN")
        self.assertEqual(workflow_unreadable.workflow.status, "INCONSISTENT")
        self.assertEqual(workflow_unreadable.workflow.next_stage, "RETRY")
        self.assertEqual(workflow_unreadable.workflow.allowed_actions, ())

        workflow_unknown = self._build(
            workflow_read_status=builder.READ_UNKNOWN_PHASE,
        )
        self.assertEqual(
            workflow_unknown.consistency.codes,
            ("UNKNOWN_WORKFLOW_PHASE",),
        )
        self.assertTrue(workflow_unknown.workflow.exists)
        self.assertEqual(workflow_unknown.workflow.phase, "UNKNOWN")
        self.assertEqual(workflow_unknown.workflow.next_stage, "RETRY")

        duplicate_units = self._build(
            workflow_read_status=builder.READ_DUPLICATE_UNIT_ID,
        )
        self.assertEqual(
            duplicate_units.consistency.codes,
            ("DUPLICATE_UNIT_ID",),
        )
        self.assertTrue(duplicate_units.workflow.exists)
        self.assertEqual(duplicate_units.workflow.phase, "UNKNOWN")

        standalone_child_unreadable = self._build(
            topology=builder.TOPOLOGY_STANDALONE_A2,
            child_read_status=builder.READ_UNREADABLE,
        )
        self.assertEqual(
            standalone_child_unreadable.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )
        self.assertIsNotNone(standalone_child_unreadable.active_child_task)
        self.assertEqual(
            standalone_child_unreadable.active_child_task.phase,
            "UNKNOWN",
        )
        self.assertEqual(
            standalone_child_unreadable.active_child_task.next_stage,
            "RETRY",
        )

        wrapped_child_unreadable = self._build(
            workflow=self._workflow(route="A2", phase="A2_ACTIVE"),
            child_read_status=builder.READ_UNREADABLE,
        )
        self.assertEqual(
            wrapped_child_unreadable.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )
        self.assertIsNotNone(wrapped_child_unreadable.active_child_task)
        self.assertEqual(
            wrapped_child_unreadable.active_child_task.phase,
            "UNKNOWN",
        )

    def test_oversized_candidate_state_is_unreadable(self):
        child = self._child(
            phase="WAIT_CANDIDATE_CHOICE",
            candidates=[{}, {}, {}],
            candidate_revision=1,
            candidate_generation="1:1",
        )
        with mock.patch.object(builder, "_MAX_REVISION", 2):
            snapshot = self._build(
                child=child,
                topology=builder.TOPOLOGY_STANDALONE_A2,
            )
        self.assertEqual(
            snapshot.consistency.codes,
            ("CHILD_STATE_UNREADABLE",),
        )
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")


if __name__ == "__main__":
    unittest.main()

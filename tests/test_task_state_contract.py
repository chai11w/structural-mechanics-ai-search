from itertools import product
import unittest

from tiku_agent import a3_runtime
from tiku_agent import task_state_contract as contract
from tiku_agent.state import KNOWN_PHASES


WORKFLOW_PHASE_MATRIX = {
    "IDLE": (
        "IDLE",
        "UPLOAD_IMAGE",
        frozenset({"upload_image", "reset_session"}),
    ),
    "UNDERSTANDING_PAGE": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "AUTO_GROUNDING_PAGE": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "AUTO_VALIDATING_CROPS": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "WAIT_UNIT_SELECTION": (
        "WAITING_USER",
        "SELECT_UNIT",
        frozenset(
            {
                "select_unit",
                "prepare_units",
                "finish_page",
                "upload_image",
                "reset_session",
            }
        ),
    ),
    "CROP_REQUIRED": (
        "WAITING_USER",
        "SUBMIT_CROP",
        frozenset(
            {
                "submit_crop",
                "select_unit",
                "prepare_units",
                "cancel_current_unit",
                "finish_page",
                "upload_image",
                "reset_session",
            }
        ),
    ),
    "VERIFYING_CROP": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "A2_ACTIVE": (
        "RUNNING",
        "FOLLOW_CHILD_TASK",
        frozenset(
            {
                "select_unit",
                "cancel_current_unit",
                "finish_page",
                "upload_image",
                "reset_session",
            }
        ),
    ),
    "COMPLETE": (
        "COMPLETED",
        "DONE",
        frozenset({"upload_image", "reset_session"}),
    ),
    "ERROR": (
        "FAILED",
        "RETRY",
        frozenset({"retry_current_stage", "upload_image", "reset_session"}),
    ),
    "UNKNOWN": ("INCONSISTENT", "RETRY", frozenset()),
}

CHILD_PHASE_MATRIX = {
    "IDLE": ("IDLE", "UPLOAD_IMAGE", frozenset()),
    "PROCESSING": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "WAIT_CHAPTER": (
        "WAITING_USER",
        "SET_CHAPTER",
        frozenset(
            {
                "set_chapter",
                "global_search",
                "select_question",
                "explain_failure",
                "cancel",
            }
        ),
    ),
    "WAIT_QUESTION_CHOICE": (
        "WAITING_USER",
        "SELECT_QUESTION",
        frozenset({"select_question", "explain_failure", "cancel"}),
    ),
    "WAIT_CANDIDATE_CHOICE": (
        "WAITING_USER",
        "SELECT_CANDIDATE",
        frozenset(
            {
                "set_chapter",
                "select_question",
                "select_candidate",
                "reject_candidates",
                "show_candidates",
                "explain_failure",
                "cancel",
            }
        ),
    ),
    "READY_TO_ROUTE": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "READY_FOR_SEARCH": ("RUNNING", "SYSTEM_CONTINUE", frozenset()),
    "ANSWERED": (
        "COMPLETED",
        "DONE",
        frozenset(
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
            }
        ),
    ),
    "CANCELLED": ("CANCELLED", "DONE", frozenset()),
    "ERROR": (
        "FAILED",
        "RETRY",
        frozenset(
            {
                "set_chapter",
                "select_question",
                "select_candidate",
                "explain_failure",
                "retry_search",
                "cancel",
            }
        ),
    ),
    "NO_MATCH": (
        "NO_MATCH",
        "DONE",
        frozenset({"set_chapter", "select_question", "explain_failure", "cancel"}),
    ),
    "UNKNOWN": ("INCONSISTENT", "RETRY", frozenset()),
}

EXPECTED_TASK_ACTIONS = frozenset(
    {
        "upload_image",
        "reset_session",
        "retry_current_stage",
        "select_unit",
        "prepare_units",
        "submit_crop",
        "cancel_current_unit",
        "finish_page",
        "set_chapter",
        "global_search",
        "select_question",
        "select_candidate",
        "reject_candidates",
        "show_candidates",
        "report_answer_mismatch",
        "resend_answer",
        "explain_failure",
        "retry_search",
        "cancel",
    }
)

EXPECTED_WORKFLOW_STEPS = frozenset(
    {
        "IMAGE_ACCEPTED",
        "ROUTE_DECIDED",
        "PAGE_UNDERSTOOD",
        "UNIT_CATALOG_READY",
        "UNIT_SELECTED",
        "CHILD_TASK_STARTED",
        "WORKFLOW_COMPLETED",
    }
)

EXPECTED_CHILD_STEPS = frozenset(
    {
        "QUESTION_ACCEPTED",
        "QUESTION_ANALYZED",
        "CHAPTER_RESOLVED",
        "SEARCH_ROUTE_SELECTED",
        "SEARCH_COMPLETED",
        "CANDIDATES_READY",
        "ANSWER_PREPARED",
    }
)

# The public JSON is an ordered projection.  Keep these expectations as
# independent tuples (rather than deriving them from the implementation's
# constants) so a producer cannot silently change ordering while all set-based
# tests remain green.
CANONICAL_WORKFLOW_STEPS = (
    "IMAGE_ACCEPTED",
    "ROUTE_DECIDED",
    "PAGE_UNDERSTOOD",
    "UNIT_CATALOG_READY",
    "UNIT_SELECTED",
    "CHILD_TASK_STARTED",
    "WORKFLOW_COMPLETED",
)

CANONICAL_CHILD_STEPS = (
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

ALLOWED_ROUTE_PHASES = {
    "PENDING": frozenset({"UNDERSTANDING_PAGE", "ERROR"}),
    "A1": frozenset({"COMPLETE"}),
    "A2": frozenset({"A2_ACTIVE"}),
    "A3": frozenset(
        {
            "UNDERSTANDING_PAGE",
            "AUTO_GROUNDING_PAGE",
            "AUTO_VALIDATING_CROPS",
            "WAIT_UNIT_SELECTION",
            "CROP_REQUIRED",
            "VERIFYING_CROP",
            "A2_ACTIVE",
            "COMPLETE",
            "ERROR",
        }
    ),
}

# These tables intentionally duplicate the normative phase predicates from
# docs/task_state_snapshot_v1_contract.md.  Keeping the expected values here
# independent of the implementation constants catches accidental mapping
# drift even when individual phase/view tests still pass.
EXPECTED_WORKFLOW_STEPS_BY_ROUTE = {
    "NONE": frozenset(),
    "PENDING": frozenset({"IMAGE_ACCEPTED"}),
    "A1": frozenset({"IMAGE_ACCEPTED", "ROUTE_DECIDED", "WORKFLOW_COMPLETED"}),
    "A2": frozenset(
        {"IMAGE_ACCEPTED", "ROUTE_DECIDED", "CHILD_TASK_STARTED"}
    ),
    "A3": EXPECTED_WORKFLOW_STEPS,
}

EXPECTED_WORKFLOW_STEPS_BY_PHASE = {
    "IDLE": frozenset(),
    "UNDERSTANDING_PAGE": frozenset({"IMAGE_ACCEPTED", "ROUTE_DECIDED"}),
    "AUTO_GROUNDING_PAGE": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
        }
    ),
    "AUTO_VALIDATING_CROPS": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
        }
    ),
    "WAIT_UNIT_SELECTION": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
            "UNIT_SELECTED",
            "CHILD_TASK_STARTED",
        }
    ),
    "CROP_REQUIRED": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
            "UNIT_SELECTED",
            "CHILD_TASK_STARTED",
        }
    ),
    "VERIFYING_CROP": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
            "UNIT_SELECTED",
            "CHILD_TASK_STARTED",
        }
    ),
    "A2_ACTIVE": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
            "UNIT_SELECTED",
            "CHILD_TASK_STARTED",
        }
    ),
    "COMPLETE": EXPECTED_WORKFLOW_STEPS,
    "ERROR": frozenset(
        {
            "IMAGE_ACCEPTED",
            "ROUTE_DECIDED",
            "PAGE_UNDERSTOOD",
            "UNIT_CATALOG_READY",
            "UNIT_SELECTED",
            "CHILD_TASK_STARTED",
        }
    ),
    "UNKNOWN": frozenset(),
}

EXPECTED_CHILD_STEPS_BY_PHASE = {
    "IDLE": frozenset(),
    "PROCESSING": frozenset({"QUESTION_ACCEPTED"}),
    "WAIT_CHAPTER": frozenset({"QUESTION_ACCEPTED", "QUESTION_ANALYZED"}),
    "WAIT_QUESTION_CHOICE": frozenset(
        {"QUESTION_ACCEPTED", "QUESTION_ANALYZED"}
    ),
    "READY_TO_ROUTE": frozenset(
        {"QUESTION_ACCEPTED", "QUESTION_ANALYZED", "CHAPTER_RESOLVED"}
    ),
    "READY_FOR_SEARCH": frozenset(
        {
            "QUESTION_ACCEPTED",
            "QUESTION_ANALYZED",
            "CHAPTER_RESOLVED",
            "SEARCH_ROUTE_SELECTED",
        }
    ),
    "WAIT_CANDIDATE_CHOICE": frozenset(
        {
            "QUESTION_ACCEPTED",
            "QUESTION_ANALYZED",
            "CHAPTER_RESOLVED",
            "SEARCH_ROUTE_SELECTED",
            "SEARCH_COMPLETED",
            "CANDIDATES_READY",
        }
    ),
    "ANSWERED": EXPECTED_CHILD_STEPS,
    "CANCELLED": EXPECTED_CHILD_STEPS,
    "ERROR": frozenset(
        {
            "QUESTION_ACCEPTED",
            "QUESTION_ANALYZED",
            "CHAPTER_RESOLVED",
            "SEARCH_ROUTE_SELECTED",
            "SEARCH_COMPLETED",
            "CANDIDATES_READY",
        }
    ),
    "NO_MATCH": frozenset(
        {
            "QUESTION_ACCEPTED",
            "QUESTION_ANALYZED",
            "CHAPTER_RESOLVED",
            "SEARCH_ROUTE_SELECTED",
            "SEARCH_COMPLETED",
        }
    ),
    "UNKNOWN": frozenset(),
}


class TaskStateContractTests(unittest.TestCase):
    def _workflow(self, *, phase="WAIT_UNIT_SELECTION", route="A3", **overrides):
        status, next_stage, _actions = WORKFLOW_PHASE_MATRIX[phase]
        values = {
            "exists": True,
            "workflow_id": "search_workflow_12345678",
            "kind": "IMAGE_SEARCH",
            "route": route,
            "task_revision": 7,
            "phase": phase,
            "status": status,
            "completed_steps": (),
            "allowed_actions": (),
            "next_stage": next_stage,
        }
        values.update(overrides)
        return contract.WorkflowStateView(**values)

    def _child(self, *, phase="WAIT_CANDIDATE_CHOICE", unit_id="", **overrides):
        status, next_stage, _actions = CHILD_PHASE_MATRIX[phase]
        values = {
            "task_id": "search_child_12345678",
            "kind": "A2_QUESTION",
            "unit_id": unit_id,
            "task_revision": 3,
            "phase": phase,
            "status": status,
            "completed_steps": (),
            "allowed_actions": (),
            "next_stage": next_stage,
            "chapter": "2静定结构",
            "candidate_count": 3,
            "candidate_generation": "3:1",
        }
        values.update(overrides)
        return contract.ChildTaskStateView(**values)

    @staticmethod
    def _unit(
        unit_id="g1-u1",
        page_index=1,
        display_label="四-1",
        status="AVAILABLE",
    ):
        return contract.UnitStateView(unit_id, page_index, display_label, status)

    def _snapshot_for(self, route, phase):
        workflow = self._workflow(route=route, phase=phase)
        units = ()
        current_unit = None
        child = None

        if phase in {"CROP_REQUIRED", "VERIFYING_CROP"}:
            current_unit = self._unit(status="ACTIVE")
            units = (current_unit,)
        elif phase == "A2_ACTIVE":
            if route == "A3":
                current_unit = self._unit(status="ACTIVE")
                units = (current_unit,)
                child = self._child(unit_id=current_unit.unit_id)
            else:
                child = self._child(unit_id="")
        elif route == "A3" and phase == "WAIT_UNIT_SELECTION":
            units = (self._unit(status="AVAILABLE"),)
        elif route == "A3" and phase == "COMPLETE":
            units = (
                self._unit("g1-u1", 1, "四-1", "COMPLETED"),
                self._unit("g1-u2", 2, "四-2", "CLOSED"),
            )

        return contract.TaskStateSnapshotV1(
            workflow=workflow,
            active_child_task=child,
            current_unit=current_unit,
            units=units,
        )

    def test_public_v1_literals_are_exact(self):
        self.assertEqual(contract.TASK_STATE_CONTRACT, "task_state_snapshot")
        self.assertEqual(contract.TASK_STATE_SCHEMA_VERSION, 1)
        self.assertEqual(contract.PHASE_UNKNOWN, "UNKNOWN")
        self.assertEqual(contract.PHASE_NAMESPACES, frozenset({"workflow", "child_task"}))
        self.assertEqual(
            contract.TASK_STATUSES,
            frozenset(
                {
                    "IDLE",
                    "RUNNING",
                    "WAITING_USER",
                    "COMPLETED",
                    "NO_MATCH",
                    "CANCELLED",
                    "FAILED",
                    "INCONSISTENT",
                }
            ),
        )
        self.assertEqual(
            contract.NEXT_STAGES,
            frozenset(
                {
                    "UPLOAD_IMAGE",
                    "SYSTEM_CONTINUE",
                    "SELECT_UNIT",
                    "SUBMIT_CROP",
                    "FOLLOW_CHILD_TASK",
                    "SET_CHAPTER",
                    "SELECT_QUESTION",
                    "SELECT_CANDIDATE",
                    "RETRY",
                    "DONE",
                }
            ),
        )
        self.assertEqual(contract.TASK_ACTIONS, EXPECTED_TASK_ACTIONS)
        self.assertEqual(
            contract.UNIT_STATUSES,
            frozenset({"AVAILABLE", "PREPARED", "ACTIVE", "COMPLETED", "CLOSED"}),
        )
        self.assertEqual(contract.CONSISTENCY_STATUSES, frozenset({"OK", "INCONSISTENT"}))
        self.assertEqual(contract.CONSISTENCY_CODES, EXPECTED_CONSISTENCY_CODES)
        self.assertEqual(contract.WORKFLOW_KINDS, frozenset({"NONE", "IMAGE_SEARCH"}))
        self.assertEqual(
            contract.WORKFLOW_ROUTES,
            frozenset({"NONE", "PENDING", "A1", "A2", "A3"}),
        )
        self.assertEqual(contract.CHILD_KIND_A2_QUESTION, "A2_QUESTION")
        self.assertEqual(contract.WORKFLOW_COMPLETED_STEPS, EXPECTED_WORKFLOW_STEPS)
        self.assertEqual(contract.CHILD_COMPLETED_STEPS, EXPECTED_CHILD_STEPS)
        self.assertEqual(
            contract.COMPLETED_STEPS,
            EXPECTED_WORKFLOW_STEPS | EXPECTED_CHILD_STEPS,
        )
        self.assertEqual(
            contract.WORKFLOW_CURRENT_UNIT_PHASES,
            frozenset({"CROP_REQUIRED", "VERIFYING_CROP", "A2_ACTIVE"}),
        )
        for removed in {
            "cancel_child_task",
            "retry_child_task",
            "search_image",
            "continue_search",
        }:
            with self.subTest(removed=removed):
                self.assertNotIn(removed, contract.TASK_ACTIONS)

    def test_serialized_v1_shape_and_task_revision_keys_are_exact(self):
        empty = contract.empty_task_state_snapshot().to_dict()
        self.assertEqual(
            empty,
            {
                "schema_version": 1,
                "workflow": {
                    "exists": False,
                    "workflow_id": "",
                    "kind": "NONE",
                    "route": "NONE",
                    "task_revision": 0,
                    "phase": "IDLE",
                    "status": "IDLE",
                    "completed_steps": [],
                    "allowed_actions": [],
                    "next_stage": "UPLOAD_IMAGE",
                },
                "active_child_task": None,
                "current_unit": None,
                "units": [],
                "consistency": {"status": "OK", "codes": []},
            },
        )

        unit = self._unit("g1-u2", 2, "四-2", "ACTIVE")
        workflow = self._workflow(
            phase="A2_ACTIVE",
            completed_steps=("IMAGE_ACCEPTED", "ROUTE_DECIDED", "UNIT_SELECTED"),
            allowed_actions=("select_unit", "finish_page"),
        )
        child = self._child(
            unit_id=unit.unit_id,
            completed_steps=("QUESTION_ACCEPTED", "SEARCH_COMPLETED", "CANDIDATES_READY"),
            allowed_actions=("select_candidate", "reject_candidates"),
        )
        payload = contract.TaskStateSnapshotV1(
            workflow=workflow,
            active_child_task=child,
            current_unit=unit,
            units=(unit,),
        ).to_dict()

        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "workflow",
                "active_child_task",
                "current_unit",
                "units",
                "consistency",
            },
        )
        self.assertEqual(
            set(payload["workflow"]),
            {
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
            },
        )
        self.assertEqual(
            set(payload["active_child_task"]),
            {
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
            },
        )
        self.assertEqual(
            set(payload["current_unit"]),
            {"unit_id", "page_index", "display_label", "status"},
        )
        self.assertEqual(set(payload["consistency"]), {"status", "codes"})
        self.assertEqual(payload["workflow"]["task_revision"], 7)
        self.assertEqual(payload["active_child_task"]["task_revision"], 3)
        self.assertNotIn("revision", payload["workflow"])
        self.assertNotIn("revision", payload["active_child_task"])
        self.assertNotIn("child_tasks", payload)

    def test_all_authoritative_phase_contracts_match_exact_matrices(self):
        a3_phases = {
            a3_runtime.A3_PHASE_IDLE,
            a3_runtime.A3_PHASE_UNDERSTANDING,
            a3_runtime.A3_PHASE_AUTO_GROUNDING,
            a3_runtime.A3_PHASE_AUTO_VALIDATING,
            a3_runtime.A3_PHASE_WAIT_SELECTION,
            a3_runtime.A3_PHASE_CROP_REQUIRED,
            a3_runtime.A3_PHASE_VERIFYING,
            a3_runtime.A3_PHASE_A2_ACTIVE,
            a3_runtime.A3_PHASE_COMPLETE,
            a3_runtime.A3_PHASE_ERROR,
        }
        self.assertEqual(set(WORKFLOW_PHASE_MATRIX) - {"UNKNOWN"}, a3_phases)
        self.assertEqual(set(CHILD_PHASE_MATRIX) - {"UNKNOWN"}, KNOWN_PHASES)
        self.assertEqual(set(contract.WORKFLOW_PHASE_CONTRACTS), set(WORKFLOW_PHASE_MATRIX))
        self.assertEqual(set(contract.CHILD_PHASE_CONTRACTS), set(CHILD_PHASE_MATRIX))

        for namespace, expected, actual in (
            ("workflow", WORKFLOW_PHASE_MATRIX, contract.WORKFLOW_PHASE_CONTRACTS),
            ("child_task", CHILD_PHASE_MATRIX, contract.CHILD_PHASE_CONTRACTS),
        ):
            for phase, (status, next_stage, actions) in expected.items():
                with self.subTest(namespace=namespace, phase=phase):
                    item = actual[phase]
                    self.assertEqual(item.namespace, namespace)
                    self.assertEqual(item.value, phase)
                    self.assertEqual(item.status, status)
                    self.assertEqual(item.next_stage, next_stage)
                    self.assertEqual(frozenset(item.action_candidates), actions)
                    self.assertEqual(len(item.action_candidates), len(set(item.action_candidates)))

        with self.assertRaisesRegex(ValueError, "unknown workflow phase"):
            contract.phase_contract("workflow", "WAIT_CANDIDATE_CHOICE")
        with self.assertRaisesRegex(ValueError, "unknown child_task phase"):
            contract.phase_contract("child_task", "WAIT_UNIT_SELECTION")
        with self.assertRaisesRegex(ValueError, "namespace"):
            contract.phase_contract("mixed", "IDLE")

    def test_completed_step_phase_and_route_tables_are_exact(self):
        """Freeze every route/phase upper-bound predicate independently."""

        self.assertEqual(
            set(contract.WORKFLOW_COMPLETED_STEPS_BY_ROUTE),
            set(EXPECTED_WORKFLOW_STEPS_BY_ROUTE),
        )
        self.assertEqual(
            set(contract.WORKFLOW_COMPLETED_STEPS_BY_PHASE),
            set(EXPECTED_WORKFLOW_STEPS_BY_PHASE),
        )
        self.assertEqual(
            set(contract.CHILD_COMPLETED_STEPS_BY_PHASE),
            set(EXPECTED_CHILD_STEPS_BY_PHASE),
        )

        for route, expected in EXPECTED_WORKFLOW_STEPS_BY_ROUTE.items():
            with self.subTest(table="workflow_by_route", route=route):
                self.assertEqual(
                    contract.WORKFLOW_COMPLETED_STEPS_BY_ROUTE[route],
                    expected,
                )
        for phase, expected in EXPECTED_WORKFLOW_STEPS_BY_PHASE.items():
            with self.subTest(table="workflow_by_phase", phase=phase):
                self.assertEqual(
                    contract.WORKFLOW_COMPLETED_STEPS_BY_PHASE[phase],
                    expected,
                )
        for phase, expected in EXPECTED_CHILD_STEPS_BY_PHASE.items():
            with self.subTest(table="child_by_phase", phase=phase):
                self.assertEqual(
                    contract.CHILD_COMPLETED_STEPS_BY_PHASE[phase],
                    expected,
                )

    def test_phase_contract_unknown_and_non_string_inputs_fail_closed(self):
        """Unknown raw phases must not be stringified or silently treated as IDLE."""

        for namespace in ("workflow", "child_task"):
            with self.subTest(namespace=namespace, value="BOGUS"):
                with self.assertRaisesRegex(ValueError, f"unknown {namespace} phase"):
                    contract.phase_contract(namespace, "BOGUS")
            for value in (None, 1, True, b"IDLE"):
                with self.subTest(namespace=namespace, value=repr(value)):
                    with self.assertRaisesRegex(ValueError, "invalid task phase"):
                        contract.phase_contract(namespace, value)

        # Runtime normalization is represented by the stable UNKNOWN sentinel,
        # never by exposing an arbitrary source phase in a public view.
        workflow = self._workflow(
            phase="UNKNOWN",
            route="PENDING",
            status="INCONSISTENT",
            completed_steps=(),
            allowed_actions=(),
            next_stage="RETRY",
        )
        snapshot = contract.TaskStateSnapshotV1(
            workflow=workflow,
            consistency=contract.ConsistencyView(
                status="INCONSISTENT",
                codes=("UNKNOWN_WORKFLOW_PHASE",),
            ),
        )
        self.assertEqual(snapshot.workflow.phase, "UNKNOWN")
        self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.allowed_actions, ())
        self.assertEqual(snapshot.workflow.next_stage, "RETRY")
        for namespace in ("workflow", "child_task"):
            for invalid in (None, 1, True, b"IDLE", ["IDLE"]):
                with self.subTest(namespace=namespace, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "invalid task phase"):
                        contract.phase_contract(namespace, invalid)

    def test_route_phase_and_required_component_matrix(self):
        for route, phases in ALLOWED_ROUTE_PHASES.items():
            for phase in phases:
                with self.subTest(valid=True, route=route, phase=phase):
                    snapshot = self._snapshot_for(route, phase)
                    self.assertEqual(snapshot.workflow.route, route)
                    self.assertEqual(snapshot.workflow.phase, phase)

        concrete_phases = set(WORKFLOW_PHASE_MATRIX) - {"UNKNOWN"}
        for route, phase in product(ALLOWED_ROUTE_PHASES, concrete_phases):
            if phase in ALLOWED_ROUTE_PHASES[route]:
                continue
            with self.subTest(valid=False, route=route, phase=phase):
                with self.assertRaises(ValueError):
                    self._snapshot_for(route, phase)

        with self.assertRaises(ValueError):
            self._workflow(phase="IDLE", route="NONE")

        for phase in ("CROP_REQUIRED", "VERIFYING_CROP"):
            with self.subTest(missing_current=phase):
                workflow = self._workflow(phase=phase, route="A3")
                with self.assertRaises(ValueError):
                    contract.TaskStateSnapshotV1(workflow=workflow)

    def test_candidate_generation_has_exact_v1_shape_and_task_binding(self):
        """Freeze the public ``task_revision:candidate_revision`` projection.

        The child view intentionally does not expose ``candidate_revision`` as
        a separate key.  V1 can therefore enforce the shape, positive integer
        components, and task-revision prefix here; the runtime builder is
        responsible for comparing the second component with its authoritative
        counter and emitting the mismatch consistency code when needed.
        """

        valid = self._child(candidate_count=3, candidate_generation="3:1")
        self.assertEqual(valid.candidate_generation, "3:1")
        self.assertIsNone(
            contract.validate_candidate_generation(
                3,
                3,
                "3:1",
                candidate_revision=1,
            )
        )
        # An empty result may still be a real, incremented search generation;
        # no public generation string is emitted in that case.
        self.assertIsNone(
            contract.validate_candidate_generation(
                3,
                0,
                "",
                candidate_revision=0,
            )
        )
        self.assertIsNone(
            contract.validate_candidate_generation(
                3,
                0,
                "",
                candidate_revision=4,
            )
        )
        for authoritative_revision in (0, 2, 4):
            with self.subTest(authoritative_revision=authoritative_revision):
                with self.assertRaisesRegex(
                    ValueError,
                    "candidate_generation does not match candidate revision",
                ):
                    contract.validate_candidate_generation(
                        3,
                        3,
                        "3:1",
                        candidate_revision=authoritative_revision,
                    )
        self.assertEqual(
            self._child(
                phase="PROCESSING",
                candidate_count=0,
                candidate_generation="",
            ).candidate_generation,
            "",
        )

        invalid_builders = {
            "nonempty_count_without_generation": lambda: self._child(
                candidate_count=3,
                candidate_generation="",
            ),
            "empty_count_with_generation": lambda: self._child(
                candidate_count=0,
                candidate_generation="3:1",
            ),
            "task_revision_prefix_mismatch": lambda: self._child(
                candidate_count=3,
                candidate_generation="4:1",
            ),
            "zero_task_component": lambda: self._child(
                candidate_count=3,
                candidate_generation="0:1",
            ),
            "zero_candidate_component": lambda: self._child(
                candidate_count=3,
                candidate_generation="3:0",
            ),
            "leading_zero_task_component": lambda: self._child(
                candidate_count=3,
                candidate_generation="03:1",
            ),
            "leading_zero_candidate_component": lambda: self._child(
                candidate_count=3,
                candidate_generation="3:01",
            ),
            "non_numeric_component": lambda: self._child(
                candidate_count=3,
                candidate_generation="3:x",
            ),
            "too_many_task_digits": lambda: self._child(
                candidate_count=3,
                candidate_generation="12345678:1",
            ),
            "too_many_candidate_digits": lambda: self._child(
                candidate_count=3,
                candidate_generation="3:12345678",
            ),
        }
        for label, build in invalid_builders.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    build()

        # A mismatch code is a fail-closed projection supplied by the later
        # runtime builder after it has compared the second component with the
        # authoritative candidate_revision.  It must still round-trip using
        # the already-cleaned, internally valid child shape.
        inconsistent_child = self._child(
            status="INCONSISTENT",
            next_stage="RETRY",
            allowed_actions=(),
            candidate_count=3,
            candidate_generation="3:1",
        )
        mismatch = contract.TaskStateSnapshotV1(
            workflow=contract.empty_task_state_snapshot().workflow,
            active_child_task=inconsistent_child,
            consistency=contract.ConsistencyView(
                status="INCONSISTENT",
                codes=("CHILD_CANDIDATE_GENERATION_MISMATCH",),
            ),
        )
        self.assertEqual(
            mismatch.to_dict()["consistency"],
            {
                "status": "INCONSISTENT",
                "codes": ["CHILD_CANDIDATE_GENERATION_MISMATCH"],
            },
        )
        self.assertEqual(mismatch.active_child_task.status, "INCONSISTENT")
        self.assertEqual(mismatch.active_child_task.allowed_actions, ())
        self.assertEqual(mismatch.active_child_task.next_stage, "RETRY")

    def test_active_idle_child_is_rejected_for_all_supported_topologies(self):
        """An IDLE child is not a current task in standalone or wrapped mode."""

        def idle_child(**overrides):
            values = {
                "phase": "IDLE",
                "status": "IDLE",
                "next_stage": "UPLOAD_IMAGE",
                "candidate_count": 0,
                "candidate_generation": "",
            }
            values.update(overrides)
            return self._child(**values)

        active_unit = self._unit("g1-u1", 1, "四-1", "ACTIVE")
        cases = {
            "standalone_a2": {
                "workflow": contract.empty_task_state_snapshot().workflow,
                "active_child_task": idle_child(),
            },
            "direct_a2_wrapper": {
                "workflow": self._workflow(route="A2", phase="A2_ACTIVE"),
                "active_child_task": idle_child(),
            },
            "a3_wrapper": {
                "workflow": self._workflow(route="A3", phase="A2_ACTIVE"),
                "active_child_task": idle_child(unit_id=active_unit.unit_id),
                "current_unit": active_unit,
                "units": (active_unit,),
            },
        }
        for topology, kwargs in cases.items():
            with self.subTest(topology=topology):
                with self.assertRaises((TypeError, ValueError)):
                    contract.TaskStateSnapshotV1(**kwargs)

    def test_completed_steps_use_independent_canonical_order(self):
        """JSON step arrays are ordered subsequences, never arbitrary sets."""

        self.assertEqual(
            contract.WORKFLOW_COMPLETED_STEP_ORDER,
            CANONICAL_WORKFLOW_STEPS,
        )
        self.assertEqual(contract.CHILD_COMPLETED_STEP_ORDER, CANONICAL_CHILD_STEPS)

        # Fields may be cleaned independently, so an ordered sparse sequence
        # remains valid; only the canonical relative order is frozen.
        workflow = self._workflow(
            route="A3",
            phase="WAIT_UNIT_SELECTION",
            completed_steps=("IMAGE_ACCEPTED", "PAGE_UNDERSTOOD"),
        )
        child = self._child(
            phase="WAIT_CANDIDATE_CHOICE",
            completed_steps=("QUESTION_ACCEPTED", "CHAPTER_RESOLVED"),
        )
        self.assertEqual(
            workflow.completed_steps,
            ("IMAGE_ACCEPTED", "PAGE_UNDERSTOOD"),
        )
        self.assertEqual(
            child.completed_steps,
            ("QUESTION_ACCEPTED", "CHAPTER_RESOLVED"),
        )

        invalid = {
            "workflow_reverse": lambda: self._workflow(
                route="A3",
                phase="WAIT_UNIT_SELECTION",
                completed_steps=("ROUTE_DECIDED", "IMAGE_ACCEPTED"),
            ),
            "workflow_late_before_early": lambda: self._workflow(
                route="A3",
                phase="WAIT_UNIT_SELECTION",
                completed_steps=("UNIT_SELECTED", "PAGE_UNDERSTOOD"),
            ),
            "child_reverse": lambda: self._child(
                phase="WAIT_CANDIDATE_CHOICE",
                completed_steps=("QUESTION_ANALYZED", "QUESTION_ACCEPTED"),
            ),
            "child_late_before_early": lambda: self._child(
                phase="WAIT_CANDIDATE_CHOICE",
                completed_steps=("CANDIDATES_READY", "CHAPTER_RESOLVED"),
            ),
        }
        for label, build in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    build()

        # Full canonical arrays must serialize in exactly the declared order.
        complete_workflow = self._workflow(
            route="A3",
            phase="COMPLETE",
            completed_steps=CANONICAL_WORKFLOW_STEPS,
        )
        complete_child = self._child(
            phase="ANSWERED",
            completed_steps=CANONICAL_CHILD_STEPS,
        )
        self.assertEqual(
            complete_workflow.to_dict()["completed_steps"],
            list(CANONICAL_WORKFLOW_STEPS),
        )
        self.assertEqual(
            complete_child.to_dict()["completed_steps"],
            list(CANONICAL_CHILD_STEPS),
        )

    def test_task_revision_zero_is_reserved_for_empty_projection(self):
        empty = contract.empty_task_state_snapshot()
        self.assertEqual(empty.workflow.task_revision, 0)

        invalid_existing = {
            "workflow_zero": lambda: self._workflow(task_revision=0),
            "workflow_negative": lambda: self._workflow(task_revision=-1),
            "child_zero": lambda: self._child(
                phase="PROCESSING",
                task_revision=0,
                candidate_count=0,
                candidate_generation="",
            ),
            "child_negative": lambda: self._child(
                phase="PROCESSING",
                task_revision=-1,
                candidate_count=0,
                candidate_generation="",
            ),
        }
        for label, build in invalid_existing.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    build()

        # Parent and child revisions are independent namespaces.  Equality is
        # therefore legal and must not be treated as a collision or binding.
        same_revision_workflow = self._workflow(
            route="A2",
            phase="A2_ACTIVE",
            task_revision=9,
        )
        same_revision_child = self._child(
            task_revision=9,
            candidate_generation="9:1",
        )
        same_revision = contract.TaskStateSnapshotV1(
            workflow=same_revision_workflow,
            active_child_task=same_revision_child,
        )
        self.assertEqual(same_revision.workflow.task_revision, 9)
        self.assertEqual(same_revision.active_child_task.task_revision, 9)

    def test_strict_runtime_types_reject_coercion(self):
        empty_workflow = contract.empty_task_state_snapshot().workflow
        invalid_builders = {
            "workflow_exists_int": lambda: self._workflow(exists=1),
            "workflow_exists_string": lambda: self._workflow(exists="true"),
            # Optional identifiers still have a strict string type when they
            # carry the empty/missing sentinel.  Falsey non-strings must not
            # bypass validation through ``if value`` branches.
            "missing_workflow_id_int": lambda: self._workflow(
                exists=False,
                workflow_id=0,
                kind="NONE",
                route="NONE",
                phase="IDLE",
                status="IDLE",
                task_revision=0,
                completed_steps=(),
                allowed_actions=(),
                next_stage="UPLOAD_IMAGE",
            ),
            "missing_workflow_id_bool": lambda: self._workflow(
                exists=False,
                workflow_id=False,
                kind="NONE",
                route="NONE",
                phase="IDLE",
                status="IDLE",
                task_revision=0,
                completed_steps=(),
                allowed_actions=(),
                next_stage="UPLOAD_IMAGE",
            ),
            "workflow_revision_bool": lambda: self._workflow(task_revision=True),
            "workflow_revision_float": lambda: self._workflow(task_revision=1.0),
            "workflow_id_int": lambda: self._workflow(workflow_id=7),
            "workflow_steps_list": lambda: self._workflow(completed_steps=[]),
            "workflow_actions_list": lambda: self._workflow(allowed_actions=[]),
            "child_revision_bool": lambda: self._child(task_revision=True),
            "child_task_id_int": lambda: self._child(task_id=7),
            "child_unit_id_int_falsey": lambda: self._child(unit_id=0),
            "child_unit_id_bool_falsey": lambda: self._child(unit_id=False),
            "child_candidate_count_bool": lambda: self._child(candidate_count=True),
            "child_chapter_int": lambda: self._child(chapter=7),
            "child_steps_list": lambda: self._child(completed_steps=[]),
            "child_actions_list": lambda: self._child(allowed_actions=[]),
            "child_generation_int_falsey": lambda: self._child(candidate_generation=0),
            "child_generation_bool_falsey": lambda: self._child(candidate_generation=False),
            "unit_page_index_bool": lambda: self._unit(page_index=True),
            "unit_id_int": lambda: self._unit(unit_id=7),
            "consistency_codes_list": lambda: contract.ConsistencyView(
                status="INCONSISTENT",
                codes=["WORKFLOW_ID_MISSING"],
            ),
            "snapshot_units_list": lambda: contract.TaskStateSnapshotV1(
                workflow=self._workflow(),
                units=[],
            ),
            "schema_version_bool": lambda: contract.TaskStateSnapshotV1(
                workflow=empty_workflow,
                schema_version=True,
            ),
            "schema_version_float": lambda: contract.TaskStateSnapshotV1(
                workflow=empty_workflow,
                schema_version=1.0,
            ),
            "unit_flag_int": lambda: contract.resolve_unit_status(completed=1),
        }
        for label, build in invalid_builders.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    build()

    def test_consistency_codes_round_trip_and_validate_shape(self):
        self.assertEqual(contract.CONSISTENCY_CODES, EXPECTED_CONSISTENCY_CODES)
        for code in sorted(EXPECTED_CONSISTENCY_CODES):
            with self.subTest(code=code):
                view = contract.ConsistencyView(
                    status="INCONSISTENT",
                    codes=(code,),
                )
                self.assertEqual(
                    view.to_dict(),
                    {"status": "INCONSISTENT", "codes": [code]},
                )

        self.assertEqual(
            contract.ConsistencyView().to_dict(),
            {"status": "OK", "codes": []},
        )
        invalid = (
            ("OK", ("WORKFLOW_ID_MISSING",)),
            ("INCONSISTENT", ()),
            ("INCONSISTENT", ("NOT_A_CONTRACT_CODE",)),
            (
                "INCONSISTENT",
                ("WORKFLOW_ID_MISSING", "WORKFLOW_ID_MISSING"),
            ),
        )
        for status, codes in invalid:
            with self.subTest(status=status, codes=codes):
                with self.assertRaises(ValueError):
                    contract.ConsistencyView(status=status, codes=codes)

    def test_every_consistency_code_requires_a_fail_closed_projection(self):
        for code in sorted(EXPECTED_CONSISTENCY_CODES):
            consistency = contract.ConsistencyView(
                status="INCONSISTENT",
                codes=(code,),
            )
            with self.subTest(code=code, projection="parent_fail_closed"):
                failed_closed = self._workflow(
                    phase="A2_ACTIVE",
                    route="A3",
                    status="INCONSISTENT",
                    allowed_actions=(),
                    next_stage="RETRY",
                )
                snapshot = contract.TaskStateSnapshotV1(
                    workflow=failed_closed,
                    consistency=consistency,
                )
                self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
                self.assertEqual(snapshot.workflow.allowed_actions, ())

            with self.subTest(code=code, projection="normal_parent_rejected"):
                normal = self._workflow(phase="WAIT_UNIT_SELECTION", route="A3")
                with self.assertRaises(ValueError):
                    contract.TaskStateSnapshotV1(
                        workflow=normal,
                        units=(self._unit(status="AVAILABLE"),),
                        consistency=consistency,
                    )

        empty = contract.empty_task_state_snapshot().workflow
        unknown_child = self._child(
            phase="UNKNOWN",
            status="INCONSISTENT",
            allowed_actions=(),
            next_stage="RETRY",
        )
        unknown_snapshot = contract.TaskStateSnapshotV1(
            workflow=empty,
            active_child_task=unknown_child,
            consistency=contract.ConsistencyView(
                status="INCONSISTENT",
                codes=("UNKNOWN_CHILD_PHASE",),
            ),
        )
        self.assertEqual(unknown_snapshot.active_child_task.phase, "UNKNOWN")

        missing_id_child = self._child(
            task_id="",
            status="INCONSISTENT",
            allowed_actions=(),
            next_stage="RETRY",
        )
        missing_id_snapshot = contract.TaskStateSnapshotV1(
            workflow=empty,
            active_child_task=missing_id_child,
            consistency=contract.ConsistencyView(
                status="INCONSISTENT",
                codes=("CHILD_TASK_ID_MISSING",),
            ),
        )
        self.assertEqual(missing_id_snapshot.active_child_task.task_id, "")

        normal_child = self._child()
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=empty,
                active_child_task=normal_child,
                consistency=contract.ConsistencyView(
                    status="INCONSISTENT",
                    codes=("UNKNOWN_CHILD_PHASE",),
                ),
            )

        with self.assertRaises(ValueError):
            inconsistent_with_action = self._child(
                status="INCONSISTENT",
                allowed_actions=("select_candidate",),
                next_stage="RETRY",
            )
            contract.TaskStateSnapshotV1(
                workflow=empty,
                active_child_task=inconsistent_with_action,
                consistency=contract.ConsistencyView(
                    status="INCONSISTENT",
                    codes=("UNKNOWN_CHILD_PHASE",),
                ),
            )

    def test_parent_child_identity_standalone_and_residual_boundaries(self):
        direct = self._snapshot_for("A2", "A2_ACTIVE")
        self.assertIsNone(direct.current_unit)
        self.assertEqual(direct.units, ())
        self.assertNotEqual(
            direct.workflow.workflow_id,
            direct.active_child_task.task_id,
        )
        self.assertEqual(direct.workflow.task_revision, 7)
        self.assertEqual(direct.active_child_task.task_revision, 3)

        standalone_child = self._child(unit_id="")
        standalone = contract.TaskStateSnapshotV1(
            workflow=contract.empty_task_state_snapshot().workflow,
            active_child_task=standalone_child,
        )
        self.assertFalse(standalone.workflow.exists)
        self.assertEqual(standalone.workflow.workflow_id, "")
        self.assertEqual(standalone.active_child_task.task_id, standalone_child.task_id)

        wrapped_a3 = self._snapshot_for("A3", "A2_ACTIVE")
        self.assertEqual(
            wrapped_a3.active_child_task.unit_id,
            wrapped_a3.current_unit.unit_id,
        )
        self.assertNotEqual(
            wrapped_a3.workflow.workflow_id,
            wrapped_a3.active_child_task.task_id,
        )

        with self.assertRaises(ValueError):
            self._workflow(workflow_id="")
        with self.assertRaises(ValueError):
            self._child(task_id="")

        shared_id = "search_shared_12345678"
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=self._workflow(
                    phase="A2_ACTIVE",
                    route="A2",
                    workflow_id=shared_id,
                ),
                active_child_task=self._child(task_id=shared_id, unit_id=""),
            )

        for parent_phase in ("WAIT_UNIT_SELECTION", "COMPLETE"):
            with self.subTest(residual_child=parent_phase):
                base = self._snapshot_for("A3", parent_phase)
                residual = self._child(
                    phase="ANSWERED",
                    unit_id="",
                    status="COMPLETED",
                    next_stage="DONE",
                )
                with self.assertRaises(ValueError):
                    contract.TaskStateSnapshotV1(
                        workflow=base.workflow,
                        active_child_task=residual,
                        units=base.units,
                    )

        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=self._workflow(phase="A2_ACTIVE", route="A3"),
                active_child_task=self._child(unit_id=""),
            )

        active = self._unit("g1-u1", 1, "四-1", "ACTIVE")
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=self._workflow(phase="A2_ACTIVE", route="A3"),
                active_child_task=self._child(unit_id="g1-u2"),
                current_unit=active,
                units=(active,),
            )

        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=contract.empty_task_state_snapshot().workflow,
                active_child_task=self._child(unit_id="g1-u1"),
            )

    def test_unit_projection_exhaustively_follows_v1_priority(self):
        # ``closed`` is a legacy boolean that may mean either membership in
        # searched_unit_ids or a page-finished fallback.  The resolver only
        # receives flags, so ``completed=True, closed=True`` must retain the
        # documented priority; identity-set overlap is diagnosed by the
        # snapshot builder and is tested separately below.
        for completed, closed_or_finished, active, prepared, workflow_finished in product(
            (False, True),
            repeat=5,
        ):
            if completed:
                expected = "COMPLETED"
            elif closed_or_finished or workflow_finished:
                expected = "CLOSED"
            elif active:
                expected = "ACTIVE"
            elif prepared:
                expected = "PREPARED"
            else:
                expected = "AVAILABLE"
            with self.subTest(
                completed=completed,
                closed_or_finished=closed_or_finished,
                active=active,
                prepared=prepared,
                workflow_finished=workflow_finished,
            ):
                self.assertEqual(
                    contract.resolve_unit_status(
                        completed=completed,
                        closed=closed_or_finished,
                        active=active,
                        prepared=prepared,
                        workflow_finished=workflow_finished,
                    ),
                    expected,
                )

    def test_unit_state_overlap_is_a_fail_closed_consistency_boundary(self):
        """The builder, not the flag-only resolver, owns ID-set overlap checks."""

        # A double-true flag is intentionally still resolved by priority: the
        # second flag can represent page_finished rather than searched IDs.
        self.assertEqual(
            contract.resolve_unit_status(completed=True, closed=True),
            "COMPLETED",
        )

        workflow = self._workflow(
            route="A3",
            phase="WAIT_UNIT_SELECTION",
            status="INCONSISTENT",
            next_stage="RETRY",
            allowed_actions=(),
        )
        snapshot = contract.TaskStateSnapshotV1(
            workflow=workflow,
            consistency=contract.ConsistencyView(
                status="INCONSISTENT",
                codes=("UNIT_STATE_OVERLAP",),
            ),
        )
        self.assertEqual(snapshot.consistency.status, "INCONSISTENT")
        self.assertEqual(snapshot.consistency.codes, ("UNIT_STATE_OVERLAP",))
        self.assertEqual(snapshot.workflow.status, "INCONSISTENT")
        self.assertEqual(snapshot.workflow.allowed_actions, ())
        self.assertEqual(snapshot.workflow.next_stage, "RETRY")

    def test_unit_snapshot_structural_invariants(self):
        wait_workflow = self._workflow(phase="WAIT_UNIT_SELECTION", route="A3")
        crop_workflow = self._workflow(phase="CROP_REQUIRED", route="A3")

        duplicate_first = self._unit("g1-u1", 1, "四-1", "AVAILABLE")
        duplicate_second = self._unit("g1-u1", 2, "四-2", "PREPARED")
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=wait_workflow,
                units=(duplicate_first, duplicate_second),
            )

        first_active = self._unit("g1-u1", 1, "四-1", "ACTIVE")
        second_active = self._unit("g1-u2", 2, "四-2", "ACTIVE")
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=crop_workflow,
                units=(first_active, second_active),
            )

        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=crop_workflow,
                units=(first_active,),
            )

        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=crop_workflow,
                current_unit=first_active,
                units=(),
            )

        closed = self._unit("g1-u1", 1, "四-1", "CLOSED")
        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=crop_workflow,
                current_unit=closed,
                units=(closed,),
            )

        with self.assertRaises(ValueError):
            contract.TaskStateSnapshotV1(
                workflow=wait_workflow,
                current_unit=first_active,
                units=(first_active,),
            )

        for phase in ("CROP_REQUIRED", "VERIFYING_CROP"):
            with self.subTest(required_current=phase):
                with self.assertRaises(ValueError):
                    contract.TaskStateSnapshotV1(
                        workflow=self._workflow(phase=phase, route="A3"),
                    )

        complete = self._workflow(phase="COMPLETE", route="A3")
        for open_status in ("AVAILABLE", "PREPARED", "ACTIVE"):
            with self.subTest(complete_open_status=open_status):
                unit = self._unit(status=open_status)
                kwargs = {"current_unit": unit} if open_status == "ACTIVE" else {}
                with self.assertRaises(ValueError):
                    contract.TaskStateSnapshotV1(
                        workflow=complete,
                        units=(unit,),
                        **kwargs,
                    )

        final_snapshot = contract.TaskStateSnapshotV1(
            workflow=complete,
            units=(
                self._unit("g1-u1", 1, "四-1", "COMPLETED"),
                self._unit("g1-u2", 2, "四-2", "CLOSED"),
            ),
        )
        self.assertEqual(
            {unit.status for unit in final_snapshot.units},
            {"COMPLETED", "CLOSED"},
        )

    def test_action_candidates_and_allowed_action_subsets_are_enforced(self):
        workflow_actions = tuple(sorted(WORKFLOW_PHASE_MATRIX["WAIT_UNIT_SELECTION"][2]))
        workflow = self._workflow(
            phase="WAIT_UNIT_SELECTION",
            allowed_actions=workflow_actions,
        )
        self.assertEqual(set(workflow.allowed_actions), set(workflow_actions))

        for invalid in ("submit_crop", "set_chapter"):
            with self.subTest(namespace="workflow", invalid=invalid):
                with self.assertRaises(ValueError):
                    self._workflow(
                        phase="WAIT_UNIT_SELECTION",
                        allowed_actions=(invalid,),
                    )
        with self.assertRaises(ValueError):
            self._workflow(
                phase="WAIT_UNIT_SELECTION",
                allowed_actions=("select_unit", "select_unit"),
            )

        child_actions = tuple(sorted(CHILD_PHASE_MATRIX["WAIT_CANDIDATE_CHOICE"][2]))
        child = self._child(allowed_actions=child_actions)
        self.assertEqual(set(child.allowed_actions), set(child_actions))

        for invalid in ("global_search", "retry_search", "select_unit"):
            with self.subTest(namespace="child_task", invalid=invalid):
                with self.assertRaises(ValueError):
                    self._child(allowed_actions=(invalid,))
        with self.assertRaises(ValueError):
            self._child(allowed_actions=("select_candidate", "select_candidate"))

        with self.assertRaises(ValueError):
            self._workflow(
                phase="A2_ACTIVE",
                status="INCONSISTENT",
                next_stage="RETRY",
                allowed_actions=("select_unit",),
            )

    def test_completed_step_namespaces_and_action_allowlists_do_not_cross(self):
        for step in sorted(EXPECTED_WORKFLOW_STEPS):
            with self.subTest(valid_namespace="workflow", step=step):
                phase = "COMPLETE" if step == "WORKFLOW_COMPLETED" else "WAIT_UNIT_SELECTION"
                view = self._workflow(phase=phase, completed_steps=(step,))
                self.assertEqual(view.completed_steps, (step,))
                if phase == "COMPLETE":
                    contract.TaskStateSnapshotV1(
                        workflow=view,
                        units=(
                            self._unit("g1-u1", 1, "四-1", "COMPLETED"),
                            self._unit("g1-u2", 2, "四-2", "CLOSED"),
                        ),
                    )
            with self.subTest(invalid_namespace="child_task", step=step):
                with self.assertRaises(ValueError):
                    self._child(completed_steps=(step,))

        for step in sorted(EXPECTED_CHILD_STEPS):
            with self.subTest(valid_namespace="child_task", step=step):
                phase = "ANSWERED" if step == "ANSWER_PREPARED" else "WAIT_CANDIDATE_CHOICE"
                view = self._child(phase=phase, completed_steps=(step,))
                self.assertEqual(view.completed_steps, (step,))
            with self.subTest(invalid_namespace="workflow", step=step):
                with self.assertRaises(ValueError):
                    self._workflow(completed_steps=(step,))

        with self.assertRaises(ValueError):
            self._workflow(
                completed_steps=("IMAGE_ACCEPTED", "IMAGE_ACCEPTED"),
            )
        with self.assertRaises(ValueError):
            self._child(
                completed_steps=("QUESTION_ACCEPTED", "QUESTION_ACCEPTED"),
            )

    def test_unknown_phase_has_no_completed_step_upper_bound(self):
        self.assertEqual(
            contract.WORKFLOW_COMPLETED_STEPS_BY_PHASE["UNKNOWN"],
            frozenset(),
        )
        self.assertEqual(
            contract.CHILD_COMPLETED_STEPS_BY_PHASE["UNKNOWN"],
            frozenset(),
        )
        for step in sorted(EXPECTED_WORKFLOW_STEPS):
            with self.subTest(namespace="workflow", step=step):
                with self.assertRaises(ValueError):
                    self._workflow(
                        phase="UNKNOWN",
                        status="INCONSISTENT",
                        next_stage="RETRY",
                        allowed_actions=(),
                        completed_steps=(step,),
                    )
        for step in sorted(EXPECTED_CHILD_STEPS):
            with self.subTest(namespace="child_task", step=step):
                with self.assertRaises(ValueError):
                    self._child(
                        phase="UNKNOWN",
                        status="INCONSISTENT",
                        next_stage="RETRY",
                        allowed_actions=(),
                        completed_steps=(step,),
                    )


if __name__ == "__main__":
    unittest.main()

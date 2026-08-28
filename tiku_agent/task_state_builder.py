"""Pure projection from a frozen A3/A2 read-set to TaskStateSnapshotV1.

This module deliberately performs no store reads, lock acquisition, filesystem
access, logging, or state mutation.  Callers are responsible for freezing the
authoritative states and collecting capability/file evidence while holding the
appropriate runtime locks.  The builder only validates and projects that
already-frozen input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any

from tiku_agent.state import AgentState
from tiku_agent import task_state_contract as contract

if TYPE_CHECKING:
    from tiku_agent.a3_runtime import A3SessionState as _A3SessionStateT
else:
    _A3SessionStateT = Any


TOPOLOGY_A3_WRAPPER = "A3_WRAPPER"
TOPOLOGY_STANDALONE_A2 = "STANDALONE_A2"
TASK_STATE_TOPOLOGIES = frozenset(
    {TOPOLOGY_A3_WRAPPER, TOPOLOGY_STANDALONE_A2}
)

READ_OK = "OK"
READ_MISSING = "MISSING"
READ_UNREADABLE = "UNREADABLE"
READ_UNKNOWN_PHASE = "UNKNOWN_PHASE"
READ_DUPLICATE_UNIT_ID = "DUPLICATE_UNIT_ID"
WORKFLOW_READ_STATUSES = frozenset(
    {
        READ_OK,
        READ_MISSING,
        READ_UNREADABLE,
        READ_UNKNOWN_PHASE,
        READ_DUPLICATE_UNIT_ID,
    }
)
CHILD_READ_STATUSES = frozenset(
    {READ_OK, READ_MISSING, READ_UNREADABLE, READ_UNKNOWN_PHASE}
)

CHILD_OBSERVATION_LIVE = "LIVE"
CHILD_OBSERVATION_RESPONSE_FROZEN = "RESPONSE_FROZEN"
CHILD_OBSERVATIONS = frozenset(
    {CHILD_OBSERVATION_LIVE, CHILD_OBSERVATION_RESPONSE_FROZEN}
)

_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_REVISION = 1_000_000

_CONSISTENCY_CODE_ORDER = (
    contract.CONSISTENCY_WORKFLOW_ID_MISSING,
    contract.CONSISTENCY_CHILD_ID_MISSING,
    contract.CONSISTENCY_ACTIVE_CHILD_MISSING,
    contract.CONSISTENCY_ACTIVE_UNIT_MISSING,
    contract.CONSISTENCY_ACTIVE_UNIT_CLOSED,
    contract.CONSISTENCY_UNIT_STATE_OVERLAP,
    contract.CONSISTENCY_DUPLICATE_UNIT_ID,
    contract.CONSISTENCY_UNKNOWN_WORKFLOW_PHASE,
    contract.CONSISTENCY_UNKNOWN_CHILD_PHASE,
    contract.CONSISTENCY_PARENT_CHILD_ID_COLLISION,
    contract.CONSISTENCY_ORPHAN_CHILD_TASK,
    contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE,
    contract.CONSISTENCY_CHILD_STATE_UNREADABLE,
    contract.CONSISTENCY_WORKFLOW_ROUTE_PHASE_MISMATCH,
    contract.CONSISTENCY_WORKFLOW_ROUTE_UNIT_MISMATCH,
    contract.CONSISTENCY_WORKFLOW_COMPLETE_UNIT_OPEN,
    contract.CONSISTENCY_CHILD_CANDIDATE_GENERATION_MISMATCH,
)
if frozenset(_CONSISTENCY_CODE_ORDER) != contract.CONSISTENCY_CODES:
    raise RuntimeError("task-state builder consistency code order is incomplete")


@dataclass(frozen=True)
class TaskStateReadSet:
    """One already-frozen authoritative read-set.

    ``*_read_status`` keeps missing and unreadable records distinct without
    asking the pure builder to catch store exceptions.  An ``OK`` outcome must
    carry the corresponding state; all other outcomes must not.
    """

    session_id: str
    topology: str
    workflow_state: _A3SessionStateT | None = None
    child_state: AgentState | None = None
    workflow_read_status: str = READ_MISSING
    child_read_status: str = READ_MISSING
    child_observation: str = CHILD_OBSERVATION_LIVE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or not self.session_id
            or self.session_id != self.session_id.strip()
            or len(self.session_id) > 256
            or _CONTROL_RE.search(self.session_id)
        ):
            raise ValueError("session_id must already be normalized")
        if self.topology not in TASK_STATE_TOPOLOGIES:
            raise ValueError("unknown task-state topology")
        if self.workflow_read_status not in WORKFLOW_READ_STATUSES:
            raise ValueError("unknown workflow read status")
        if self.child_read_status not in CHILD_READ_STATUSES:
            raise ValueError("unknown child read status")
        if self.child_observation not in CHILD_OBSERVATIONS:
            raise ValueError("unknown child observation")
        if (self.workflow_read_status == READ_OK) != (
            self.workflow_state is not None
        ):
            raise ValueError("workflow read status and state disagree")
        if (self.child_read_status == READ_OK) != (self.child_state is not None):
            raise ValueError("child read status and state disagree")


@dataclass(frozen=True)
class TaskStateBuildEvidence:
    """Trusted non-state facts gathered outside the pure builder.

    Paths are accepted only as exact equality evidence.  The caller must have
    already established readability, containment in the controlled runtime
    directory, and file existence; this module never touches the filesystem.
    """

    trusted_image_event: bool = False
    reset_session_available: bool = False
    verified_source_page_path: str = ""
    workflow_retry_available: bool = False
    retryable_child_task: tuple[str, int] | None = None
    verified_controlled_crop_paths: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.trusted_image_event,
            self.reset_session_available,
            self.workflow_retry_available,
        ):
            if type(value) is not bool:
                raise ValueError("task-state capability flags must be boolean")
        if not isinstance(self.verified_source_page_path, str):
            raise ValueError("verified_source_page_path must be text")
        if self.retryable_child_task is not None:
            if (
                type(self.retryable_child_task) is not tuple
                or len(self.retryable_child_task) != 2
            ):
                raise ValueError("retryable_child_task must be an identity tuple")
            task_id, revision = self.retryable_child_task
            if not _valid_public_id(task_id) or not _valid_positive_revision(revision):
                raise ValueError("invalid retryable child task identity")
        if type(self.verified_controlled_crop_paths) is not tuple:
            raise ValueError("verified crop evidence must be a tuple")
        seen_units: set[str] = set()
        for item in self.verified_controlled_crop_paths:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("invalid verified crop evidence")
            unit_id, path = item
            if (
                not isinstance(unit_id, str)
                or not unit_id
                or unit_id in seen_units
                or not isinstance(path, str)
                or not path
            ):
                raise ValueError("invalid verified crop evidence")
            seen_units.add(unit_id)


def build_task_state_snapshot_v1(
    read_set: TaskStateReadSet,
    evidence: TaskStateBuildEvidence | None = None,
) -> contract.TaskStateSnapshotV1:
    """Build one V1 snapshot without I/O, locks, or mutation."""

    if type(read_set) is not TaskStateReadSet:
        raise ValueError("read_set must be a TaskStateReadSet")
    if evidence is None:
        evidence = TaskStateBuildEvidence()
    if type(evidence) is not TaskStateBuildEvidence:
        raise ValueError("evidence must be TaskStateBuildEvidence")

    codes: set[str] = set()
    workflow = _inspect_workflow(read_set, codes)
    route = _workflow_route(workflow) if workflow is not None else contract.WORKFLOW_ROUTE_NONE
    raw_workflow_phase = str(workflow.phase) if workflow is not None else "IDLE"
    workflow_phase = (
        raw_workflow_phase
        if raw_workflow_phase in contract.WORKFLOW_PHASE_CONTRACTS
        else contract.PHASE_UNKNOWN
    )

    child_required = bool(
        workflow is not None
        and workflow_phase == "A2_ACTIVE"
        and route in {"A2", "A3"}
    )
    child_relevant = _child_is_relevant(read_set, workflow, child_required)
    child = _inspect_child(read_set, codes, relevant=child_relevant)

    if read_set.topology == TOPOLOGY_STANDALONE_A2 and workflow is not None:
        codes.add(contract.CONSISTENCY_WORKFLOW_ROUTE_PHASE_MISMATCH)

    units: tuple[contract.UnitStateView, ...] = ()
    current_unit: contract.UnitStateView | None = None
    if workflow is not None:
        _collect_workflow_consistency_codes(
            workflow,
            route=route,
            phase=workflow_phase,
            child=child,
            child_required=child_required,
            codes=codes,
        )
        if not _workflow_has_fatal_read_code(codes):
            units, current_unit = _build_units(workflow, route, evidence)
            if workflow_phase == "COMPLETE" and any(
                unit.status
                not in {contract.UNIT_COMPLETED, contract.UNIT_CLOSED}
                for unit in units
            ):
                codes.add(contract.CONSISTENCY_WORKFLOW_COMPLETE_UNIT_OPEN)

    if child_required:
        if child is None and not _child_read_failed(read_set):
            codes.add(contract.CONSISTENCY_ACTIVE_CHILD_MISSING)
    elif (
        read_set.topology == TOPOLOGY_A3_WRAPPER
        and read_set.workflow_read_status == READ_MISSING
        and workflow is None
        and child is not None
    ):
        codes.add(contract.CONSISTENCY_ORPHAN_CHILD_TASK)

    if child is not None:
        _collect_child_consistency_codes(child, codes)
        if (
            workflow is not None
            and workflow.workflow_search_id
            and child.current_search_id
            and workflow.workflow_search_id == child.current_search_id
        ):
            codes.add(contract.CONSISTENCY_PARENT_CHILD_ID_COLLISION)

    if codes:
        return _build_inconsistent_snapshot(
            read_set,
            workflow=workflow,
            route=route,
            phase=workflow_phase,
            child=child,
            codes=codes,
        )

    child_view = (
        _build_child_view(
            child,
            unit_id=(workflow.selected_unit_id if workflow is not None and route == "A3" else ""),
            evidence=evidence,
        )
        if child is not None
        else None
    )
    workflow_view = _build_workflow_view(
        workflow,
        route=route,
        phase=workflow_phase,
        child_valid=child_view is not None,
        units=units,
        current_unit=current_unit,
        evidence=evidence,
    )
    return contract.TaskStateSnapshotV1(
        workflow=workflow_view,
        active_child_task=child_view,
        current_unit=current_unit,
        units=units,
    )


def _valid_public_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _PUBLIC_ID_RE.fullmatch(value)
        and contract.is_public_task_state_text(value, 128)
    )


def _valid_positive_revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_REVISION


def _valid_optional_index(value: object) -> bool:
    return value is None or (type(value) is int and value > 0)


def _valid_public_text(value: object, max_chars: int) -> bool:
    return contract.is_public_task_state_text(value, max_chars)


def _ordered_codes(codes: set[str]) -> tuple[str, ...]:
    return tuple(code for code in _CONSISTENCY_CODE_ORDER if code in codes)


def _workflow_route(state: _A3SessionStateT | None) -> str:
    if state is None:
        return contract.WORKFLOW_ROUTE_NONE
    return contract.WORKFLOW_ROUTE_PENDING if state.entry_route == "" else state.entry_route


def _workflow_has_fatal_read_code(codes: set[str]) -> bool:
    return bool(
        codes
        & {
            contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE,
            contract.CONSISTENCY_UNKNOWN_WORKFLOW_PHASE,
            contract.CONSISTENCY_DUPLICATE_UNIT_ID,
        }
    )


def _child_read_failed(read_set: TaskStateReadSet) -> bool:
    return read_set.child_read_status in {READ_UNREADABLE, READ_UNKNOWN_PHASE}


def _inspect_workflow(
    read_set: TaskStateReadSet,
    codes: set[str],
) -> _A3SessionStateT | None:
    status = read_set.workflow_read_status
    if status == READ_MISSING:
        return None
    if status == READ_UNREADABLE:
        codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)
        return None
    if status == READ_UNKNOWN_PHASE:
        codes.add(contract.CONSISTENCY_UNKNOWN_WORKFLOW_PHASE)
        return None
    if status == READ_DUPLICATE_UNIT_ID:
        codes.add(contract.CONSISTENCY_DUPLICATE_UNIT_ID)
        return None

    state = read_set.workflow_state
    try:
        schema_safe = state is not None and workflow_state_schema_is_safe(state)
    except Exception:
        schema_safe = False
    if not schema_safe:
        codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)
        return None

    phase_known = (
        isinstance(state.phase, str)
        and state.phase in contract.WORKFLOW_PHASE_CONTRACTS
        and state.phase != contract.PHASE_UNKNOWN
    )
    duplicate_units = _has_duplicate_unit_ids(state.units)
    if not phase_known:
        codes.add(contract.CONSISTENCY_UNKNOWN_WORKFLOW_PHASE)
    if duplicate_units:
        codes.add(contract.CONSISTENCY_DUPLICATE_UNIT_ID)

    if (
        state.session_id != read_set.session_id
        or not _valid_positive_revision(state.task_revision)
    ):
        codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)
    elif phase_known and not duplicate_units:
        try:
            state.validate()
        except (AttributeError, KeyError, TypeError, ValueError):
            codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)

    if not state.workflow_search_id:
        codes.add(contract.CONSISTENCY_WORKFLOW_ID_MISSING)
    elif not _valid_public_id(state.workflow_search_id):
        codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)
    return state


def _inspect_child(
    read_set: TaskStateReadSet,
    codes: set[str],
    *,
    relevant: bool,
) -> AgentState | None:
    if not relevant:
        return None
    status = read_set.child_read_status
    if status == READ_MISSING:
        return None
    if status == READ_UNREADABLE:
        codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)
        return None
    if status == READ_UNKNOWN_PHASE:
        codes.add(contract.CONSISTENCY_UNKNOWN_CHILD_PHASE)
        return None

    state = read_set.child_state
    if type(state) is not AgentState:
        codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)
        return None
    if state.phase == "IDLE":
        return None
    if (
        state.phase == "CANCELLED"
        and read_set.child_observation == CHILD_OBSERVATION_LIVE
    ):
        return None

    try:
        schema_safe = child_state_schema_is_safe(state)
    except Exception:
        schema_safe = False
    if not schema_safe:
        codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)
        return state

    phase_known = (
        isinstance(state.phase, str)
        and state.phase in contract.CHILD_PHASE_CONTRACTS
        and state.phase != contract.PHASE_UNKNOWN
    )
    if not phase_known:
        codes.add(contract.CONSISTENCY_UNKNOWN_CHILD_PHASE)
    if (
        state.session_id != read_set.session_id
        or not _valid_positive_revision(state.task_revision)
    ):
        codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)
    elif phase_known:
        try:
            state.validate()
        except (AttributeError, KeyError, TypeError, ValueError):
            codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)

    if not state.current_search_id:
        codes.add(contract.CONSISTENCY_CHILD_ID_MISSING)
    elif not _valid_public_id(state.current_search_id):
        codes.add(contract.CONSISTENCY_CHILD_STATE_UNREADABLE)
    return state


def _child_is_relevant(
    read_set: TaskStateReadSet,
    workflow: _A3SessionStateT | None,
    child_required: bool,
) -> bool:
    if child_required:
        return True
    if workflow is not None:
        # Once the parent has returned to selection or completion, any A2
        # record is residual history and must not become an active child.
        return False
    if read_set.topology == TOPOLOGY_STANDALONE_A2:
        return read_set.child_read_status != READ_MISSING
    # With no A3 parent, a readable non-idle live child is an orphan.  Read
    # errors are also relevant because they cannot be downgraded to missing.
    return read_set.child_read_status != READ_MISSING


def _has_duplicate_unit_ids(units: object) -> bool:
    if type(units) is not list:
        return False
    values: list[str] = []
    for item in units:
        if not isinstance(item, Mapping):
            return False
        value = item.get("unit_id")
        if not isinstance(value, str):
            return False
        values.append(value)
    return len(values) != len(set(values))


def workflow_state_schema_is_safe(state: _A3SessionStateT) -> bool:
    """Return whether builder-visible A3 fields have safe primitive shapes."""

    required_fields = (
        "session_id",
        "entry_route",
        "phase",
        "source_page_path",
        "page_understanding",
        "units",
        "selected_unit_id",
        "completed_unit_ids",
        "searched_unit_ids",
        "crop_drafts",
        "auto_crop_enabled",
        "auto_crops",
        "requested_unit_ids",
        "crop_review_required",
        "crop_review_code",
        "task_revision",
        "workflow_search_id",
        "page_finished",
    )
    if any(not hasattr(state, name) for name in required_fields):
        return False
    if not all(
        isinstance(value, str)
        for value in (
            state.session_id,
            state.entry_route,
            state.phase,
            state.source_page_path,
            state.selected_unit_id,
            state.crop_review_code,
            state.workflow_search_id,
        )
    ):
        return False
    if type(state.page_understanding) is not dict or type(state.units) is not list:
        return False
    if type(state.crop_drafts) is not dict or type(state.auto_crops) is not dict:
        return False
    if not all(
        type(value) is bool
        for value in (
            state.auto_crop_enabled,
            state.crop_review_required,
            state.page_finished,
        )
    ):
        return False
    if not all(
        type(values) is list and all(isinstance(value, str) for value in values)
        for values in (
            state.completed_unit_ids,
            state.searched_unit_ids,
            state.requested_unit_ids,
        )
    ):
        return False
    if type(state.task_revision) is not int or isinstance(state.task_revision, bool):
        return False
    for item in state.units:
        if not isinstance(item, Mapping):
            return False
        unit_id = item.get("unit_id")
        page_index = item.get("page_index")
        display_label = item.get("display_label")
        searchability = item.get("searchability")
        if (
            not _valid_public_id(unit_id)
            or type(page_index) is not int
            or not 1 <= page_index <= 10_000
            or not _valid_public_text(display_label, 64)
            or not isinstance(searchability, str)
        ):
            return False
    if any(not isinstance(key, str) for key in state.crop_drafts):
        return False
    for key, item in state.auto_crops.items():
        if not isinstance(key, str) or not isinstance(item, Mapping):
            return False
        if not isinstance(item.get("validation_status", ""), str):
            return False
        if not isinstance(item.get("path", ""), str):
            return False
    return True


def child_state_schema_is_safe(state: AgentState) -> bool:
    """Return whether builder-visible A2 fields have safe primitive shapes."""

    if not all(
        isinstance(value, str)
        for value in (
            state.session_id,
            state.phase,
            state.current_image_path,
            state.current_question_image_path,
            state.current_chapter,
            state.current_route,
            state.current_search_id,
            state.chapter_scope_status,
            state.last_error,
            state.candidate_generation,
        )
    ):
        return False
    if not _valid_public_text(state.current_chapter, 64):
        return False
    if not all(
        type(values) is list
        for values in (
            state.current_loads,
            state.questions,
            state.candidates,
            state.last_answer_paths,
        )
    ):
        return False
    if not all(isinstance(item, Mapping) for item in state.current_loads):
        return False
    if not all(isinstance(item, Mapping) for item in state.questions):
        return False
    if not all(isinstance(item, Mapping) for item in state.candidates):
        return False
    if len(state.candidates) > _MAX_REVISION:
        return False
    if not all(isinstance(path, str) for path in state.last_answer_paths):
        return False
    if not _valid_optional_index(state.selected_question):
        return False
    if not _valid_optional_index(state.selected_rank):
        return False
    if not _valid_positive_revision(state.task_revision):
        return False
    if (
        type(state.candidate_revision) is not int
        or isinstance(state.candidate_revision, bool)
        or not 0 <= state.candidate_revision <= _MAX_REVISION
    ):
        return False
    return type(state.global_search_offered) is bool


def _collect_workflow_consistency_codes(
    state: _A3SessionStateT,
    *,
    route: str,
    phase: str,
    child: AgentState | None,
    child_required: bool,
    codes: set[str],
) -> None:
    """Collect structural parent errors without repairing the input."""

    del child, child_required  # Kept explicit in the boundary for 3.2.2 binding.
    if _workflow_has_fatal_read_code(codes):
        return

    if route not in contract.WORKFLOW_PHASES_BY_ROUTE:
        codes.add(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE)
        return
    if phase not in contract.WORKFLOW_PHASES_BY_ROUTE[route]:
        codes.add(contract.CONSISTENCY_WORKFLOW_ROUTE_PHASE_MISMATCH)

    # A1/direct-A2 may retain A3's generic parent container, but must not carry
    # an A3 unit catalogue or current-unit meaning into the public snapshot.
    has_unit_evidence = bool(
        state.units
        or state.selected_unit_id
        or state.completed_unit_ids
        or state.searched_unit_ids
        or state.crop_drafts
        or state.auto_crops
        or state.requested_unit_ids
    )
    if route != "A3" and has_unit_evidence:
        codes.add(contract.CONSISTENCY_WORKFLOW_ROUTE_UNIT_MISMATCH)

    completed = set(state.completed_unit_ids)
    closed = set(state.searched_unit_ids)
    if completed & closed:
        codes.add(contract.CONSISTENCY_UNIT_STATE_OVERLAP)

    if route != "A3" or phase not in contract.WORKFLOW_CURRENT_UNIT_PHASES:
        return

    searchable_ids = {
        str(item.get("unit_id") or "")
        for item in state.units
        if item.get("searchability") == "searchable_candidate"
    }
    selected = state.selected_unit_id
    if not selected or selected not in searchable_ids:
        codes.add(contract.CONSISTENCY_ACTIVE_UNIT_MISSING)
    elif selected in completed or selected in closed or state.page_finished:
        codes.add(contract.CONSISTENCY_ACTIVE_UNIT_CLOSED)


def _collect_child_consistency_codes(
    state: AgentState,
    codes: set[str],
) -> None:
    """Validate the private candidate counter against the public generation."""

    if codes & {
        contract.CONSISTENCY_CHILD_STATE_UNREADABLE,
        contract.CONSISTENCY_UNKNOWN_CHILD_PHASE,
    }:
        return
    try:
        contract.validate_candidate_generation(
            state.task_revision,
            len(state.candidates),
            state.candidate_generation,
            state.candidate_revision,
        )
    except (TypeError, ValueError):
        codes.add(contract.CONSISTENCY_CHILD_CANDIDATE_GENERATION_MISMATCH)


def _build_units(
    state: _A3SessionStateT,
    route: str,
    evidence: TaskStateBuildEvidence,
) -> tuple[tuple[contract.UnitStateView, ...], contract.UnitStateView | None]:
    if route != "A3":
        return (), None

    completed = set(state.completed_unit_ids)
    closed = set(state.searched_unit_ids)
    verified_crops = dict(evidence.verified_controlled_crop_paths)
    active_phase = state.phase in contract.WORKFLOW_CURRENT_UNIT_PHASES
    views: list[contract.UnitStateView] = []

    searchable = sorted(
        (
            item
            for item in state.units
            if item.get("searchability") == "searchable_candidate"
        ),
        key=lambda item: int(item["page_index"]),
    )
    for item in searchable:
        unit_id = str(item["unit_id"])
        auto_crop = state.auto_crops.get(unit_id)
        prepared = bool(
            state.auto_crop_enabled
            and state.phase not in {"COMPLETE", "ERROR"}
            and isinstance(auto_crop, Mapping)
            and auto_crop.get("validation_status") == "auto_ready"
            and isinstance(auto_crop.get("path"), str)
            and bool(auto_crop.get("path"))
            and verified_crops.get(unit_id) == auto_crop.get("path")
        )
        status = contract.resolve_unit_status(
            completed=unit_id in completed,
            closed=unit_id in closed,
            active=bool(
                active_phase
                and not state.page_finished
                and state.selected_unit_id == unit_id
            ),
            prepared=prepared,
            workflow_finished=state.page_finished,
        )
        views.append(
            contract.UnitStateView(
                unit_id=unit_id,
                page_index=int(item["page_index"]),
                display_label=str(item.get("display_label") or ""),
                status=status,
            )
        )

    units = tuple(views)
    current = next(
        (unit for unit in units if unit.status == contract.UNIT_ACTIVE),
        None,
    )
    return units, current


def _build_child_view(
    state: AgentState,
    *,
    unit_id: str,
    evidence: TaskStateBuildEvidence,
) -> contract.ChildTaskStateView:
    phase = state.phase
    phase_spec = contract.CHILD_PHASE_CONTRACTS[phase]
    return contract.ChildTaskStateView(
        task_id=state.current_search_id,
        kind=contract.CHILD_KIND_A2_QUESTION,
        unit_id=unit_id,
        task_revision=state.task_revision,
        phase=phase,
        status=phase_spec.status,
        completed_steps=_child_completed_steps(state),
        allowed_actions=_child_allowed_actions(state, evidence),
        next_stage=phase_spec.next_stage,
        chapter=state.current_chapter,
        candidate_count=len(state.candidates),
        candidate_generation=state.candidate_generation,
    )


def _child_completed_steps(state: AgentState) -> tuple[str, ...]:
    accepted = bool(
        state.task_revision > 0
        and state.current_search_id
        and state.current_image_path
    )
    analyzed = bool(
        accepted
        and (
            state.current_question_image_path
            or state.current_loads
            or state.questions
            or state.selected_question is not None
            or state.chapter_scope_status
            or state.current_chapter
            or state.candidates
            or state.last_answer_paths
        )
    )
    search_route = state.current_route in {"main", "symbolic"}
    search_complete = search_route and state.candidate_revision > 0
    candidates_ready = bool(
        state.candidates
        and state.candidate_revision > 0
        and state.candidate_generation
        == f"{state.task_revision}:{state.candidate_revision}"
    )
    answer_prepared = bool(
        state.phase == "ANSWERED"
        and state.last_answer_paths
        and type(state.selected_rank) is int
        and 1 <= state.selected_rank <= len(state.candidates)
    )
    facts = {
        contract.CHILD_STEP_QUESTION_ACCEPTED: accepted,
        contract.CHILD_STEP_QUESTION_ANALYZED: analyzed,
        contract.CHILD_STEP_CHAPTER_RESOLVED: bool(state.current_chapter),
        contract.CHILD_STEP_ROUTE_SELECTED: search_route,
        contract.CHILD_STEP_SEARCH_COMPLETED: search_complete,
        contract.CHILD_STEP_CANDIDATES_READY: candidates_ready,
        contract.CHILD_STEP_ANSWER_PREPARED: answer_prepared,
    }
    allowed = contract.CHILD_COMPLETED_STEPS_BY_PHASE[state.phase]
    return tuple(
        step
        for step in contract.CHILD_COMPLETED_STEP_ORDER
        if facts[step] and step in allowed
    )


def _child_allowed_actions(
    state: AgentState,
    evidence: TaskStateBuildEvidence,
) -> tuple[str, ...]:
    candidates = contract.CHILD_PHASE_CONTRACTS[state.phase].action_candidates
    active_image = bool(state.active_image_path)
    has_questions = bool(state.questions)
    has_candidates = bool(state.candidates)
    has_answers = bool(state.last_answer_paths)
    retryable = bool(
        active_image
        and evidence.retryable_child_task
        == (state.current_search_id, state.task_revision)
    )
    predicates = {
        contract.ACTION_CANCEL: True,
        contract.ACTION_SET_CHAPTER: active_image,
        contract.ACTION_GLOBAL_SEARCH: bool(
            active_image and state.global_search_offered
        ),
        contract.ACTION_SELECT_QUESTION: has_questions,
        contract.ACTION_SELECT_CANDIDATE: has_candidates,
        contract.ACTION_REJECT_CANDIDATES: has_candidates,
        contract.ACTION_SHOW_CANDIDATES: has_candidates,
        contract.ACTION_REPORT_ANSWER_MISMATCH: bool(
            has_candidates and has_answers
        ),
        contract.ACTION_RESEND_ANSWER: has_answers,
        contract.ACTION_EXPLAIN_FAILURE: bool(state.last_error),
        contract.ACTION_RETRY_SEARCH: retryable,
    }
    return tuple(action for action in candidates if predicates.get(action, False))


def _build_workflow_view(
    state: _A3SessionStateT | None,
    *,
    route: str,
    phase: str,
    child_valid: bool,
    units: tuple[contract.UnitStateView, ...],
    current_unit: contract.UnitStateView | None,
    evidence: TaskStateBuildEvidence,
) -> contract.WorkflowStateView:
    if state is None:
        return _empty_workflow_view()

    phase_spec = contract.WORKFLOW_PHASE_CONTRACTS[phase]
    return contract.WorkflowStateView(
        exists=True,
        workflow_id=state.workflow_search_id,
        kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
        route=route,
        task_revision=state.task_revision,
        phase=phase,
        status=phase_spec.status,
        completed_steps=_workflow_completed_steps(
            state,
            route=route,
            phase=phase,
            child_valid=child_valid,
        ),
        allowed_actions=_workflow_allowed_actions(
            state,
            route=route,
            units=units,
            current_unit=current_unit,
            evidence=evidence,
        ),
        next_stage=phase_spec.next_stage,
    )


def _workflow_completed_steps(
    state: _A3SessionStateT,
    *,
    route: str,
    phase: str,
    child_valid: bool,
) -> tuple[str, ...]:
    page_understood = bool(
        state.entry_route == "A3"
        and isinstance(state.page_understanding, Mapping)
        and state.page_understanding
    )
    unit_ids = {
        str(item.get("unit_id") or "")
        for item in state.units
        if isinstance(item, Mapping)
    }
    unit_selected = bool(
        state.entry_route == "A3"
        and (
            state.selected_unit_id
            or state.completed_unit_ids
            or state.searched_unit_ids
            or any(unit_id in unit_ids for unit_id in state.crop_drafts)
        )
    )
    child_started = bool(
        (state.phase == "A2_ACTIVE" and child_valid)
        or state.completed_unit_ids
        or state.searched_unit_ids
    )
    facts = {
        contract.WORKFLOW_STEP_IMAGE_ACCEPTED: bool(state.source_page_path),
        contract.WORKFLOW_STEP_ROUTE_DECIDED: state.entry_route in {"A1", "A2", "A3"},
        contract.WORKFLOW_STEP_PAGE_UNDERSTOOD: page_understood,
        contract.WORKFLOW_STEP_UNIT_CATALOG_READY: page_understood,
        contract.WORKFLOW_STEP_UNIT_SELECTED: unit_selected,
        contract.WORKFLOW_STEP_CHILD_STARTED: child_started,
        contract.WORKFLOW_STEP_COMPLETED: state.phase == "COMPLETE",
    }
    allowed = (
        contract.WORKFLOW_COMPLETED_STEPS_BY_ROUTE[route]
        & contract.WORKFLOW_COMPLETED_STEPS_BY_PHASE[phase]
    )
    return tuple(
        step
        for step in contract.WORKFLOW_COMPLETED_STEP_ORDER
        if facts[step] and step in allowed
    )


def _workflow_allowed_actions(
    state: _A3SessionStateT,
    *,
    route: str,
    units: tuple[contract.UnitStateView, ...],
    current_unit: contract.UnitStateView | None,
    evidence: TaskStateBuildEvidence,
) -> tuple[str, ...]:
    candidates = contract.WORKFLOW_PHASE_CONTRACTS[state.phase].action_candidates
    remaining = any(
        unit.status not in {contract.UNIT_COMPLETED, contract.UNIT_CLOSED}
        for unit in units
    )
    exact_readable_source = bool(
        state.source_page_path
        and evidence.verified_source_page_path == state.source_page_path
    )
    predicates = {
        contract.ACTION_UPLOAD_IMAGE: evidence.trusted_image_event,
        contract.ACTION_RESET_SESSION: evidence.reset_session_available,
        contract.ACTION_RETRY_CURRENT_STAGE: bool(
            state.phase == "ERROR"
            and exact_readable_source
            and evidence.workflow_retry_available
        ),
        contract.ACTION_SELECT_UNIT: bool(
            route == "A3" and not state.page_finished and remaining
        ),
        contract.ACTION_PREPARE_UNITS: bool(
            route == "A3"
            and state.auto_crop_enabled
            and state.phase in {"WAIT_UNIT_SELECTION", "CROP_REQUIRED"}
            and remaining
        ),
        contract.ACTION_SUBMIT_CROP: bool(
            route == "A3"
            and state.phase == "CROP_REQUIRED"
            and current_unit is not None
        ),
        contract.ACTION_CANCEL_CURRENT_UNIT: bool(
            route == "A3"
            and state.phase in {"CROP_REQUIRED", "A2_ACTIVE"}
            and current_unit is not None
        ),
        contract.ACTION_FINISH_PAGE: bool(
            route == "A3" and not state.page_finished
        ),
    }
    return tuple(action for action in candidates if predicates.get(action, False))


def _build_inconsistent_snapshot(
    read_set: TaskStateReadSet,
    *,
    workflow: _A3SessionStateT | None,
    route: str,
    phase: str,
    child: AgentState | None,
    codes: set[str],
) -> contract.TaskStateSnapshotV1:
    if workflow is None and read_set.workflow_read_status != READ_MISSING:
        # A typed read failure is not an expired/absent session.  Keep a
        # stable UNKNOWN placeholder so callers cannot mistake the failure
        # for an ordinary IDLE projection.
        workflow_view = _inconsistent_workflow_placeholder()
    elif workflow is None:
        workflow_view = _empty_workflow_view()
    else:
        safe_route = (
            route
            if route in contract.WORKFLOW_ROUTES
            and route != contract.WORKFLOW_ROUTE_NONE
            else contract.WORKFLOW_ROUTE_PENDING
        )
        safe_phase = (
            phase
            if phase in contract.WORKFLOW_PHASE_CONTRACTS
            else contract.PHASE_UNKNOWN
        )
        workflow_view = contract.WorkflowStateView(
            exists=True,
            workflow_id=(
                workflow.workflow_search_id
                if _valid_public_id(workflow.workflow_search_id)
                else ""
            ),
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route=safe_route,
            task_revision=(
                workflow.task_revision
                if _valid_positive_revision(workflow.task_revision)
                else 0
            ),
            phase=safe_phase,
            status=contract.STATUS_INCONSISTENT,
            completed_steps=(),
            allowed_actions=(),
            next_stage=contract.NEXT_RETRY,
        )

    child_view: contract.ChildTaskStateView | None = None
    expose_child = bool(
        child is not None
        and (
            read_set.workflow_read_status == READ_MISSING
            or (
                workflow is not None
                and route == "A2"
                and phase == "A2_ACTIVE"
                and contract.CONSISTENCY_PARENT_CHILD_ID_COLLISION not in codes
            )
        )
    )
    if expose_child and child is not None:
        safe_child_phase = (
            child.phase
            if isinstance(child.phase, str)
            and child.phase in contract.CHILD_PHASE_CONTRACTS
            else contract.PHASE_UNKNOWN
        )
        child_view = contract.ChildTaskStateView(
            task_id=(
                child.current_search_id
                if _valid_public_id(child.current_search_id)
                else ""
            ),
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id=(
                workflow.selected_unit_id
                if workflow is not None
                and route == "A3"
                and _valid_public_id(workflow.selected_unit_id)
                else ""
            ),
            task_revision=(
                child.task_revision
                if _valid_positive_revision(child.task_revision)
                else 0
            ),
            phase=safe_child_phase,
            status=contract.STATUS_INCONSISTENT,
            completed_steps=(),
            allowed_actions=(),
            next_stage=contract.NEXT_RETRY,
            chapter=(
                child.current_chapter
                if _valid_public_text(child.current_chapter, 64)
                else ""
            ),
            candidate_count=0,
            candidate_generation="",
        )
    elif _should_expose_child_placeholder(read_set, workflow, route, phase):
        child_view = _inconsistent_child_placeholder()

    return contract.TaskStateSnapshotV1(
        workflow=workflow_view,
        active_child_task=child_view,
        current_unit=None,
        units=(),
        consistency=contract.ConsistencyView(
            status=contract.CONSISTENCY_INCONSISTENT,
            codes=_ordered_codes(codes),
        ),
    )


def _empty_workflow_view() -> contract.WorkflowStateView:
    return contract.WorkflowStateView(
        exists=False,
        workflow_id="",
        kind=contract.WORKFLOW_KIND_NONE,
        route=contract.WORKFLOW_ROUTE_NONE,
        task_revision=0,
        phase="IDLE",
        status=contract.STATUS_IDLE,
        completed_steps=(),
        allowed_actions=(),
        next_stage=contract.NEXT_UPLOAD_IMAGE,
    )


def _inconsistent_workflow_placeholder() -> contract.WorkflowStateView:
    return contract.WorkflowStateView(
        exists=True,
        workflow_id="",
        kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
        route=contract.WORKFLOW_ROUTE_PENDING,
        task_revision=0,
        phase=contract.PHASE_UNKNOWN,
        status=contract.STATUS_INCONSISTENT,
        completed_steps=(),
        allowed_actions=(),
        next_stage=contract.NEXT_RETRY,
    )


def _inconsistent_child_placeholder() -> contract.ChildTaskStateView:
    return contract.ChildTaskStateView(
        task_id="",
        kind=contract.CHILD_KIND_A2_QUESTION,
        unit_id="",
        task_revision=0,
        phase=contract.PHASE_UNKNOWN,
        status=contract.STATUS_INCONSISTENT,
        completed_steps=(),
        allowed_actions=(),
        next_stage=contract.NEXT_RETRY,
        chapter="",
        candidate_count=0,
        candidate_generation="",
    )


def _should_expose_child_placeholder(
    read_set: TaskStateReadSet,
    workflow: _A3SessionStateT | None,
    route: str,
    phase: str,
) -> bool:
    if read_set.child_read_status not in {READ_UNREADABLE, READ_UNKNOWN_PHASE}:
        return False
    if read_set.topology == TOPOLOGY_STANDALONE_A2:
        return True
    return bool(
        workflow is not None
        and route == "A2"
        and phase == "A2_ACTIVE"
    )

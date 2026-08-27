"""Executable vocabulary for the authoritative task-state snapshot contract.

Phase 3.1 defines names, normalized lifecycle meanings, and serialization
shapes only. Runtime state collection and HTTP exposure are later batches.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping


TASK_STATE_CONTRACT = "task_state_snapshot"
TASK_STATE_SCHEMA_VERSION = 1

PHASE_NAMESPACE_WORKFLOW = "workflow"
PHASE_NAMESPACE_CHILD = "child_task"
PHASE_NAMESPACES = frozenset({PHASE_NAMESPACE_WORKFLOW, PHASE_NAMESPACE_CHILD})
PHASE_UNKNOWN = "UNKNOWN"

STATUS_IDLE = "IDLE"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING_USER = "WAITING_USER"
STATUS_COMPLETED = "COMPLETED"
STATUS_NO_MATCH = "NO_MATCH"
STATUS_CANCELLED = "CANCELLED"
STATUS_FAILED = "FAILED"
STATUS_INCONSISTENT = "INCONSISTENT"
TASK_STATUSES = frozenset(
    {
        STATUS_IDLE,
        STATUS_RUNNING,
        STATUS_WAITING_USER,
        STATUS_COMPLETED,
        STATUS_NO_MATCH,
        STATUS_CANCELLED,
        STATUS_FAILED,
        STATUS_INCONSISTENT,
    }
)

NEXT_UPLOAD_IMAGE = "UPLOAD_IMAGE"
NEXT_SYSTEM_CONTINUE = "SYSTEM_CONTINUE"
NEXT_SELECT_UNIT = "SELECT_UNIT"
NEXT_SUBMIT_CROP = "SUBMIT_CROP"
NEXT_FOLLOW_CHILD = "FOLLOW_CHILD_TASK"
NEXT_SET_CHAPTER = "SET_CHAPTER"
NEXT_SELECT_QUESTION = "SELECT_QUESTION"
NEXT_SELECT_CANDIDATE = "SELECT_CANDIDATE"
NEXT_RETRY = "RETRY"
NEXT_DONE = "DONE"
NEXT_STAGES = frozenset(
    {
        NEXT_UPLOAD_IMAGE,
        NEXT_SYSTEM_CONTINUE,
        NEXT_SELECT_UNIT,
        NEXT_SUBMIT_CROP,
        NEXT_FOLLOW_CHILD,
        NEXT_SET_CHAPTER,
        NEXT_SELECT_QUESTION,
        NEXT_SELECT_CANDIDATE,
        NEXT_RETRY,
        NEXT_DONE,
    }
)

# Values deliberately use stable, machine-readable action IDs. They are not
# model prompt prose and are not a promise that the action is allowed in every
# state; the later builder filters these candidates using live state.
ACTION_UPLOAD_IMAGE = "upload_image"
ACTION_RESET_SESSION = "reset_session"
ACTION_RETRY_CURRENT_STAGE = "retry_current_stage"
ACTION_SELECT_UNIT = "select_unit"
ACTION_PREPARE_UNITS = "prepare_units"
ACTION_SUBMIT_CROP = "submit_crop"
ACTION_CANCEL_CURRENT_UNIT = "cancel_current_unit"
ACTION_FINISH_PAGE = "finish_page"
ACTION_SET_CHAPTER = "set_chapter"
ACTION_SELECT_QUESTION = "select_question"
ACTION_SELECT_CANDIDATE = "select_candidate"
ACTION_REJECT_CANDIDATES = "reject_candidates"
ACTION_SHOW_CANDIDATES = "show_candidates"
ACTION_REPORT_ANSWER_MISMATCH = "report_answer_mismatch"
ACTION_RESEND_ANSWER = "resend_answer"
ACTION_EXPLAIN_FAILURE = "explain_failure"
ACTION_GLOBAL_SEARCH = "global_search"
ACTION_RETRY_SEARCH = "retry_search"
ACTION_CANCEL = "cancel"
TASK_ACTIONS = frozenset(
    {
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
        ACTION_RETRY_CURRENT_STAGE,
        ACTION_SELECT_UNIT,
        ACTION_PREPARE_UNITS,
        ACTION_SUBMIT_CROP,
        ACTION_CANCEL_CURRENT_UNIT,
        ACTION_FINISH_PAGE,
        ACTION_SET_CHAPTER,
        ACTION_SELECT_QUESTION,
        ACTION_SELECT_CANDIDATE,
        ACTION_REJECT_CANDIDATES,
        ACTION_SHOW_CANDIDATES,
        ACTION_REPORT_ANSWER_MISMATCH,
        ACTION_RESEND_ANSWER,
        ACTION_EXPLAIN_FAILURE,
        ACTION_GLOBAL_SEARCH,
        ACTION_RETRY_SEARCH,
        ACTION_CANCEL,
    }
)

WORKFLOW_STEP_IMAGE_ACCEPTED = "IMAGE_ACCEPTED"
WORKFLOW_STEP_ROUTE_DECIDED = "ROUTE_DECIDED"
WORKFLOW_STEP_PAGE_UNDERSTOOD = "PAGE_UNDERSTOOD"
WORKFLOW_STEP_UNIT_CATALOG_READY = "UNIT_CATALOG_READY"
WORKFLOW_STEP_UNIT_SELECTED = "UNIT_SELECTED"
WORKFLOW_STEP_CHILD_STARTED = "CHILD_TASK_STARTED"
WORKFLOW_STEP_COMPLETED = "WORKFLOW_COMPLETED"

CHILD_STEP_QUESTION_ACCEPTED = "QUESTION_ACCEPTED"
CHILD_STEP_QUESTION_ANALYZED = "QUESTION_ANALYZED"
CHILD_STEP_CHAPTER_RESOLVED = "CHAPTER_RESOLVED"
CHILD_STEP_ROUTE_SELECTED = "SEARCH_ROUTE_SELECTED"
CHILD_STEP_SEARCH_COMPLETED = "SEARCH_COMPLETED"
CHILD_STEP_CANDIDATES_READY = "CANDIDATES_READY"
CHILD_STEP_ANSWER_PREPARED = "ANSWER_PREPARED"

COMPLETED_STEPS = frozenset(
    {
        WORKFLOW_STEP_IMAGE_ACCEPTED,
        WORKFLOW_STEP_ROUTE_DECIDED,
        WORKFLOW_STEP_PAGE_UNDERSTOOD,
        WORKFLOW_STEP_UNIT_CATALOG_READY,
        WORKFLOW_STEP_UNIT_SELECTED,
        WORKFLOW_STEP_CHILD_STARTED,
        WORKFLOW_STEP_COMPLETED,
        CHILD_STEP_QUESTION_ACCEPTED,
        CHILD_STEP_QUESTION_ANALYZED,
        CHILD_STEP_CHAPTER_RESOLVED,
        CHILD_STEP_ROUTE_SELECTED,
        CHILD_STEP_SEARCH_COMPLETED,
        CHILD_STEP_CANDIDATES_READY,
        CHILD_STEP_ANSWER_PREPARED,
    }
)
WORKFLOW_COMPLETED_STEPS = frozenset(
    {
        WORKFLOW_STEP_IMAGE_ACCEPTED,
        WORKFLOW_STEP_ROUTE_DECIDED,
        WORKFLOW_STEP_PAGE_UNDERSTOOD,
        WORKFLOW_STEP_UNIT_CATALOG_READY,
        WORKFLOW_STEP_UNIT_SELECTED,
        WORKFLOW_STEP_CHILD_STARTED,
        WORKFLOW_STEP_COMPLETED,
    }
)
CHILD_COMPLETED_STEPS = COMPLETED_STEPS - WORKFLOW_COMPLETED_STEPS

# The wire representation preserves this order.  A tuple is deliberately
# exported alongside the sets so producers cannot accidentally serialize a
# hash-set order or an arbitrary caller order.
WORKFLOW_COMPLETED_STEP_ORDER = (
    WORKFLOW_STEP_IMAGE_ACCEPTED,
    WORKFLOW_STEP_ROUTE_DECIDED,
    WORKFLOW_STEP_PAGE_UNDERSTOOD,
    WORKFLOW_STEP_UNIT_CATALOG_READY,
    WORKFLOW_STEP_UNIT_SELECTED,
    WORKFLOW_STEP_CHILD_STARTED,
    WORKFLOW_STEP_COMPLETED,
)
CHILD_COMPLETED_STEP_ORDER = (
    CHILD_STEP_QUESTION_ACCEPTED,
    CHILD_STEP_QUESTION_ANALYZED,
    CHILD_STEP_CHAPTER_RESOLVED,
    CHILD_STEP_ROUTE_SELECTED,
    CHILD_STEP_SEARCH_COMPLETED,
    CHILD_STEP_CANDIDATES_READY,
    CHILD_STEP_ANSWER_PREPARED,
)

UNIT_AVAILABLE = "AVAILABLE"
UNIT_PREPARED = "PREPARED"
UNIT_ACTIVE = "ACTIVE"
UNIT_COMPLETED = "COMPLETED"
UNIT_CLOSED = "CLOSED"
UNIT_STATUSES = frozenset(
    {UNIT_AVAILABLE, UNIT_PREPARED, UNIT_ACTIVE, UNIT_COMPLETED, UNIT_CLOSED}
)

CONSISTENCY_OK = "OK"
CONSISTENCY_INCONSISTENT = "INCONSISTENT"
CONSISTENCY_STATUSES = frozenset({CONSISTENCY_OK, CONSISTENCY_INCONSISTENT})

CONSISTENCY_WORKFLOW_ID_MISSING = "WORKFLOW_ID_MISSING"
CONSISTENCY_CHILD_ID_MISSING = "CHILD_TASK_ID_MISSING"
CONSISTENCY_ACTIVE_CHILD_MISSING = "ACTIVE_CHILD_TASK_MISSING"
CONSISTENCY_ACTIVE_UNIT_MISSING = "ACTIVE_UNIT_MISSING"
CONSISTENCY_ACTIVE_UNIT_CLOSED = "ACTIVE_UNIT_CLOSED"
CONSISTENCY_UNIT_STATE_OVERLAP = "UNIT_STATE_OVERLAP"
CONSISTENCY_DUPLICATE_UNIT_ID = "DUPLICATE_UNIT_ID"
CONSISTENCY_UNKNOWN_WORKFLOW_PHASE = "UNKNOWN_WORKFLOW_PHASE"
CONSISTENCY_UNKNOWN_CHILD_PHASE = "UNKNOWN_CHILD_PHASE"
CONSISTENCY_PARENT_CHILD_ID_COLLISION = "PARENT_CHILD_ID_COLLISION"
CONSISTENCY_ORPHAN_CHILD_TASK = "ORPHAN_CHILD_TASK"
CONSISTENCY_WORKFLOW_STATE_UNREADABLE = "WORKFLOW_STATE_UNREADABLE"
CONSISTENCY_CHILD_STATE_UNREADABLE = "CHILD_STATE_UNREADABLE"
CONSISTENCY_WORKFLOW_ROUTE_PHASE_MISMATCH = "WORKFLOW_ROUTE_PHASE_MISMATCH"
CONSISTENCY_WORKFLOW_ROUTE_UNIT_MISMATCH = "WORKFLOW_ROUTE_UNIT_MISMATCH"
CONSISTENCY_WORKFLOW_COMPLETE_UNIT_OPEN = "WORKFLOW_COMPLETE_UNIT_OPEN"
CONSISTENCY_CHILD_CANDIDATE_GENERATION_MISMATCH = (
    "CHILD_CANDIDATE_GENERATION_MISMATCH"
)
CONSISTENCY_CODES = frozenset(
    {
        CONSISTENCY_WORKFLOW_ID_MISSING,
        CONSISTENCY_CHILD_ID_MISSING,
        CONSISTENCY_ACTIVE_CHILD_MISSING,
        CONSISTENCY_ACTIVE_UNIT_MISSING,
        CONSISTENCY_ACTIVE_UNIT_CLOSED,
        CONSISTENCY_UNIT_STATE_OVERLAP,
        CONSISTENCY_DUPLICATE_UNIT_ID,
        CONSISTENCY_UNKNOWN_WORKFLOW_PHASE,
        CONSISTENCY_UNKNOWN_CHILD_PHASE,
        CONSISTENCY_PARENT_CHILD_ID_COLLISION,
        CONSISTENCY_ORPHAN_CHILD_TASK,
        CONSISTENCY_WORKFLOW_STATE_UNREADABLE,
        CONSISTENCY_CHILD_STATE_UNREADABLE,
        CONSISTENCY_WORKFLOW_ROUTE_PHASE_MISMATCH,
        CONSISTENCY_WORKFLOW_ROUTE_UNIT_MISMATCH,
        CONSISTENCY_WORKFLOW_COMPLETE_UNIT_OPEN,
        CONSISTENCY_CHILD_CANDIDATE_GENERATION_MISMATCH,
    }
)

WORKFLOW_KIND_NONE = "NONE"
WORKFLOW_KIND_IMAGE_SEARCH = "IMAGE_SEARCH"
WORKFLOW_KINDS = frozenset({WORKFLOW_KIND_NONE, WORKFLOW_KIND_IMAGE_SEARCH})
CHILD_KIND_A2_QUESTION = "A2_QUESTION"

WORKFLOW_ROUTE_NONE = "NONE"
WORKFLOW_ROUTE_PENDING = "PENDING"
WORKFLOW_ROUTES = frozenset(
    {WORKFLOW_ROUTE_NONE, WORKFLOW_ROUTE_PENDING, "A1", "A2", "A3"}
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CANDIDATE_GENERATION_RE = re.compile(r"^([1-9][0-9]{0,6}):([1-9][0-9]{0,6})$")

def _validate_status(value: str, expected: str) -> None:
    if value not in TASK_STATUSES:
        raise ValueError("invalid task status")
    if value not in {expected, STATUS_INCONSISTENT}:
        raise ValueError("task status does not match phase")


def _validate_revision(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError("invalid task revision")


def _validate_positive_revision(value: int, name: str) -> None:
    _validate_revision(value)
    if value == 0:
        raise ValueError(f"{name} must be positive for an existing task")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")


def _validate_optional_id(value: str, name: str) -> None:
    """Validate an ID-shaped optional field, including its empty value."""

    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    if value and not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")


def _validate_candidate_generation(
    task_revision: int,
    candidate_count: int,
    value: str,
    candidate_revision: int | None = None,
) -> None:
    """Validate the public projection of ``task_revision:candidate_revision``.

    The child view intentionally does not expose ``candidate_revision`` as a
    separate field.  Its generation therefore has to carry both positive
    integers and the first integer must equal the child task revision.  The
    runtime builder can additionally pass the authoritative counter to verify
    the second integer exactly; keeping that check here makes the operation a
    pure, side-effect-free boundary between raw state and the public view.
    """

    _validate_revision(task_revision)
    if type(candidate_count) is not int or not 0 <= candidate_count <= 1_000_000:
        raise ValueError("invalid candidate_count")
    if candidate_revision is not None:
        _validate_revision(candidate_revision)

    if not isinstance(value, str):
        raise ValueError("invalid candidate_generation")
    if candidate_count == 0:
        if value:
            raise ValueError("empty candidate set must have no generation")
        return
    match = _CANDIDATE_GENERATION_RE.fullmatch(value)
    if match is None or int(match.group(1)) != task_revision:
        raise ValueError("candidate_generation does not match task revision")
    if candidate_revision is not None:
        if candidate_revision == 0 or int(match.group(2)) != candidate_revision:
            raise ValueError("candidate_generation does not match candidate revision")


def validate_candidate_generation(
    task_revision: int,
    candidate_count: int,
    value: str,
    candidate_revision: int | None = None,
) -> None:
    """Validate a candidate-generation projection without mutating state.

    ``candidate_revision`` is optional because the V1 public child view keeps
    that authoritative counter private.  Callers constructing a view from raw
    ``AgentState`` should pass it so a stale or forged second component is
    rejected before the view is exposed.
    """

    _validate_candidate_generation(
        task_revision,
        candidate_count,
        value,
        candidate_revision,
    )


def _validate_text(value: str, max_chars: int, name: str) -> None:
    if not isinstance(value, str) or len(value) > max_chars or _CONTROL_RE.search(value):
        raise ValueError(f"invalid {name}")


def _validate_tokens(values: tuple[str, ...], allowed: frozenset[str], name: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} values must be a tuple")
    if len(values) != len(set(values)) or any(value not in allowed for value in values):
        raise ValueError(f"invalid {name}")


def _validate_canonical_order(
    values: tuple[str, ...], order: tuple[str, ...], name: str
) -> None:
    expected = tuple(token for token in order if token in values)
    if values != expected:
        raise ValueError(f"{name} values must use canonical order")


@dataclass(frozen=True)
class PhaseContract:
    namespace: str
    value: str
    status: str
    next_stage: str
    action_candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.namespace not in PHASE_NAMESPACES:
            raise ValueError("unknown task phase namespace")
        if not _TOKEN_RE.fullmatch(self.value):
            raise ValueError("invalid task phase")
        if self.status not in TASK_STATUSES:
            raise ValueError("invalid phase status")
        if (self.status == STATUS_INCONSISTENT) != (self.value == PHASE_UNKNOWN):
            raise ValueError("inconsistent status is reserved for UNKNOWN phase")
        if self.next_stage not in NEXT_STAGES:
            raise ValueError("invalid next stage")
        _validate_tokens(self.action_candidates, TASK_ACTIONS, "task action")

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "value": self.value,
            "status": self.status,
            "next_stage": self.next_stage,
            "action_candidates": list(self.action_candidates),
        }


def _phase(
    namespace: str,
    value: str,
    status: str,
    next_stage: str,
    *actions: str,
) -> PhaseContract:
    return PhaseContract(namespace, value, status, next_stage, tuple(actions))


_WORKFLOW_PHASE_CONTRACTS = {
    "IDLE": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "IDLE",
        STATUS_IDLE,
        NEXT_UPLOAD_IMAGE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    "UNDERSTANDING_PAGE": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "UNDERSTANDING_PAGE",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "AUTO_GROUNDING_PAGE": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "AUTO_GROUNDING_PAGE",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "AUTO_VALIDATING_CROPS": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "AUTO_VALIDATING_CROPS",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "WAIT_UNIT_SELECTION": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "WAIT_UNIT_SELECTION",
        STATUS_WAITING_USER,
        NEXT_SELECT_UNIT,
        ACTION_SELECT_UNIT,
        ACTION_PREPARE_UNITS,
        ACTION_FINISH_PAGE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    "CROP_REQUIRED": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "CROP_REQUIRED",
        STATUS_WAITING_USER,
        NEXT_SUBMIT_CROP,
        ACTION_SUBMIT_CROP,
        ACTION_SELECT_UNIT,
        ACTION_PREPARE_UNITS,
        ACTION_CANCEL_CURRENT_UNIT,
        ACTION_FINISH_PAGE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    "VERIFYING_CROP": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "VERIFYING_CROP",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "A2_ACTIVE": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "A2_ACTIVE",
        STATUS_RUNNING,
        NEXT_FOLLOW_CHILD,
        ACTION_SELECT_UNIT,
        ACTION_CANCEL_CURRENT_UNIT,
        ACTION_FINISH_PAGE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    "COMPLETE": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "COMPLETE",
        STATUS_COMPLETED,
        NEXT_DONE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    "ERROR": _phase(
        PHASE_NAMESPACE_WORKFLOW,
        "ERROR",
        STATUS_FAILED,
        NEXT_RETRY,
        ACTION_RETRY_CURRENT_STAGE,
        ACTION_UPLOAD_IMAGE,
        ACTION_RESET_SESSION,
    ),
    PHASE_UNKNOWN: _phase(
        PHASE_NAMESPACE_WORKFLOW,
        PHASE_UNKNOWN,
        STATUS_INCONSISTENT,
        NEXT_RETRY,
    ),
}
WORKFLOW_PHASE_CONTRACTS: Mapping[str, PhaseContract] = MappingProxyType(
    _WORKFLOW_PHASE_CONTRACTS
)

_CHILD_PHASE_CONTRACTS = {
    "IDLE": _phase(
        PHASE_NAMESPACE_CHILD,
        "IDLE",
        STATUS_IDLE,
        NEXT_UPLOAD_IMAGE,
    ),
    "PROCESSING": _phase(
        PHASE_NAMESPACE_CHILD,
        "PROCESSING",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "WAIT_CHAPTER": _phase(
        PHASE_NAMESPACE_CHILD,
        "WAIT_CHAPTER",
        STATUS_WAITING_USER,
        NEXT_SET_CHAPTER,
        ACTION_SET_CHAPTER,
        ACTION_GLOBAL_SEARCH,
        ACTION_SELECT_QUESTION,
        ACTION_EXPLAIN_FAILURE,
        ACTION_CANCEL,
    ),
    "WAIT_QUESTION_CHOICE": _phase(
        PHASE_NAMESPACE_CHILD,
        "WAIT_QUESTION_CHOICE",
        STATUS_WAITING_USER,
        NEXT_SELECT_QUESTION,
        ACTION_SELECT_QUESTION,
        ACTION_EXPLAIN_FAILURE,
        ACTION_CANCEL,
    ),
    "WAIT_CANDIDATE_CHOICE": _phase(
        PHASE_NAMESPACE_CHILD,
        "WAIT_CANDIDATE_CHOICE",
        STATUS_WAITING_USER,
        NEXT_SELECT_CANDIDATE,
        ACTION_SET_CHAPTER,
        ACTION_SELECT_QUESTION,
        ACTION_SELECT_CANDIDATE,
        ACTION_REJECT_CANDIDATES,
        ACTION_SHOW_CANDIDATES,
        ACTION_EXPLAIN_FAILURE,
        ACTION_CANCEL,
    ),
    "READY_TO_ROUTE": _phase(
        PHASE_NAMESPACE_CHILD,
        "READY_TO_ROUTE",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "READY_FOR_SEARCH": _phase(
        PHASE_NAMESPACE_CHILD,
        "READY_FOR_SEARCH",
        STATUS_RUNNING,
        NEXT_SYSTEM_CONTINUE,
    ),
    "ANSWERED": _phase(
        PHASE_NAMESPACE_CHILD,
        "ANSWERED",
        STATUS_COMPLETED,
        NEXT_DONE,
        ACTION_SET_CHAPTER,
        ACTION_SELECT_QUESTION,
        ACTION_SELECT_CANDIDATE,
        ACTION_REJECT_CANDIDATES,
        ACTION_SHOW_CANDIDATES,
        ACTION_REPORT_ANSWER_MISMATCH,
        ACTION_RESEND_ANSWER,
        ACTION_EXPLAIN_FAILURE,
        ACTION_CANCEL,
    ),
    "CANCELLED": _phase(
        PHASE_NAMESPACE_CHILD,
        "CANCELLED",
        STATUS_CANCELLED,
        NEXT_DONE,
    ),
    "ERROR": _phase(
        PHASE_NAMESPACE_CHILD,
        "ERROR",
        STATUS_FAILED,
        NEXT_RETRY,
        ACTION_SET_CHAPTER,
        ACTION_SELECT_QUESTION,
        ACTION_SELECT_CANDIDATE,
        ACTION_EXPLAIN_FAILURE,
        ACTION_RETRY_SEARCH,
        ACTION_CANCEL,
    ),
    "NO_MATCH": _phase(
        PHASE_NAMESPACE_CHILD,
        "NO_MATCH",
        STATUS_NO_MATCH,
        NEXT_DONE,
        ACTION_SET_CHAPTER,
        ACTION_SELECT_QUESTION,
        ACTION_EXPLAIN_FAILURE,
        ACTION_CANCEL,
    ),
    PHASE_UNKNOWN: _phase(
        PHASE_NAMESPACE_CHILD,
        PHASE_UNKNOWN,
        STATUS_INCONSISTENT,
        NEXT_RETRY,
    ),
}
CHILD_PHASE_CONTRACTS: Mapping[str, PhaseContract] = MappingProxyType(
    _CHILD_PHASE_CONTRACTS
)

# A unit is considered current only while the parent is actively working on
# that unit.  WAIT_UNIT_SELECTION deliberately has no current unit: the
# previous selection may remain in the persisted parent state as history.
WORKFLOW_CURRENT_UNIT_PHASES = frozenset(
    {
        "CROP_REQUIRED",
        "VERIFYING_CROP",
        "A2_ACTIVE",
    }
)

WORKFLOW_PHASES_BY_ROUTE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        WORKFLOW_ROUTE_NONE: frozenset({"IDLE"}),
        WORKFLOW_ROUTE_PENDING: frozenset({"UNDERSTANDING_PAGE", "ERROR"}),
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
)

_WORKFLOW_STEPS_THROUGH_ROUTE = frozenset(
    {WORKFLOW_STEP_IMAGE_ACCEPTED, WORKFLOW_STEP_ROUTE_DECIDED}
)
_WORKFLOW_STEPS_THROUGH_CATALOG = frozenset(
    {
        *_WORKFLOW_STEPS_THROUGH_ROUTE,
        WORKFLOW_STEP_PAGE_UNDERSTOOD,
        WORKFLOW_STEP_UNIT_CATALOG_READY,
    }
)
_WORKFLOW_STEPS_THROUGH_CHILD = frozenset(
    {
        *_WORKFLOW_STEPS_THROUGH_CATALOG,
        WORKFLOW_STEP_UNIT_SELECTED,
        WORKFLOW_STEP_CHILD_STARTED,
    }
)
WORKFLOW_COMPLETED_STEPS_BY_ROUTE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        WORKFLOW_ROUTE_NONE: frozenset(),
        WORKFLOW_ROUTE_PENDING: frozenset({WORKFLOW_STEP_IMAGE_ACCEPTED}),
        "A1": frozenset(
            {
                WORKFLOW_STEP_IMAGE_ACCEPTED,
                WORKFLOW_STEP_ROUTE_DECIDED,
                WORKFLOW_STEP_COMPLETED,
            }
        ),
        "A2": frozenset(
            {
                WORKFLOW_STEP_IMAGE_ACCEPTED,
                WORKFLOW_STEP_ROUTE_DECIDED,
                WORKFLOW_STEP_CHILD_STARTED,
            }
        ),
        "A3": WORKFLOW_COMPLETED_STEPS,
    }
)
WORKFLOW_COMPLETED_STEPS_BY_PHASE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "IDLE": frozenset(),
        "UNDERSTANDING_PAGE": _WORKFLOW_STEPS_THROUGH_ROUTE,
        "AUTO_GROUNDING_PAGE": _WORKFLOW_STEPS_THROUGH_CATALOG,
        "AUTO_VALIDATING_CROPS": _WORKFLOW_STEPS_THROUGH_CATALOG,
        "WAIT_UNIT_SELECTION": _WORKFLOW_STEPS_THROUGH_CHILD,
        "CROP_REQUIRED": _WORKFLOW_STEPS_THROUGH_CHILD,
        "VERIFYING_CROP": _WORKFLOW_STEPS_THROUGH_CHILD,
        "A2_ACTIVE": _WORKFLOW_STEPS_THROUGH_CHILD,
        "COMPLETE": WORKFLOW_COMPLETED_STEPS,
        "ERROR": _WORKFLOW_STEPS_THROUGH_CHILD,
        PHASE_UNKNOWN: frozenset(),
    }
)

_CHILD_STEPS_THROUGH_ANALYSIS = frozenset(
    {CHILD_STEP_QUESTION_ACCEPTED, CHILD_STEP_QUESTION_ANALYZED}
)
_CHILD_STEPS_THROUGH_ROUTE = frozenset(
    {
        *_CHILD_STEPS_THROUGH_ANALYSIS,
        CHILD_STEP_CHAPTER_RESOLVED,
        CHILD_STEP_ROUTE_SELECTED,
    }
)
_CHILD_STEPS_THROUGH_SEARCH = frozenset(
    {*_CHILD_STEPS_THROUGH_ROUTE, CHILD_STEP_SEARCH_COMPLETED}
)
_CHILD_STEPS_THROUGH_CANDIDATES = frozenset(
    {*_CHILD_STEPS_THROUGH_SEARCH, CHILD_STEP_CANDIDATES_READY}
)
CHILD_COMPLETED_STEPS_BY_PHASE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "IDLE": frozenset(),
        "PROCESSING": frozenset({CHILD_STEP_QUESTION_ACCEPTED}),
        "WAIT_CHAPTER": _CHILD_STEPS_THROUGH_ANALYSIS,
        "WAIT_QUESTION_CHOICE": _CHILD_STEPS_THROUGH_ANALYSIS,
        "READY_TO_ROUTE": frozenset(
            {*_CHILD_STEPS_THROUGH_ANALYSIS, CHILD_STEP_CHAPTER_RESOLVED}
        ),
        "READY_FOR_SEARCH": _CHILD_STEPS_THROUGH_ROUTE,
        "WAIT_CANDIDATE_CHOICE": _CHILD_STEPS_THROUGH_CANDIDATES,
        "ANSWERED": CHILD_COMPLETED_STEPS,
        "CANCELLED": CHILD_COMPLETED_STEPS,
        "ERROR": _CHILD_STEPS_THROUGH_CANDIDATES,
        "NO_MATCH": _CHILD_STEPS_THROUGH_SEARCH,
        PHASE_UNKNOWN: frozenset(),
    }
)


def _validate_workflow_completed_steps(
    route: str,
    phase: str,
    values: tuple[str, ...],
) -> None:
    allowed = WORKFLOW_COMPLETED_STEPS_BY_ROUTE[route] & (
        WORKFLOW_COMPLETED_STEPS_BY_PHASE[phase]
    )
    if any(value not in allowed for value in values):
        raise ValueError("workflow completed step is impossible for route/phase")


def _validate_child_completed_steps(phase: str, values: tuple[str, ...]) -> None:
    if any(value not in CHILD_COMPLETED_STEPS_BY_PHASE[phase] for value in values):
        raise ValueError("child completed step is impossible for phase")


def phase_contract(namespace: str, value: str) -> PhaseContract:
    """Return the reviewed normalized meaning for one raw runtime phase."""

    if namespace == PHASE_NAMESPACE_WORKFLOW:
        contracts = WORKFLOW_PHASE_CONTRACTS
    elif namespace == PHASE_NAMESPACE_CHILD:
        contracts = CHILD_PHASE_CONTRACTS
    else:
        raise ValueError("unknown task phase namespace")
    if type(value) is not str:
        raise ValueError("invalid task phase")
    try:
        return contracts[value]
    except KeyError as exc:
        raise ValueError(f"unknown {namespace} phase: {value}") from exc


def resolve_unit_status(
    *,
    completed: bool = False,
    closed: bool = False,
    active: bool = False,
    prepared: bool = False,
    workflow_finished: bool = False,
) -> str:
    """Project legacy unit flags into one mutually exclusive public status."""

    flags = (completed, closed, active, prepared, workflow_finished)
    if any(type(flag) is not bool for flag in flags):
        raise ValueError("unit status flags must be boolean")
    if completed:
        return UNIT_COMPLETED
    if closed or workflow_finished:
        return UNIT_CLOSED
    if active:
        return UNIT_ACTIVE
    if prepared:
        return UNIT_PREPARED
    return UNIT_AVAILABLE


@dataclass(frozen=True)
class WorkflowStateView:
    exists: bool
    workflow_id: str
    kind: str
    route: str
    task_revision: int
    phase: str
    status: str
    completed_steps: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    next_stage: str = NEXT_DONE

    def __post_init__(self) -> None:
        if type(self.exists) is not bool:
            raise ValueError("workflow exists must be boolean")
        if self.kind not in WORKFLOW_KINDS:
            raise ValueError("invalid workflow kind")
        if self.route not in WORKFLOW_ROUTES:
            raise ValueError("invalid workflow route")
        _validate_revision(self.task_revision)
        contract = phase_contract(PHASE_NAMESPACE_WORKFLOW, self.phase)
        _validate_status(self.status, contract.status)
        _validate_tokens(
            self.completed_steps,
            WORKFLOW_COMPLETED_STEPS,
            "workflow completed step",
        )
        _validate_canonical_order(
            self.completed_steps,
            WORKFLOW_COMPLETED_STEP_ORDER,
            "workflow completed step",
        )
        _validate_workflow_completed_steps(
            self.route,
            self.phase,
            self.completed_steps,
        )
        _validate_tokens(
            self.allowed_actions,
            frozenset(contract.action_candidates),
            "workflow action for phase",
        )
        if self.next_stage not in NEXT_STAGES:
            raise ValueError("invalid next stage")
        if self.status == STATUS_INCONSISTENT:
            if self.next_stage != NEXT_RETRY:
                raise ValueError("inconsistent workflow must require retry")
        elif self.next_stage != contract.next_stage:
            raise ValueError("next stage does not match phase")
        if self.status == STATUS_INCONSISTENT and self.allowed_actions:
            raise ValueError("inconsistent workflow must have no allowed actions")
        if self.exists:
            _validate_optional_id(self.workflow_id, "workflow_id")
            if not self.workflow_id and self.status != STATUS_INCONSISTENT:
                raise ValueError("existing workflow requires workflow_id")
            if self.kind == WORKFLOW_KIND_NONE:
                raise ValueError("existing workflow requires a concrete kind")
            if self.route == WORKFLOW_ROUTE_NONE:
                raise ValueError("existing workflow requires a concrete route")
            if self.status != STATUS_INCONSISTENT:
                _validate_positive_revision(self.task_revision, "workflow task_revision")
        else:
            _validate_optional_id(self.workflow_id, "workflow_id")
            if any(
                (
                    self.workflow_id,
                    self.route != WORKFLOW_ROUTE_NONE,
                    self.task_revision,
                    self.kind != WORKFLOW_KIND_NONE,
                    self.phase != "IDLE",
                    self.status != STATUS_IDLE,
                    self.completed_steps,
                    self.allowed_actions,
                    self.next_stage != NEXT_UPLOAD_IMAGE,
                )
            ):
                raise ValueError("missing workflow must use the empty IDLE projection")

    def to_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "workflow_id": self.workflow_id,
            "kind": self.kind,
            "route": self.route,
            "task_revision": self.task_revision,
            "phase": self.phase,
            "status": self.status,
            "completed_steps": list(self.completed_steps),
            "allowed_actions": list(self.allowed_actions),
            "next_stage": self.next_stage,
        }


@dataclass(frozen=True)
class ChildTaskStateView:
    task_id: str
    kind: str
    unit_id: str
    task_revision: int
    phase: str
    status: str
    completed_steps: tuple[str, ...] = ()
    allowed_actions: tuple[str, ...] = ()
    next_stage: str = NEXT_DONE
    chapter: str = ""
    candidate_count: int = 0
    candidate_generation: str = ""

    def __post_init__(self) -> None:
        _validate_optional_id(self.task_id, "task_id")
        if not self.task_id and self.status != STATUS_INCONSISTENT:
            raise ValueError("child task requires task_id")
        if self.kind != CHILD_KIND_A2_QUESTION:
            raise ValueError("invalid child task kind")
        _validate_optional_id(self.unit_id, "unit_id")
        _validate_revision(self.task_revision)
        if self.status != STATUS_INCONSISTENT:
            _validate_positive_revision(self.task_revision, "child task_revision")
        contract = phase_contract(PHASE_NAMESPACE_CHILD, self.phase)
        _validate_status(self.status, contract.status)
        _validate_tokens(
            self.completed_steps,
            CHILD_COMPLETED_STEPS,
            "child completed step",
        )
        _validate_canonical_order(
            self.completed_steps,
            CHILD_COMPLETED_STEP_ORDER,
            "child completed step",
        )
        _validate_child_completed_steps(self.phase, self.completed_steps)
        _validate_tokens(
            self.allowed_actions,
            frozenset(contract.action_candidates),
            "child action for phase",
        )
        if self.next_stage not in NEXT_STAGES:
            raise ValueError("invalid next stage")
        if self.status == STATUS_INCONSISTENT:
            if self.next_stage != NEXT_RETRY:
                raise ValueError("inconsistent child task must require retry")
        elif self.next_stage != contract.next_stage:
            raise ValueError("next stage does not match phase")
        _validate_text(self.chapter, 64, "chapter")
        if type(self.candidate_count) is not int or not 0 <= self.candidate_count <= 1_000_000:
            raise ValueError("invalid candidate_count")
        _validate_candidate_generation(
            self.task_revision,
            self.candidate_count,
            self.candidate_generation,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "unit_id": self.unit_id,
            "task_revision": self.task_revision,
            "phase": self.phase,
            "status": self.status,
            "completed_steps": list(self.completed_steps),
            "allowed_actions": list(self.allowed_actions),
            "next_stage": self.next_stage,
            "chapter": self.chapter,
            "candidate_count": self.candidate_count,
            "candidate_generation": self.candidate_generation,
        }


@dataclass(frozen=True)
class UnitStateView:
    unit_id: str
    page_index: int
    display_label: str
    status: str

    def __post_init__(self) -> None:
        _validate_id(self.unit_id, "unit_id")
        if type(self.page_index) is not int or not 1 <= self.page_index <= 10_000:
            raise ValueError("invalid page_index")
        _validate_text(self.display_label, 64, "display_label")
        if self.status not in UNIT_STATUSES:
            raise ValueError("invalid unit status")

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "page_index": self.page_index,
            "display_label": self.display_label,
            "status": self.status,
        }


@dataclass(frozen=True)
class ConsistencyView:
    status: str = CONSISTENCY_OK
    codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in CONSISTENCY_STATUSES:
            raise ValueError("invalid consistency status")
        _validate_tokens(self.codes, CONSISTENCY_CODES, "consistency code")
        if len(self.codes) != len(set(self.codes)):
            raise ValueError("consistency codes must be unique")
        if (self.status == CONSISTENCY_OK) != (not self.codes):
            raise ValueError("consistency status and codes disagree")

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "codes": list(self.codes)}


@dataclass(frozen=True)
class TaskStateSnapshotV1:
    workflow: WorkflowStateView
    active_child_task: ChildTaskStateView | None = None
    current_unit: UnitStateView | None = None
    units: tuple[UnitStateView, ...] = ()
    consistency: ConsistencyView = ConsistencyView()
    schema_version: int = TASK_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != TASK_STATE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported task-state schema version")
        if type(self.workflow) is not WorkflowStateView:
            raise ValueError("workflow must be a WorkflowStateView")
        if (
            self.active_child_task is not None
            and type(self.active_child_task) is not ChildTaskStateView
        ):
            raise ValueError("active_child_task must be a ChildTaskStateView")
        if self.current_unit is not None and type(self.current_unit) is not UnitStateView:
            raise ValueError("current_unit must be a UnitStateView")
        if type(self.units) is not tuple:
            raise ValueError("units must be a tuple")
        if any(type(unit) is not UnitStateView for unit in self.units):
            raise ValueError("units must contain only UnitStateView values")
        if type(self.consistency) is not ConsistencyView:
            raise ValueError("consistency must be a ConsistencyView")

        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("unit ids must be unique")
        page_indexes = [unit.page_index for unit in self.units]
        if len(page_indexes) != len(set(page_indexes)):
            raise ValueError("unit page indexes must be unique")
        if page_indexes != sorted(page_indexes):
            raise ValueError("units must use ascending page_index order")

        if not self.workflow.exists and (self.units or self.current_unit is not None):
            raise ValueError("missing workflow cannot expose units")
        if self.workflow.route != "A3" and (
            self.units or self.current_unit is not None
        ):
            raise ValueError("only an A3 workflow may expose units")

        active_count = sum(unit.status == UNIT_ACTIVE for unit in self.units)
        if active_count > 1:
            raise ValueError("units may have at most one active unit")
        if active_count != int(self.current_unit is not None):
            raise ValueError("active unit and current_unit must agree")
        if self.current_unit is not None:
            if (
                not self.workflow.exists
                or self.workflow.route != "A3"
                or self.workflow.phase not in WORKFLOW_CURRENT_UNIT_PHASES
            ):
                raise ValueError("current_unit requires an active workflow phase")
            matching = next(
                (unit for unit in self.units if unit.unit_id == self.current_unit.unit_id),
                None,
            )
            if matching != self.current_unit or self.current_unit.status != UNIT_ACTIVE:
                raise ValueError("current_unit must be the active unit in units")

        if self.workflow.phase == "COMPLETE" and any(
            unit.status not in {UNIT_COMPLETED, UNIT_CLOSED} for unit in self.units
        ):
            raise ValueError("completed workflow cannot expose open units")

        child = self.active_child_task
        if child is not None and child.phase == "IDLE":
            raise ValueError("active child task cannot be IDLE")
        if child is not None and not self.workflow.exists:
            if child.unit_id:
                raise ValueError("standalone child task cannot be bound to a unit")
        if child is not None and self.workflow.exists:
            if self.workflow.phase != "A2_ACTIVE":
                raise ValueError("active child task requires A2_ACTIVE workflow")
            if self.workflow.route not in {"A2", "A3"}:
                raise ValueError("active child task requires an A2 or A3 route")
            if (
                self.workflow.workflow_id
                and child.task_id
                and self.workflow.workflow_id == child.task_id
            ):
                raise ValueError("workflow and child task ids must differ")
            if self.workflow.route == "A2" and child.unit_id:
                raise ValueError("direct A2 child task cannot be bound to a unit")
            if self.workflow.route == "A3":
                if not child.unit_id:
                    raise ValueError("A3 child task requires a unit")
                if (
                    self.current_unit is None
                    or self.current_unit.unit_id != child.unit_id
                ):
                    raise ValueError("A3 child task unit must match current_unit")

        if self.consistency.status == CONSISTENCY_INCONSISTENT:
            if self.workflow.exists and self.workflow.status != STATUS_INCONSISTENT:
                raise ValueError("inconsistent workflow must fail closed")
            if child is not None and (
                child.status != STATUS_INCONSISTENT
                or child.next_stage != NEXT_RETRY
                or child.allowed_actions
            ):
                raise ValueError("inconsistent child task must fail closed")
        else:
            if self.workflow.status == STATUS_INCONSISTENT:
                raise ValueError("consistent snapshot cannot have inconsistent workflow status")
            if child is not None and child.status == STATUS_INCONSISTENT:
                raise ValueError("consistent snapshot cannot have inconsistent child status")
            if self.workflow.phase not in WORKFLOW_PHASES_BY_ROUTE[self.workflow.route]:
                raise ValueError("workflow route and phase disagree")
            if (
                self.workflow.route == "A3"
                and self.workflow.phase in {"CROP_REQUIRED", "VERIFYING_CROP"}
                and self.current_unit is None
            ):
                raise ValueError("active A3 crop phase requires current_unit")
            if (
                self.workflow.exists
                and self.workflow.phase == "A2_ACTIVE"
                and child is None
            ):
                raise ValueError("A2_ACTIVE workflow requires an active child task")
            if (
                self.workflow.route == "A3"
                and self.workflow.phase == "A2_ACTIVE"
                and self.current_unit is None
            ):
                raise ValueError("A3 child workflow requires current_unit")
            # ChildTaskStateView validates the public generation shape.  Keep
            # this guard as a defensive invariant for subclasses/foreign view
            # implementations that might bypass that validation; the builder
            # must pass the private candidate_revision separately.
            if child is not None:
                _validate_candidate_generation(
                    child.task_revision,
                    child.candidate_count,
                    child.candidate_generation,
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workflow": self.workflow.to_dict(),
            "active_child_task": (
                self.active_child_task.to_dict()
                if self.active_child_task is not None
                else None
            ),
            "current_unit": (
                self.current_unit.to_dict() if self.current_unit is not None else None
            ),
            "units": [unit.to_dict() for unit in self.units],
            "consistency": self.consistency.to_dict(),
        }


def empty_task_state_snapshot() -> TaskStateSnapshotV1:
    """Return the only valid projection for a missing or expired session."""

    return TaskStateSnapshotV1(
        workflow=WorkflowStateView(
            exists=False,
            workflow_id="",
            kind=WORKFLOW_KIND_NONE,
            route=WORKFLOW_ROUTE_NONE,
            task_revision=0,
            phase="IDLE",
            status=STATUS_IDLE,
            completed_steps=(),
            allowed_actions=(),
            next_stage=NEXT_UPLOAD_IMAGE,
        )
    )

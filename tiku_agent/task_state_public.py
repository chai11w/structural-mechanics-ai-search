"""Pure public mapping for the authoritative task-state snapshot.

Phase 3.3.1 freezes the shape and placement of the V1 task state without
changing any HTTP or streaming route.  Runtime code is responsible for
producing a :class:`TaskStateSnapshotV1`; this module only turns that typed
value into a detached, JSON-safe public object.

The eventual public payloads will carry the object under the same
``task_state`` key. Existing ``session`` fields and legacy state fields stay
outside this module and remain compatibility data until a later phase.
"""

from __future__ import annotations

from collections.abc import Mapping
import json

from tiku_agent.task_state_contract import (
    TASK_STATE_SCHEMA_VERSION,
    TaskStateSnapshotV1,
    empty_task_state_snapshot,
    is_public_task_state_text,
)


PUBLIC_TASK_STATE_FIELD = "task_state"
PUBLIC_TASK_STATE_SCHEMA_VERSION = TASK_STATE_SCHEMA_VERSION

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "workflow",
        "active_child_task",
        "current_unit",
        "units",
        "consistency",
    }
)
_WORKFLOW_KEYS = frozenset(
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
    }
)
_CHILD_KEYS = frozenset(
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
    }
)
_UNIT_KEYS = frozenset({"unit_id", "page_index", "display_label", "status"})
_CONSISTENCY_KEYS = frozenset({"status", "codes"})


class PublicTaskStateError(ValueError):
    """Raised when a value cannot be exposed as the frozen V1 public shape."""


def public_task_state_snapshot(snapshot: TaskStateSnapshotV1) -> dict[str, object]:
    """Return a detached JSON-safe V1 object for a public response.

    The input is deliberately typed-only.  Callers handling a missing or
    expired session should pass ``empty_task_state_snapshot()``; callers
    handling an unreadable live state must pass the runtime's inconsistent V1
    snapshot.  Accepting arbitrary mappings here would allow a legacy or
    client-controlled state object to bypass the contract.
    """

    value = snapshot
    if type(value) is not TaskStateSnapshotV1:
        raise PublicTaskStateError("task state must be a TaskStateSnapshotV1")
    try:
        # Call the canonical serializer directly.  Combined with the exact
        # type check, this prevents an instance/subclass override from
        # bypassing the frozen V1 dataclass invariants.
        raw = TaskStateSnapshotV1.to_dict(value)
    except Exception as exc:  # pragma: no cover - defensive invariant guard.
        raise PublicTaskStateError("task state serialization failed") from exc
    _validate_shape(raw)
    _validate_public_strings(raw)

    # Round-tripping through the standard JSON encoder both proves that no
    # non-JSON value or NaN can cross the boundary and detaches every nested
    # list/dict from the dataclass's serialization result.
    try:
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        detached = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PublicTaskStateError("task state is not JSON-safe") from exc
    if not isinstance(detached, dict):  # pragma: no cover - encoder invariant.
        raise PublicTaskStateError("task state must serialize to an object")
    return detached


def empty_public_task_state_snapshot() -> dict[str, object]:
    """Return the explicit public projection for a missing/expired session."""

    return public_task_state_snapshot(empty_task_state_snapshot())


def with_public_task_state(
    payload: Mapping[str, object],
    snapshot: TaskStateSnapshotV1,
) -> dict[str, object]:
    """Copy a controlled public payload and attach the canonical V1 object.

    A caller-supplied ``task_state`` value is always replaced; public code
    must never trust a state object that arrived in an upstream payload.
    The input mapping is not mutated. Callers that keep server-only
    finalization metadata (for example, an ``_AgentPayload`` instance) must
    not replace that object with this plain-dict return value. They should
    construct the specialized payload from this result or assign only the
    returned ``task_state`` value onto the original object.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("public payload must be a mapping")
    result = dict(payload)
    result[PUBLIC_TASK_STATE_FIELD] = public_task_state_snapshot(snapshot)
    return result


def public_task_state_json(snapshot: TaskStateSnapshotV1) -> str:
    """Serialize the canonical V1 object for parity assertions and NDJSON."""

    return json.dumps(
        public_task_state_snapshot(snapshot),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _validate_shape(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != _ROOT_KEYS:
        raise PublicTaskStateError("task state has an unsupported root shape")
    if value.get("schema_version") != PUBLIC_TASK_STATE_SCHEMA_VERSION:
        raise PublicTaskStateError("unsupported task state schema version")

    workflow = value.get("workflow")
    if not isinstance(workflow, Mapping) or set(workflow) != _WORKFLOW_KEYS:
        raise PublicTaskStateError("task state has an unsupported workflow shape")

    child = value.get("active_child_task")
    if child is not None and (
        not isinstance(child, Mapping) or set(child) != _CHILD_KEYS
    ):
        raise PublicTaskStateError("task state has an unsupported child shape")

    current_unit = value.get("current_unit")
    if current_unit is not None and (
        not isinstance(current_unit, Mapping) or set(current_unit) != _UNIT_KEYS
    ):
        raise PublicTaskStateError("task state has an unsupported current-unit shape")

    units = value.get("units")
    if not isinstance(units, list):
        raise PublicTaskStateError("task state units must be a list")
    for unit in units:
        if not isinstance(unit, Mapping) or set(unit) != _UNIT_KEYS:
            raise PublicTaskStateError("task state has an unsupported unit shape")

    consistency = value.get("consistency")
    if not isinstance(consistency, Mapping) or set(consistency) != _CONSISTENCY_KEYS:
        raise PublicTaskStateError(
            "task state has an unsupported consistency shape"
        )


def _validate_public_strings(value: object) -> None:
    """Defend the already-safe typed contract against in-process corruption."""

    if isinstance(value, str):
        if not is_public_task_state_text(value):
            raise PublicTaskStateError("task state contains a sensitive value")
        return
    if isinstance(value, Mapping):
        for item in value.values():
            _validate_public_strings(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_public_strings(item)


__all__ = [
    "PUBLIC_TASK_STATE_FIELD",
    "PUBLIC_TASK_STATE_SCHEMA_VERSION",
    "PublicTaskStateError",
    "empty_public_task_state_snapshot",
    "public_task_state_json",
    "public_task_state_snapshot",
    "with_public_task_state",
]

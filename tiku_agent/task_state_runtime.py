"""Lock-adjacent runtime helpers for authoritative task-state snapshots.

The runtime classes own lock acquisition.  This module performs one classified
store read per state, proves the file/capability evidence that cannot belong in
the pure builder, and delegates the final projection to ``task_state_builder``.
It never falls back to legacy public snapshots, trace data, or response history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.state import AgentState
from tiku_agent import task_state_contract as contract
from tiku_agent.task_state_builder import (
    CHILD_OBSERVATION_LIVE,
    CHILD_OBSERVATION_RESPONSE_FROZEN,
    READ_DUPLICATE_UNIT_ID,
    READ_MISSING,
    READ_OK,
    READ_UNKNOWN_PHASE,
    READ_UNREADABLE,
    TOPOLOGY_A3_WRAPPER,
    TOPOLOGY_STANDALONE_A2,
    TaskStateBuildEvidence,
    TaskStateReadSet,
    build_task_state_snapshot_v1,
    child_state_schema_is_safe,
    workflow_state_schema_is_safe,
)


class _StateStore(Protocol):
    def load(self, session_id: str) -> object | None: ...


@dataclass(frozen=True)
class TaskStateEntryCapabilities:
    """Trusted facts supplied by the concrete HTTP/response entry point.

    Upload and reset availability are properties of an entry point, not facts
    that can be inferred from persisted task state.  Defaults therefore expose
    neither action.  Stage 3.3 may opt in only at an entry that really supports
    the corresponding operation.
    """

    trusted_image_event: bool = False
    reset_session_available: bool = False

    def __post_init__(self) -> None:
        if type(self.trusted_image_event) is not bool:
            raise ValueError("trusted_image_event must be boolean")
        if type(self.reset_session_available) is not bool:
            raise ValueError("reset_session_available must be boolean")


def read_workflow_state_once(
    store: _StateStore,
    session_id: str,
    *,
    expected_type: type[object],
) -> tuple[object | None, str]:
    """Read one A3 state and classify only facts visible without guessing."""

    state, status = _read_state_once(store, session_id)
    if status != READ_OK or state is None:
        return state, status
    return _classify_workflow_state(state, expected_type=expected_type)


def read_child_state_once(
    store: _StateStore,
    session_id: str,
) -> tuple[AgentState | None, str]:
    """Read one A2 state and keep missing, unreadable, and unknown distinct."""

    state, status = _read_state_once(store, session_id)
    if status != READ_OK or state is None:
        return None, status
    return _classify_child_state(state)


def classify_frozen_child_state(
    state: AgentState | None,
) -> tuple[AgentState | None, str]:
    """Classify an already-frozen A2 state without touching a lock or store."""

    if state is None:
        return None, READ_MISSING
    return _classify_child_state(state)


def _classify_workflow_state(
    state: object,
    *,
    expected_type: type[object],
) -> tuple[object | None, str]:
    try:
        if type(state) is not expected_type:
            return None, READ_UNREADABLE
        phase = state.phase
        units = state.units
        if not isinstance(phase, str):
            return None, READ_UNREADABLE
        if (
            phase == contract.PHASE_UNKNOWN
            or phase not in contract.WORKFLOW_PHASE_CONTRACTS
        ):
            return None, READ_UNKNOWN_PHASE
        if _has_stable_duplicate_unit_ids(units):
            return None, READ_DUPLICATE_UNIT_ID
        if not workflow_state_schema_is_safe(state):
            return None, READ_UNREADABLE
        state.validate()
    except Exception:  # noqa: BLE001 - malformed loaded state is unreadable.
        return None, READ_UNREADABLE
    return state, READ_OK


def _classify_child_state(
    state: object,
) -> tuple[AgentState | None, str]:
    try:
        if type(state) is not AgentState:
            return None, READ_UNREADABLE
        phase = state.phase
        if not isinstance(phase, str):
            return None, READ_UNREADABLE
        if (
            phase == contract.PHASE_UNKNOWN
            or phase not in contract.CHILD_PHASE_CONTRACTS
        ):
            return None, READ_UNKNOWN_PHASE
        if phase == "IDLE":
            return state, READ_OK
        if not child_state_schema_is_safe(state):
            return None, READ_UNREADABLE
        state.validate()
    except Exception:  # noqa: BLE001 - malformed loaded state is unreadable.
        return None, READ_UNREADABLE
    return state, READ_OK


def build_a3_runtime_snapshot_v1(
    session_id: str,
    *,
    workflow_state: object | None,
    workflow_read_status: str,
    child_state: AgentState | None,
    child_read_status: str,
    workflow_artifacts: SessionArtifacts,
    child_artifacts: SessionArtifacts,
    workflow_retry_supported: bool,
    child_retry_supported: bool,
    capabilities: TaskStateEntryCapabilities | None = None,
) -> contract.TaskStateSnapshotV1:
    """Project one already-locked A3/A2 read-set."""

    entry = _entry_capabilities(capabilities)
    if type(workflow_retry_supported) is not bool:
        raise ValueError("workflow_retry_supported must be boolean")
    if type(child_retry_supported) is not bool:
        raise ValueError("child_retry_supported must be boolean")
    evidence = _build_runtime_evidence(
        session_id,
        workflow_state=(workflow_state if workflow_read_status == READ_OK else None),
        child_state=(child_state if child_read_status == READ_OK else None),
        workflow_artifacts=workflow_artifacts,
        child_artifacts=child_artifacts,
        workflow_retry_supported=workflow_retry_supported,
        child_retry_supported=child_retry_supported,
        capabilities=entry,
    )
    return build_task_state_snapshot_v1(
        TaskStateReadSet(
            session_id=session_id,
            topology=TOPOLOGY_A3_WRAPPER,
            workflow_state=workflow_state,
            child_state=child_state,
            workflow_read_status=workflow_read_status,
            child_read_status=child_read_status,
            child_observation=CHILD_OBSERVATION_LIVE,
        ),
        evidence,
    )


def build_standalone_a2_runtime_snapshot_v1(
    session_id: str,
    *,
    child_state: AgentState | None,
    child_read_status: str,
    child_artifacts: SessionArtifacts,
    child_retry_supported: bool,
    capabilities: TaskStateEntryCapabilities | None = None,
    response_frozen: bool = False,
) -> contract.TaskStateSnapshotV1:
    """Project one standalone A2 read or an already-frozen response state."""

    entry = _entry_capabilities(capabilities)
    if type(child_retry_supported) is not bool:
        raise ValueError("child_retry_supported must be boolean")
    evidence = _build_runtime_evidence(
        session_id,
        workflow_state=None,
        child_state=(child_state if child_read_status == READ_OK else None),
        workflow_artifacts=None,
        child_artifacts=child_artifacts,
        workflow_retry_supported=False,
        child_retry_supported=child_retry_supported,
        capabilities=entry,
    )
    return build_task_state_snapshot_v1(
        TaskStateReadSet(
            session_id=session_id,
            topology=TOPOLOGY_STANDALONE_A2,
            child_state=child_state,
            workflow_read_status=READ_MISSING,
            child_read_status=child_read_status,
            child_observation=(
                CHILD_OBSERVATION_RESPONSE_FROZEN
                if response_frozen
                else CHILD_OBSERVATION_LIVE
            ),
        ),
        evidence,
    )


def _read_state_once(
    store: _StateStore,
    session_id: str,
) -> tuple[object | None, str]:
    try:
        state = store.load(session_id)
    except Exception:  # noqa: BLE001 - public status must not expose store details.
        return None, READ_UNREADABLE
    if state is None:
        return None, READ_MISSING
    return state, READ_OK


def _has_stable_duplicate_unit_ids(units: object) -> bool:
    if type(units) is not list:
        return False
    ids: list[str] = []
    for item in units:
        if not isinstance(item, Mapping):
            return False
        unit_id = item.get("unit_id")
        if not isinstance(unit_id, str):
            return False
        ids.append(unit_id)
    return len(ids) != len(set(ids))


def _entry_capabilities(
    capabilities: TaskStateEntryCapabilities | None,
) -> TaskStateEntryCapabilities:
    if capabilities is None:
        return TaskStateEntryCapabilities()
    if type(capabilities) is not TaskStateEntryCapabilities:
        raise ValueError("capabilities must be TaskStateEntryCapabilities")
    return capabilities


def _build_runtime_evidence(
    session_id: str,
    *,
    workflow_state: object | None,
    child_state: AgentState | None,
    workflow_artifacts: SessionArtifacts | None,
    child_artifacts: SessionArtifacts,
    workflow_retry_supported: bool,
    child_retry_supported: bool,
    capabilities: TaskStateEntryCapabilities,
) -> TaskStateBuildEvidence:
    verified_source = ""
    verified_crops: list[tuple[str, str]] = []
    workflow_retry_available = False

    if workflow_state is not None and workflow_artifacts is not None:
        try:
            raw_source = getattr(workflow_state, "source_page_path", "")
            if isinstance(raw_source, str):
                verified_source = _verified_controlled_file(
                    raw_source,
                    workflow_artifacts.session_dir(session_id) / "uploads",
                    direct_child=True,
                )
        except Exception:  # noqa: BLE001 - malformed state cannot grant evidence.
            verified_source = ""

        try:
            auto_crops = getattr(workflow_state, "auto_crops", None)
            if isinstance(auto_crops, Mapping):
                crop_dir = workflow_artifacts.session_dir(session_id) / "crops"
                for unit_id, record in auto_crops.items():
                    if (
                        not isinstance(unit_id, str)
                        or not unit_id
                        or not isinstance(record, Mapping)
                        or record.get("validation_status") != "auto_ready"
                    ):
                        continue
                    raw_crop = record.get("path", "")
                    if not isinstance(raw_crop, str):
                        continue
                    verified = _verified_controlled_file(
                        raw_crop,
                        crop_dir,
                        direct_child=True,
                    )
                    if verified:
                        verified_crops.append((unit_id, verified))
        except Exception:  # noqa: BLE001 - malformed crop evidence is omitted.
            verified_crops = []

        try:
            workflow_retry_available = bool(
                workflow_retry_supported
                and getattr(workflow_state, "phase", "") == "ERROR"
                and verified_source
            )
        except Exception:  # noqa: BLE001 - malformed state cannot grant retry.
            workflow_retry_available = False

    retryable_child_task: tuple[str, int] | None = None
    try:
        if (
            child_retry_supported
            and child_state is not None
            and child_state.phase == "ERROR"
        ):
            retry_image = child_state.current_image_path
            if _verified_controlled_file(
                retry_image,
                child_artifacts.session_dir(session_id) / "uploads",
                direct_child=True,
            ):
                retryable_child_task = (
                    child_state.current_search_id,
                    child_state.task_revision,
                )
    except Exception:  # noqa: BLE001 - malformed state cannot grant retry.
        retryable_child_task = None

    kwargs: dict[str, Any] = {
        "trusted_image_event": capabilities.trusted_image_event,
        "reset_session_available": capabilities.reset_session_available,
        "verified_source_page_path": verified_source,
        "workflow_retry_available": workflow_retry_available,
        "retryable_child_task": retryable_child_task,
        "verified_controlled_crop_paths": tuple(sorted(verified_crops)),
    }
    try:
        return TaskStateBuildEvidence(**kwargs)
    except ValueError:
        # An invalid child identity is already handled by the pure builder's
        # consistency checks.  It must not turn evidence collection itself into
        # a public exception or manufacture a retry action.
        kwargs["retryable_child_task"] = None
        kwargs["verified_controlled_crop_paths"] = ()
        return TaskStateBuildEvidence(**kwargs)


def _verified_controlled_file(
    raw_path: str,
    controlled_root: Path,
    *,
    direct_child: bool,
) -> str:
    if not raw_path:
        return ""
    try:
        target = Path(raw_path).resolve(strict=True)
        root = controlled_root.resolve()
        if not target.is_file():
            return ""
        if direct_child:
            if target.parent != root:
                return ""
        elif root not in target.parents:
            return ""
        with target.open("rb") as stream:
            stream.read(1)
    except Exception:  # noqa: BLE001 - a probe failure only removes capability evidence.
        return ""
    return raw_path

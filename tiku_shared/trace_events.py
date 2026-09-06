"""Privacy-bounded structured trace events with fail-open local persistence."""

from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import secrets
import sqlite3
import stat
from threading import Condition, Lock, RLock, Thread, current_thread
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from uuid import uuid4
from weakref import WeakMethod

from tiku_shared.trace_context import current_request_id, current_trace_id, is_valid_trace_id


TRACE_EVENT_SCHEMA_VERSION = 1
TRACE_CLEANUP_SCHEMA_VERSION = 2
TRACE_STORE_SCHEMA_VERSION = 1
TRACE_ABSENT_STORE_ID = "absent"
TRACE_MAINTENANCE_LOCK_FILENAME = ".checkpoint_retention.lock"
DEFAULT_TRACE_EVENT_QUEUE_CAPACITY = 1024
DEFAULT_TRACE_EVENT_SQLITE_TIMEOUT_SECONDS = 0.25
TRACE_EVENT_HEALTH_COUNTER_MAX = 2_147_483_647
MAX_TRACE_EVENT_ROWS = TRACE_EVENT_HEALTH_COUNTER_MAX

TRACE_EVENT_TYPES = frozenset(
    {
        "request_received",
        "route_decided",
        "stage_started",
        "stage_finished",
        "model_call_started",
        "model_call_finished",
        "tool_finished",
        "cost_run_written",
        "public_response_finalized",
        "feedback_recorded",
        "request_failed",
    }
)
TERMINAL_EVENT_TYPES = frozenset({"public_response_finalized", "request_failed"})
TRACE_EVENT_OUTCOMES = frozenset(
    {
        "started",
        "success",
        "waiting",
        "candidates",
        "answered",
        "no_match",
        "needs_input",
        "partial",
        "cancelled",
        "rejected",
        "skipped",
        "error",
    }
)

_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRACE_STORE_ID_RE = re.compile(r"^trs_[0-9a-f]{16}$")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_ENDPOINT_RE = re.compile(r"^/[A-Za-z0-9_/:.-]{0,127}$")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_PROTOCOL_STATUSES = frozenset({"SUCCESS", "NO_MATCH", "NEEDS_INPUT", "PARTIAL", "ERROR"})
_PROTOCOL_LAYERS = frozenset(
    {"login", "quota", "queue", "upload", "network", "session", "tool", "media", "feedback"}
)
_PROTOCOL_ACTIONS = frozenset(
    {
        "",
        "relogin",
        "retry_upload",
        "retry_request",
        "retry_search",
        "change_chapter",
        "new_chat",
        "retry_feedback",
    }
)
_DIMENSION_FIELDS = (
    "request_id",
    "response_id",
    "session_key",
    "identity_key",
    "workflow_search_id",
    "search_id",
    "unit_id",
    "run_id",
    "call_id",
    "provider_request_id",
    "feedback_id",
    "rated_response_id",
)

_ATTRIBUTE_ALLOWLISTS: dict[str, frozenset[str]] = {
    "request_received": frozenset({"method", "endpoint", "response_mode"}),
    "route_decided": frozenset(
        {"route", "question_count", "candidate_count", "unit_count"}
    ),
    "stage_started": frozenset({"operation", "attempt_count"}),
    "stage_finished": frozenset(
        {
            "operation",
            "completed",
            "question_count",
            "candidate_count",
            "unit_count",
            "error_kind",
        }
    ),
    "model_call_started": frozenset(
        {"provider", "model", "call_type", "attempt_count"}
    ),
    "model_call_finished": frozenset(
        {
            "provider",
            "model",
            "call_type",
            "input_tokens",
            "image_tokens",
            "cached_tokens",
            "output_tokens",
            "total_tokens",
            "attempt_count",
            "pricing_status",
            "estimated_cost_micros",
            "error_kind",
        }
    ),
    "tool_finished": frozenset(
        {
            "tool",
            "completed",
            "candidate_count",
            "question_count",
            "error_kind",
            "error_code",
        }
    ),
    "cost_run_written": frozenset(
        {
            "task_kind",
            "call_count",
            "total_tokens",
            "estimated_cost_micros",
            "warning_codes",
        }
    ),
    "public_response_finalized": frozenset(
        {
            "endpoint",
            "response_mode",
            "intent",
            "media_status",
            "image_count",
            "text_length",
            "http_status",
            "route",
            "candidate_count",
            "question_count",
            "unit_count",
        }
    ),
    "feedback_recorded": frozenset({"rating", "feedback_scope"}),
    "request_failed": frozenset(
        {"endpoint", "response_mode", "http_status", "error_kind"}
    ),
}

_COUNT_ATTRIBUTES = frozenset(
    {
        "question_count",
        "candidate_count",
        "unit_count",
        "input_tokens",
        "image_tokens",
        "cached_tokens",
        "output_tokens",
        "total_tokens",
        "attempt_count",
        "call_count",
        "estimated_cost_micros",
        "image_count",
        "text_length",
    }
)
_SYMBOL_ATTRIBUTES = frozenset(
    {
        "response_mode",
        "route",
        "operation",
        "provider",
        "model",
        "call_type",
        "pricing_status",
        "tool",
        "task_kind",
        "intent",
        "media_status",
    }
)


class TraceEventValidationError(ValueError):
    """An event was rejected before any untrusted value reached persistence."""


class DuplicateTerminalEvent(RuntimeError):
    """A trace already has its one authoritative terminal event."""


class TraceEventQueueFull(RuntimeError):
    """The bounded writer queue cannot accept another event without waiting."""


class TraceEventRecorderClosed(RuntimeError):
    """The recorder no longer accepts events."""


class TraceEventCapacityError(RuntimeError):
    """The configured trace row budget cannot admit another event."""


class TraceCleanupDriftError(RuntimeError):
    """The trace cleanup candidates changed after the read-only snapshot."""


class TraceEventMaintenanceError(RuntimeError):
    """A retention maintenance fence is active for this trace database."""


_TRACE_PATH_LOCKS_GUARD = Lock()
_TRACE_PATH_LOCKS: dict[str, RLock] = {}


def _trace_path_key(path: str | Path) -> str:
    return str(_trace_absolute_path(path)).casefold()


def _trace_absolute_path(value: str | Path) -> Path:
    """Normalize a path without resolving links that must be rejected."""

    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _trace_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _trace_reject_linked_path(path: Path, *, stop: Path | None = None) -> None:
    current = _trace_absolute_path(path)
    boundary = _trace_absolute_path(stop) if stop is not None else None
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            details = None
        except OSError as exc:
            raise TraceEventMaintenanceError(
                "trace path metadata is unavailable"
            ) from exc
        if details is not None and (
            current.is_symlink()
            or bool(
                getattr(details, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        ):
            raise TraceEventMaintenanceError("trace path contains a link")
        if boundary is not None and current == boundary:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


def _trace_path_lock(path: str | Path) -> RLock:
    key = _trace_path_key(path)
    with _TRACE_PATH_LOCKS_GUARD:
        return _TRACE_PATH_LOCKS.setdefault(key, RLock())


@contextmanager
def _trace_writer_maintenance_lock(path: str | Path) -> Iterator[None]:
    """Take the retention fence without ever waiting on a user request.

    The retention coordinator owns the same lock file for the duration of an
    apply.  A trace writer therefore either obtains the short-lived lock or
    fails open immediately; it must never hold up the request thread.
    """

    path = _trace_absolute_path(path)
    lock_path = path.parent / TRACE_MAINTENANCE_LOCK_FILENAME
    try:
        _trace_reject_linked_path(lock_path, stop=path.parent)
        if _trace_lexists(lock_path) and not lock_path.is_file():
            raise OSError("unsafe maintenance lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(lock_path, flags, 0o600)
        stream = os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        raise TraceEventMaintenanceError("trace maintenance fence is unavailable") from exc

    locked = False
    try:
        # msvcrt.locking requires a byte at the requested position.  Keeping a
        # one-byte file also makes the lock compatible with the coordinator.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError) as exc:
            raise TraceEventMaintenanceError("trace maintenance fence is active") from exc
        yield
    finally:
        if locked:
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
        try:
            stream.close()
        except OSError:
            pass


def new_event_id() -> str:
    return f"evt_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TraceCleanupCandidate:
    trace_id: str
    event_count: int
    first_occurred_at: str
    max_occurred_at: str
    events_hash: str

    def __post_init__(self) -> None:
        if type(self.trace_id) is not str or not is_valid_trace_id(self.trace_id):
            raise TraceEventValidationError("invalid cleanup trace_id")
        if type(self.event_count) is not int or self.event_count < 1:
            raise TraceEventValidationError("invalid cleanup event_count")
        first = _normalize_timestamp(self.first_occurred_at)
        maximum = _normalize_timestamp(self.max_occurred_at)
        if first > maximum:
            raise TraceEventValidationError("invalid cleanup timestamp order")
        if type(self.events_hash) is not str or not _SHA256_RE.fullmatch(
            self.events_hash
        ):
            raise TraceEventValidationError("invalid cleanup events_hash")
        object.__setattr__(self, "first_occurred_at", first)
        object.__setattr__(self, "max_occurred_at", maximum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "event_count": self.event_count,
            "first_occurred_at": self.first_occurred_at,
            "max_occurred_at": self.max_occurred_at,
            "events_hash": self.events_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TraceCleanupCandidate":
        if not isinstance(payload, Mapping) or set(payload) != {
            "trace_id",
            "event_count",
            "first_occurred_at",
            "max_occurred_at",
            "events_hash",
        }:
            raise TraceEventValidationError("invalid cleanup candidate")
        return cls(
            trace_id=payload["trace_id"],
            event_count=payload["event_count"],
            first_occurred_at=payload["first_occurred_at"],
            max_occurred_at=payload["max_occurred_at"],
            events_hash=payload["events_hash"],
        )


@dataclass(frozen=True)
class TraceCleanupSnapshot:
    cutoff: str
    store_id: str
    candidates: tuple[TraceCleanupCandidate, ...]
    candidate_count: int
    event_count: int
    snapshot_hash: str
    schema_version: int = TRACE_CLEANUP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRACE_CLEANUP_SCHEMA_VERSION
        ):
            raise TraceEventValidationError("unsupported cleanup schema_version")
        cutoff = _normalize_timestamp(self.cutoff)
        if type(self.store_id) is not str or not (
            self.store_id == TRACE_ABSENT_STORE_ID
            or _TRACE_STORE_ID_RE.fullmatch(self.store_id)
        ):
            raise TraceEventValidationError("invalid cleanup store_id")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, TraceCleanupCandidate) for item in self.candidates
        ):
            raise TraceEventValidationError("invalid cleanup candidates")
        if tuple(sorted(self.candidates, key=lambda item: item.trace_id)) != self.candidates:
            raise TraceEventValidationError("cleanup candidates must be ordered")
        trace_ids = [item.trace_id for item in self.candidates]
        if len(trace_ids) != len(set(trace_ids)):
            raise TraceEventValidationError("duplicate cleanup trace_id")
        if type(self.candidate_count) is not int or self.candidate_count != len(
            self.candidates
        ):
            raise TraceEventValidationError("invalid cleanup snapshot candidate_count")
        if self.candidate_count and self.store_id == TRACE_ABSENT_STORE_ID:
            raise TraceEventValidationError("cleanup candidates require a store_id")
        if any(item.max_occurred_at > cutoff for item in self.candidates):
            raise TraceEventValidationError("cleanup candidate exceeds cutoff")
        expected_count = sum(item.event_count for item in self.candidates)
        if type(self.event_count) is not int or self.event_count != expected_count:
            raise TraceEventValidationError("invalid cleanup snapshot event_count")
        if type(self.snapshot_hash) is not str or not _SHA256_RE.fullmatch(
            self.snapshot_hash
        ):
            raise TraceEventValidationError("invalid cleanup snapshot_hash")
        expected_hash = _trace_cleanup_snapshot_hash(
            schema_version=self.schema_version,
            cutoff=cutoff,
            store_id=self.store_id,
            candidates=self.candidates,
            candidate_count=self.candidate_count,
            event_count=self.event_count,
        )
        if self.snapshot_hash != expected_hash:
            raise TraceEventValidationError("invalid cleanup snapshot_hash")
        object.__setattr__(self, "cutoff", cutoff)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cutoff": self.cutoff,
            "store_id": self.store_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "candidate_count": self.candidate_count,
            "event_count": self.event_count,
            "snapshot_hash": self.snapshot_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TraceCleanupSnapshot":
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "cutoff",
            "store_id",
            "candidates",
            "candidate_count",
            "event_count",
            "snapshot_hash",
        }:
            raise TraceEventValidationError("invalid cleanup snapshot")
        raw_candidates = payload["candidates"]
        if not isinstance(raw_candidates, list):
            raise TraceEventValidationError("invalid cleanup candidates")
        return cls(
            schema_version=payload["schema_version"],
            cutoff=payload["cutoff"],
            store_id=payload["store_id"],
            candidates=tuple(
                TraceCleanupCandidate.from_dict(item) for item in raw_candidates
            ),
            candidate_count=payload["candidate_count"],
            event_count=payload["event_count"],
            snapshot_hash=payload["snapshot_hash"],
        )


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    trace_id: str
    event_type: str
    occurred_at: str
    stage: str
    outcome: str
    request_id: str = ""
    response_id: str = ""
    session_key: str = ""
    identity_key: str = ""
    workflow_search_id: str = ""
    search_id: str = ""
    unit_id: str = ""
    run_id: str = ""
    call_id: str = ""
    provider_request_id: str = ""
    feedback_id: str = ""
    rated_response_id: str = ""
    protocol_status: str = ""
    protocol_layer: str = ""
    protocol_code: str = ""
    protocol_retryable: bool | None = None
    protocol_action: str = ""
    duration_ms: int | None = None
    safe_attributes: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = TRACE_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACE_EVENT_SCHEMA_VERSION:
            raise TraceEventValidationError("unsupported schema_version")
        if not _EVENT_ID_RE.fullmatch(str(self.event_id or "")):
            raise TraceEventValidationError("invalid event_id")
        if not is_valid_trace_id(self.trace_id):
            raise TraceEventValidationError("invalid trace_id")
        if self.event_type not in TRACE_EVENT_TYPES:
            raise TraceEventValidationError("unregistered event_type")
        if not _STAGE_RE.fullmatch(str(self.stage or "")):
            raise TraceEventValidationError("invalid stage")
        if self.outcome not in TRACE_EVENT_OUTCOMES:
            raise TraceEventValidationError("unregistered outcome")

        object.__setattr__(self, "occurred_at", _normalize_timestamp(self.occurred_at))
        for name in _DIMENSION_FIELDS:
            object.__setattr__(self, name, _validate_identifier(name, getattr(self, name)))
        protocol = _validate_protocol_fields(
            status=self.protocol_status,
            layer=self.protocol_layer,
            code=self.protocol_code,
            retryable=self.protocol_retryable,
            action=self.protocol_action,
        )
        for name, value in protocol.items():
            object.__setattr__(self, f"protocol_{name}", value)
        if self.duration_ms is not None:
            object.__setattr__(self, "duration_ms", _bounded_int(self.duration_ms, "duration_ms"))
        attributes = _validate_safe_attributes(self.event_type, self.safe_attributes)
        object.__setattr__(self, "safe_attributes", MappingProxyType(attributes))

    @classmethod
    def create(
        cls,
        *,
        trace_id: str,
        event_type: str,
        stage: str,
        outcome: str,
        event_id: str | None = None,
        occurred_at: str | None = None,
        protocol: Mapping[str, Any] | Any | None = None,
        safe_attributes: Mapping[str, Any] | None = None,
        duration_ms: int | None = None,
        **dimensions: Any,
    ) -> "TraceEvent":
        unknown = set(dimensions) - set(_DIMENSION_FIELDS)
        if unknown:
            raise TraceEventValidationError("unregistered identifier field")
        protocol_fields = _protocol_mapping(protocol)
        return cls(
            event_id=event_id or new_event_id(),
            trace_id=trace_id,
            event_type=str(event_type or ""),
            occurred_at=occurred_at or utc_now(),
            stage=str(stage or ""),
            outcome=str(outcome or ""),
            duration_ms=duration_ms,
            safe_attributes={} if safe_attributes is None else safe_attributes,
            protocol_status=protocol_fields.get("status", ""),
            protocol_layer=protocol_fields.get("layer", ""),
            protocol_code=protocol_fields.get("code", ""),
            protocol_retryable=protocol_fields.get("retryable"),
            protocol_action=protocol_fields.get("action", ""),
            **{name: dimensions.get(name, "") for name in _DIMENSION_FIELDS},
        )

    @property
    def protocol(self) -> dict[str, Any]:
        if not self.protocol_status:
            return {}
        return {
            "status": self.protocol_status,
            "layer": self.protocol_layer,
            "code": self.protocol_code,
            "retryable": self.protocol_retryable,
            "action": self.protocol_action,
        }

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "stage": self.stage,
            "outcome": self.outcome,
        }
        payload.update(
            {name: getattr(self, name) for name in _DIMENSION_FIELDS if getattr(self, name)}
        )
        if self.protocol:
            payload["protocol"] = self.protocol
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        payload["safe_attributes"] = dict(self.safe_attributes)
        return payload


class SQLiteTraceEventStore:
    """Low-level SQLite sink used by the recorder's single background writer."""

    def __init__(
        self,
        path: str | Path,
        *,
        write_timeout_seconds: float = DEFAULT_TRACE_EVENT_SQLITE_TIMEOUT_SECONDS,
        max_rows: int | None = None,
    ) -> None:
        self.path = _trace_absolute_path(path)
        # Fail closed for existing links or non-regular database paths.  A
        # missing file remains valid and is created only by an explicit write.
        _trace_reject_linked_path(self.path)
        if _trace_lexists(self.path) and not self.path.is_file():
            raise ValueError("trace database path must be a regular file")
        if isinstance(write_timeout_seconds, bool) or not isinstance(
            write_timeout_seconds, (int, float)
        ):
            raise ValueError("write_timeout_seconds must be a number")
        if write_timeout_seconds < 0 or write_timeout_seconds > 5:
            raise ValueError("write_timeout_seconds must be between 0 and 5")
        if max_rows is not None and (
            type(max_rows) is not int or max_rows < 1 or max_rows > MAX_TRACE_EVENT_ROWS
        ):
            raise ValueError(f"max_rows must be between 1 and {MAX_TRACE_EVENT_ROWS}")
        self._write_timeout_seconds = float(write_timeout_seconds)
        self._max_rows = max_rows
        self._lock = Lock()
        self._path_lock = _trace_path_lock(self.path)
        self._pending_flusher: WeakMethod[Any] | None = None

    @property
    def max_rows(self) -> int | None:
        return self._max_rows

    def _attach_recorder(self, recorder: "TraceEventRecorder") -> None:
        self._pending_flusher = WeakMethod(recorder.flush)

    def _flush_pending(self) -> None:
        reference = self._pending_flusher
        callback = reference() if reference is not None else None
        if callback is not None:
            callback()

    def ensure_store_identity(self) -> str:
        """Create or migrate the store and return its persistent identity."""

        _trace_reject_linked_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _trace_reject_linked_path(self.path)
        if _trace_lexists(self.path) and not self.path.is_file():
            raise TraceEventMaintenanceError("trace database path is not a regular file")
        with self._path_lock, self._lock, _trace_writer_maintenance_lock(self.path):
            _trace_reject_linked_path(self.path)
            existed = _trace_lexists(self.path)
            with closing(
                sqlite3.connect(self.path, timeout=self._write_timeout_seconds)
            ) as connection:
                identity = _prepare_trace_store_for_write(
                    connection, allow_create=not existed
                )
                connection.commit()
                return identity

    def store_id(self) -> str:
        """Return the persistent identity without creating or migrating the store."""

        _trace_reject_linked_path(self.path)
        if not _trace_lexists(self.path):
            return TRACE_ABSENT_STORE_ID
        if not self.path.is_file():
            raise TraceCleanupDriftError("trace database path is not a regular file")
        with self._path_lock, self._lock:
            try:
                with closing(
                    _open_readonly_sqlite(
                        self.path, timeout=self._write_timeout_seconds
                    )
                ) as connection:
                    if not _verify_trace_events_readable(connection):
                        raise TraceCleanupDriftError("trace store schema is unavailable")
                    return _trace_store_identity_from_connection(connection)
            except (OSError, sqlite3.Error) as exc:
                raise TraceCleanupDriftError("trace store identity is unavailable") from exc

    def write(self, event: TraceEvent) -> None:
        if not isinstance(event, TraceEvent):
            raise TypeError("event must be a TraceEvent")
        _trace_reject_linked_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _trace_reject_linked_path(self.path)
        if _trace_lexists(self.path) and not self.path.is_file():
            raise TraceEventMaintenanceError("trace database path is not a regular file")
        with self._path_lock, self._lock, _trace_writer_maintenance_lock(self.path):
            _trace_reject_linked_path(self.path)
            existed = _trace_lexists(self.path)
            with closing(
                sqlite3.connect(self.path, timeout=self._write_timeout_seconds)
            ) as connection:
                _prepare_trace_store_for_write(
                    connection, allow_create=not existed
                )
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if self._max_rows is not None:
                        current_rows = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM trace_events"
                            ).fetchone()[0]
                        )
                        if current_rows >= self._max_rows:
                            raise TraceEventCapacityError("trace event capacity exhausted")
                    connection.execute(
                        """
                        INSERT INTO trace_events (
                            event_id, schema_version, trace_id, event_type, occurred_at,
                            stage, outcome, request_id, response_id, session_key,
                            identity_key, workflow_search_id, search_id, unit_id, run_id,
                            call_id, provider_request_id, feedback_id, rated_response_id,
                            protocol_status, protocol_layer, protocol_code,
                            protocol_retryable, protocol_action, duration_ms,
                            safe_attributes_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _event_row(event),
                    )
                except sqlite3.IntegrityError as exc:
                    connection.rollback()
                    if event.event_type in TERMINAL_EVENT_TYPES and self._has_terminal(
                        connection, event.trace_id
                    ):
                        raise DuplicateTerminalEvent("trace terminal already recorded") from exc
                    raise
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()

    def capacity_snapshot(self) -> dict[str, Any]:
        """Return bounded row capacity state without creating or changing the database."""

        unavailable = {
            "available": False,
            "configured": self._max_rows is not None,
            "max_rows": self._max_rows,
            "current_rows": None,
            "remaining_rows": None,
            "at_capacity": False,
        }
        current_rows = 0
        try:
            _trace_reject_linked_path(self.path)
        except TraceEventMaintenanceError:
            return unavailable
        if _trace_lexists(self.path):
            if not self.path.is_file():
                return unavailable
            try:
                with self._path_lock, self._lock:
                    with closing(
                        _open_readonly_sqlite(
                            self.path, timeout=self._write_timeout_seconds
                        )
                    ) as connection:
                        if not _verify_trace_events_readable(connection):
                            return unavailable
                        # A complete table without the persistent identity is
                        # not the store governed by this contract.
                        _trace_store_identity_from_connection(connection)
                        current_rows = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM trace_events"
                            ).fetchone()[0]
                        )
            except (OSError, sqlite3.Error, TraceCleanupDriftError, TypeError, ValueError):
                return unavailable
        remaining_rows = (
            None
            if self._max_rows is None
            else max(0, self._max_rows - current_rows)
        )
        return {
            "available": True,
            "configured": self._max_rows is not None,
            "max_rows": self._max_rows,
            "current_rows": min(current_rows, TRACE_EVENT_HEALTH_COUNTER_MAX),
            "remaining_rows": remaining_rows,
            "at_capacity": self._max_rows is not None and current_rows >= self._max_rows,
        }

    def cleanup_candidates(self, *, cutoff: str) -> TraceCleanupSnapshot:
        """Freeze complete trace timelines whose newest event is at or before cutoff."""

        clean_cutoff = _normalize_timestamp(cutoff)
        self._flush_pending()
        _trace_reject_linked_path(self.path)
        if not _trace_lexists(self.path):
            return _build_trace_cleanup_snapshot(
                clean_cutoff, TRACE_ABSENT_STORE_ID, ()
            )
        if not self.path.is_file():
            raise TraceCleanupDriftError("trace database path is not a regular file")
        with self._path_lock, self._lock:
            try:
                with closing(
                    _open_readonly_sqlite(
                        self.path, timeout=self._write_timeout_seconds
                    )
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    store_id = _trace_store_identity_from_connection(connection)
                    return _trace_cleanup_snapshot_from_connection(
                        connection, clean_cutoff, store_id=store_id
                    )
            except (OSError, sqlite3.Error) as exc:
                raise TraceCleanupDriftError("trace store is unavailable") from exc

    def apply_cleanup(self, snapshot: TraceCleanupSnapshot) -> dict[str, Any]:
        """Delete an unchanged candidate set atomically, one complete trace at a time."""

        if not isinstance(snapshot, TraceCleanupSnapshot):
            raise TypeError("snapshot must be a TraceCleanupSnapshot")
        self._flush_pending()
        _trace_reject_linked_path(self.path)
        if not _trace_lexists(self.path):
            current = _build_trace_cleanup_snapshot(
                snapshot.cutoff, TRACE_ABSENT_STORE_ID, ()
            )
            if current != snapshot:
                raise TraceCleanupDriftError("trace store identity changed")
            return _trace_cleanup_result(snapshot, deleted_event_count=0)
        if not self.path.is_file():
            raise TraceCleanupDriftError("trace database path is not a regular file")

        with self._path_lock, self._lock:
            try:
                connection = _open_existing_sqlite(
                    self.path, timeout=self._write_timeout_seconds
                )
            except (OSError, sqlite3.Error) as exc:
                raise TraceCleanupDriftError("trace store is unavailable") from exc
            with closing(connection):
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    actual_store_id = _trace_store_identity_from_connection(connection)
                    if actual_store_id != snapshot.store_id:
                        raise TraceCleanupDriftError("trace store identity changed")
                    deleted_event_count = 0
                    status = "applied"
                    if snapshot.candidates and _cleanup_candidate_traces_absent(
                        connection, snapshot
                    ):
                        status = "already_satisfied"
                    else:
                        current = _trace_cleanup_snapshot_from_connection(
                            connection,
                            snapshot.cutoff,
                            store_id=actual_store_id,
                        )
                        if current != snapshot:
                            raise TraceCleanupDriftError(
                                "trace cleanup candidates changed"
                            )
                        for candidate in snapshot.candidates:
                            cursor = connection.execute(
                                "DELETE FROM trace_events WHERE trace_id = ?",
                                (candidate.trace_id,),
                            )
                            deleted_event_count += int(cursor.rowcount)
                        if deleted_event_count != snapshot.event_count:
                            raise TraceCleanupDriftError(
                                "trace cleanup delete count changed"
                            )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        return _trace_cleanup_result(
            snapshot,
            status=status,
            deleted_event_count=deleted_event_count,
        )

    def events_for_trace(self, trace_id: str, *, limit: int = 1000) -> list[TraceEvent]:
        clean_trace_id = str(trace_id or "").strip()
        if not is_valid_trace_id(clean_trace_id):
            raise TraceEventValidationError("invalid trace_id")
        clean_limit = _bounded_query_limit(limit)
        self._flush_pending()
        _trace_reject_linked_path(self.path)
        if not _trace_lexists(self.path):
            return []
        if not self.path.is_file():
            raise TraceCleanupDriftError("trace database path is not a regular file")
        with self._path_lock, self._lock:
            with closing(
                _open_readonly_sqlite(
                    self.path, timeout=self._write_timeout_seconds
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                if not _verify_trace_events_readable(connection):
                    raise TraceCleanupDriftError("trace store schema is unavailable")
                rows = connection.execute(
                    "SELECT * FROM trace_events WHERE trace_id = ? "
                    "ORDER BY occurred_at ASC, rowid ASC LIMIT ?",
                    (clean_trace_id, clean_limit),
                ).fetchall()
        return [_event_from_row(row) for row in rows]

    query_trace = events_for_trace
    query_by_trace = events_for_trace

    def flush(self) -> None:
        """Writes commit synchronously; retained for lifecycle symmetry."""

    def close(self) -> None:
        """Connections are scoped per operation; retained for lifecycle symmetry."""

    @staticmethod
    def _has_terminal(connection: sqlite3.Connection, trace_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM trace_events WHERE trace_id = ? "
            "AND event_type IN ('public_response_finalized', 'request_failed') LIMIT 1",
            (trace_id,),
        ).fetchone() is not None


class TraceEventRecorder:
    """Validate synchronously, then enqueue persistence without blocking requests."""

    def __init__(
        self,
        store: SQLiteTraceEventStore,
        *,
        queue_capacity: int = DEFAULT_TRACE_EVENT_QUEUE_CAPACITY,
    ) -> None:
        if (
            type(queue_capacity) is not int
            or queue_capacity < 1
            or queue_capacity > 100_000
        ):
            raise ValueError("queue_capacity must be between 1 and 100000")
        self.store = store
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._queue: Queue[TraceEvent] = Queue(maxsize=queue_capacity)
        self._queue_capacity = queue_capacity
        self._worker: Thread | None = None
        self._closed = False
        self._store_closed = False
        self._accepted = 0
        self._completed = 0
        self._written = 0
        self._dropped = 0
        self._write_failures = 0
        self._validation_rejections = 0
        self._duplicate_terminals = 0
        self._last_failure_kind = ""
        self._last_failure_at = ""
        attach = getattr(store, "_attach_recorder", None)
        if callable(attach):
            attach(self)

    def record(self, event: TraceEvent | None = None, **event_fields: Any) -> TraceEvent | None:
        try:
            if event is not None and event_fields:
                raise TraceEventValidationError("event fields cannot accompany an event")
            candidate = event if event is not None else TraceEvent.create(**event_fields)
            if not isinstance(candidate, TraceEvent):
                raise TraceEventValidationError("event must be a TraceEvent")
        except Exception as exc:  # noqa: BLE001 - validation is intentionally fail-open.
            self._record_validation_rejection(type(exc).__name__)
            return None
        try:
            self._enqueue(candidate)
        except Exception as exc:  # noqa: BLE001 - observability must not affect the request.
            self._record_write_failure(type(exc).__name__)
            return None
        return candidate

    def reject_validation(self, error: BaseException | str) -> None:
        kind = error if isinstance(error, str) else type(error).__name__
        self._record_validation_rejection(str(kind or "TraceEventValidationError"))

    def note_duplicate_terminal(self) -> None:
        with self._lock:
            self._duplicate_terminals = _saturated_increment(
                self._duplicate_terminals
            )

    def health(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {
                "written": self._written,
                "dropped": self._dropped,
                "write_failures": self._write_failures,
                "validation_rejections": self._validation_rejections,
                "duplicate_terminals": self._duplicate_terminals,
                "pending": self._accepted - self._completed,
                "queue_capacity": self._queue_capacity,
                "accepting": not self._closed,
                "last_failure_kind": self._last_failure_kind,
                "last_failure_at": self._last_failure_at,
            }
        capacity, capacity_unavailable = _safe_store_capacity_snapshot(self.store)
        reasons = []
        if result["write_failures"]:
            reasons.append("write_failures")
        if result["validation_rejections"]:
            reasons.append("validation_rejections")
        if result["duplicate_terminals"]:
            reasons.append("duplicate_terminals")
        if capacity["at_capacity"]:
            reasons.append("capacity_exhausted")
        if capacity_unavailable:
            reasons.append("capacity_snapshot_unavailable")
        result["status"] = "degraded" if reasons else "ok"
        result["current_reasons"] = reasons
        result["capacity"] = capacity
        return result

    def flush(self) -> None:
        with self._condition:
            if current_thread() is self._worker:
                return
            target = self._accepted
            while self._completed < target:
                self._condition.wait()
        try:
            self.store.flush()
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(type(exc).__name__)

    def close(self) -> None:
        with self._condition:
            if self._store_closed:
                return
            self._closed = True
            target = self._accepted
            while self._completed < target:
                self._condition.wait()
            worker = self._worker
        if worker is not None and worker is not current_thread():
            worker.join()
        try:
            self.store.flush()
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(type(exc).__name__)
        try:
            self.store.close()
        except Exception as exc:  # noqa: BLE001
            self._record_write_failure(type(exc).__name__)
        finally:
            with self._lock:
                self._store_closed = True

    def _enqueue(self, event: TraceEvent) -> None:
        with self._condition:
            if self._closed:
                raise TraceEventRecorderClosed("trace event recorder is closed")
            try:
                self._queue.put_nowait(event)
            except Full as exc:
                raise TraceEventQueueFull("trace event queue is full") from exc
            self._accepted += 1
            if self._worker is None:
                worker = Thread(
                    target=self._drain_queue,
                    name="trace-event-writer",
                    daemon=True,
                )
                self._worker = worker
                try:
                    worker.start()
                except BaseException:
                    self._worker = None
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self._accepted -= 1
                    raise

    def _drain_queue(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except Empty:
                with self._condition:
                    if self._queue.empty():
                        self._worker = None
                        self._condition.notify_all()
                        return
                continue

            try:
                self.store.write(event)
            except DuplicateTerminalEvent:
                self._finish_duplicate_terminal()
            except BaseException as exc:  # noqa: BLE001 - the writer must keep draining.
                self._finish_write_failure(type(exc).__name__)
            else:
                self._finish_write_success()
            finally:
                self._queue.task_done()

    def _finish_write_success(self) -> None:
        with self._condition:
            self._written = _saturated_increment(self._written)
            self._completed += 1
            self._condition.notify_all()

    def _finish_duplicate_terminal(self) -> None:
        with self._condition:
            self._duplicate_terminals = _saturated_increment(
                self._duplicate_terminals
            )
            self._completed += 1
            self._condition.notify_all()

    def _finish_write_failure(self, kind: str) -> None:
        with self._condition:
            self._dropped = _saturated_increment(self._dropped)
            self._write_failures = _saturated_increment(self._write_failures)
            self._last_failure_kind = _safe_failure_kind(kind)
            self._last_failure_at = utc_now()
            self._completed += 1
            self._condition.notify_all()

    def _record_validation_rejection(self, kind: str) -> None:
        with self._lock:
            self._dropped = _saturated_increment(self._dropped)
            self._validation_rejections = _saturated_increment(
                self._validation_rejections
            )
            self._last_failure_kind = _safe_failure_kind(kind)
            self._last_failure_at = utc_now()

    def _record_write_failure(self, kind: str) -> None:
        with self._lock:
            self._dropped = _saturated_increment(self._dropped)
            self._write_failures = _saturated_increment(self._write_failures)
            self._last_failure_kind = _safe_failure_kind(kind)
            self._last_failure_at = utc_now()


@dataclass
class TraceEventSession:
    recorder: TraceEventRecorder | None
    trace_id: str
    _dimensions: dict[str, str] = field(default_factory=dict, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)
    _terminal_attempted: bool = field(default=False, repr=False)

    @property
    def dimensions(self) -> dict[str, str]:
        with self._lock:
            return dict(self._dimensions)

    @property
    def terminal_attempted(self) -> bool:
        with self._lock:
            return self._terminal_attempted

    def bind(self, **dimensions: Any) -> bool:
        try:
            clean = _validate_dimensions(dimensions)
        except Exception as exc:  # noqa: BLE001 - binding diagnostics is fail-open.
            if self.recorder is not None:
                self.recorder.reject_validation(exc)
            return False
        with self._lock:
            self._dimensions.update(clean)
        return True

    def record(self, event_type: str, **event_fields: Any) -> TraceEvent | None:
        if self.recorder is None:
            return None
        if event_type in TERMINAL_EVENT_TYPES:
            with self._lock:
                if self._terminal_attempted:
                    self.recorder.note_duplicate_terminal()
                    return None
                self._terminal_attempted = True
        with self._lock:
            dimensions = dict(self._dimensions)
        explicit_dimensions = {
            name: event_fields.pop(name)
            for name in tuple(event_fields)
            if name in _DIMENSION_FIELDS
        }
        try:
            dimensions.update(_validate_dimensions(explicit_dimensions))
        except Exception as exc:  # noqa: BLE001
            self.recorder.reject_validation(exc)
            return None
        return self.recorder.record(
            trace_id=self.trace_id,
            event_type=event_type,
            **dimensions,
            **event_fields,
        )


_ACTIVE_TRACE_EVENT_SESSION: ContextVar[TraceEventSession | None] = ContextVar(
    "active_trace_event_session", default=None
)


def current_trace_event_session() -> TraceEventSession | None:
    return _ACTIVE_TRACE_EVENT_SESSION.get()


@contextmanager
def trace_event_session_scope(session: TraceEventSession) -> Iterator[TraceEventSession]:
    """Rebind one request-owned session, including its shared terminal guard."""

    if not isinstance(session, TraceEventSession):
        raise TypeError("session must be a TraceEventSession")
    token = _ACTIVE_TRACE_EVENT_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_TRACE_EVENT_SESSION.reset(token)


@contextmanager
def trace_event_scope(
    recorder: TraceEventRecorder | None,
    *,
    trace_id: str = "",
    request_id: str = "",
    **dimensions: Any,
) -> Iterator[TraceEventSession]:
    clean_trace_id = str(trace_id or current_trace_id()).strip()
    if not is_valid_trace_id(clean_trace_id):
        raise TraceEventValidationError("invalid trace_id")
    if request_id or current_request_id():
        dimensions["request_id"] = request_id or current_request_id()
    clean_dimensions = _validate_dimensions(dimensions)
    session = TraceEventSession(recorder, clean_trace_id, clean_dimensions)
    token = _ACTIVE_TRACE_EVENT_SESSION.set(session)
    try:
        yield session
    finally:
        _ACTIVE_TRACE_EVENT_SESSION.reset(token)


def bind_trace_event_dimensions(**dimensions: Any) -> bool:
    session = current_trace_event_session()
    return session.bind(**dimensions) if session is not None else False


def record_trace_event(
    event_type: str,
    *,
    stage: str,
    outcome: str,
    occurred_at: str | None = None,
    protocol: Mapping[str, Any] | Any | None = None,
    duration_ms: int | None = None,
    safe_attributes: Mapping[str, Any] | None = None,
    **dimensions: Any,
) -> TraceEvent | None:
    session = current_trace_event_session()
    if session is None:
        return None
    return session.record(
        event_type,
        stage=stage,
        outcome=outcome,
        occurred_at=occurred_at,
        protocol=protocol,
        duration_ms=duration_ms,
        safe_attributes=safe_attributes,
        **dimensions,
    )


def record_public_terminal(
    *,
    stage: str,
    outcome: str,
    failed: bool = False,
    protocol: Mapping[str, Any] | Any | None = None,
    duration_ms: int | None = None,
    safe_attributes: Mapping[str, Any] | None = None,
    **dimensions: Any,
) -> TraceEvent | None:
    return record_trace_event(
        "request_failed" if failed else "public_response_finalized",
        stage=stage,
        outcome=outcome,
        protocol=protocol,
        duration_ms=duration_ms,
        safe_attributes=safe_attributes,
        **dimensions,
    )


def _validate_dimensions(dimensions: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(dimensions) - set(_DIMENSION_FIELDS)
    if unknown:
        raise TraceEventValidationError("unregistered identifier field")
    return {name: _validate_identifier(name, value) for name, value in dimensions.items()}


def _validate_identifier(name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TraceEventValidationError(f"invalid {name}")
    clean = value.strip()
    if clean and not _OPAQUE_ID_RE.fullmatch(clean):
        raise TraceEventValidationError(f"invalid {name}")
    return clean


def _normalize_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise TraceEventValidationError("invalid occurred_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TraceEventValidationError("invalid occurred_at") from exc
    if parsed.tzinfo is None:
        raise TraceEventValidationError("occurred_at must include timezone")
    return parsed.astimezone(UTC).isoformat()


def _protocol_mapping(protocol: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if protocol is None:
        return {}
    if isinstance(protocol, Mapping):
        payload = dict(protocol)
    else:
        to_dict = getattr(protocol, "to_dict", None)
        if not callable(to_dict):
            raise TraceEventValidationError("invalid protocol")
        payload = to_dict()
        if not isinstance(payload, Mapping):
            raise TraceEventValidationError("invalid protocol")
        payload = dict(payload)
    allowed = {"status", "layer", "code", "retryable", "action", "request_id", "search_id", "schema_version"}
    if set(payload) - allowed:
        raise TraceEventValidationError("unregistered protocol field")
    return {key: payload[key] for key in ("status", "layer", "code", "retryable", "action") if key in payload}


def _validate_protocol_fields(
    *, status: Any, layer: Any, code: Any, retryable: Any, action: Any
) -> dict[str, Any]:
    values = (status, layer, code, retryable, action)
    if all(value in ("", None) for value in values):
        return {"status": "", "layer": "", "code": "", "retryable": None, "action": ""}
    clean_status = _enum_value(status).upper()
    clean_layer = _enum_value(layer).lower()
    clean_code = str(code or "").strip().upper()
    clean_action = _enum_value(action).lower()
    if clean_status not in _PROTOCOL_STATUSES:
        raise TraceEventValidationError("invalid protocol status")
    if clean_layer not in _PROTOCOL_LAYERS:
        raise TraceEventValidationError("invalid protocol layer")
    if not _CODE_RE.fullmatch(clean_code):
        raise TraceEventValidationError("invalid protocol code")
    if type(retryable) is not bool:
        raise TraceEventValidationError("invalid protocol retryable")
    if clean_action not in _PROTOCOL_ACTIONS:
        raise TraceEventValidationError("invalid protocol action")
    return {
        "status": clean_status,
        "layer": clean_layer,
        "code": clean_code,
        "retryable": retryable,
        "action": clean_action,
    }


def _validate_safe_attributes(event_type: str, attributes: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(attributes, Mapping):
        raise TraceEventValidationError("safe_attributes must be a mapping")
    allowed = _ATTRIBUTE_ALLOWLISTS[event_type]
    if set(attributes) - allowed:
        raise TraceEventValidationError("unregistered safe attribute")
    return {key: _validate_safe_attribute(key, value) for key, value in attributes.items()}


def _validate_safe_attribute(name: str, value: Any) -> Any:
    if name in _COUNT_ATTRIBUTES:
        return _bounded_int(value, name)
    if name == "http_status":
        number = _bounded_int(value, name)
        if number < 100 or number > 599:
            raise TraceEventValidationError("invalid http_status")
        return number
    if name == "completed":
        if type(value) is not bool:
            raise TraceEventValidationError("invalid completed")
        return value
    if name == "method":
        clean = str(value or "").strip().upper()
        if clean not in _HTTP_METHODS:
            raise TraceEventValidationError("invalid method")
        return clean
    if name == "endpoint":
        clean = str(value or "").strip()
        if not _ENDPOINT_RE.fullmatch(clean):
            raise TraceEventValidationError("invalid endpoint")
        return clean
    if name == "error_kind":
        clean = str(value or "").strip()
        if not _SYMBOL_RE.fullmatch(clean):
            raise TraceEventValidationError("invalid error_kind")
        return clean
    if name == "error_code":
        clean = str(value or "").strip().upper()
        if not _CODE_RE.fullmatch(clean):
            raise TraceEventValidationError("invalid error_code")
        return clean
    if name == "warning_codes":
        if not isinstance(value, (list, tuple)) or len(value) > 16:
            raise TraceEventValidationError("invalid warning_codes")
        clean_codes = []
        for item in value:
            clean = str(item or "").strip().upper()
            if not _CODE_RE.fullmatch(clean):
                raise TraceEventValidationError("invalid warning_codes")
            clean_codes.append(clean)
        return clean_codes
    if name == "rating":
        clean = str(value or "").strip().lower()
        if clean not in {"positive", "negative"}:
            raise TraceEventValidationError("invalid rating")
        return clean
    if name == "feedback_scope":
        clean = str(value or "").strip().lower()
        if clean not in {"page", "question"}:
            raise TraceEventValidationError("invalid feedback_scope")
        return clean
    if name in _SYMBOL_ATTRIBUTES:
        clean = str(value or "").strip()
        if not _SYMBOL_RE.fullmatch(clean):
            raise TraceEventValidationError(f"invalid {name}")
        return clean
    raise TraceEventValidationError("unregistered safe attribute")


def _bounded_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0 or value > 1_000_000_000_000_000:
        raise TraceEventValidationError(f"invalid {name}")
    return value


def _bounded_query_limit(value: Any) -> int:
    if type(value) is not int or value < 1 or value > 10_000:
        raise TraceEventValidationError("invalid query limit")
    return value


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _safe_failure_kind(value: str) -> str:
    clean = str(value or "").strip()
    return clean if _SYMBOL_RE.fullmatch(clean) else "ObservabilityError"


def _saturated_increment(value: int) -> int:
    return min(TRACE_EVENT_HEALTH_COUNTER_MAX, value + 1)


def _safe_store_capacity_snapshot(
    store: Any,
) -> tuple[dict[str, Any], bool]:
    unavailable = {
        "available": False,
        "configured": False,
        "max_rows": None,
        "current_rows": None,
        "remaining_rows": None,
        "at_capacity": False,
    }
    snapshotter = getattr(store, "capacity_snapshot", None)
    if not callable(snapshotter):
        return unavailable, False
    try:
        raw = snapshotter()
        if not isinstance(raw, Mapping) or raw.get("available") is not True:
            raise ValueError("capacity snapshot unavailable")
        configured = raw.get("configured") is True
        maximum = raw.get("max_rows")
        current = raw.get("current_rows")
        remaining = raw.get("remaining_rows")
        at_capacity = raw.get("at_capacity")
        if type(current) is not int or not 0 <= current <= TRACE_EVENT_HEALTH_COUNTER_MAX:
            raise ValueError("invalid capacity current_rows")
        if configured:
            if type(maximum) is not int or not 1 <= maximum <= MAX_TRACE_EVENT_ROWS:
                raise ValueError("invalid capacity max_rows")
            if type(remaining) is not int or not 0 <= remaining <= maximum:
                raise ValueError("invalid capacity remaining_rows")
        elif maximum is not None or remaining is not None:
            raise ValueError("invalid unconfigured capacity")
        if type(at_capacity) is not bool:
            raise ValueError("invalid capacity status")
        return {
            "available": True,
            "configured": configured,
            "max_rows": maximum,
            "current_rows": current,
            "remaining_rows": remaining,
            "at_capacity": at_capacity,
        }, False
    except Exception:  # noqa: BLE001 - health output must stay fail-open and bounded.
        return unavailable, True


def _trace_cleanup_snapshot_hash(
    *,
    schema_version: int,
    cutoff: str,
    store_id: str,
    candidates: tuple[TraceCleanupCandidate, ...],
    candidate_count: int,
    event_count: int,
) -> str:
    payload = {
        "schema_version": schema_version,
        "cutoff": cutoff,
        "store_id": store_id,
        "candidates": [item.to_dict() for item in candidates],
        "candidate_count": candidate_count,
        "event_count": event_count,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_trace_cleanup_snapshot(
    cutoff: str,
    store_id: str,
    candidates: tuple[TraceCleanupCandidate, ...],
) -> TraceCleanupSnapshot:
    clean_cutoff = _normalize_timestamp(cutoff)
    ordered = tuple(sorted(candidates, key=lambda item: item.trace_id))
    candidate_count = len(ordered)
    event_count = sum(item.event_count for item in ordered)
    return TraceCleanupSnapshot(
        cutoff=clean_cutoff,
        store_id=store_id,
        candidates=ordered,
        candidate_count=candidate_count,
        event_count=event_count,
        snapshot_hash=_trace_cleanup_snapshot_hash(
            schema_version=TRACE_CLEANUP_SCHEMA_VERSION,
            cutoff=clean_cutoff,
            store_id=store_id,
            candidates=ordered,
            candidate_count=candidate_count,
            event_count=event_count,
        ),
    )


def _open_readonly_sqlite(
    path: Path,
    *,
    timeout: float = DEFAULT_TRACE_EVENT_SQLITE_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open an existing SQLite file without allowing accidental creation."""

    try:
        _trace_reject_linked_path(path)
        if not _trace_lexists(path) or not path.is_file():
            raise FileNotFoundError(str(path))
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=float(timeout))
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (OSError, sqlite3.Error):
        raise


def _open_existing_sqlite(
    path: Path,
    *,
    timeout: float = DEFAULT_TRACE_EVENT_SQLITE_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open an existing SQLite file read-write, never creating a replacement."""

    _trace_reject_linked_path(path)
    if not _trace_lexists(path) or not path.is_file():
        raise FileNotFoundError(str(path))
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    uri = resolved.as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=float(timeout))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _trace_store_identity_from_connection(connection: sqlite3.Connection) -> str:
    """Read the identity encoded in SQLite's persistent database header."""

    if not _trace_events_table_exists(connection):
        raise TraceCleanupDriftError("trace store schema is unavailable")
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (TypeError, ValueError, sqlite3.Error) as exc:
        raise TraceCleanupDriftError("trace store identity is unavailable") from exc
    # SQLite exposes both pragmas as unsigned 32-bit values.  We reserve the
    # high nibble as a format marker and use the remaining 60 bits for the
    # random identity, while leaving the visible schema as a single table.
    value = (application_id << 31) | user_version
    if value <= 0 or value >> 60 != 0x2:
        raise TraceCleanupDriftError("trace store identity is invalid")
    return f"trs_{value:016x}"


def _new_trace_store_identity(connection: sqlite3.Connection) -> str:
    value = (0x2 << 60) | secrets.randbits(60)
    # Keep both pragma values within SQLite's portable signed range.
    application_id = (value >> 31) & 0x7FFFFFFF
    user_version = value & 0x7FFFFFFF
    if not application_id or not user_version:
        return _new_trace_store_identity(connection)
    connection.execute(f"PRAGMA application_id = {application_id}")
    connection.execute(f"PRAGMA user_version = {user_version}")
    return f"trs_{value:016x}"


def _prepare_trace_store_for_write(
    connection: sqlite3.Connection, *, allow_create: bool = True
) -> str:
    """Create/validate the trace schema and initialize its identity."""

    connection.execute("PRAGMA foreign_keys = ON")
    if not allow_create:
        # Validate an existing database before changing journal mode or
        # creating indexes.  An empty/partial file is evidence of corruption
        # or replacement, not an absent store that may be initialized.
        try:
            valid = _verify_trace_events_readable(connection)
        except sqlite3.Error as exc:
            raise TraceCleanupDriftError("trace store schema is invalid") from exc
        if not valid:
            raise TraceCleanupDriftError("trace store schema is invalid")
    connection.execute("PRAGMA journal_mode = WAL")
    _create_schema(connection)
    if not _verify_trace_events_readable(connection):
        raise TraceCleanupDriftError("trace store schema is invalid")
    try:
        return _trace_store_identity_from_connection(connection)
    except TraceCleanupDriftError:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id != 0 or user_version != 0:
            raise
        return _new_trace_store_identity(connection)


def _verify_trace_events_readable(connection: sqlite3.Connection) -> bool:
    """Return whether a connection exposes the complete Trace V1 schema."""

    if not _trace_events_table_exists(connection):
        return False
    required = {
        "event_id",
        "schema_version",
        "trace_id",
        "event_type",
        "occurred_at",
        "stage",
        "outcome",
        *_DIMENSION_FIELDS,
        "protocol_status",
        "protocol_layer",
        "protocol_code",
        "protocol_retryable",
        "protocol_action",
        "duration_ms",
        "safe_attributes_json",
    }
    try:
        actual = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(trace_events)")
        }
    except sqlite3.Error:
        return False
    return required <= actual


def _trace_events_table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trace_events'"
    ).fetchone() is not None


def _trace_cleanup_snapshot_from_connection(
    connection: sqlite3.Connection,
    cutoff: str,
    *,
    store_id: str,
) -> TraceCleanupSnapshot:
    clean_cutoff = _normalize_timestamp(cutoff)
    if not _verify_trace_events_readable(connection):
        raise TraceCleanupDriftError("trace store schema is unavailable")
    rows = connection.execute(
        """
        WITH eligible AS (
            SELECT trace_id
            FROM trace_events
            GROUP BY trace_id
            HAVING MAX(occurred_at) <= ?
        )
        SELECT events.*
        FROM trace_events AS events
        INNER JOIN eligible ON eligible.trace_id = events.trace_id
        ORDER BY events.trace_id ASC, events.occurred_at ASC, events.event_id ASC
        """,
        (clean_cutoff,),
    )
    candidates: list[TraceCleanupCandidate] = []
    trace_id = ""
    event_count = 0
    first_occurred_at = ""
    max_occurred_at = ""
    digest = hashlib.sha256()

    def finish_candidate() -> None:
        if not trace_id:
            return
        candidates.append(
            TraceCleanupCandidate(
                trace_id=trace_id,
                event_count=event_count,
                first_occurred_at=first_occurred_at,
                max_occurred_at=max_occurred_at,
                events_hash=digest.hexdigest(),
            )
        )

    for row in rows:
        row_trace_id = str(row["trace_id"] or "")
        occurred_at = _normalize_timestamp(str(row["occurred_at"] or ""))
        if occurred_at != str(row["occurred_at"]):
            raise TraceEventValidationError("noncanonical trace occurred_at")
        if row_trace_id != trace_id:
            finish_candidate()
            trace_id = row_trace_id
            event_count = 0
            first_occurred_at = occurred_at
            max_occurred_at = occurred_at
            digest = hashlib.sha256()
        event_count += 1
        max_occurred_at = occurred_at
        encoded = json.dumps(
            {key: row[key] for key in row.keys()},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    finish_candidate()
    return _build_trace_cleanup_snapshot(clean_cutoff, store_id, tuple(candidates))


def _trace_cleanup_result(
    snapshot: TraceCleanupSnapshot,
    *,
    status: str = "applied",
    deleted_event_count: int,
) -> dict[str, Any]:
    satisfied = status in {"applied", "already_satisfied"}
    return {
        "status": status,
        "snapshot_hash": snapshot.snapshot_hash,
        "cutoff": snapshot.cutoff,
        "candidate_count": snapshot.candidate_count,
        "event_count": snapshot.event_count,
        "deleted_trace_count": snapshot.candidate_count if satisfied else 0,
        "deleted_event_count": (
            snapshot.event_count if status == "already_satisfied" else deleted_event_count
        ),
    }


def _cleanup_candidate_traces_absent(
    connection: sqlite3.Connection,
    snapshot: TraceCleanupSnapshot,
) -> bool:
    return all(
        connection.execute(
            "SELECT 1 FROM trace_events WHERE trace_id = ? LIMIT 1",
            (candidate.trace_id,),
        ).fetchone()
        is None
        for candidate in snapshot.candidates
    )


def _event_row(event: TraceEvent) -> tuple[Any, ...]:
    return (
        event.event_id,
        event.schema_version,
        event.trace_id,
        event.event_type,
        event.occurred_at,
        event.stage,
        event.outcome,
        *(getattr(event, name) for name in _DIMENSION_FIELDS),
        event.protocol_status,
        event.protocol_layer,
        event.protocol_code,
        None if event.protocol_retryable is None else int(event.protocol_retryable),
        event.protocol_action,
        event.duration_ms,
        json.dumps(dict(event.safe_attributes), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


def _event_from_row(row: sqlite3.Row) -> TraceEvent:
    return TraceEvent(
        event_id=str(row["event_id"]),
        schema_version=int(row["schema_version"]),
        trace_id=str(row["trace_id"]),
        event_type=str(row["event_type"]),
        occurred_at=str(row["occurred_at"]),
        stage=str(row["stage"]),
        outcome=str(row["outcome"]),
        **{name: str(row[name] or "") for name in _DIMENSION_FIELDS},
        protocol_status=str(row["protocol_status"] or ""),
        protocol_layer=str(row["protocol_layer"] or ""),
        protocol_code=str(row["protocol_code"] or ""),
        protocol_retryable=(
            None if row["protocol_retryable"] is None else bool(row["protocol_retryable"])
        ),
        protocol_action=str(row["protocol_action"] or ""),
        duration_ms=(None if row["duration_ms"] is None else int(row["duration_ms"])),
        safe_attributes=json.loads(str(row["safe_attributes_json"])),
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS trace_events (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            trace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            stage TEXT NOT NULL,
            outcome TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            response_id TEXT NOT NULL DEFAULT '',
            session_key TEXT NOT NULL DEFAULT '',
            identity_key TEXT NOT NULL DEFAULT '',
            workflow_search_id TEXT NOT NULL DEFAULT '',
            search_id TEXT NOT NULL DEFAULT '',
            unit_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            call_id TEXT NOT NULL DEFAULT '',
            provider_request_id TEXT NOT NULL DEFAULT '',
            feedback_id TEXT NOT NULL DEFAULT '',
            rated_response_id TEXT NOT NULL DEFAULT '',
            protocol_status TEXT NOT NULL DEFAULT '',
            protocol_layer TEXT NOT NULL DEFAULT '',
            protocol_code TEXT NOT NULL DEFAULT '',
            protocol_retryable INTEGER,
            protocol_action TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER,
            safe_attributes_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_trace_time "
        "ON trace_events(trace_id, occurred_at, event_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_request_time "
        "ON trace_events(request_id, occurred_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_identity_time "
        "ON trace_events(identity_key, occurred_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_workflow_time "
        "ON trace_events(workflow_search_id, occurred_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_search_time "
        "ON trace_events(search_id, occurred_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_events_run_time "
        "ON trace_events(run_id, occurred_at)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trace_events_one_terminal "
        "ON trace_events(trace_id) WHERE event_type IN "
        "('public_response_finalized', 'request_failed')"
    )

"""Privacy-bounded structured trace events with fail-open local persistence."""

from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sqlite3
from threading import Condition, Lock, Thread, current_thread
from types import MappingProxyType
from typing import Any, Iterator, Mapping
from uuid import uuid4
from weakref import WeakMethod

from tiku_shared.trace_context import current_request_id, current_trace_id, is_valid_trace_id


TRACE_EVENT_SCHEMA_VERSION = 1
DEFAULT_TRACE_EVENT_QUEUE_CAPACITY = 1024
DEFAULT_TRACE_EVENT_SQLITE_TIMEOUT_SECONDS = 0.25

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


def new_event_id() -> str:
    return f"evt_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> None:
        self.path = Path(path)
        if isinstance(write_timeout_seconds, bool) or not isinstance(
            write_timeout_seconds, (int, float)
        ):
            raise ValueError("write_timeout_seconds must be a number")
        if write_timeout_seconds < 0 or write_timeout_seconds > 5:
            raise ValueError("write_timeout_seconds must be between 0 and 5")
        self._write_timeout_seconds = float(write_timeout_seconds)
        self._lock = Lock()
        self._pending_flusher: WeakMethod[Any] | None = None

    def _attach_recorder(self, recorder: "TraceEventRecorder") -> None:
        self._pending_flusher = WeakMethod(recorder.flush)

    def _flush_pending(self) -> None:
        reference = self._pending_flusher
        callback = reference() if reference is not None else None
        if callback is not None:
            callback()

    def write(self, event: TraceEvent) -> None:
        if not isinstance(event, TraceEvent):
            raise TypeError("event must be a TraceEvent")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with closing(
                sqlite3.connect(self.path, timeout=self._write_timeout_seconds)
            ) as connection:
                with connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    _create_schema(connection)
                    try:
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
                        if event.event_type in TERMINAL_EVENT_TYPES and self._has_terminal(
                            connection, event.trace_id
                        ):
                            raise DuplicateTerminalEvent("trace terminal already recorded") from exc
                        raise

    def events_for_trace(self, trace_id: str, *, limit: int = 1000) -> list[TraceEvent]:
        clean_trace_id = str(trace_id or "").strip()
        if not is_valid_trace_id(clean_trace_id):
            raise TraceEventValidationError("invalid trace_id")
        clean_limit = _bounded_query_limit(limit)
        self._flush_pending()
        if not self.path.is_file():
            return []
        with self._lock:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.row_factory = sqlite3.Row
                with connection:
                    _create_schema(connection)
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
            self._duplicate_terminals += 1

    def health(self) -> dict[str, Any]:
        with self._lock:
            degraded = bool(
                self._dropped
                or self._write_failures
                or self._validation_rejections
                or self._duplicate_terminals
            )
            return {
                "status": "degraded" if degraded else "ok",
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
            self._written += 1
            self._completed += 1
            self._condition.notify_all()

    def _finish_duplicate_terminal(self) -> None:
        with self._condition:
            self._duplicate_terminals += 1
            self._completed += 1
            self._condition.notify_all()

    def _finish_write_failure(self, kind: str) -> None:
        with self._condition:
            self._dropped += 1
            self._write_failures += 1
            self._last_failure_kind = _safe_failure_kind(kind)
            self._last_failure_at = utc_now()
            self._completed += 1
            self._condition.notify_all()

    def _record_validation_rejection(self, kind: str) -> None:
        with self._lock:
            self._dropped += 1
            self._validation_rejections += 1
            self._last_failure_kind = _safe_failure_kind(kind)
            self._last_failure_at = utc_now()

    def _record_write_failure(self, kind: str) -> None:
        with self._lock:
            self._dropped += 1
            self._write_failures += 1
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

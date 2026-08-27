"""Authoritative, privacy-bounded records for finalized public responses."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import sqlite3
from threading import Lock
from typing import Callable
from uuid import uuid4


RESPONSE_SCHEMA_VERSION = 1
DEFAULT_RESPONSE_RETENTION_DAYS = 30
MAX_RESPONSE_RETENTION_DAYS = 365

_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
_TRACE_ID_RE = re.compile(r"^trace_[0-9a-f]{32}$")
_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
_SESSION_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_PHASE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_RE = re.compile(
    r"(?:https?://|[A-Za-z]:[\\/]|(?:^|\s)/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)|"
    r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|cookie|secret|"
    r"traceback|raw_model_output|reasoning|prompt)\b)",
    re.IGNORECASE,
)

_STATUSES = frozenset({"SUCCESS", "NO_MATCH", "NEEDS_INPUT", "PARTIAL", "ERROR"})
_LAYERS = frozenset(
    {"login", "quota", "queue", "upload", "network", "session", "tool", "media", "feedback"}
)
_ACTIONS = frozenset(
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
_RESPONSE_MODES = frozenset({"json", "stream"})
_MEDIA_STATUSES = frozenset({"", "complete", "partial", "incomplete", "unavailable"})
_IMAGE_ROUTES = frozenset({"", "A1", "A2", "A3"})


class ResponseStoreError(RuntimeError):
    """Base class for authoritative response-store failures."""


class ResponseFinalizationCancelled(ResponseStoreError):
    """The caller withdrew delivery before the response could be committed."""


class ResponseValidationError(ValueError):
    """A response projection contains an unsafe or malformed value."""


class ResponseConflictError(ResponseStoreError):
    """One trace was already finalized with a different public projection."""


class ResponseOwnershipError(ResponseStoreError):
    """The requested response is absent, expired, or owned by another session."""


def new_response_id() -> str:
    return f"resp_{uuid4().hex}"


def is_valid_response_id(value: object) -> bool:
    return bool(_RESPONSE_ID_RE.fullmatch(str(value or "")))


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResponseProjection:
    """Safe fields captured after media processing and before public delivery."""

    trace_id: str
    identity_key: str
    session_key: str
    request_id: str
    status: str
    layer: str
    code: str
    retryable: bool = False
    action: str = ""
    workflow_search_id: str = ""
    search_id: str = ""
    unit_id: str = ""
    phase: str = "IDLE"
    task_revision: int = 0
    candidate_count: int = 0
    chapter: str = ""
    image_route: str = ""
    intent: str = "public_response"
    response_mode: str = "json"
    media_status: str = ""
    image_count: int = 0
    text_length: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        _validate_projection(self)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ResponseRecord:
    """One committed server-authored response identity and its safe projection."""

    response_id: str
    created_at: str
    expires_at: str
    trace_id: str
    identity_key: str
    session_key: str
    request_id: str
    status: str
    layer: str
    code: str
    retryable: bool = False
    action: str = ""
    workflow_search_id: str = ""
    search_id: str = ""
    unit_id: str = ""
    phase: str = "IDLE"
    task_revision: int = 0
    candidate_count: int = 0
    chapter: str = ""
    image_route: str = ""
    intent: str = "public_response"
    response_mode: str = "json"
    media_status: str = ""
    image_count: int = 0
    text_length: int = 0
    duration_ms: int = 0
    schema_version: int = RESPONSE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not is_valid_response_id(self.response_id):
            raise ResponseValidationError("invalid response_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.expires_at, "expires_at")
        created_at = _parse_timestamp(self.created_at)
        expires_at = _parse_timestamp(self.expires_at)
        if expires_at <= created_at:
            raise ResponseValidationError("expires_at must follow created_at")
        if expires_at - created_at > timedelta(days=MAX_RESPONSE_RETENTION_DAYS):
            raise ResponseValidationError("response retention exceeds maximum")
        if self.schema_version != RESPONSE_SCHEMA_VERSION:
            raise ResponseValidationError("unsupported response schema version")
        _validate_projection(self.projection())

    def projection(self) -> ResponseProjection:
        return ResponseProjection(
            trace_id=self.trace_id,
            identity_key=self.identity_key,
            session_key=self.session_key,
            request_id=self.request_id,
            status=self.status,
            layer=self.layer,
            code=self.code,
            retryable=self.retryable,
            action=self.action,
            workflow_search_id=self.workflow_search_id,
            search_id=self.search_id,
            unit_id=self.unit_id,
            phase=self.phase,
            task_revision=self.task_revision,
            candidate_count=self.candidate_count,
            chapter=self.chapter,
            image_route=self.image_route,
            intent=self.intent,
            response_mode=self.response_mode,
            media_status=self.media_status,
            image_count=self.image_count,
            text_length=self.text_length,
            duration_ms=self.duration_ms,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SQLiteResponseStore:
    """Synchronous local store used as the ownership authority for feedback."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = DEFAULT_RESPONSE_RETENTION_DAYS,
        clock: Callable[[], datetime] = utc_now,
        sqlite_timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.retention_days = _validated_retention_days(retention_days)
        self._clock = clock
        self._sqlite_timeout_seconds = max(0.05, float(sqlite_timeout_seconds))
        self._lock = Lock()

    def finalize(
        self,
        projection: ResponseProjection,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ResponseRecord:
        """Commit one response projection while its delivery remains active."""

        if not isinstance(projection, ResponseProjection):
            raise TypeError("projection must be a ResponseProjection")
        _raise_if_cancelled(cancelled)
        now = self._now()
        record = ResponseRecord(
            response_id=new_response_id(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=self.retention_days)).isoformat(),
            **projection.to_dict(),
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with closing(
                    sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    with connection:
                        _raise_if_cancelled(cancelled)
                        _create_schema(connection)
                        _raise_if_cancelled(cancelled)
                        if cancelled is not None:
                            # Acquire the full write lock before the final
                            # cancellation check. Once held, commit cannot sit
                            # behind another reader after delivery is withdrawn.
                            connection.execute("BEGIN EXCLUSIVE")
                            _raise_if_cancelled(cancelled)
                        existing = connection.execute(
                            "SELECT * FROM public_responses WHERE trace_id = ?",
                            (projection.trace_id,),
                        ).fetchone()
                        if existing is not None:
                            committed = _record_from_row(existing)
                            if committed.projection() == projection:
                                record = committed
                            else:
                                raise ResponseConflictError(
                                    "trace already finalized with a different response projection"
                                )
                        else:
                            try:
                                connection.execute(
                                    """
                                    INSERT INTO public_responses (
                                        response_id, schema_version, created_at, expires_at,
                                        trace_id, identity_key, session_key, request_id,
                                        workflow_search_id, search_id, unit_id,
                                        status, layer, code, retryable, action,
                                        phase, task_revision, candidate_count, chapter,
                                        image_route, intent, response_mode, media_status,
                                        image_count, text_length, duration_ms
                                    ) VALUES (
                                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                                    )
                                    """,
                                    _record_values(record),
                                )
                            except sqlite3.IntegrityError as exc:
                                existing = connection.execute(
                                    "SELECT * FROM public_responses WHERE trace_id = ?",
                                    (projection.trace_id,),
                                ).fetchone()
                                if existing is not None:
                                    committed = _record_from_row(existing)
                                    if committed.projection() == projection:
                                        record = committed
                                    else:
                                        raise ResponseConflictError(
                                            "trace already finalized with a different response projection"
                                        ) from exc
                                else:
                                    raise ResponseConflictError(
                                        "response identity could not be committed"
                                    ) from exc
                        _raise_if_cancelled(cancelled)
        except (
            ResponseConflictError,
            ResponseFinalizationCancelled,
            ResponseValidationError,
        ):
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ResponseStoreError("response database is unavailable") from exc
        return record

    def get(self, response_id: str) -> ResponseRecord | None:
        clean_response_id = _validated_response_id(response_id)
        row = self._query_one(
            "SELECT * FROM public_responses WHERE response_id = ?",
            (clean_response_id,),
        )
        return _record_from_row(row) if row is not None else None

    def get_by_trace(self, trace_id: str) -> ResponseRecord | None:
        clean_trace_id = _validated_trace_id(trace_id)
        row = self._query_one(
            "SELECT * FROM public_responses WHERE trace_id = ?",
            (clean_trace_id,),
        )
        return _record_from_row(row) if row is not None else None

    def discard_unexposed(self, response_id: str, *, trace_id: str) -> bool:
        """Remove a committed row only while its stream result is still private."""

        clean_response_id = _validated_response_id(response_id)
        clean_trace_id = _validated_trace_id(trace_id)
        if not self.path.exists():
            return False
        try:
            with self._lock:
                with closing(
                    sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
                ) as connection:
                    with connection:
                        _create_schema(connection)
                        cursor = connection.execute(
                            """
                            DELETE FROM public_responses
                            WHERE response_id = ? AND trace_id = ?
                            """,
                            (clean_response_id, clean_trace_id),
                        )
                        return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise ResponseStoreError("response database is unavailable") from exc

    def get_owned(
        self,
        response_id: str,
        *,
        identity_key: str,
        session_key: str,
        include_expired: bool = False,
    ) -> ResponseRecord | None:
        """Return only an exact owner match; never reveal why another row failed."""

        clean_response_id = _validated_response_id(response_id)
        clean_identity_key = _validated_identity_key(identity_key)
        clean_session_key = _validated_session_key(session_key)
        row = self._query_one(
            """
            SELECT * FROM public_responses
            WHERE response_id = ? AND identity_key = ? AND session_key = ?
            """,
            (clean_response_id, clean_identity_key, clean_session_key),
        )
        if row is None:
            return None
        record = _record_from_row(row)
        if not include_expired and _parse_timestamp(record.expires_at) <= self._now():
            return None
        return record

    def require_owned(
        self,
        response_id: str,
        *,
        identity_key: str,
        session_key: str,
    ) -> ResponseRecord:
        record = self.get_owned(
            response_id,
            identity_key=identity_key,
            session_key=session_key,
        )
        if record is None:
            raise ResponseOwnershipError(
                "response is unavailable or is not owned by this session"
            )
        return record

    def _query_one(
        self, statement: str, parameters: tuple[object, ...]
    ) -> sqlite3.Row | None:
        if not self.path.exists():
            return None
        try:
            with self._lock:
                with closing(
                    sqlite3.connect(self.path, timeout=self._sqlite_timeout_seconds)
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    _create_schema(connection)
                    return connection.execute(statement, parameters).fetchone()
        except sqlite3.Error as exc:
            raise ResponseStoreError("response database is unavailable") from exc

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ResponseStoreError("response store clock must return an aware datetime")
        return value.astimezone(UTC)


def _validate_projection(projection: ResponseProjection) -> None:
    _validated_trace_id(projection.trace_id)
    _validated_identity_key(projection.identity_key)
    _validated_session_key(projection.session_key)
    if not _REQUEST_ID_RE.fullmatch(str(projection.request_id or "")):
        raise ResponseValidationError("invalid request_id")
    for name in ("workflow_search_id", "search_id", "unit_id"):
        value = str(getattr(projection, name) or "")
        if value and not _OPAQUE_ID_RE.fullmatch(value):
            raise ResponseValidationError(f"invalid {name}")
        if value and _SENSITIVE_RE.search(value):
            raise ResponseValidationError(f"unsafe {name}")
    if projection.status not in _STATUSES:
        raise ResponseValidationError("invalid protocol status")
    if projection.layer not in _LAYERS:
        raise ResponseValidationError("invalid protocol layer")
    if not _CODE_RE.fullmatch(str(projection.code or "")):
        raise ResponseValidationError("invalid protocol code")
    if type(projection.retryable) is not bool:
        raise ResponseValidationError("retryable must be boolean")
    if projection.action not in _ACTIONS:
        raise ResponseValidationError("invalid protocol action")
    if not _PHASE_RE.fullmatch(str(projection.phase or "")):
        raise ResponseValidationError("invalid phase")
    if not _SYMBOL_RE.fullmatch(str(projection.intent or "")):
        raise ResponseValidationError("invalid intent")
    if projection.response_mode not in _RESPONSE_MODES:
        raise ResponseValidationError("invalid response mode")
    if projection.media_status not in _MEDIA_STATUSES:
        raise ResponseValidationError("invalid media status")
    if projection.image_route not in _IMAGE_ROUTES:
        raise ResponseValidationError("invalid image route")
    _validate_safe_text(projection.chapter, "chapter", max_length=80)
    for name, maximum in (
        ("task_revision", 1_000_000_000),
        ("candidate_count", 1_000_000),
        ("image_count", 1_000_000),
        ("text_length", 10_000_000),
        ("duration_ms", 86_400_000),
    ):
        value = getattr(projection, name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ResponseValidationError(f"invalid {name}")


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ResponseFinalizationCancelled("response delivery was cancelled")


def _validate_safe_text(value: object, name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or len(value) > max_length:
        raise ResponseValidationError(f"invalid {name}")
    if value != value.strip() or _CONTROL_RE.search(value) or _SENSITIVE_RE.search(value):
        raise ResponseValidationError(f"unsafe {name}")


def _validated_response_id(value: object) -> str:
    clean = str(value or "").strip()
    if not is_valid_response_id(clean):
        raise ResponseValidationError("invalid response_id")
    return clean


def _validated_trace_id(value: object) -> str:
    clean = str(value or "").strip()
    if not _TRACE_ID_RE.fullmatch(clean):
        raise ResponseValidationError("invalid trace_id")
    return clean


def _validated_identity_key(value: object) -> str:
    clean = str(value or "").strip()
    if not _OPAQUE_ID_RE.fullmatch(clean) or _SENSITIVE_RE.search(clean):
        raise ResponseValidationError("invalid identity_key")
    return clean


def _validated_session_key(value: object) -> str:
    clean = str(value or "").strip()
    if not _SESSION_KEY_RE.fullmatch(clean):
        raise ResponseValidationError("invalid session_key")
    return clean


def _validated_retention_days(value: object) -> int:
    if isinstance(value, bool):
        raise ResponseValidationError("invalid response retention days")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ResponseValidationError("invalid response retention days") from exc
    if not 1 <= days <= MAX_RESPONSE_RETENTION_DAYS:
        raise ResponseValidationError("invalid response retention days")
    return days


def _validate_timestamp(value: object, name: str) -> None:
    try:
        _parse_timestamp(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise ResponseValidationError(f"invalid {name}") from exc


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _record_values(record: ResponseRecord) -> tuple[object, ...]:
    return (
        record.response_id,
        record.schema_version,
        record.created_at,
        record.expires_at,
        record.trace_id,
        record.identity_key,
        record.session_key,
        record.request_id,
        record.workflow_search_id,
        record.search_id,
        record.unit_id,
        record.status,
        record.layer,
        record.code,
        int(record.retryable),
        record.action,
        record.phase,
        record.task_revision,
        record.candidate_count,
        record.chapter,
        record.image_route,
        record.intent,
        record.response_mode,
        record.media_status,
        record.image_count,
        record.text_length,
        record.duration_ms,
    )


def _record_from_row(row: sqlite3.Row) -> ResponseRecord:
    return ResponseRecord(
        response_id=str(row["response_id"]),
        schema_version=int(row["schema_version"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        trace_id=str(row["trace_id"]),
        identity_key=str(row["identity_key"]),
        session_key=str(row["session_key"]),
        request_id=str(row["request_id"]),
        workflow_search_id=str(row["workflow_search_id"]),
        search_id=str(row["search_id"]),
        unit_id=str(row["unit_id"]),
        status=str(row["status"]),
        layer=str(row["layer"]),
        code=str(row["code"]),
        retryable=bool(row["retryable"]),
        action=str(row["action"]),
        phase=str(row["phase"]),
        task_revision=int(row["task_revision"]),
        candidate_count=int(row["candidate_count"]),
        chapter=str(row["chapter"]),
        image_route=str(row["image_route"]),
        intent=str(row["intent"]),
        response_mode=str(row["response_mode"]),
        media_status=str(row["media_status"]),
        image_count=int(row["image_count"]),
        text_length=int(row["text_length"]),
        duration_ms=int(row["duration_ms"]),
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS public_responses (
            response_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            request_id TEXT NOT NULL,
            workflow_search_id TEXT NOT NULL DEFAULT '',
            search_id TEXT NOT NULL DEFAULT '',
            unit_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            layer TEXT NOT NULL,
            code TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT 'IDLE',
            task_revision INTEGER NOT NULL DEFAULT 0,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            chapter TEXT NOT NULL DEFAULT '',
            image_route TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT 'public_response',
            response_mode TEXT NOT NULL DEFAULT 'json',
            media_status TEXT NOT NULL DEFAULT '',
            image_count INTEGER NOT NULL DEFAULT 0,
            text_length INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_public_responses_trace "
        "ON public_responses(trace_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_public_responses_owner_created "
        "ON public_responses(identity_key, session_key, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_public_responses_expiry "
        "ON public_responses(expires_at)"
    )


__all__ = [
    "DEFAULT_RESPONSE_RETENTION_DAYS",
    "MAX_RESPONSE_RETENTION_DAYS",
    "RESPONSE_SCHEMA_VERSION",
    "ResponseConflictError",
    "ResponseFinalizationCancelled",
    "ResponseOwnershipError",
    "ResponseProjection",
    "ResponseRecord",
    "ResponseStoreError",
    "ResponseValidationError",
    "SQLiteResponseStore",
    "is_valid_response_id",
    "new_response_id",
]

"""Shared result protocol for one user-visible request chain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any
from uuid import uuid4


REQUEST_PROTOCOL_SCHEMA_VERSION = 1
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{7,127}$")
_V1_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "layer",
        "code",
        "retryable",
        "action",
        "request_id",
        "search_id",
    }
)
_LEGACY_FIELDS = frozenset(
    {
        "status",
        "outcome",
        "layer",
        "code",
        "retryable",
        "action",
        "recovery_action",
        "request_id",
        "search_id",
        "search_key",
    }
)
_LEGACY_CODE_ALIASES = {
    "LEGACY_SUCCESS": "REQUEST_SUCCEEDED",
    "LEGACY_TOOL_ERROR": "TOOL_FAILED",
    "PROVIDER_FAILED": "TOOL_FAILED",
}


class RequestStatus(str, Enum):
    """The only five semantic outcomes exposed across the request chain."""

    SUCCESS = "SUCCESS"
    NO_MATCH = "NO_MATCH"
    NEEDS_INPUT = "NEEDS_INPUT"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"

    # Read compatibility for results emitted before schema v1.
    TOOL_ERROR = "ERROR"


class RequestLayer(str, Enum):
    LOGIN = "login"
    QUOTA = "quota"
    QUEUE = "queue"
    UPLOAD = "upload"
    NETWORK = "network"
    SESSION = "session"
    TOOL = "tool"
    MEDIA = "media"
    FEEDBACK = "feedback"


class RequestAction(str, Enum):
    NONE = ""
    RELOGIN = "relogin"
    RETRY_UPLOAD = "retry_upload"
    RETRY_REQUEST = "retry_request"
    RETRY_SEARCH = "retry_search"
    CHANGE_CHAPTER = "change_chapter"
    NEW_CHAT = "new_chat"
    RETRY_FEEDBACK = "retry_feedback"


@dataclass(frozen=True)
class ProtocolReason:
    status: RequestStatus
    layer: RequestLayer
    retryable: bool
    action: RequestAction = RequestAction.NONE


PROTOCOL_REASONS: dict[str, ProtocolReason] = {
    "REQUEST_SUCCEEDED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "MESSAGE_INVALID": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "CLARIFICATION_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "LOAD_ROUTE_NEEDS_REVIEW": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "QUESTION_INDEX_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "CANDIDATE_RANK_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "CANDIDATE_LIST_UNAVAILABLE": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.SESSION,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "SELECTION_OUT_OF_RANGE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "NO_MORE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH,
        RequestLayer.SESSION,
        False,
        RequestAction.CHANGE_CHAPTER,
    ),
    "REQUEST_OUT_OF_SCOPE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "ACTION_NOT_ALLOWED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "SERVICE_UNAVAILABLE": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_REQUEST
    ),
    "NO_MATCH": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "CHAPTER_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "PARTIAL_RESULT": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "TOOL_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "AGENT_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "EXTERNAL_LOAD_NOT_FOUND": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "TRIAGE_A1_STOPPED": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "TRIAGE_A3_REQUIRES_REUPLOAD": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "LOGIN_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.LOGIN, False, RequestAction.RELOGIN
    ),
    "INVITE_INVALID": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.LOGIN, False, RequestAction.RELOGIN
    ),
    "LOGIN_EXPIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.LOGIN, False, RequestAction.RELOGIN
    ),
    "GLOBAL_DAILY_QUOTA_EXCEEDED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.QUOTA, False
    ),
    "INVITE_DAILY_QUOTA_EXCEEDED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.QUOTA, False
    ),
    "INVITE_IDENTITY_MISSING": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.QUOTA, False, RequestAction.RELOGIN
    ),
    "QUEUE_FULL": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.QUEUE, True, RequestAction.RETRY_REQUEST
    ),
    "QUEUE_TIMEOUT": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.QUEUE, True, RequestAction.RETRY_REQUEST
    ),
    "UPLOAD_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.UPLOAD, False, RequestAction.RETRY_UPLOAD
    ),
    "UPLOAD_TOO_LARGE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.UPLOAD, False, RequestAction.RETRY_UPLOAD
    ),
    "UPLOAD_UNSUPPORTED_FORMAT": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.UPLOAD, False, RequestAction.RETRY_UPLOAD
    ),
    "UPLOAD_DECODE_FAILED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.UPLOAD, False, RequestAction.RETRY_UPLOAD
    ),
    "UPLOAD_PERSIST_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.UPLOAD, True, RequestAction.RETRY_UPLOAD
    ),
    "NETWORK_UNAVAILABLE": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.NETWORK, True, RequestAction.RETRY_REQUEST
    ),
    "REQUEST_TIMEOUT": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.NETWORK, True, RequestAction.RETRY_REQUEST
    ),
    "SESSION_EXPIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False, RequestAction.NEW_CHAT
    ),
    "SESSION_RESET": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.SESSION, False
    ),
    "STALE_ACTION": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "STALE_CANDIDATE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False, RequestAction.RETRY_UPLOAD
    ),
    "MEDIA_NOT_FOUND": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.MEDIA, True, RequestAction.RETRY_REQUEST
    ),
    "MEDIA_PERSIST_FAILED": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.MEDIA, True, RequestAction.RETRY_REQUEST
    ),
    "FEEDBACK_INVALID": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.FEEDBACK, False
    ),
    "FEEDBACK_TOO_LARGE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.FEEDBACK, False
    ),
    "FEEDBACK_SAVE_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.FEEDBACK, True, RequestAction.RETRY_FEEDBACK
    ),
    # Public tool outcomes used by the deterministic user-output catalog.
    "UNKNOWN_CHAPTER": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "PAGE_NO_SEARCHABLE_UNITS": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "QUESTION_UNITS_PREPARED": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.TOOL, False
    ),
    "COARSE_CANDIDATES_FOUND": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.TOOL, False
    ),
    "GLOBAL_CANDIDATES_FOUND": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.TOOL, False
    ),
    "RERANK_COMPLETED": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.TOOL, False
    ),
    "RERANK_EMPTY_COARSE_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "RERANK_INCOMPLETE_COARSE_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "RERANK_SKIPPED_NO_IMAGE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "MULTI_DETECTION_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "MULTI_CROPS_UNAVAILABLE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "STRUCTURE_CLASSIFICATION_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "STRUCTURE_FILTER_SKIPPED_NO_IMAGE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "STRUCTURE_TYPE_UNCERTAIN": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "NO_COARSE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_RELIABLE_RERANK_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_GLOBAL_COARSE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_GLOBAL_RELIABLE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "ANSWER_FILES_FOUND": ProtocolReason(
        RequestStatus.SUCCESS, RequestLayer.TOOL, False
    ),
    "ANSWER_FILES_NOT_FOUND": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "IMAGE_ANALYSIS_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "MULTI_DETAIL_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "COARSE_SEARCH_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "GLOBAL_SEARCH_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "RERANK_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "ANSWER_LOOKUP_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "BANK_ROUTE_FAILED": ProtocolReason(
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        False,
    ),
}


@dataclass(frozen=True)
class RequestProtocol:
    """Machine-readable metadata shared by API, logs, feedback and admin UI."""

    status: RequestStatus | str
    layer: RequestLayer | str
    code: str
    retryable: bool = False
    action: RequestAction | str = RequestAction.NONE
    request_id: str = ""
    search_id: str = ""
    schema_version: int = REQUEST_PROTOCOL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != REQUEST_PROTOCOL_SCHEMA_VERSION
        ):
            raise ValueError("invalid protocol schema version")
        if type(self.retryable) is not bool:
            raise ValueError("protocol retryable must be boolean")
        object.__setattr__(self, "status", normalize_status(self.status))
        object.__setattr__(self, "layer", RequestLayer(self.layer))
        object.__setattr__(self, "action", RequestAction(self.action))
        clean_code = str(self.code or "").strip().upper()
        if not _CODE_RE.fullmatch(clean_code):
            raise ValueError("protocol code must be stable upper snake case")
        object.__setattr__(self, "code", clean_code)
        for field_name in ("request_id", "search_id"):
            value = str(getattr(self, field_name) or "").strip()
            if value and not _ID_RE.fullmatch(value):
                raise ValueError(f"invalid {field_name}")
            object.__setattr__(self, field_name, value)

    @classmethod
    def from_code(
        cls,
        code: str,
        *,
        request_id: str = "",
        search_id: str = "",
    ) -> "RequestProtocol":
        clean_code = str(code or "").strip().upper()
        try:
            reason = PROTOCOL_REASONS[clean_code]
        except KeyError as exc:
            raise ValueError(f"unregistered protocol code: {clean_code}") from exc
        return cls(
            status=reason.status,
            layer=reason.layer,
            code=clean_code,
            retryable=reason.retryable,
            action=reason.action,
            request_id=request_id,
            search_id=search_id,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RequestProtocol":
        """Read one canonical schema-v1 payload and verify its registered tuple.

        Legacy aliases are intentionally unavailable here.  Trusted callers that
        still own a pre-v1 payload must opt in through :meth:`from_legacy_dict` so
        omitting ``schema_version`` cannot downgrade validation.
        """

        values = _strict_mapping(payload, allowed_fields=_V1_FIELDS)
        if set(values) != _V1_FIELDS:
            raise ValueError("protocol v1 fields are incomplete")
        if (
            type(values["schema_version"]) is not int
            or values["schema_version"] != REQUEST_PROTOCOL_SCHEMA_VERSION
        ):
            raise ValueError("invalid protocol schema version")

        status = _strict_string(values["status"], "status")
        layer = _strict_string(values["layer"], "layer")
        code = _strict_string(values["code"], "code")
        action = _strict_string(values["action"], "action")
        request_id = _strict_string(values["request_id"], "request_id")
        search_id = _strict_string(values["search_id"], "search_id")
        retryable = values["retryable"]
        if type(retryable) is not bool:
            raise ValueError("protocol retryable must be boolean")
        if not _CODE_RE.fullmatch(code):
            raise ValueError("protocol code must be canonical upper snake case")
        if status not in {item.value for item in RequestStatus}:
            raise ValueError("invalid canonical protocol status")
        if layer not in {item.value for item in RequestLayer}:
            raise ValueError("invalid canonical protocol layer")
        if action not in {item.value for item in RequestAction}:
            raise ValueError("invalid canonical protocol action")
        _validate_id(request_id, "request_id")
        _validate_id(search_id, "search_id")

        reason = _registered_reason(code)
        if (
            RequestStatus(status) is not reason.status
            or RequestLayer(layer) is not reason.layer
            or retryable is not reason.retryable
            or RequestAction(action) is not reason.action
        ):
            raise ValueError("protocol fields conflict with registered code")
        return cls.from_code(code, request_id=request_id, search_id=search_id)

    @classmethod
    def from_legacy_dict(cls, payload: Mapping[str, Any]) -> "RequestProtocol":
        """Normalize one trusted pre-v1 payload into a registered v1 protocol.

        Only named legacy fields and aliases are supported.  Unknown codes and
        contradictory metadata remain errors instead of becoming trusted v1
        protocol values.
        """

        values = _strict_mapping(payload, allowed_fields=_LEGACY_FIELDS)
        raw_code = _required_legacy_string(values, "code").strip().upper()
        if not _CODE_RE.fullmatch(raw_code):
            raise ValueError("invalid legacy protocol code")
        code = _LEGACY_CODE_ALIASES.get(raw_code, raw_code)
        reason = _registered_reason(code)

        statuses = _legacy_alias_values(values, "status", "outcome")
        for value in statuses:
            if normalize_status(value) is not reason.status:
                raise ValueError("legacy status conflicts with registered code")

        if "layer" in values:
            layer = _required_legacy_string(values, "layer").strip().lower()
            if RequestLayer(layer) is not reason.layer:
                raise ValueError("legacy layer conflicts with registered code")

        if "retryable" in values:
            retryable = values["retryable"]
            if type(retryable) is not bool:
                raise ValueError("legacy retryable must be boolean")
            if retryable is not reason.retryable:
                raise ValueError("legacy retryable conflicts with registered code")

        actions = _legacy_alias_values(values, "action", "recovery_action")
        for value in actions:
            clean_action = _strict_string(value, "legacy action").strip().lower()
            if RequestAction(clean_action) is not reason.action:
                raise ValueError("legacy action conflicts with registered code")

        request_id = _optional_legacy_id(values, "request_id")
        search_ids = _legacy_alias_values(values, "search_id", "search_key")
        normalized_search_ids = tuple(
            _strict_string(value, "legacy search id").strip() for value in search_ids
        )
        if len(set(normalized_search_ids)) > 1:
            raise ValueError("legacy search ids conflict")
        search_id = normalized_search_ids[0] if normalized_search_ids else ""
        _validate_id(request_id, "request_id")
        _validate_id(search_id, "search_id")
        return cls.from_code(code, request_id=request_id, search_id=search_id)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["layer"] = self.layer.value
        payload["action"] = self.action.value
        return payload


def normalize_status(value: RequestStatus | str) -> RequestStatus:
    clean = value.value if isinstance(value, RequestStatus) else str(value or "").strip().upper()
    if clean == "TOOL_ERROR":
        clean = RequestStatus.ERROR.value
    return RequestStatus(clean)


def _strict_mapping(
    payload: Mapping[str, Any],
    *,
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("protocol payload must be a mapping")
    values = dict(payload)
    if any(type(key) is not str for key in values) or not set(values) <= allowed_fields:
        raise ValueError("protocol payload contains unknown fields")
    return values


def _strict_string(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"protocol {field_name} must be a string")
    return value


def _validate_id(value: str, field_name: str) -> None:
    if value and not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {field_name}")


def _registered_reason(code: str) -> ProtocolReason:
    try:
        return PROTOCOL_REASONS[code]
    except KeyError as exc:
        raise ValueError(f"unregistered protocol code: {code}") from exc


def _required_legacy_string(values: Mapping[str, Any], field_name: str) -> str:
    if field_name not in values:
        raise ValueError(f"legacy protocol requires {field_name}")
    return _strict_string(values[field_name], f"legacy {field_name}")


def _legacy_alias_values(
    values: Mapping[str, Any],
    primary: str,
    alias: str,
) -> tuple[Any, ...]:
    present = tuple(values[name] for name in (primary, alias) if name in values)
    if len(present) == 2:
        if primary == "status":
            if normalize_status(present[0]) is not normalize_status(present[1]):
                raise ValueError("legacy status aliases conflict")
        elif primary == "action":
            first = _strict_string(present[0], "legacy action").strip().lower()
            second = _strict_string(present[1], "legacy recovery action").strip().lower()
            if first != second:
                raise ValueError("legacy action aliases conflict")
    return present


def _optional_legacy_id(values: Mapping[str, Any], field_name: str) -> str:
    if field_name not in values:
        return ""
    return _strict_string(values[field_name], f"legacy {field_name}").strip()


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


def new_search_id() -> str:
    return f"search_{uuid4().hex}"

"""Shared result protocol for one user-visible request chain."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any
from uuid import uuid4


REQUEST_PROTOCOL_SCHEMA_VERSION = 1
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{7,127}$")


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
    "IMAGE_ANALYZED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "SINGLE_QUESTION_DETECTED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "MULTI_QUESTION_DETECTED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "TRIAGE_SINGLE_QUESTION_CONFIRMED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "QUESTION_UNITS_PREPARED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "SCOPE_ANALYSIS_REUSED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "BANK_ROUTE_SELECTED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "STRUCTURE_FILTER_NOT_APPLICABLE": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "STRUCTURE_CLASSIFIED_FROM_TEXT": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "STRUCTURE_CLASSIFIED_FROM_IMAGE": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "COARSE_CANDIDATES_FOUND": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "GLOBAL_CANDIDATES_FOUND": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "RERANK_NOT_REQUIRED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "RERANK_COMPLETED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "CANDIDATE_ACTION_CANCEL": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "CANDIDATE_DELETE_SELECTED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "CANDIDATE_ANSWER_SELECTED": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "ANSWER_FILES_FOUND": ProtocolReason(RequestStatus.SUCCESS, RequestLayer.TOOL, False),
    "MESSAGE_INVALID": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.SESSION, False
    ),
    "CLARIFICATION_REQUIRED": ProtocolReason(
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
    "NO_COARSE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "CHAPTER_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "UNKNOWN_CHAPTER": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "GLOBAL_SEARCH_IMAGE_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False, RequestAction.RETRY_UPLOAD
    ),
    "CANDIDATE_NUMBER_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "CANDIDATE_DELETE_RANK_OUT_OF_RANGE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "CANDIDATE_RANK_OUT_OF_RANGE": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "CANDIDATE_RANK_INVALID": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "LOAD_ROUTE_MIXED_REVIEW_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "LOAD_ROUTE_INPUT_UNUSABLE": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    # Compatibility for results emitted before the route categories were
    # split into separate stable codes.
    "LOAD_ROUTE_NEEDS_REVIEW": ProtocolReason(
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.RETRY_UPLOAD,
    ),
    "PARTIAL_RESULT": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "TOOL_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "TOOL_INPUT_REQUIRED": ProtocolReason(
        RequestStatus.NEEDS_INPUT, RequestLayer.TOOL, False
    ),
    "MULTI_DETECTION_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "STRUCTURE_FILTER_SKIPPED_NO_IMAGE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "STRUCTURE_TYPE_UNCERTAIN": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "RERANK_SKIPPED_NO_IMAGE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, False
    ),
    "BANK_ROUTE_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, False
    ),
    "GLOBAL_SEARCH_UNSUPPORTED_ROUTE": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, False
    ),
    "CANDIDATE_ACTION_INVALID_STATE": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, False
    ),
    "NO_GLOBAL_COARSE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_GLOBAL_RELIABLE_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_CANDIDATES_TO_RERANK": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "ANSWER_FILES_NOT_FOUND": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False
    ),
    "NO_RELIABLE_RERANK_CANDIDATES": ProtocolReason(
        RequestStatus.NO_MATCH, RequestLayer.TOOL, False, RequestAction.CHANGE_CHAPTER
    ),
    "MULTI_CROPS_UNAVAILABLE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "STRUCTURE_CLASSIFICATION_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "GLOBAL_RERANK_INCOMPLETE": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "RERANK_INCOMPLETE_COARSE_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "RERANK_EMPTY_COARSE_FALLBACK": ProtocolReason(
        RequestStatus.PARTIAL, RequestLayer.TOOL, True
    ),
    "IMAGE_ANALYSIS_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "MULTI_DETAIL_INVALID": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "MULTI_DETAIL_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "MULTI_DETECTION_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "COARSE_SEARCH_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "GLOBAL_SEARCH_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "RERANK_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "ANSWER_LOOKUP_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "AGENT_FAILED": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.RETRY_SEARCH
    ),
    "AGENT_FAILED_NO_IMAGE": ProtocolReason(
        RequestStatus.ERROR, RequestLayer.TOOL, True, RequestAction.NEW_CHAT
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
    def from_dict(cls, payload: dict[str, Any]) -> "RequestProtocol":
        """Read schema-v1 and the pre-v1 outcome/recovery field names."""

        return cls(
            status=payload.get("status") or payload.get("outcome") or "ERROR",
            layer=payload.get("layer") or RequestLayer.TOOL,
            code=payload.get("code") or "TOOL_FAILED",
            retryable=bool(payload.get("retryable")),
            action=payload.get("action") or payload.get("recovery_action") or "",
            request_id=payload.get("request_id") or "",
            search_id=payload.get("search_id") or payload.get("search_key") or "",
            schema_version=int(payload.get("schema_version") or REQUEST_PROTOCOL_SCHEMA_VERSION),
        )

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


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


def new_search_id() -> str:
    return f"search_{uuid4().hex}"

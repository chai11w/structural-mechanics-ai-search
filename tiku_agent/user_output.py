"""Deterministic, fail-closed user output contract for the 8790 agent.

This module deliberately accepts structured facts instead of arbitrary user-visible
strings.  It does not call a model and it does not change business state.  Stage 3
only establishes the pure output boundary; production call sites are migrated in
later stages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from enum import Enum
import logging
import re
from types import MappingProxyType
from typing import Any

from tiku_shared.chapter_catalog import CHAPTER_DEFINITIONS, UNSUPPORTED_TOPIC_DEFINITIONS
from tiku_shared.request_protocol import (
    PROTOCOL_REASONS,
    REQUEST_PROTOCOL_SCHEMA_VERSION,
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
    ProtocolReason,
)


USER_OUTPUT_SCHEMA_VERSION = 1
_MAX_OUTPUT_CHARS = 320
_MAX_PROGRESS_SEQUENCE = 1_000_000_000
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{7,127}$")
_PHASE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_STABLE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_KNOWN_PHASES = frozenset(
    {
        "IDLE",
        "PROCESSING",
        "WAIT_CHAPTER",
        "WAIT_QUESTION_CHOICE",
        "WAIT_CANDIDATE_CHOICE",
        "READY_TO_ROUTE",
        "READY_FOR_SEARCH",
        "ANSWERED",
        "CANCELLED",
        "ERROR",
        "NO_MATCH",
        "UNDERSTANDING_PAGE",
        "AUTO_GROUNDING_PAGE",
        "AUTO_VALIDATING_CROPS",
        "WAIT_UNIT_SELECTION",
        "CROP_REQUIRED",
        "VERIFYING_CROP",
        "A2_ACTIVE",
        "COMPLETE",
    }
)
_A2_PHASES = frozenset(
    {
        "IDLE",
        "PROCESSING",
        "WAIT_CHAPTER",
        "WAIT_QUESTION_CHOICE",
        "WAIT_CANDIDATE_CHOICE",
        "READY_TO_ROUTE",
        "READY_FOR_SEARCH",
        "ANSWERED",
        "CANCELLED",
        "ERROR",
        "NO_MATCH",
    }
)
_A3_PHASES = frozenset(
    {
        "IDLE",
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
)
_QUESTION_LABEL_RE = re.compile(
    r"^(?:(?:图片)?第\s*[1-9][0-9]{0,3}\s*(?:题|小题)|"
    r"[1-9][0-9]{0,3}(?:-[1-9][0-9]{0,3})?(?:题|小题)?|"
    r"[一二三四五六七八九十百]+-[1-9][0-9]{0,3})$"
)
_MESSAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[A-Za-z]:[\\/]"),
    re.compile(
        r"(?:^|[\s\"'(])/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|cookie)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{6,}|AKIA[A-Z0-9]{8,})\b"),
    re.compile(
        r"(?:^|[^A-Za-z0-9])(?:token|secret|password|api[_-]?key)[_:= -][A-Za-z0-9_-]{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[?&])token=", re.IGNORECASE),
    re.compile(r"\btraceback\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]{0,80}(?:Error|Exception)\s*:", re.IGNORECASE),
    re.compile(
        r"\b(?:invalid_observation_schema|schema_error|raw_model_output|reasoning|"
        r"route[_ -]?code|reason[_ -]?code|confidence|debug|prompt)\b",
        re.IGNORECASE,
    ),
)
_FORBIDDEN_FACT_NAMES = frozenset(
    {
        "authorization",
        "confidence",
        "cookie",
        "debug",
        "detail",
        "error",
        "error_message",
        "exception",
        "http_detail",
        "message",
        "model_output",
        "password",
        "path",
        "prompt",
        "raw",
        "raw_model_output",
        "reason",
        "reason_code",
        "reasoning",
        "route_code",
        "session_id",
        "stack",
        "text",
        "token",
        "traceback",
        "url",
    }
)
_RETRY_ACTIONS = frozenset(
    {
        "retry_upload",
        "retry_request",
        "retry_search",
        "retry_current_stage",
        "retry_feedback",
    }
)
_PUBLIC_CHAPTER_NAMES = frozenset(
    definition.display_name
    for definition in (*CHAPTER_DEFINITIONS, *UNSUPPORTED_TOPIC_DEFINITIONS)
    if definition.display_name
)
_SUPPORTED_CHAPTER_NAMES = tuple(
    definition.display_name for definition in CHAPTER_DEFINITIONS
)
logger = logging.getLogger(__name__)


class FinalOutputKind(str, Enum):
    RESULT = "result"
    TRANSPORT_ERROR = "transport_error"
    CLIENT_ERROR = "client_error"


class OutputKind(str, Enum):
    RESULT = "result"
    TRANSPORT_ERROR = "transport_error"
    CLIENT_ERROR = "client_error"
    PROGRESS = "progress"


class UserAction(str, Enum):
    UPLOAD_IMAGE = "upload_image"
    RETRY_UPLOAD = "retry_upload"
    RETRY_REQUEST = "retry_request"
    RETRY_SEARCH = "retry_search"
    RETRY_CURRENT_STAGE = "retry_current_stage"
    SELECT_QUESTION = "select_question"
    PREPARE_UNITS = "prepare_units"
    CROP_QUESTION = "crop_question"
    SELECT_CANDIDATE = "select_candidate"
    SHOW_CANDIDATES = "show_candidates"
    CONTINUE_SEARCH = "continue_search"
    CHANGE_CHAPTER = "change_chapter"
    GLOBAL_SEARCH = "global_search"
    CANCEL_CURRENT_QUESTION = "cancel_current_question"
    FINISH_PAGE = "finish_page"
    NEW_CHAT = "new_chat"
    RELOGIN = "relogin"
    RETRY_FEEDBACK = "retry_feedback"
    CONTACT_AUTHOR = "contact_author"
    REJECT_CANDIDATES = "reject_candidates"
    REPORT_ANSWER_MISMATCH = "report_answer_mismatch"
    RESEND_ANSWER = "resend_answer"
    CONTINUE_CURRENT = "continue_current"


_PROTOCOL_ACTION_MAP = MappingProxyType(
    {
        RequestAction.RELOGIN: UserAction.RELOGIN,
        RequestAction.RETRY_UPLOAD: UserAction.RETRY_UPLOAD,
        RequestAction.RETRY_REQUEST: UserAction.RETRY_REQUEST,
        RequestAction.RETRY_SEARCH: UserAction.RETRY_SEARCH,
        RequestAction.CHANGE_CHAPTER: UserAction.CHANGE_CHAPTER,
        RequestAction.NEW_CHAT: UserAction.NEW_CHAT,
        RequestAction.RETRY_FEEDBACK: UserAction.RETRY_FEEDBACK,
    }
)


class OutputContractError(ValueError):
    """A deliberately detail-free contract violation used for safe logging."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class FinalOutputRequestV1:
    schema_version: int
    kind: FinalOutputKind | str
    message_key: str
    protocol: RequestProtocol
    phase: str
    facts: Mapping[str, Any]
    allowed_actions: tuple[UserAction | str, ...]
    notice_keys: tuple[str, ...] = ()
    contact: PublicContactV1 | None = None


@dataclass(frozen=True)
class ProgressOutputRequestV1:
    schema_version: int
    progress_key: str
    request_id: str
    search_id: str
    sequence: int
    facts: Mapping[str, Any]


@dataclass(frozen=True)
class PublicContactV1:
    """Explicit, bounded public contact data; never inferred from an error."""

    label: str
    channel: str
    value: str


_PUBLIC_MESSAGE_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class PublicMessageV1:
    schema_version: int
    kind: OutputKind
    message_key: str
    text: str
    protocol: RequestProtocol | None
    allowed_actions: tuple[UserAction, ...]
    request_id: str
    search_id: str
    sequence: int | None = None
    stage: str | None = None
    contact: PublicContactV1 | None = None
    _factory_token: InitVar[object | None] = None

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _PUBLIC_MESSAGE_FACTORY_TOKEN:
            raise ValueError("public messages must be created by the renderer")
        if (
            type(self.schema_version) is not int
            or self.schema_version != USER_OUTPUT_SCHEMA_VERSION
        ):
            raise ValueError("invalid public message schema")
        if not isinstance(self.kind, OutputKind):
            raise ValueError("invalid public message kind")
        if not isinstance(self.text, str) or not self.text or len(self.text) > _MAX_OUTPUT_CHARS:
            raise ValueError("invalid public message text")
        if _CONTROL_RE.search(self.text) or _contains_sensitive_text(self.text):
            raise ValueError("unsafe public message text")
        if not isinstance(self.message_key, str) or not _MESSAGE_KEY_RE.fullmatch(
            self.message_key
        ):
            raise ValueError("invalid public message key")
        if (
            not isinstance(self.allowed_actions, tuple)
            or len(set(self.allowed_actions)) != len(self.allowed_actions)
            or any(not isinstance(action, UserAction) for action in self.allowed_actions)
        ):
            raise ValueError("invalid public message action")
        request_id_required = self.kind is OutputKind.PROGRESS or (
            self.protocol is not None and self.protocol.code != "SERVICE_UNAVAILABLE"
        )
        _safe_public_id(self.request_id, required=request_id_required)
        _safe_public_id(self.search_id)
        if self.kind is OutputKind.PROGRESS:
            progress_entry = _PROGRESS_CATALOG.get(self.message_key)
            expected_stage = (
                "processing" if self.message_key == "progress.safe" else None
            )
            if progress_entry is not None:
                expected_stage = progress_entry.stage
            if (
                self.protocol is not None
                or self.allowed_actions
                or not isinstance(self.sequence, int)
                or isinstance(self.sequence, bool)
                or not 1 <= self.sequence <= _MAX_PROGRESS_SEQUENCE
                or self.stage != expected_stage
                or self.contact is not None
            ):
                raise ValueError("invalid public progress message")
        else:
            if self.protocol is None or self.sequence is not None or self.stage is not None:
                raise ValueError("invalid public final message")
            try:
                protocol = _validate_protocol(self.protocol)
                _validate_kind_protocol(self.kind, protocol)
            except OutputContractError as exc:
                raise ValueError("invalid public final protocol") from exc
            if (
                self.request_id != self.protocol.request_id
                or self.search_id != self.protocol.search_id
            ):
                raise ValueError("public message protocol ids differ")
            try:
                contact = _normalize_contact(self.contact)
            except OutputContractError as exc:
                raise ValueError("invalid public contact") from exc
            if contact != self.contact:
                raise ValueError("noncanonical public contact")
            contact_action = UserAction.CONTACT_AUTHOR in self.allowed_actions
            if (contact is None) != (not contact_action):
                raise ValueError("public contact action mismatch")
            if contact is not None and (
                self.protocol.status is not RequestStatus.NO_MATCH
                or self.message_key
                not in {
                    "search.candidates.rejected",
                    "search.no_match.chapter",
                    "search.no_match.global",
                }
            ):
                raise ValueError("public contact not allowed")

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical public payload used by JSON and stream wrappers."""

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "message_key": self.message_key,
            "text": self.text,
            "allowed_actions": [item.value for item in self.allowed_actions],
            "request_id": self.request_id,
            "search_id": self.search_id,
        }
        if self.protocol is not None:
            protocol_payload = self.protocol.to_dict()
            protocol_payload.pop("schema_version", None)
            protocol_payload.pop("request_id", None)
            protocol_payload.pop("search_id", None)
            payload.update(protocol_payload)
        if self.sequence is not None:
            payload["sequence"] = self.sequence
        if self.stage is not None:
            payload["stage"] = self.stage
        if self.contact is not None:
            payload["contact"] = {
                "label": self.contact.label,
                "channel": self.contact.channel,
                "value": self.contact.value,
            }
        return payload

    def to_stream_event(self) -> dict[str, Any]:
        """Wrap the same canonical payload without changing its semantics."""

        if self.kind is OutputKind.PROGRESS:
            event_type = "progress"
        elif self.kind in {OutputKind.TRANSPORT_ERROR, OutputKind.CLIENT_ERROR} or (
            self.protocol is not None and self.protocol.status is RequestStatus.ERROR
        ):
            event_type = "error"
        else:
            event_type = "result"
        return {"type": event_type, "data": self.to_dict()}


Renderer = Callable[[Mapping[str, Any], str], str]


@dataclass(frozen=True)
class CatalogEntry:
    allowed_kinds: frozenset[FinalOutputKind]
    allowed_statuses: frozenset[RequestStatus]
    allowed_layers: frozenset[RequestLayer]
    allowed_phases: frozenset[str]
    allowed_codes: frozenset[str]
    required_facts: frozenset[str]
    optional_facts: frozenset[str]
    mentioned_actions: frozenset[UserAction]
    permitted_actions: frozenset[UserAction]
    renderer: Renderer
    max_chars: int = 180
    requires_usable_result: bool = False
    requires_delivery: bool = False
    requires_active_image: bool = False
    terminal_without_action: bool = False
    semantic_validator: Callable[[Mapping[str, Any]], None] | None = None


@dataclass(frozen=True)
class NoticeEntry:
    allowed_statuses: frozenset[RequestStatus]
    allowed_codes: frozenset[str]
    text: str


@dataclass(frozen=True)
class ProgressCatalogEntry:
    stage: str
    required_facts: frozenset[str]
    optional_facts: frozenset[str]
    renderer: Callable[[Mapping[str, Any]], str]
    max_chars: int = 100


def _fixed(text: str) -> Renderer:
    return lambda _facts, _bounded_text: text


def _format_names(values: Sequence[str]) -> str:
    return "、".join(values)


def _render_chapter_required(facts: Mapping[str, Any], _bounded_text: str) -> str:
    chapters = facts.get("supported_chapters")
    if chapters:
        return f"请告诉我题目所属章节或题型。目前支持：{_format_names(chapters)}。"
    return "请告诉我题目所属章节或题型。"


def _render_supported_chapters(
    _facts: Mapping[str, Any], _bounded_text: str
) -> str:
    return (
        f"当前支持：{_format_names(_SUPPORTED_CHAPTER_NAMES)}。"
        "矩阵位移法和影响线仅支持含具体外荷载的题目。"
    )


def _render_chapter_saved(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"已保存章节“{facts['chapter_name']}”。"


def _render_chapter_unsupported(facts: Mapping[str, Any], _bounded_text: str) -> str:
    text = f"“{facts['chapter_name']}”暂不在当前支持范围内。"
    chapters = facts.get("supported_chapters")
    if chapters:
        text += f"请选择：{_format_names(chapters)}。"
    else:
        text += "请换一个已支持的章节或题型。"
    return text


def _render_questions_ready(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"这张图里识别到 {facts['question_count']} 道题，请告诉我要查第几题。"


def _render_candidates_ready(facts: Mapping[str, Any], _bounded_text: str) -> str:
    label = facts.get("question_label")
    prefix = f"{label}：" if label else ""
    return f"{prefix}找到了 {facts['candidate_count']} 道较相似的候选题，请选择候选编号。"


def _render_candidates_recalled(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"当前有 {facts['candidate_count']} 道候选题，请选择候选编号。"


def _render_no_match_chapter(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"在“{facts['chapter_name']}”里没有找到足够可靠的相似题。请按可用操作继续。"


def _render_answer_ready(facts: Mapping[str, Any], _bounded_text: str) -> str:
    count = facts["delivered_image_count"]
    label = facts.get("question_label")
    prefix = f"{label}的" if label else ""
    return f"{prefix}答案图片已发出，共 {count} 张。"


def _render_answer_resent(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"答案图片已重新发出，共 {facts['delivered_image_count']} 张。"


_A1_REASON_TEXT = MappingProxyType(
    {
        "unrelated_image": "这张图不是可检索的结构力学题，请重新上传一张完整的结构力学题图。",
        "image_unclear": "题图不够清楚，无法确认完整结构和荷载，请重新上传一张清楚、完整的题图。",
        "structure_incomplete": "题图没有完整显示结构和支座，请重新上传包含完整结构、支座和实际荷载的题图。",
        "load_incomplete": "题图没有完整显示实际荷载，请重新上传包含完整结构和实际荷载的题图。",
        "original_structure_missing": "题图没有完整的原结构图，请重新上传包含原结构、支座和实际荷载的题图。",
        "unsupported_content": "当前图片不适合进入题库检索，请重新上传包含完整结构和实际荷载的题图。",
    }
)


def _render_a1_reason(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return _A1_REASON_TEXT[facts["a1_reason"]]


def _render_page_selection(facts: Mapping[str, Any], _bounded_text: str) -> str:
    count = facts.get("remaining_count", facts.get("question_count", 0))
    return f"当前还有 {count} 道题可处理，请选择题号。"


def _render_units_prepared(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return (
        f"已整理 {facts['question_count']} 道题：{facts['ready_count']} 道可直接处理，"
        f"{facts['manual_count']} 道需要你手动裁剪。"
    )


def _render_crop_required(facts: Mapping[str, Any], _bounded_text: str) -> str:
    prefix = (
        f"已停止{facts['previous_question_label']}，现在处理{facts['question_label']}。"
        if facts.get("previous_question_label")
        else ""
    )
    return f"{prefix}{facts['question_label']}需要你手动框选，请只框选这一题后提交。"


_CROP_REASON_TEXT = MappingProxyType(
    {
        "image_unclear": "裁剪图不够清楚",
        "region_missing": "裁剪图没有完整包含目标题目",
        "invalid_crop": "裁剪范围无法用于检索",
        "multiple_questions": "裁剪图中仍包含多道题",
        "wrong_question": "裁剪图与所选题目不匹配",
        "multiple_diagrams": "裁剪区域中仍包含多个结构图",
        "loads_incomplete": "结构荷载不完整",
        "structure_incomplete": "结构图或支座不完整",
        "no_external_load": "裁剪图中没有可用于检索的外荷载",
        "unconfirmed": "裁剪图完整性暂时无法确认",
    }
)


def _render_crop_rejected(facts: Mapping[str, Any], _bounded_text: str) -> str:
    prefix = f"{facts['question_label']}：" if facts.get("question_label") else ""
    return f"{prefix}{_CROP_REASON_TEXT[facts['crop_reason']]}，请重新裁剪。"


def _render_unit_cancelled(facts: Mapping[str, Any], _bounded_text: str) -> str:
    return f"{facts['question_label']}已结束，还剩 {facts['remaining_count']} 道题。"


def _render_unit_stopped_remaining(
    facts: Mapping[str, Any], _bounded_text: str
) -> str:
    return (
        f"已停止{facts['question_label']}的当前处理，"
        f"整页还有 {facts['remaining_count']} 道题可选择。"
    )


def _render_unit_stopped_complete(
    facts: Mapping[str, Any], _bounded_text: str
) -> str:
    return (
        f"已停止{facts['question_label']}的当前处理，这页没有其他待处理题目。"
        "可以上传一张新题图。"
    )


def _render_page_candidates_ready(facts: Mapping[str, Any], _bounded_text: str) -> str:
    switch_prefix = (
        f"已停止{facts['previous_question_label']}，现在处理{facts['question_label']}。"
        if facts.get("previous_question_label")
        else ""
    )
    sources = facts.get("source_chapters")
    source_text = f"从{_format_names(sources)}" if sources else ""
    separator = "：" if sources else ""
    return (
        f"{switch_prefix}{facts['question_label']}{separator}{source_text}找到了 "
        f"{facts['candidate_count']} 道较相似的候选题，请选择候选编号。"
    )


def _render_page_answer_remaining(facts: Mapping[str, Any], _bounded_text: str) -> str:
    switch_prefix = (
        f"已停止{facts['previous_question_label']}，现在处理{facts['question_label']}。"
        if facts.get("previous_question_label")
        else ""
    )
    return (
        f"{switch_prefix}{facts['question_label']}的答案图片已发出，共 {facts['delivered_image_count']} 张；"
        f"整页还剩 {facts['remaining_count']} 道题。"
    )


def _render_page_answer_complete(facts: Mapping[str, Any], _bounded_text: str) -> str:
    switch_prefix = (
        f"已停止{facts['previous_question_label']}，现在处理{facts['question_label']}。"
        if facts.get("previous_question_label")
        else ""
    )
    return (
        f"{switch_prefix}{facts['question_label']}的答案图片已发出，共 {facts['delivered_image_count']} 张；"
        "这页题目已经处理完成。"
    )


def _render_progress_chapter(facts: Mapping[str, Any]) -> str:
    return f"正在“{facts['chapter_name']}”中检索相似题。"


def _render_progress_unit(facts: Mapping[str, Any]) -> str:
    return f"正在分析{facts['question_label']}。"


def _render_progress_waiting(facts: Mapping[str, Any]) -> str:
    seconds = facts.get("retry_after_seconds")
    return f"请求正在排队，预计 {seconds} 秒后开始。" if seconds else "请求正在排队。"


def _require_positive(facts: Mapping[str, Any], key: str) -> None:
    if facts.get(key, 0) <= 0:
        raise OutputContractError("positive_fact_required")


def _validate_question_list_facts(facts: Mapping[str, Any]) -> None:
    _require_positive(facts, "question_count")


def _validate_candidate_facts(facts: Mapping[str, Any]) -> None:
    _require_positive(facts, "candidate_count")


def _validate_page_selection_facts(facts: Mapping[str, Any]) -> None:
    counts = [facts.get(key) for key in ("question_count", "remaining_count") if key in facts]
    if not counts or any(value <= 0 for value in counts):
        raise OutputContractError("selection_count_missing")
    if (
        "question_count" in facts
        and "remaining_count" in facts
        and facts["remaining_count"] > facts["question_count"]
    ):
        raise OutputContractError("selection_count_mismatch")


def _validate_units_facts(facts: Mapping[str, Any]) -> None:
    question_count = facts["question_count"]
    ready_count = facts["ready_count"]
    manual_count = facts["manual_count"]
    if question_count <= 0 or ready_count + manual_count != question_count:
        raise OutputContractError("unit_count_mismatch")


def _validate_remaining_facts(facts: Mapping[str, Any]) -> None:
    _require_positive(facts, "remaining_count")


def _validate_exhausted_facts(facts: Mapping[str, Any]) -> None:
    if facts.get("continuation_available") is not False:
        raise OutputContractError("continuation_state_mismatch")


def _validate_continuation_facts(facts: Mapping[str, Any]) -> None:
    if facts.get("continuation_available") is not True:
        raise OutputContractError("continuation_state_mismatch")


def _validate_no_units_facts(facts: Mapping[str, Any]) -> None:
    if facts.get("question_count") != 0:
        raise OutputContractError("no_units_mismatch")


def _validate_stopped_complete_facts(facts: Mapping[str, Any]) -> None:
    if facts.get("remaining_count") != 0:
        raise OutputContractError("remaining_state_mismatch")


def _validate_crop_preserved_facts(facts: Mapping[str, Any]) -> None:
    if facts.get("crop_draft_preserved") is not True:
        raise OutputContractError("crop_not_preserved")


_TOOL = frozenset({RequestLayer.TOOL})
_SESSION = frozenset({RequestLayer.SESSION})
_TOOL_OR_SESSION = frozenset({RequestLayer.TOOL, RequestLayer.SESSION})
_SUCCESS = frozenset({RequestStatus.SUCCESS})
_NEEDS_INPUT = frozenset({RequestStatus.NEEDS_INPUT})
_NO_MATCH = frozenset({RequestStatus.NO_MATCH})
_SUCCESS_OR_PARTIAL = frozenset({RequestStatus.SUCCESS, RequestStatus.PARTIAL})
_ERROR = frozenset({RequestStatus.ERROR})
_TRANSPORT_KINDS = (FinalOutputKind.TRANSPORT_ERROR,)
_NETWORK_KINDS = (FinalOutputKind.TRANSPORT_ERROR, FinalOutputKind.CLIENT_ERROR)
_GUIDANCE_PHASES = (
    "WAIT_CHAPTER",
    "WAIT_QUESTION_CHOICE",
    "WAIT_CANDIDATE_CHOICE",
    "NO_MATCH",
    "ERROR",
    "WAIT_UNIT_SELECTION",
    "CROP_REQUIRED",
    "A2_ACTIVE",
    "COMPLETE",
)
_GUIDANCE_ACTIONS = (
    UserAction.UPLOAD_IMAGE,
    UserAction.SELECT_QUESTION,
    UserAction.PREPARE_UNITS,
    UserAction.CROP_QUESTION,
    UserAction.SELECT_CANDIDATE,
    UserAction.SHOW_CANDIDATES,
    UserAction.CONTINUE_SEARCH,
    UserAction.CHANGE_CHAPTER,
    UserAction.GLOBAL_SEARCH,
    UserAction.CANCEL_CURRENT_QUESTION,
    UserAction.FINISH_PAGE,
    UserAction.NEW_CHAT,
    UserAction.REJECT_CANDIDATES,
    UserAction.REPORT_ANSWER_MISMATCH,
    UserAction.RESEND_ANSWER,
    UserAction.CONTINUE_CURRENT,
)


def _entry(
    *,
    statuses: frozenset[RequestStatus],
    layers: frozenset[RequestLayer],
    codes: Sequence[str],
    renderer: Renderer,
    kinds: Sequence[FinalOutputKind] = (FinalOutputKind.RESULT,),
    phases: Sequence[str] = tuple(_KNOWN_PHASES),
    required: Sequence[str] = (),
    optional: Sequence[str] = (),
    mentioned: Sequence[UserAction] = (),
    permitted: Sequence[UserAction] | None = None,
    max_chars: int = 180,
    requires_usable_result: bool = False,
    requires_delivery: bool = False,
    requires_active_image: bool = False,
    terminal_without_action: bool = False,
    semantic_validator: Callable[[Mapping[str, Any]], None] | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        allowed_kinds=frozenset(kinds),
        allowed_statuses=statuses,
        allowed_layers=layers,
        allowed_phases=frozenset(phases),
        allowed_codes=frozenset(codes),
        required_facts=frozenset(required),
        optional_facts=frozenset(optional),
        mentioned_actions=frozenset(mentioned),
        permitted_actions=frozenset(mentioned if permitted is None else permitted),
        renderer=renderer,
        max_chars=max_chars,
        requires_usable_result=requires_usable_result,
        requires_delivery=requires_delivery,
        requires_active_image=requires_active_image,
        terminal_without_action=terminal_without_action,
        semantic_validator=semantic_validator,
    )


_CATALOG: Mapping[str, CatalogEntry] = MappingProxyType(
    {
        "conversation.greeting": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "你好，我是力答，一个结构力学题库搜题助手。"
                "我可以识别你发来的题图、判断题目章节、从题库寻找相似题，并返回对应答案。"
                "发一张结构力学题图给我看看吧。"
            ),
            mentioned=(UserAction.UPLOAD_IMAGE,),
        ),
        "conversation.courtesy": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed("不客气，需要时可以继续。"),
        ),
        "conversation.farewell": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed("好的，需要时可以继续使用力答。"),
        ),
        "conversation.identity": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed("我是力答，一个结构力学题库搜题助手。"),
        ),
        "conversation.capability": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "我可以识别结构力学题图、检索相似题并返回对应答案。"
            ),
        ),
        "conversation.supported_chapters": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_supported_chapters,
        ),
        "conversation.workflow": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "处理流程包括题图识别、候选检索、候选确认和答案交付。"
            ),
        ),
        "conversation.general": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed("我可以继续帮助你处理结构力学题库检索。"),
        ),
        "conversation.current": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_fixed("当前任务进度已保留，请按当前可用操作继续。"),
            phases=_GUIDANCE_PHASES,
            optional=("continuation_available", "global_search_offered"),
            permitted=_GUIDANCE_ACTIONS,
        ),
        "conversation.out_of_scope": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("REQUEST_OUT_OF_SCOPE",),
            renderer=_fixed(
                "我目前只处理结构力学题库检索相关内容，请上传结构力学题图。"
            ),
            mentioned=(UserAction.UPLOAD_IMAGE,),
        ),
        "conversation.action_rejected": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("ACTION_NOT_ALLOWED",),
            renderer=_fixed("当前状态不能执行这个操作，请按当前可用操作继续。"),
            phases=_GUIDANCE_PHASES,
            optional=("continuation_available", "global_search_offered"),
            permitted=_GUIDANCE_ACTIONS,
        ),
        "search.upload.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.UPLOAD, RequestLayer.TOOL}),
            codes=("UPLOAD_REQUIRED", "EXTERNAL_LOAD_NOT_FOUND"),
            renderer=_fixed("请重新上传一张清楚、完整的题图。"),
            phases=("IDLE", "PROCESSING", "NO_MATCH", "ERROR"),
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "search.cancelled": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "当前检索已取消，需要继续时请上传一张新题图。"
            ),
            phases=("CANCELLED",),
            mentioned=(UserAction.UPLOAD_IMAGE,),
        ),
        "search.chapter.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=_TOOL,
            codes=("CHAPTER_REQUIRED", "UNKNOWN_CHAPTER"),
            renderer=_render_chapter_required,
            phases=("WAIT_CHAPTER",),
            optional=("supported_chapters", "global_search_offered"),
            mentioned=(UserAction.CHANGE_CHAPTER,),
            permitted=(UserAction.CHANGE_CHAPTER, UserAction.GLOBAL_SEARCH),
        ),
        "search.chapter.saved": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_chapter_saved,
            phases=(
                "PROCESSING",
                "WAIT_CHAPTER",
                "READY_TO_ROUTE",
                "READY_FOR_SEARCH",
                "WAIT_CANDIDATE_CHOICE",
            ),
            required=("chapter_name",),
        ),
        "search.chapter.unsupported": _entry(
            statuses=_NEEDS_INPUT,
            layers=_TOOL_OR_SESSION,
            codes=("UNKNOWN_CHAPTER", "REQUEST_OUT_OF_SCOPE"),
            renderer=_render_chapter_unsupported,
            phases=("WAIT_CHAPTER",),
            required=("chapter_name",),
            optional=("supported_chapters",),
            mentioned=(UserAction.CHANGE_CHAPTER,),
        ),
        "search.clarification.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=(
                "MESSAGE_INVALID",
                "CLARIFICATION_REQUIRED",
                "QUESTION_INDEX_REQUIRED",
                "CANDIDATE_RANK_REQUIRED",
                "SELECTION_OUT_OF_RANGE",
            ),
            renderer=_fixed(
                "我还不能确定你的意思，请按当前可用操作再说明一下。"
            ),
            phases=(
                "IDLE",
                "WAIT_CHAPTER",
                "WAIT_QUESTION_CHOICE",
                "WAIT_CANDIDATE_CHOICE",
                "ANSWERED",
                "NO_MATCH",
                "ERROR",
            ),
            optional=("continuation_available", "global_search_offered"),
            permitted=_GUIDANCE_ACTIONS,
        ),
        "search.questions.ready": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=_TOOL,
            codes=(
                "QUESTION_UNITS_PREPARED",
                "MULTI_CROPS_UNAVAILABLE",
                "REQUEST_SUCCEEDED",
            ),
            renderer=_render_questions_ready,
            phases=("WAIT_QUESTION_CHOICE",),
            required=("question_count",),
            mentioned=(UserAction.SELECT_QUESTION,),
            permitted=(UserAction.SELECT_QUESTION, UserAction.RETRY_SEARCH),
            semantic_validator=_validate_question_list_facts,
        ),
        "search.candidates.ready": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=_TOOL,
            codes=(
                "COARSE_CANDIDATES_FOUND",
                "MULTI_DETECTION_FALLBACK",
                "RERANK_COMPLETED",
                "RERANK_EMPTY_COARSE_FALLBACK",
                "RERANK_INCOMPLETE_COARSE_FALLBACK",
                "RERANK_SKIPPED_NO_IMAGE",
                "STRUCTURE_CLASSIFICATION_FALLBACK",
                "STRUCTURE_FILTER_SKIPPED_NO_IMAGE",
                "STRUCTURE_TYPE_UNCERTAIN",
                "REQUEST_SUCCEEDED",
            ),
            renderer=_render_candidates_ready,
            phases=("WAIT_CANDIDATE_CHOICE",),
            required=("candidate_count",),
            optional=(
                "question_label",
                "page_index",
                "has_usable_result",
                "continuation_available",
            ),
            mentioned=(UserAction.SELECT_CANDIDATE,),
            permitted=(
                UserAction.SELECT_CANDIDATE,
                UserAction.CONTINUE_SEARCH,
                UserAction.RETRY_SEARCH,
            ),
            requires_usable_result=True,
            semantic_validator=_validate_candidate_facts,
        ),
        "search.global.candidates.ready": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("GLOBAL_CANDIDATES_FOUND", "REQUEST_SUCCEEDED"),
            renderer=lambda facts, _bounded_text: (
                f"从{_format_names(facts['source_chapters'])}找到了 "
                f"{facts['candidate_count']} 道较相似的候选题，请选择候选编号。"
            ),
            phases=("WAIT_CANDIDATE_CHOICE",),
            required=("candidate_count", "source_chapters"),
            mentioned=(UserAction.SELECT_CANDIDATE,),
            requires_usable_result=True,
            semantic_validator=_validate_candidate_facts,
        ),
        "search.candidates.unavailable": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CANDIDATE_LIST_UNAVAILABLE",),
            renderer=_fixed(
                "当前候选列表不可用，请重新上传题图后再选择。"
            ),
            phases=("IDLE", "WAIT_CANDIDATE_CHOICE", "NO_MATCH", "ERROR"),
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "search.candidates.recalled": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_candidates_recalled,
            phases=("WAIT_CANDIDATE_CHOICE",),
            required=("candidate_count",),
            mentioned=(UserAction.SELECT_CANDIDATE,),
            requires_usable_result=True,
            semantic_validator=_validate_candidate_facts,
        ),
        "search.candidates.rejected_more": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "这批候选已排除，可以继续搜索下一批。"
            ),
            phases=("WAIT_CANDIDATE_CHOICE",),
            required=("continuation_available",),
            mentioned=(UserAction.CONTINUE_SEARCH,),
            permitted=(UserAction.CONTINUE_SEARCH, UserAction.CHANGE_CHAPTER),
            semantic_validator=_validate_continuation_facts,
        ),
        "search.candidates.rejected": _entry(
            statuses=_NO_MATCH,
            layers=_SESSION,
            codes=("NO_MORE_CANDIDATES",),
            renderer=_fixed("当前候选已处理完，请按可用操作继续。"),
            phases=("WAIT_CANDIDATE_CHOICE", "NO_MATCH"),
            required=("continuation_available",),
            optional=("global_search_offered", "author_contact_available"),
            permitted=(
                UserAction.CHANGE_CHAPTER,
                UserAction.RETRY_UPLOAD,
                UserAction.GLOBAL_SEARCH,
                UserAction.CONTACT_AUTHOR,
            ),
            semantic_validator=_validate_exhausted_facts,
        ),
        "search.no_match.chapter": _entry(
            statuses=_NO_MATCH,
            layers=_TOOL,
            codes=("NO_MATCH", "NO_COARSE_CANDIDATES", "NO_RELIABLE_RERANK_CANDIDATES"),
            renderer=_render_no_match_chapter,
            phases=("NO_MATCH",),
            required=("chapter_name",),
            optional=("global_search_offered", "author_contact_available"),
            permitted=(
                UserAction.CHANGE_CHAPTER,
                UserAction.RETRY_UPLOAD,
                UserAction.GLOBAL_SEARCH,
                UserAction.CONTACT_AUTHOR,
            ),
        ),
        "search.no_match.global": _entry(
            statuses=_NO_MATCH,
            layers=_TOOL,
            codes=(
                "NO_GLOBAL_COARSE_CANDIDATES",
                "NO_GLOBAL_RELIABLE_CANDIDATES",
                "NO_MATCH",
            ),
            renderer=_fixed("全局题库中也没有找到足够可靠的相似题。请按可用操作继续。"),
            phases=("NO_MATCH",),
            optional=("author_contact_available",),
            permitted=(
                UserAction.CHANGE_CHAPTER,
                UserAction.RETRY_UPLOAD,
                UserAction.CONTACT_AUTHOR,
            ),
        ),
        "search.answer.ready": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=frozenset({RequestLayer.TOOL, RequestLayer.MEDIA}),
            codes=("ANSWER_FILES_FOUND", "MEDIA_PERSIST_FAILED", "REQUEST_SUCCEEDED"),
            renderer=_render_answer_ready,
            phases=("ANSWERED",),
            required=("delivered_image_count",),
            optional=("question_label", "page_index", "has_usable_result"),
            permitted=(
                UserAction.RETRY_REQUEST,
                UserAction.SHOW_CANDIDATES,
                UserAction.RESEND_ANSWER,
                UserAction.REPORT_ANSWER_MISMATCH,
                UserAction.UPLOAD_IMAGE,
            ),
            requires_usable_result=True,
            requires_delivery=True,
        ),
        "search.answer.mismatch": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "这个答案已标记为不匹配，请返回候选列表重新选择。"
            ),
            phases=("ANSWERED",),
            optional=("continuation_available",),
            mentioned=(UserAction.SHOW_CANDIDATES,),
            permitted=(
                UserAction.SHOW_CANDIDATES,
                UserAction.SELECT_CANDIDATE,
                UserAction.CONTINUE_SEARCH,
            ),
        ),
        "search.answer.resent": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=frozenset({RequestLayer.TOOL, RequestLayer.MEDIA}),
            codes=("ANSWER_FILES_FOUND", "MEDIA_PERSIST_FAILED", "REQUEST_SUCCEEDED"),
            renderer=_render_answer_resent,
            phases=("ANSWERED",),
            required=("delivered_image_count",),
            optional=("has_usable_result",),
            permitted=(
                UserAction.RETRY_REQUEST,
                UserAction.SHOW_CANDIDATES,
                UserAction.REPORT_ANSWER_MISMATCH,
            ),
            requires_usable_result=True,
            requires_delivery=True,
        ),
        "search.answer.missing": _entry(
            statuses=_NO_MATCH,
            layers=_TOOL,
            codes=("ANSWER_FILES_NOT_FOUND",),
            renderer=_fixed("这道候选题暂时没有可交付的答案图片，请返回候选列表重新选择。"),
            phases=("WAIT_CANDIDATE_CHOICE", "NO_MATCH"),
            mentioned=(UserAction.SHOW_CANDIDATES,),
            permitted=(UserAction.SHOW_CANDIDATES, UserAction.SELECT_CANDIDATE),
        ),
        "search.failed.retryable": _entry(
            statuses=_ERROR,
            layers=_TOOL,
            codes=(
                "AGENT_FAILED",
                "ANSWER_LOOKUP_FAILED",
                "COARSE_SEARCH_FAILED",
                "GLOBAL_SEARCH_FAILED",
                "IMAGE_ANALYSIS_FAILED",
                "MULTI_DETAIL_FAILED",
                "RERANK_FAILED",
                "TOOL_FAILED",
            ),
            renderer=_fixed("这次检索没有完成，题图仍已保留，请重试当前检索。"),
            phases=("ERROR",),
            required=("active_image_preserved",),
            mentioned=(UserAction.RETRY_SEARCH,),
            requires_active_image=True,
        ),
        "search.failed.nonretryable": _entry(
            statuses=_ERROR,
            layers=_TOOL,
            codes=("BANK_ROUTE_FAILED",),
            renderer=_fixed(
                "当前题型暂时无法进入题库检索，请上传一张新题图。"
            ),
            phases=("ERROR",),
            mentioned=(UserAction.UPLOAD_IMAGE,),
            permitted=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        ),
        "triage.a1.reasoned": _entry(
            statuses=_NEEDS_INPUT,
            layers=_TOOL,
            codes=("TRIAGE_A1_STOPPED",),
            renderer=_render_a1_reason,
            phases=("PROCESSING", "COMPLETE"),
            required=("a1_reason",),
            mentioned=(UserAction.RETRY_UPLOAD,),
            max_chars=100,
        ),
        "triage.a1.fallback": _entry(
            statuses=_NEEDS_INPUT,
            layers=_TOOL,
            codes=("TRIAGE_A1_STOPPED",),
            renderer=_fixed(
                "当前题图不能进入题库检索，请重新上传包含完整结构和实际荷载的题图。"
            ),
            phases=("PROCESSING", "COMPLETE"),
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "triage.a1.no_external_load": _entry(
            statuses=_NEEDS_INPUT,
            layers=_TOOL,
            codes=("TRIAGE_A1_STOPPED", "EXTERNAL_LOAD_NOT_FOUND"),
            renderer=_fixed(
                "这张图没有可用于检索的完整外荷载，请重新上传包含完整结构和实际荷载的题图。"
            ),
            phases=("PROCESSING", "COMPLETE"),
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "page.no_units": _entry(
            statuses=_NO_MATCH,
            layers=_TOOL,
            codes=("PAGE_NO_SEARCHABLE_UNITS",),
            renderer=_fixed(
                "这页没有识别到可检索的完整结构题，请重新上传清楚、完整的题图。"
            ),
            phases=("COMPLETE",),
            required=("question_count",),
            mentioned=(UserAction.RETRY_UPLOAD,),
            semantic_validator=_validate_no_units_facts,
        ),
        "page.selection.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED", "QUESTION_INDEX_REQUIRED"),
            renderer=_render_page_selection,
            phases=("WAIT_UNIT_SELECTION",),
            optional=("question_count", "remaining_count"),
            mentioned=(UserAction.SELECT_QUESTION,),
            permitted=(
                UserAction.SELECT_QUESTION,
                UserAction.PREPARE_UNITS,
                UserAction.FINISH_PAGE,
            ),
            semantic_validator=_validate_page_selection_facts,
        ),
        "page.current.guidance": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_fixed(
                "当前整页进度已保留，请按页面可用操作继续。"
            ),
            phases=(
                "WAIT_UNIT_SELECTION",
                "CROP_REQUIRED",
                "A2_ACTIVE",
                "ERROR",
                "COMPLETE",
            ),
            optional=("continuation_available", "global_search_offered"),
            permitted=_GUIDANCE_ACTIONS,
        ),
        "page.units.prepared": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=_TOOL_OR_SESSION,
            codes=(
                "QUESTION_UNITS_PREPARED",
                "MULTI_CROPS_UNAVAILABLE",
                "REQUEST_SUCCEEDED",
            ),
            renderer=_render_units_prepared,
            phases=("WAIT_UNIT_SELECTION",),
            required=("question_count", "ready_count", "manual_count"),
            mentioned=(UserAction.SELECT_QUESTION,),
            permitted=(UserAction.SELECT_QUESTION, UserAction.RETRY_CURRENT_STAGE),
            requires_usable_result=True,
            semantic_validator=_validate_units_facts,
        ),
        "page.crop.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_render_crop_required,
            phases=("CROP_REQUIRED",),
            required=("question_label",),
            optional=("page_index", "previous_question_label"),
            mentioned=(UserAction.CROP_QUESTION,),
            permitted=(
                UserAction.CROP_QUESTION,
                UserAction.CANCEL_CURRENT_QUESTION,
                UserAction.FINISH_PAGE,
                UserAction.CONTINUE_CURRENT,
            ),
        ),
        "page.crop.rejected": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_render_crop_rejected,
            phases=("CROP_REQUIRED",),
            required=("crop_reason",),
            optional=("question_label", "page_index"),
            mentioned=(UserAction.CROP_QUESTION,),
            permitted=(
                UserAction.CROP_QUESTION,
                UserAction.CANCEL_CURRENT_QUESTION,
                UserAction.FINISH_PAGE,
                UserAction.CONTINUE_CURRENT,
            ),
        ),
        "page.crop.verification_failed": _entry(
            statuses=_ERROR,
            layers=_TOOL,
            codes=("SERVICE_UNAVAILABLE",),
            renderer=_fixed(
                "裁剪图已保留，但这次校验没有完成，请重新提交当前裁剪。"
            ),
            phases=("CROP_REQUIRED",),
            required=("crop_draft_preserved",),
            mentioned=(UserAction.RETRY_REQUEST,),
            semantic_validator=_validate_crop_preserved_facts,
        ),
        "page.namespace.clarification": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_fixed("请说明你要选择整页中的题号，还是当前候选题编号。"),
            phases=("A2_ACTIVE",),
            mentioned=(UserAction.SELECT_QUESTION, UserAction.SELECT_CANDIDATE),
        ),
        "page.cancel.scope_required.current": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_fixed("请选择：取消当前题、结束整页，或继续当前题。"),
            phases=("CROP_REQUIRED", "A2_ACTIVE"),
            mentioned=(
                UserAction.CANCEL_CURRENT_QUESTION,
                UserAction.FINISH_PAGE,
                UserAction.CONTINUE_CURRENT,
            ),
        ),
        "page.cancel.scope_required.page": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("CLARIFICATION_REQUIRED",),
            renderer=_fixed("请选择：结束整页，或继续当前操作。"),
            phases=("WAIT_UNIT_SELECTION", "CROP_REQUIRED", "A2_ACTIVE"),
            mentioned=(UserAction.FINISH_PAGE, UserAction.CONTINUE_CURRENT),
        ),
        "page.unit.cancelled_remaining": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_unit_cancelled,
            phases=("WAIT_UNIT_SELECTION",),
            required=("question_label", "remaining_count"),
            optional=("page_index",),
            mentioned=(UserAction.SELECT_QUESTION,),
            semantic_validator=_validate_remaining_facts,
        ),
        "page.unit.stopped_remaining": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_unit_stopped_remaining,
            phases=("WAIT_UNIT_SELECTION",),
            required=("question_label", "remaining_count"),
            optional=("page_index",),
            mentioned=(UserAction.SELECT_QUESTION,),
            permitted=(UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE),
            semantic_validator=_validate_remaining_facts,
        ),
        "page.unit.stopped_complete": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_render_unit_stopped_complete,
            phases=("COMPLETE",),
            required=("question_label", "remaining_count"),
            optional=("page_index",),
            mentioned=(UserAction.UPLOAD_IMAGE,),
            permitted=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
            semantic_validator=_validate_stopped_complete_facts,
        ),
        "page.unit.candidates.ready": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=_TOOL,
            codes=(
                "COARSE_CANDIDATES_FOUND",
                "GLOBAL_CANDIDATES_FOUND",
                "MULTI_DETECTION_FALLBACK",
                "RERANK_COMPLETED",
                "RERANK_EMPTY_COARSE_FALLBACK",
                "RERANK_INCOMPLETE_COARSE_FALLBACK",
                "RERANK_SKIPPED_NO_IMAGE",
                "STRUCTURE_CLASSIFICATION_FALLBACK",
                "STRUCTURE_FILTER_SKIPPED_NO_IMAGE",
                "STRUCTURE_TYPE_UNCERTAIN",
                "REQUEST_SUCCEEDED",
            ),
            renderer=_render_page_candidates_ready,
            phases=("A2_ACTIVE",),
            required=("question_label", "candidate_count"),
            optional=(
                "page_index",
                "has_usable_result",
                "source_chapters",
                "previous_question_label",
            ),
            mentioned=(UserAction.SELECT_CANDIDATE,),
            permitted=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
            requires_usable_result=True,
            semantic_validator=_validate_candidate_facts,
        ),
        "page.unit.answer.delivered_remaining": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=frozenset({RequestLayer.TOOL, RequestLayer.MEDIA}),
            codes=("ANSWER_FILES_FOUND", "MEDIA_PERSIST_FAILED", "REQUEST_SUCCEEDED"),
            renderer=_render_page_answer_remaining,
            phases=("WAIT_UNIT_SELECTION",),
            required=("question_label", "delivered_image_count", "remaining_count"),
            optional=("page_index", "has_usable_result", "previous_question_label"),
            mentioned=(UserAction.SELECT_QUESTION,),
            permitted=(UserAction.SELECT_QUESTION, UserAction.RETRY_REQUEST),
            requires_usable_result=True,
            requires_delivery=True,
            semantic_validator=_validate_remaining_facts,
        ),
        "page.unit.answer.delivered_complete": _entry(
            statuses=_SUCCESS_OR_PARTIAL,
            layers=frozenset({RequestLayer.TOOL, RequestLayer.MEDIA}),
            codes=("ANSWER_FILES_FOUND", "MEDIA_PERSIST_FAILED", "REQUEST_SUCCEEDED"),
            renderer=_render_page_answer_complete,
            phases=("COMPLETE",),
            required=("question_label", "delivered_image_count"),
            optional=("page_index", "has_usable_result", "previous_question_label"),
            permitted=(
                UserAction.RETRY_REQUEST,
                UserAction.UPLOAD_IMAGE,
                UserAction.NEW_CHAT,
            ),
            requires_usable_result=True,
            requires_delivery=True,
        ),
        "page.completed": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed("这页题目已经处理完成。"),
            phases=("COMPLETE",),
            permitted=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        ),
        "page.ended": _entry(
            statuses=_SUCCESS,
            layers=_TOOL,
            codes=("REQUEST_SUCCEEDED",),
            renderer=_fixed(
                "已结束这页题目的处理，当前对话记录仍然保留。可以上传一张新题图。"
            ),
            phases=("COMPLETE",),
            mentioned=(UserAction.UPLOAD_IMAGE,),
            permitted=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        ),
        "page.session.reset": _entry(
            statuses=_SUCCESS,
            layers=_TOOL_OR_SESSION,
            codes=("REQUEST_SUCCEEDED", "SESSION_RESET"),
            renderer=_fixed("当前会话已清空，请上传一张新题图。"),
            phases=("IDLE",),
            mentioned=(UserAction.UPLOAD_IMAGE,),
        ),
        "page.stale.selection": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("STALE_ACTION",),
            renderer=_fixed("刚才的题目选择已经失效，请按当前页面状态重新选择。"),
            phases=("WAIT_UNIT_SELECTION",),
            optional=("remaining_count",),
            mentioned=(UserAction.SELECT_QUESTION,),
        ),
        "page.stale.candidate": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("STALE_CANDIDATE",),
            renderer=_fixed("刚才的候选状态已经失效，请重新上传题图。"),
            phases=("A2_ACTIVE", "WAIT_UNIT_SELECTION"),
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "page.failed.retryable": _entry(
            statuses=_ERROR,
            layers=_TOOL,
            codes=("SERVICE_UNAVAILABLE",),
            renderer=_fixed("当前步骤没有完成，请重新提交。"),
            phases=("ERROR",),
            mentioned=(UserAction.RETRY_REQUEST,),
        ),
        "system.login.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.LOGIN}),
            codes=("LOGIN_REQUIRED", "INVITE_INVALID", "LOGIN_EXPIRED"),
            renderer=_fixed("登录状态无效，请重新登录后再试。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RELOGIN,),
        ),
        "system.quota.unavailable": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.QUOTA}),
            codes=("GLOBAL_DAILY_QUOTA_EXCEEDED", "INVITE_DAILY_QUOTA_EXCEEDED"),
            renderer=_fixed("今天的可用次数已经用完，请明天再试。"),
            kinds=_TRANSPORT_KINDS,
            terminal_without_action=True,
        ),
        "system.quota.identity_missing": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.QUOTA}),
            codes=("INVITE_IDENTITY_MISSING",),
            renderer=_fixed("当前身份信息不完整，请重新登录。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RELOGIN,),
        ),
        "system.queue.full": _entry(
            statuses=_ERROR,
            layers=frozenset({RequestLayer.QUEUE}),
            codes=("QUEUE_FULL", "QUEUE_TIMEOUT"),
            renderer=_fixed("当前请求较多，这次没有处理完成，请稍后重新提交。"),
            kinds=_TRANSPORT_KINDS,
            optional=("retry_after_seconds",),
            mentioned=(UserAction.RETRY_REQUEST,),
        ),
        "system.upload.required": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.UPLOAD}),
            codes=("UPLOAD_REQUIRED", "UPLOAD_TOO_LARGE", "UPLOAD_UNSUPPORTED_FORMAT", "UPLOAD_DECODE_FAILED"),
            renderer=_fixed("请上传一张符合要求的题图。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "system.upload.persist_failed": _entry(
            statuses=_ERROR,
            layers=frozenset({RequestLayer.UPLOAD}),
            codes=("UPLOAD_PERSIST_FAILED",),
            renderer=_fixed("图片没有保存成功，请重新上传。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_UPLOAD,),
        ),
        "system.network.unavailable": _entry(
            statuses=_ERROR,
            layers=frozenset({RequestLayer.NETWORK}),
            codes=("NETWORK_UNAVAILABLE", "REQUEST_TIMEOUT"),
            renderer=_fixed("网络请求没有完成，请重新提交。"),
            kinds=_NETWORK_KINDS,
            mentioned=(UserAction.RETRY_REQUEST,),
        ),
        "system.session.expired": _entry(
            statuses=_NEEDS_INPUT,
            layers=_SESSION,
            codes=("SESSION_EXPIRED",),
            renderer=_fixed("当前会话已经失效，请新建会话后继续。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.NEW_CHAT,),
        ),
        "system.media.not_found": _entry(
            statuses=_ERROR,
            layers=frozenset({RequestLayer.MEDIA}),
            codes=("MEDIA_NOT_FOUND",),
            renderer=_fixed("结果图片暂时无法读取，请重新提交请求。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_REQUEST,),
        ),
        "system.feedback.invalid": _entry(
            statuses=_NEEDS_INPUT,
            layers=frozenset({RequestLayer.FEEDBACK}),
            codes=("FEEDBACK_INVALID", "FEEDBACK_TOO_LARGE"),
            renderer=_fixed("反馈内容不符合要求，请修改后重新提交。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_FEEDBACK,),
        ),
        "system.feedback.save_failed": _entry(
            statuses=_ERROR,
            layers=frozenset({RequestLayer.FEEDBACK}),
            codes=("FEEDBACK_SAVE_FAILED",),
            renderer=_fixed("反馈没有保存成功，请重新提交反馈。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_FEEDBACK,),
        ),
        "system.service.unavailable": _entry(
            statuses=_ERROR,
            layers=_TOOL,
            codes=("SERVICE_UNAVAILABLE",),
            renderer=_fixed("这次请求没有完成，请稍后重新提交。"),
            kinds=_TRANSPORT_KINDS,
            mentioned=(UserAction.RETRY_REQUEST,),
        ),
    }
)


_NOTICES: Mapping[str, NoticeEntry] = MappingProxyType(
    {
        "notice.multi_detection_fallback": NoticeEntry(
            frozenset({RequestStatus.PARTIAL}),
            frozenset({"MULTI_DETECTION_FALLBACK"}),
            "多题判断未完成，已按单题流程继续。",
        ),
        "notice.multi_crop_partial": NoticeEntry(
            frozenset({RequestStatus.PARTIAL}),
            frozenset({"MULTI_CROPS_UNAVAILABLE"}),
            "部分题目仍需要手动裁剪。",
        ),
        "notice.structure_filter_skipped": NoticeEntry(
            frozenset({RequestStatus.PARTIAL}),
            frozenset(
                {
                    "STRUCTURE_CLASSIFICATION_FALLBACK",
                    "STRUCTURE_FILTER_SKIPPED_NO_IMAGE",
                    "STRUCTURE_TYPE_UNCERTAIN",
                }
            ),
            "结构类型筛选未完整执行，当前结果按相似度保守返回。",
        ),
        "notice.rerank_coarse_fallback": NoticeEntry(
            frozenset({RequestStatus.PARTIAL}),
            frozenset(
                {
                    "RERANK_EMPTY_COARSE_FALLBACK",
                    "RERANK_INCOMPLETE_COARSE_FALLBACK",
                    "RERANK_SKIPPED_NO_IMAGE",
                }
            ),
            "精排未完整完成，当前展示的是可用的初筛结果。",
        ),
        "notice.media_partial": NoticeEntry(
            frozenset({RequestStatus.PARTIAL}),
            frozenset({"MEDIA_PERSIST_FAILED"}),
            "部分结果图片未能交付。",
        ),
    }
)

_REQUIRED_NOTICE_BY_CODE: Mapping[str, str] = MappingProxyType(
    {
        "MULTI_DETECTION_FALLBACK": "notice.multi_detection_fallback",
        "MULTI_CROPS_UNAVAILABLE": "notice.multi_crop_partial",
        "STRUCTURE_CLASSIFICATION_FALLBACK": "notice.structure_filter_skipped",
        "STRUCTURE_FILTER_SKIPPED_NO_IMAGE": "notice.structure_filter_skipped",
        "STRUCTURE_TYPE_UNCERTAIN": "notice.structure_filter_skipped",
        "RERANK_EMPTY_COARSE_FALLBACK": "notice.rerank_coarse_fallback",
        "RERANK_INCOMPLETE_COARSE_FALLBACK": "notice.rerank_coarse_fallback",
        "RERANK_SKIPPED_NO_IMAGE": "notice.rerank_coarse_fallback",
        "MEDIA_PERSIST_FAILED": "notice.media_partial",
    }
)


_PROGRESS_CATALOG: Mapping[str, ProgressCatalogEntry] = MappingProxyType(
    {
        "progress.queue.waiting": ProgressCatalogEntry(
            "queue", frozenset(), frozenset({"retry_after_seconds"}), _render_progress_waiting
        ),
        "progress.queue.started": ProgressCatalogEntry(
            "queue", frozenset(), frozenset(), lambda _facts: "请求已开始处理。"
        ),
        "progress.image.triage": ProgressCatalogEntry(
            "triage", frozenset(), frozenset(), lambda _facts: "正在检查题图是否适合检索。"
        ),
        "progress.image.analysis": ProgressCatalogEntry(
            "image_analysis", frozenset(), frozenset(), lambda _facts: "正在分析题图。"
        ),
        "progress.search.chapter": ProgressCatalogEntry(
            "chapter_search", frozenset({"chapter_name"}), frozenset(), _render_progress_chapter
        ),
        "progress.search.global": ProgressCatalogEntry(
            "global_search", frozenset(), frozenset(), lambda _facts: "正在全局题库中检索相似题。"
        ),
        "progress.page.understanding": ProgressCatalogEntry(
            "page_understanding", frozenset(), frozenset(), lambda _facts: "正在识别整页中的题目。"
        ),
        "progress.page.reunderstanding": ProgressCatalogEntry(
            "page_reunderstanding", frozenset(), frozenset(), lambda _facts: "正在重新识别整页题目。"
        ),
        "progress.page.auto_grounding": ProgressCatalogEntry(
            "auto_grounding", frozenset(), frozenset(), lambda _facts: "正在定位各道题的位置。"
        ),
        "progress.page.crop_validating": ProgressCatalogEntry(
            "crop_validating", frozenset({"question_label"}), frozenset({"page_index"}), _render_progress_unit
        ),
        "progress.page.unit_analysis": ProgressCatalogEntry(
            "unit_analysis", frozenset({"question_label"}), frozenset({"page_index"}), _render_progress_unit
        ),
        "progress.page.auto_crop_ready": ProgressCatalogEntry(
            "auto_crop", frozenset(), frozenset(), lambda _facts: "题目区域已经准备好，正在进入检索。"
        ),
    }
)


def _protocol_rule_for(code: str) -> ProtocolReason | None:
    """Use the shared protocol registry as the only public-code authority."""
    return PROTOCOL_REASONS.get(code)


def _contains_sensitive_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def _safe_public_id(value: Any, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise OutputContractError("public_id_type")
    clean = value.strip()
    if (required and not clean) or (clean and not _ID_RE.fullmatch(clean)):
        raise OutputContractError("public_id_format")
    if clean and _contains_sensitive_text(clean):
        raise OutputContractError("public_id_sensitive")
    return clean


def _validate_public_name(value: Any) -> str:
    if not isinstance(value, str):
        raise OutputContractError("fact_type")
    clean = value.strip()
    if not clean or len(clean) > 40 or _CONTROL_RE.search(clean):
        raise OutputContractError("fact_value")
    if any(char in clean for char in "<>[]{}\\/@#$%^&*=|"):
        raise OutputContractError("fact_value")
    if _STABLE_CODE_RE.fullmatch(clean) or _contains_sensitive_text(clean):
        raise OutputContractError("sensitive_fact")
    return clean


def _validate_chapter_name(value: Any) -> str:
    clean = _validate_public_name(value)
    if clean not in _PUBLIC_CHAPTER_NAMES:
        raise OutputContractError("chapter_not_registered")
    return clean


def _validate_chapter_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 7:
        raise OutputContractError("fact_type")
    names = tuple(_validate_chapter_name(item) for item in value)
    if len(set(names)) != len(names):
        raise OutputContractError("fact_value")
    return names


def _validate_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise OutputContractError("fact_value")
    return value


def _validate_page_index(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 10_000
    ):
        raise OutputContractError("page_index")
    return value


def _validate_question_label(value: Any) -> str:
    if not isinstance(value, str):
        raise OutputContractError("question_label")
    clean = value.strip()
    if not clean or len(clean) > 40 or not _QUESTION_LABEL_RE.fullmatch(clean):
        raise OutputContractError("question_label")
    return re.sub(r"\s+", "", clean)


def _validate_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise OutputContractError("fact_type")
    return value


def _validate_retry_after(value: Any) -> int:
    value = _validate_count(value)
    if value > 86_400:
        raise OutputContractError("fact_value")
    return value


def _validate_crop_reason(value: Any) -> str:
    if not isinstance(value, str) or value not in _CROP_REASON_TEXT:
        raise OutputContractError("fact_value")
    return value


def _validate_a1_reason(value: Any) -> str:
    if not isinstance(value, str) or value not in _A1_REASON_TEXT:
        raise OutputContractError("fact_value")
    return value


_FACT_VALIDATORS: Mapping[str, Callable[[Any], Any]] = MappingProxyType(
    {
        "a1_reason": _validate_a1_reason,
        "active_image_preserved": _validate_bool,
        "author_contact_available": _validate_bool,
        "candidate_count": _validate_count,
        "chapter_name": _validate_chapter_name,
        "continuation_available": _validate_bool,
        "crop_reason": _validate_crop_reason,
        "crop_draft_preserved": _validate_bool,
        "delivered_image_count": _validate_count,
        "global_search_offered": _validate_bool,
        "has_usable_result": _validate_bool,
        "manual_count": _validate_count,
        "page_index": _validate_page_index,
        "question_count": _validate_count,
        "previous_question_label": _validate_question_label,
        "ready_count": _validate_count,
        "remaining_count": _validate_count,
        "retry_after_seconds": _validate_retry_after,
        "source_chapters": _validate_chapter_names,
        "supported_chapters": _validate_chapter_names,
    }
)


def _normalize_facts(
    facts: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(facts, Mapping):
        raise OutputContractError("facts_type")
    keys = set(facts)
    if not all(isinstance(key, str) for key in keys):
        raise OutputContractError("fact_name")
    lowered = {key.lower() for key in keys}
    if lowered & _FORBIDDEN_FACT_NAMES:
        raise OutputContractError("forbidden_fact")
    allowed = required | optional
    if not required <= keys:
        raise OutputContractError("fact_required")
    if not keys <= allowed:
        raise OutputContractError("fact_extra")

    normalized: dict[str, Any] = {}
    page_index: int | None = None
    if "page_index" in facts:
        page_index = _validate_page_index(facts["page_index"])
        normalized["page_index"] = page_index

    for key, value in facts.items():
        if key == "page_index":
            continue
        if key == "question_label":
            if isinstance(value, str):
                clean_label = value.strip()
            else:
                clean_label = ""
            if clean_label and len(clean_label) <= 40 and _QUESTION_LABEL_RE.fullmatch(clean_label):
                normalized[key] = re.sub(r"\s+", "", clean_label)
            elif page_index is not None:
                normalized[key] = f"图片第 {page_index} 题"
            else:
                raise OutputContractError("question_label")
            continue
        validator = _FACT_VALIDATORS.get(key)
        if validator is None:
            raise OutputContractError("fact_not_registered")
        normalized[key] = validator(value)

    return normalized


def _normalize_actions(values: Any) -> tuple[UserAction, ...]:
    if not isinstance(values, (list, tuple)):
        raise OutputContractError("actions_type")
    actions: list[UserAction] = []
    for value in values:
        try:
            action = value if isinstance(value, UserAction) else UserAction(value)
        except (TypeError, ValueError) as exc:
            raise OutputContractError("action_value") from exc
        if action not in actions:
            actions.append(action)
    return tuple(actions)


def _normalize_contact(value: Any) -> PublicContactV1 | None:
    if value is None:
        return None
    if not isinstance(value, PublicContactV1):
        raise OutputContractError("contact_type")
    label = value.label.strip() if isinstance(value.label, str) else ""
    channel = value.channel.strip() if isinstance(value.channel, str) else ""
    contact_value = value.value.strip() if isinstance(value.value, str) else ""
    if label != "联系作者" or channel not in {"微信", "邮箱"}:
        raise OutputContractError("contact_metadata")
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,64}", contact_value):
        raise OutputContractError("contact_value")
    if _contains_sensitive_text(contact_value):
        raise OutputContractError("contact_sensitive")
    return PublicContactV1(label=label, channel=channel, value=contact_value)


def _validate_protocol(protocol: Any, entry: CatalogEntry | None = None) -> RequestProtocol:
    if not isinstance(protocol, RequestProtocol):
        raise OutputContractError("protocol_type")
    if (
        type(protocol.schema_version) is not int
        or protocol.schema_version != REQUEST_PROTOCOL_SCHEMA_VERSION
    ):
        raise OutputContractError("protocol_version")
    _safe_public_id(
        protocol.request_id,
        required=protocol.code != "SERVICE_UNAVAILABLE",
    )
    _safe_public_id(protocol.search_id)
    rule = _protocol_rule_for(protocol.code)
    if rule is None:
        raise OutputContractError("protocol_code_unknown")
    if (
        protocol.status is not rule.status
        or protocol.layer is not rule.layer
        or protocol.retryable is not rule.retryable
        or protocol.action is not rule.action
    ):
        raise OutputContractError("protocol_registry_mismatch")
    if entry is not None:
        if protocol.code not in entry.allowed_codes:
            raise OutputContractError("message_code_mismatch")
        if protocol.status not in entry.allowed_statuses or protocol.layer not in entry.allowed_layers:
            raise OutputContractError("message_protocol_mismatch")
    return protocol


def _validate_kind_protocol(kind: OutputKind, protocol: RequestProtocol) -> None:
    if kind is OutputKind.RESULT:
        if protocol.layer not in {
            RequestLayer.TOOL,
            RequestLayer.SESSION,
            RequestLayer.MEDIA,
        }:
            raise OutputContractError("kind_layer_mismatch")
        return
    if kind is OutputKind.TRANSPORT_ERROR:
        if protocol.status is RequestStatus.SUCCESS:
            raise OutputContractError("kind_status_mismatch")
        return
    if kind is OutputKind.CLIENT_ERROR:
        if protocol.status not in {RequestStatus.NEEDS_INPUT, RequestStatus.ERROR} or (
            protocol.layer
            not in {RequestLayer.NETWORK, RequestLayer.UPLOAD, RequestLayer.SESSION}
        ):
            raise OutputContractError("kind_layer_mismatch")
        return
    raise OutputContractError("kind")


def _has_usable_result(facts: Mapping[str, Any]) -> bool:
    if facts.get("has_usable_result") is True:
        return True
    return any(
        isinstance(facts.get(key), int) and facts[key] > 0
        for key in ("candidate_count", "delivered_image_count", "ready_count", "question_count")
    )


def _validate_semantics(
    protocol: RequestProtocol,
    actions: tuple[UserAction, ...],
    facts: Mapping[str, Any],
    *,
    entry: CatalogEntry | None,
    contact: PublicContactV1 | None,
) -> None:
    action_set = set(actions)
    if protocol.action is not RequestAction.NONE:
        mapped = _PROTOCOL_ACTION_MAP.get(protocol.action)
        if mapped is None or mapped not in action_set:
            raise OutputContractError("protocol_action_not_allowed")
    if protocol.retryable and not any(action.value in _RETRY_ACTIONS for action in actions):
        raise OutputContractError("retry_action_missing")
    terminal_without_action = bool(entry and entry.terminal_without_action)
    if terminal_without_action and actions:
        raise OutputContractError("terminal_action_not_allowed")
    if (
        protocol.status in {RequestStatus.NEEDS_INPUT, RequestStatus.NO_MATCH}
        and not actions
        and not terminal_without_action
    ):
        raise OutputContractError("next_action_missing")
    if protocol.status is RequestStatus.PARTIAL and not _has_usable_result(facts):
        raise OutputContractError("partial_without_result")
    if UserAction.CONTINUE_SEARCH in action_set and facts.get("continuation_available") is not True:
        raise OutputContractError("continuation_not_available")
    if UserAction.GLOBAL_SEARCH in action_set and facts.get("global_search_offered") is not True:
        raise OutputContractError("global_search_not_available")
    if UserAction.CONTACT_AUTHOR in action_set:
        if facts.get("author_contact_available") is not True or contact is None:
            raise OutputContractError("author_contact_not_available")
    elif contact is not None:
        raise OutputContractError("contact_not_allowed")
    if entry is not None:
        _validate_entry_actions(protocol, actions, entry)
        if entry.requires_usable_result and not _has_usable_result(facts):
            raise OutputContractError("usable_result_missing")
        if entry.requires_delivery and facts.get("delivered_image_count", 0) <= 0:
            raise OutputContractError("media_not_delivered")
        if entry.requires_active_image and facts.get("active_image_preserved") is not True:
            raise OutputContractError("active_image_not_preserved")
        if entry.semantic_validator is not None:
            entry.semantic_validator(facts)


def _permitted_actions_for_protocol(
    entry: CatalogEntry,
    protocol: RequestProtocol,
) -> frozenset[UserAction]:
    """Narrow a message-level action union to the current protocol variant."""

    permitted = set(entry.permitted_actions)
    retry_actions = {action for action in permitted if action.value in _RETRY_ACTIONS}
    if protocol.status is RequestStatus.SUCCESS:
        permitted.difference_update(retry_actions)
    elif protocol.status is RequestStatus.PARTIAL:
        if not protocol.retryable:
            permitted.difference_update(retry_actions)
        elif protocol.action is not RequestAction.NONE:
            mapped = _PROTOCOL_ACTION_MAP.get(protocol.action)
            permitted.difference_update(retry_actions - ({mapped} if mapped else set()))
        elif len(retry_actions) != 1:
            raise OutputContractError("retry_action_ambiguous")
    return frozenset(permitted)


def _validate_entry_actions(
    protocol: RequestProtocol,
    actions: tuple[UserAction, ...],
    entry: CatalogEntry,
) -> None:
    action_set = set(actions)
    if not action_set <= _permitted_actions_for_protocol(entry, protocol):
        raise OutputContractError("action_not_permitted")
    if not entry.mentioned_actions <= action_set:
        raise OutputContractError("mentioned_action_not_allowed")


def _validate_fallback_actions(
    protocol: RequestProtocol,
    actions: tuple[UserAction, ...],
    entry: CatalogEntry | None,
) -> None:
    if entry is not None:
        _validate_entry_actions(protocol, actions, entry)
        return
    mapped = _PROTOCOL_ACTION_MAP.get(protocol.action)
    independently_proven = frozenset({mapped}) if mapped is not None else frozenset()
    if not set(actions) <= independently_proven:
        raise OutputContractError("fallback_action_unproven")


def _safe_kind(value: Any) -> OutputKind:
    try:
        return OutputKind(FinalOutputKind(value).value)
    except (TypeError, ValueError) as exc:
        raise OutputContractError("kind") from exc


def _safe_phase(value: Any) -> str:
    clean = str(value or "").strip().upper()
    if not _PHASE_RE.fullmatch(clean) or clean not in _KNOWN_PHASES:
        raise OutputContractError("phase")
    return clean


def _render_notices(
    notice_keys: Any,
    protocol: RequestProtocol,
) -> tuple[str, ...]:
    if not isinstance(notice_keys, (list, tuple)):
        raise OutputContractError("notices_type")
    seen: set[str] = set()
    for key in notice_keys:
        if not isinstance(key, str):
            raise OutputContractError("notice_key")
        seen.add(key)
    if len(seen) > 5:
        raise OutputContractError("notice_count")
    required_notice = _REQUIRED_NOTICE_BY_CODE.get(protocol.code)
    if required_notice is not None and required_notice not in seen:
        raise OutputContractError("notice_required")
    rendered: list[str] = []
    for key, notice in _NOTICES.items():
        if key not in seen:
            continue
        if protocol.status not in notice.allowed_statuses or protocol.code not in notice.allowed_codes:
            raise OutputContractError("notice_protocol_mismatch")
        rendered.append(notice.text)
    if len(seen) != len(rendered):
        raise OutputContractError("notice_unknown")
    return tuple(rendered)


def _service_fallback(protocol: RequestProtocol | None = None) -> PublicMessageV1:
    request_id = ""
    search_id = ""
    if isinstance(protocol, RequestProtocol):
        try:
            request_id = _safe_public_id(protocol.request_id)
        except (OutputContractError, ValueError):
            pass
        try:
            search_id = _safe_public_id(protocol.search_id)
        except OutputContractError:
            pass
    safe_protocol = RequestProtocol.from_code(
        "SERVICE_UNAVAILABLE", request_id=request_id, search_id=search_id
    )
    return PublicMessageV1(
        schema_version=USER_OUTPUT_SCHEMA_VERSION,
        kind=OutputKind.TRANSPORT_ERROR,
        message_key="system.service.unavailable",
        text="这次请求没有完成，请稍后重新提交。",
        protocol=safe_protocol,
        allowed_actions=(UserAction.RETRY_REQUEST,),
        request_id=request_id,
        search_id=search_id,
        _factory_token=_PUBLIC_MESSAGE_FACTORY_TOKEN,
    )


def _status_fallback(
    request: FinalOutputRequestV1,
    protocol: RequestProtocol,
    actions: tuple[UserAction, ...],
    *,
    entry: CatalogEntry | None,
    validated_facts: Mapping[str, Any] | None = None,
    validated_contact: PublicContactV1 | None = None,
) -> PublicMessageV1:
    facts = validated_facts or {}
    contact = validated_contact
    kind = _safe_kind(request.kind)
    _validate_kind_protocol(kind, protocol)
    _validate_fallback_actions(protocol, actions, entry)
    _validate_semantics(protocol, actions, facts, entry=None, contact=contact)
    if protocol.status is RequestStatus.SUCCESS:
        key, text = "fallback.success", "这次操作已经完成。"
    elif protocol.status is RequestStatus.NEEDS_INPUT:
        key, text = "fallback.needs_input", "还需要你补充信息，请按可用操作继续。"
    elif protocol.status is RequestStatus.NO_MATCH:
        key, text = "fallback.no_match", "没有找到足够可靠的结果，请按可用操作继续。"
    elif protocol.status is RequestStatus.PARTIAL:
        if not _has_usable_result(facts):
            raise OutputContractError("fallback_partial_without_result")
        key, text = "fallback.partial", "已返回当前可用结果，但部分检查没有完成。"
    else:
        key, text = "fallback.error", "这次请求没有完成，请按可用操作重试。"
    return PublicMessageV1(
        schema_version=USER_OUTPUT_SCHEMA_VERSION,
        kind=kind,
        message_key=key,
        text=text,
        protocol=protocol,
        allowed_actions=actions,
        request_id=protocol.request_id,
        search_id=protocol.search_id,
        contact=contact,
        _factory_token=_PUBLIC_MESSAGE_FACTORY_TOKEN,
    )


def render_final_output(request: FinalOutputRequestV1) -> PublicMessageV1:
    """Validate and render one final user output, failing closed on any mismatch."""

    protocol_for_fallback = request.protocol if isinstance(request, FinalOutputRequestV1) else None
    entry: CatalogEntry | None = None
    fallback_authorized_entry: CatalogEntry | None = None
    validated_facts: Mapping[str, Any] | None = None
    validated_contact: PublicContactV1 | None = None
    try:
        if not isinstance(request, FinalOutputRequestV1):
            raise OutputContractError("request_type")
        if (
            type(request.schema_version) is not int
            or request.schema_version != USER_OUTPUT_SCHEMA_VERSION
        ):
            raise OutputContractError("schema_version")
        kind = _safe_kind(request.kind)
        if not isinstance(request.message_key, str):
            raise OutputContractError("message_key")
        entry = _CATALOG.get(request.message_key)
        actions = _normalize_actions(request.allowed_actions)
        if entry is None:
            raise OutputContractError("message_key_unknown")
        phase = _safe_phase(request.phase)
        protocol = _validate_protocol(request.protocol, entry)
        _validate_kind_protocol(kind, protocol)
        if FinalOutputKind(kind.value) not in entry.allowed_kinds:
            raise OutputContractError("message_kind_mismatch")
        if phase not in entry.allowed_phases:
            raise OutputContractError("message_phase_mismatch")
        facts = _normalize_facts(
            request.facts,
            required=entry.required_facts,
            optional=entry.optional_facts,
        )
        contact = _normalize_contact(request.contact)
        validated_facts = facts
        validated_contact = contact
        _validate_semantics(protocol, actions, facts, entry=entry, contact=contact)
        fallback_authorized_entry = entry
        notices = _render_notices(request.notice_keys, protocol)
        text = entry.renderer(facts, "")
        if notices:
            text = " ".join((text, *notices))
        if not text or len(text) > min(entry.max_chars + sum(len(item) + 1 for item in notices), _MAX_OUTPUT_CHARS):
            raise OutputContractError("rendered_length")
        if _CONTROL_RE.search(text) or _contains_sensitive_text(text):
            raise OutputContractError("rendered_sensitive")
        return PublicMessageV1(
            schema_version=USER_OUTPUT_SCHEMA_VERSION,
            kind=kind,
            message_key=request.message_key,
            text=text,
            protocol=protocol,
            allowed_actions=actions,
            request_id=protocol.request_id,
            search_id=protocol.search_id,
            contact=contact,
            _factory_token=_PUBLIC_MESSAGE_FACTORY_TOKEN,
        )
    except OutputContractError as exc:
        logger.warning("user_output_contract_violation category=%s", exc.category)
        hard_contradiction = exc.category in {
            "active_image_not_preserved",
            "action_not_permitted",
            "author_contact_not_available",
            "contact_not_allowed",
            "continuation_not_available",
            "continuation_state_mismatch",
            "crop_not_preserved",
            "fallback_partial_without_result",
            "global_search_not_available",
            "media_not_delivered",
            "mentioned_action_not_allowed",
            "message_kind_mismatch",
            "message_phase_mismatch",
            "next_action_missing",
            "no_units_mismatch",
            "page_index",
            "partial_without_result",
            "positive_fact_required",
            "protocol_action_not_allowed",
            "question_label",
            "rendered_sensitive",
            "remaining_state_mismatch",
            "retry_action_missing",
            "schema_version",
            "selection_count_missing",
            "selection_count_mismatch",
            "terminal_action_not_allowed",
            "unit_count_mismatch",
            "usable_result_missing",
        }
        if entry is not None and (
            entry.requires_delivery
            or entry.requires_usable_result
            or entry.requires_active_image
        ) and exc.category in {
            "chapter_not_registered",
            "fact_required",
            "fact_type",
            "fact_value",
        }:
            hard_contradiction = True
        if entry is not None and entry.requires_delivery and exc.category in {
            "fact_extra",
            "forbidden_fact",
        }:
            hard_contradiction = True
        try:
            if isinstance(request, FinalOutputRequestV1) and not hard_contradiction:
                protocol = _validate_protocol(request.protocol)
                actions = _normalize_actions(request.allowed_actions)
                return _status_fallback(
                    request,
                    protocol,
                    actions,
                    entry=fallback_authorized_entry,
                    validated_facts=validated_facts,
                    validated_contact=validated_contact,
                )
        except (OutputContractError, ValueError):
            pass
        return _service_fallback(protocol_for_fallback)
    except Exception:
        logger.error("user_output_unexpected_failure")
        return _service_fallback(protocol_for_fallback)


def render_progress_output(
    request: ProgressOutputRequestV1,
) -> PublicMessageV1:
    """Render registered progress text; stream/session code owns monotonic ordering."""

    request_id = ""
    search_id = ""
    sequence: int | None = None
    try:
        if not isinstance(request, ProgressOutputRequestV1):
            raise OutputContractError("progress_request_type")
        if (
            type(request.schema_version) is not int
            or request.schema_version != USER_OUTPUT_SCHEMA_VERSION
        ):
            raise OutputContractError("progress_schema_version")
        request_id = _safe_public_id(request.request_id, required=True)
        search_id = _safe_public_id(request.search_id)
        if isinstance(request.sequence, bool) or not isinstance(request.sequence, int):
            raise OutputContractError("progress_sequence")
        if not 1 <= request.sequence <= _MAX_PROGRESS_SEQUENCE:
            raise OutputContractError("progress_sequence")
        sequence = request.sequence
        entry = _PROGRESS_CATALOG.get(request.progress_key)
        if entry is None:
            raise OutputContractError("progress_key")
        facts = _normalize_facts(
            request.facts,
            required=entry.required_facts,
            optional=entry.optional_facts,
        )
        text = entry.renderer(facts)
        if not text or len(text) > entry.max_chars or _CONTROL_RE.search(text) or _contains_sensitive_text(text):
            raise OutputContractError("progress_rendered_text")
        return PublicMessageV1(
            schema_version=USER_OUTPUT_SCHEMA_VERSION,
            kind=OutputKind.PROGRESS,
            message_key=request.progress_key,
            text=text,
            protocol=None,
            allowed_actions=(),
            request_id=request_id,
            search_id=search_id,
            sequence=sequence,
            stage=entry.stage,
            _factory_token=_PUBLIC_MESSAGE_FACTORY_TOKEN,
        )
    except OutputContractError as exc:
        logger.warning("user_output_progress_violation category=%s", exc.category)
        if not request_id or sequence is None:
            raise OutputContractError("progress_not_publishable") from None
    except Exception:
        logger.error("user_output_progress_unexpected_failure")
        raise OutputContractError("progress_not_publishable") from None
    return PublicMessageV1(
        schema_version=USER_OUTPUT_SCHEMA_VERSION,
        kind=OutputKind.PROGRESS,
        message_key="progress.safe",
        text="正在处理，请稍候。",
        protocol=None,
        allowed_actions=(),
        request_id=request_id,
        search_id=search_id,
        sequence=sequence,
        stage="processing",
        _factory_token=_PUBLIC_MESSAGE_FACTORY_TOKEN,
    )


def catalog_message_keys() -> tuple[str, ...]:
    """Expose immutable catalog keys for contract tests and migration tooling."""

    return tuple(_CATALOG)


def progress_message_keys() -> tuple[str, ...]:
    return tuple(_PROGRESS_CATALOG)


def notice_keys() -> tuple[str, ...]:
    return tuple(_NOTICES)


def validate_catalog_configuration() -> None:
    """Fail fast when a developer introduces an unreachable or unsafe entry."""

    known_facts = set(_FACT_VALIDATORS) | {"question_label"}
    for key, entry in _CATALOG.items():
        if not _MESSAGE_KEY_RE.fullmatch(key):
            raise RuntimeError("invalid user output catalog key")
        if (
            not entry.allowed_kinds
            or not entry.allowed_statuses
            or not entry.allowed_layers
            or not entry.allowed_phases
        ):
            raise RuntimeError("empty user output catalog constraint")
        if not entry.allowed_phases <= _KNOWN_PHASES:
            raise RuntimeError("unknown user output phase")
        if entry.required_facts & entry.optional_facts:
            raise RuntimeError("overlapping user output facts")
        if not (entry.required_facts | entry.optional_facts) <= known_facts:
            raise RuntimeError("unknown user output fact")
        if not entry.allowed_codes or not 1 <= entry.max_chars <= _MAX_OUTPUT_CHARS:
            raise RuntimeError("invalid user output catalog limits")
        if not entry.mentioned_actions <= entry.permitted_actions:
            raise RuntimeError("user output action bounds conflict")
        if entry.terminal_without_action and entry.mentioned_actions:
            raise RuntimeError("terminal output mentions action")
        for code in entry.allowed_codes:
            rule = _protocol_rule_for(code)
            if rule is None:
                raise RuntimeError("unknown user output protocol rule")
            if rule.status not in entry.allowed_statuses or rule.layer not in entry.allowed_layers:
                raise RuntimeError("unreachable user output catalog entry")
            mapped = _PROTOCOL_ACTION_MAP.get(rule.action)
            if mapped is not None and mapped not in entry.permitted_actions:
                raise RuntimeError("protocol action not permitted by catalog")
            protocol_shape = RequestProtocol(
                status=rule.status,
                layer=rule.layer,
                code=code,
                retryable=rule.retryable,
                action=rule.action,
            )
            try:
                narrowed_actions = _permitted_actions_for_protocol(entry, protocol_shape)
            except OutputContractError as exc:
                raise RuntimeError("ambiguous user output retry action") from exc
            if not entry.mentioned_actions <= narrowed_actions:
                raise RuntimeError("unreachable user output action requirement")
            if entry.terminal_without_action and rule.action is not RequestAction.NONE:
                raise RuntimeError("terminal output has protocol action")
    for key, notice in _NOTICES.items():
        if not _MESSAGE_KEY_RE.fullmatch(key) or not notice.text or len(notice.text) > 100:
            raise RuntimeError("invalid user output notice")
        if _contains_sensitive_text(notice.text) or _CONTROL_RE.search(notice.text):
            raise RuntimeError("unsafe user output notice")
        for code in notice.allowed_codes:
            rule = _protocol_rule_for(code)
            if rule is None or rule.status not in notice.allowed_statuses:
                raise RuntimeError("unreachable user output notice")
    for key, entry in _PROGRESS_CATALOG.items():
        if not _MESSAGE_KEY_RE.fullmatch(key):
            raise RuntimeError("invalid progress catalog key")
        if entry.required_facts & entry.optional_facts:
            raise RuntimeError("overlapping progress facts")
        if not (entry.required_facts | entry.optional_facts) <= known_facts:
            raise RuntimeError("unknown progress fact")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", entry.stage):
            raise RuntimeError("invalid progress stage")
        if not 1 <= entry.max_chars <= 120:
            raise RuntimeError("invalid progress limit")


validate_catalog_configuration()


__all__ = [
    "FinalOutputKind",
    "FinalOutputRequestV1",
    "OutputContractError",
    "OutputKind",
    "ProgressOutputRequestV1",
    "PublicContactV1",
    "PublicMessageV1",
    "USER_OUTPUT_SCHEMA_VERSION",
    "UserAction",
    "catalog_message_keys",
    "notice_keys",
    "progress_message_keys",
    "render_final_output",
    "render_progress_output",
    "validate_catalog_configuration",
]

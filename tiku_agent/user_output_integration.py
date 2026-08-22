"""Pure adapters from A2/A3 state into the reviewed public-output catalog.

This module is deliberately isolated from the runtimes.  It consumes only
structured state and protocol data, never a legacy ``AgentResponse.text``.
Media delivery is finalized separately so candidate ranks cannot silently
shift when one image fails to persist.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from tiku_agent.user_output import (
    USER_OUTPUT_SCHEMA_VERSION,
    FinalOutputKind,
    FinalOutputRequestV1,
    PublicContactV1,
    PublicMessageV1,
    UserAction,
    render_final_output,
)
from tiku_shared.chapter_catalog import (
    CHAPTER_DEFINITIONS,
    UNSUPPORTED_TOPIC_DEFINITIONS,
)
from tiku_shared.request_protocol import (
    RequestProtocol,
    RequestStatus,
)


MediaPolicy = Literal["none", "candidate_set", "delivery"]

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
_A2_GUIDANCE_PHASES = frozenset(
    {
        "IDLE",
        "WAIT_CHAPTER",
        "WAIT_QUESTION_CHOICE",
        "WAIT_CANDIDATE_CHOICE",
        "ANSWERED",
        "NO_MATCH",
        "ERROR",
    }
)
_CHAPTER_SAVED_PHASES = frozenset(
    {
        "PROCESSING",
        "WAIT_CHAPTER",
        "WAIT_CANDIDATE_CHOICE",
        "READY_TO_ROUTE",
        "READY_FOR_SEARCH",
    }
)
_A3_GUIDANCE_PHASES = frozenset(
    {"WAIT_UNIT_SELECTION", "CROP_REQUIRED", "A2_ACTIVE", "ERROR", "COMPLETE"}
)

_PUBLIC_CHAPTER_NAMES: dict[str, str] = {}
for _definition in CHAPTER_DEFINITIONS:
    for _value in (
        _definition.storage_key,
        _definition.topic_id,
        _definition.display_name,
    ):
        _PUBLIC_CHAPTER_NAMES[_value] = _definition.display_name
for _definition in UNSUPPORTED_TOPIC_DEFINITIONS:
    for _value in (_definition.topic_id, _definition.display_name):
        _PUBLIC_CHAPTER_NAMES[_value] = _definition.display_name

_SUPPORTED_CHAPTERS = tuple(item.display_name for item in CHAPTER_DEFINITIONS)

_NOTICE_BY_CODE = MappingProxyType(
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

_A1_REASONS = frozenset(
    {
        "unrelated_image",
        "image_unclear",
        "structure_incomplete",
        "load_incomplete",
        "original_structure_missing",
        "unsupported_content",
    }
)
_CROP_REASONS = frozenset(
    {
        "image_unclear",
        "region_missing",
        "invalid_crop",
        "multiple_questions",
        "wrong_question",
        "multiple_diagrams",
        "loads_incomplete",
        "structure_incomplete",
        "no_external_load",
        "unconfirmed",
    }
)

_CONVERSATION_VARIANTS = MappingProxyType(
    {
        "greeting": "conversation.greeting",
        "courtesy": "conversation.courtesy",
        "farewell": "conversation.farewell",
        "identity": "conversation.identity",
        "capability": "conversation.capability",
        "supported_chapters": "conversation.supported_chapters",
        "workflow": "conversation.workflow",
        "general": "conversation.general",
    }
)
_SAFE_CONVERSATION_KEYS = frozenset(_CONVERSATION_VARIANTS.values())
_STATE_DERIVED_CONVERSATION_CODES = frozenset(
    {"CHAPTER_REQUIRED", "NO_MATCH", "AGENT_FAILED"}
)

_CANDIDATE_CODES = frozenset(
    {
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
    }
)
_RETRYABLE_SEARCH_CODES = frozenset(
    {
        "AGENT_FAILED",
        "ANSWER_LOOKUP_FAILED",
        "COARSE_SEARCH_FAILED",
        "GLOBAL_SEARCH_FAILED",
        "IMAGE_ANALYSIS_FAILED",
        "MULTI_DETAIL_FAILED",
        "RERANK_FAILED",
        "TOOL_FAILED",
    }
)

_SYSTEM_OUTPUTS: Mapping[
    str, tuple[str, tuple[UserAction, ...], FinalOutputKind]
] = MappingProxyType(
    {
        "LOGIN_REQUIRED": (
            "system.login.required",
            (UserAction.RELOGIN,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "INVITE_INVALID": (
            "system.login.required",
            (UserAction.RELOGIN,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "LOGIN_EXPIRED": (
            "system.login.required",
            (UserAction.RELOGIN,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "GLOBAL_DAILY_QUOTA_EXCEEDED": (
            "system.quota.unavailable",
            (),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "INVITE_DAILY_QUOTA_EXCEEDED": (
            "system.quota.unavailable",
            (),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "INVITE_IDENTITY_MISSING": (
            "system.quota.identity_missing",
            (UserAction.RELOGIN,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "QUEUE_FULL": (
            "system.queue.full",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "QUEUE_TIMEOUT": (
            "system.queue.full",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "UPLOAD_REQUIRED": (
            "system.upload.required",
            (UserAction.RETRY_UPLOAD,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "UPLOAD_TOO_LARGE": (
            "system.upload.required",
            (UserAction.RETRY_UPLOAD,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "UPLOAD_UNSUPPORTED_FORMAT": (
            "system.upload.required",
            (UserAction.RETRY_UPLOAD,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "UPLOAD_DECODE_FAILED": (
            "system.upload.required",
            (UserAction.RETRY_UPLOAD,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "UPLOAD_PERSIST_FAILED": (
            "system.upload.persist_failed",
            (UserAction.RETRY_UPLOAD,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "NETWORK_UNAVAILABLE": (
            "system.network.unavailable",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "REQUEST_TIMEOUT": (
            "system.network.unavailable",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "SESSION_EXPIRED": (
            "system.session.expired",
            (UserAction.NEW_CHAT,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "MEDIA_NOT_FOUND": (
            "system.media.not_found",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "FEEDBACK_INVALID": (
            "system.feedback.invalid",
            (UserAction.RETRY_FEEDBACK,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "FEEDBACK_TOO_LARGE": (
            "system.feedback.invalid",
            (UserAction.RETRY_FEEDBACK,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "FEEDBACK_SAVE_FAILED": (
            "system.feedback.save_failed",
            (UserAction.RETRY_FEEDBACK,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
        "SERVICE_UNAVAILABLE": (
            "system.service.unavailable",
            (UserAction.RETRY_REQUEST,),
            FinalOutputKind.TRANSPORT_ERROR,
        ),
    }
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(_freeze(item) for item in sorted(value, key=repr))
    return value


@dataclass(frozen=True)
class OutputDraftV1:
    """Immutable, non-rendered public-output decision."""

    message_key: str
    phase: str
    facts: Mapping[str, Any]
    allowed_actions: tuple[UserAction | str, ...]
    notice_keys: tuple[str, ...] = ()
    kind: FinalOutputKind | str = FinalOutputKind.RESULT
    media_policy: MediaPolicy = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.message_key, str) or not self.message_key:
            raise ValueError("message_key is required")
        if not isinstance(self.phase, str) or self.phase not in _KNOWN_PHASES:
            raise ValueError("phase is invalid")
        if not isinstance(self.facts, Mapping):
            raise ValueError("facts must be a mapping")
        try:
            kind = self.kind if isinstance(self.kind, FinalOutputKind) else FinalOutputKind(self.kind)
            actions = tuple(
                item if isinstance(item, UserAction) else UserAction(item)
                for item in self.allowed_actions
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("draft enum value is invalid") from exc
        if len(actions) != len(set(actions)):
            raise ValueError("draft actions must be unique")
        if self.media_policy not in {"none", "candidate_set", "delivery"}:
            raise ValueError("media_policy is invalid")
        if not isinstance(self.notice_keys, tuple) or any(
            not isinstance(item, str) or not item for item in self.notice_keys
        ):
            raise ValueError("notice_keys must be a tuple of keys")
        object.__setattr__(self, "facts", _freeze(dict(self.facts)))
        object.__setattr__(self, "allowed_actions", actions)
        object.__setattr__(self, "notice_keys", tuple(dict.fromkeys(self.notice_keys)))
        object.__setattr__(self, "kind", kind)


def _draft(
    message_key: str,
    phase: str,
    *,
    facts: Mapping[str, Any] | None = None,
    actions: Sequence[UserAction] = (),
    notices: Sequence[str] = (),
    kind: FinalOutputKind = FinalOutputKind.RESULT,
    media_policy: MediaPolicy = "none",
) -> OutputDraftV1:
    return OutputDraftV1(
        message_key=message_key,
        phase=phase,
        facts=dict(facts or {}),
        allowed_actions=tuple(actions),
        notice_keys=tuple(notices),
        kind=kind,
        media_policy=media_policy,
    )


def _clean_intent(value: Any) -> str:
    return str(value or "").strip().lower()


def _phase(state: Mapping[str, Any], default: str = "IDLE") -> str:
    value = state.get("phase")
    return value if isinstance(value, str) and value in _KNOWN_PHASES else default


def _positive_int(value: Any) -> int:
    return value if type(value) is int and value > 0 else 0


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _count(state: Mapping[str, Any], key: str, sequence_key: str) -> int:
    explicit = _nonnegative_int(state.get(key))
    return explicit if explicit is not None else len(_sequence(state.get(sequence_key)))


def _public_chapter(value: Any) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    return _PUBLIC_CHAPTER_NAMES.get(clean, "")


def _chapter_from_state(state: Mapping[str, Any]) -> str:
    for key in (
        "current_chapter",
        "chapter",
        "pending_chapter",
        "chapter_scope_topic_id",
    ):
        chapter = _public_chapter(state.get(key))
        if chapter:
            return chapter
    return ""


def _saved_chapter_from_state(state: Mapping[str, Any]) -> str:
    """Prefer the newly saved next-search chapter over the previous search."""

    return _public_chapter(state.get("pending_chapter")) or _chapter_from_state(state)


def _source_chapters(state: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    values.extend(_sequence(state.get("source_chapters")))
    for candidate in _sequence(state.get("candidates")):
        if isinstance(candidate, Mapping):
            values.extend(
                candidate.get(key)
                for key in ("chapter", "chapter_name", "source_chapter")
            )
    result: list[str] = []
    for value in values:
        public = _public_chapter(value)
        if public and public not in result:
            result.append(public)
    return tuple(result)


def _notice_keys(protocol: RequestProtocol) -> tuple[str, ...]:
    key = _NOTICE_BY_CODE.get(protocol.code)
    return (key,) if key else ()


def _final_notice_keys(
    draft: OutputDraftV1,
    protocol: RequestProtocol,
) -> tuple[str, ...]:
    """Keep explicit draft notices only when valid for the bound protocol."""

    valid = set(_notice_keys(protocol))
    return tuple(
        dict.fromkeys(
            (
                *(key for key in draft.notice_keys if key in valid),
                *valid,
            )
        )
    )


def _system_draft(
    state: Mapping[str, Any], protocol: RequestProtocol
) -> OutputDraftV1 | None:
    record = _SYSTEM_OUTPUTS.get(protocol.code)
    if record is None:
        return None
    message_key, actions, kind = record
    facts: dict[str, Any] = {}
    retry_after = _positive_int(state.get("retry_after_seconds"))
    if protocol.code in {"QUEUE_FULL", "QUEUE_TIMEOUT"} and retry_after <= 3600:
        if retry_after:
            facts["retry_after_seconds"] = retry_after
    return _draft(
        message_key,
        _phase(state),
        facts=facts,
        actions=actions,
        kind=kind,
    )


def _a2_guidance(state: Mapping[str, Any]) -> tuple[str, dict[str, bool], tuple[UserAction, ...]]:
    phase = _phase(state)
    if phase not in _A2_GUIDANCE_PHASES:
        phase = "IDLE"
    facts: dict[str, bool] = {}
    actions: list[UserAction]
    if phase == "WAIT_CHAPTER":
        actions = [UserAction.CHANGE_CHAPTER]
        if state.get("global_search_offered") is True:
            actions.append(UserAction.GLOBAL_SEARCH)
            facts["global_search_offered"] = True
    elif phase == "WAIT_QUESTION_CHOICE":
        actions = [UserAction.SELECT_QUESTION]
    elif phase == "WAIT_CANDIDATE_CHOICE":
        actions = [UserAction.SELECT_CANDIDATE]
        if state.get("continuation_available") is True:
            actions.append(UserAction.CONTINUE_SEARCH)
            facts["continuation_available"] = True
    elif phase == "ANSWERED":
        actions = [
            UserAction.SHOW_CANDIDATES,
            UserAction.RESEND_ANSWER,
            UserAction.REPORT_ANSWER_MISMATCH,
            UserAction.UPLOAD_IMAGE,
        ]
    elif phase == "NO_MATCH":
        actions = [UserAction.CHANGE_CHAPTER, UserAction.UPLOAD_IMAGE]
        if state.get("global_search_offered") is True:
            actions.append(UserAction.GLOBAL_SEARCH)
            facts["global_search_offered"] = True
    else:
        actions = [UserAction.UPLOAD_IMAGE]
    return phase, facts, tuple(actions)


def _candidate_draft(
    state: Mapping[str, Any],
    protocol: RequestProtocol,
    *,
    recalled: bool = False,
    force_global: bool = False,
) -> OutputDraftV1:
    count = _count(state, "candidate_count", "candidates")
    facts: dict[str, Any] = {"candidate_count": count, "has_usable_result": count > 0}
    selected_question = _positive_int(state.get("selected_question"))
    if selected_question:
        facts.update(
            {"question_label": f"图片第 {selected_question} 题", "page_index": selected_question}
        )
    actions = [UserAction.SELECT_CANDIDATE]
    if protocol.retryable:
        actions.append(UserAction.RETRY_SEARCH)
    if state.get("continuation_available") is True and not recalled:
        facts["continuation_available"] = True
        actions.append(UserAction.CONTINUE_SEARCH)
    sources = _source_chapters(state)
    if protocol.code == "GLOBAL_CANDIDATES_FOUND" and not sources:
        return _draft(
            "system.service.unavailable",
            "ERROR",
            actions=(UserAction.RETRY_REQUEST,),
            kind=FinalOutputKind.TRANSPORT_ERROR,
        )
    is_global = bool(sources) and (
        force_global
        or protocol.code == "GLOBAL_CANDIDATES_FOUND"
        or str(state.get("current_route") or state.get("route") or "").upper() == "GLOBAL"
    )
    if recalled:
        key = "search.candidates.recalled"
        facts.pop("has_usable_result", None)
        facts.pop("question_label", None)
        facts.pop("page_index", None)
        facts.pop("continuation_available", None)
        actions = [UserAction.SELECT_CANDIDATE]
    elif is_global and protocol.status is RequestStatus.SUCCESS:
        key = "search.global.candidates.ready"
        facts = {"candidate_count": count, "source_chapters": sources}
        actions = [UserAction.SELECT_CANDIDATE]
    else:
        key = "search.candidates.ready"
    return _draft(
        key,
        "WAIT_CANDIDATE_CHOICE",
        facts=facts,
        actions=actions,
        notices=_notice_keys(protocol),
        media_policy="candidate_set",
    )


def _answer_draft(
    state: Mapping[str, Any], *, resent: bool = False
) -> OutputDraftV1:
    count = _count(state, "delivered_image_count", "last_answer_paths")
    facts: dict[str, Any] = {"delivered_image_count": count, "has_usable_result": count > 0}
    selected_question = _positive_int(state.get("selected_question"))
    if selected_question and not resent:
        facts.update(
            {"question_label": f"图片第 {selected_question} 题", "page_index": selected_question}
        )
    if resent:
        actions = (UserAction.SHOW_CANDIDATES, UserAction.REPORT_ANSWER_MISMATCH)
        key = "search.answer.resent"
    else:
        actions = (
            UserAction.SHOW_CANDIDATES,
            UserAction.RESEND_ANSWER,
            UserAction.REPORT_ANSWER_MISMATCH,
            UserAction.UPLOAD_IMAGE,
        )
        key = "search.answer.ready"
    return _draft(
        key,
        "ANSWERED",
        facts=facts,
        actions=actions,
        media_policy="delivery",
    )


def _no_match_draft(
    state: Mapping[str, Any],
    protocol: RequestProtocol,
    author_contact_available: bool,
    *,
    force_global: bool = False,
) -> OutputDraftV1:
    route = str(state.get("current_route") or state.get("route") or "").strip().upper()
    chapter = _chapter_from_state(state)
    global_result = force_global or route == "GLOBAL" or protocol.code in {
        "NO_GLOBAL_COARSE_CANDIDATES",
        "NO_GLOBAL_RELIABLE_CANDIDATES",
    }
    facts: dict[str, Any] = {}
    actions = [UserAction.CHANGE_CHAPTER, UserAction.RETRY_UPLOAD]
    if global_result or not chapter:
        key = "search.no_match.global"
    else:
        key = "search.no_match.chapter"
        facts["chapter_name"] = chapter
        if state.get("global_search_offered") is True:
            facts["global_search_offered"] = True
            actions.append(UserAction.GLOBAL_SEARCH)
    if author_contact_available:
        facts["author_contact_available"] = True
        actions.append(UserAction.CONTACT_AUTHOR)
    return _draft(key, "NO_MATCH", facts=facts, actions=actions)


def build_a2_output_draft(
    intent: str,
    state: Mapping[str, Any],
    protocol: RequestProtocol,
    author_contact_available: bool = False,
    variant: str = "",
) -> OutputDraftV1:
    """Build one A2 output draft from reviewed structured fields only."""

    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    if not isinstance(protocol, RequestProtocol):
        raise TypeError("protocol must be RequestProtocol")
    intent_key = _clean_intent(intent)
    variant_key = _clean_intent(variant)
    phase = _phase(state)

    if protocol.code == "BANK_ROUTE_FAILED":
        return _draft(
            "search.failed.nonretryable",
            "ERROR",
            actions=(UserAction.UPLOAD_IMAGE,),
        )
    if protocol.code == "EXTERNAL_LOAD_NOT_FOUND":
        return _draft(
            "triage.a1.no_external_load",
            "PROCESSING" if phase != "COMPLETE" else "COMPLETE",
            actions=(UserAction.RETRY_UPLOAD,),
        )
    if protocol.code == "TRIAGE_A1_STOPPED" or intent_key in {
        "image_triage_stop",
        "triage_a1_stop",
    }:
        triage_phase = "COMPLETE" if phase == "COMPLETE" else "PROCESSING"
        if variant_key == "no_external_load":
            return _draft(
                "triage.a1.no_external_load",
                triage_phase,
                actions=(UserAction.RETRY_UPLOAD,),
            )
        if variant_key in _A1_REASONS:
            return _draft(
                "triage.a1.reasoned",
                triage_phase,
                facts={"a1_reason": variant_key},
                actions=(UserAction.RETRY_UPLOAD,),
            )
        return _draft(
            "triage.a1.fallback",
            triage_phase,
            actions=(UserAction.RETRY_UPLOAD,),
        )

    system = _system_draft(state, protocol)
    if system is not None:
        return system

    # A newly selected chapter starts the next search.  It must win over a
    # retained ANSWERED phase from the preceding search, which has no media to
    # deliver for this request.
    saved_chapter = _saved_chapter_from_state(state)
    if intent_key == "set_chapter" and saved_chapter:
        saved_phase = (
            phase if phase in _CHAPTER_SAVED_PHASES else "READY_TO_ROUTE"
        )
        return _draft(
            "search.chapter.saved",
            saved_phase,
            facts={"chapter_name": saved_chapter},
        )

    if intent_key == "safe_answer":
        key = _CONVERSATION_VARIANTS.get(variant_key, "conversation.general")
        actions = (UserAction.UPLOAD_IMAGE,) if key == "conversation.greeting" else ()
        return _draft(key, phase, actions=actions)
    if intent_key == "greeting":
        return _draft(
            "conversation.greeting", phase, actions=(UserAction.UPLOAD_IMAGE,)
        )
    if intent_key in {"small_talk", "courtesy"}:
        key = "conversation.farewell" if variant_key == "farewell" else "conversation.courtesy"
        return _draft(key, phase)
    if intent_key in {"capability_help", "capability"}:
        return _draft("conversation.capability", phase)

    if intent_key == "cancel":
        return _draft(
            "search.cancelled", "CANCELLED", actions=(UserAction.UPLOAD_IMAGE,)
        )
    if protocol.code == "CANDIDATE_LIST_UNAVAILABLE":
        candidate_phase = phase if phase in {"IDLE", "WAIT_CANDIDATE_CHOICE", "NO_MATCH", "ERROR"} else "IDLE"
        return _draft(
            "search.candidates.unavailable",
            candidate_phase,
            actions=(UserAction.RETRY_UPLOAD,),
        )
    if protocol.code == "ANSWER_FILES_NOT_FOUND":
        answer_phase = "WAIT_CANDIDATE_CHOICE" if phase != "NO_MATCH" else "NO_MATCH"
        return _draft(
            "search.answer.missing",
            answer_phase,
            actions=(UserAction.SHOW_CANDIDATES, UserAction.SELECT_CANDIDATE),
        )
    if protocol.code == "NO_MORE_CANDIDATES":
        facts: dict[str, Any] = {"continuation_available": False}
        actions = [UserAction.CHANGE_CHAPTER, UserAction.RETRY_UPLOAD]
        if state.get("global_search_offered") is True:
            facts["global_search_offered"] = True
            actions.append(UserAction.GLOBAL_SEARCH)
        if author_contact_available:
            facts["author_contact_available"] = True
            actions.append(UserAction.CONTACT_AUTHOR)
        return _draft(
            "search.candidates.rejected",
            "NO_MATCH" if phase == "NO_MATCH" else "WAIT_CANDIDATE_CHOICE",
            facts=facts,
            actions=actions,
        )

    # Recovery/clarification protocols describe the current request, not a
    # replay of the candidates/questions already held in state.  They must win
    # before phase-based result branches below.
    if intent_key == "reject" or protocol.code == "ACTION_NOT_ALLOWED":
        guidance_phase, facts, actions = _a2_guidance(state)
        if guidance_phase == "IDLE":
            guidance_phase = "WAIT_CHAPTER"
            actions = (UserAction.CHANGE_CHAPTER,)
        return _draft(
            "conversation.action_rejected",
            guidance_phase,
            facts=facts,
            actions=actions,
        )
    if intent_key == "clarification" or protocol.code in {
        "MESSAGE_INVALID",
        "CLARIFICATION_REQUIRED",
        "QUESTION_INDEX_REQUIRED",
        "CANDIDATE_RANK_REQUIRED",
        "SELECTION_OUT_OF_RANGE",
        "LOAD_ROUTE_NEEDS_REVIEW",
    }:
        guidance_phase, facts, actions = _a2_guidance(state)
        return _draft(
            "search.clarification.required",
            guidance_phase,
            facts=facts,
            actions=actions,
        )

    if protocol.code == "UNKNOWN_CHAPTER":
        chapter = _chapter_from_state(state)
        if chapter and phase == "WAIT_CHAPTER":
            return _draft(
                "search.chapter.unsupported",
                "WAIT_CHAPTER",
                facts={"chapter_name": chapter, "supported_chapters": _SUPPORTED_CHAPTERS},
                actions=(UserAction.CHANGE_CHAPTER,),
            )
        return _draft(
            "search.chapter.required",
            "WAIT_CHAPTER",
            facts={"supported_chapters": _SUPPORTED_CHAPTERS},
            actions=(UserAction.CHANGE_CHAPTER,),
        )
    if protocol.code == "CHAPTER_REQUIRED":
        facts: dict[str, Any] = {"supported_chapters": _SUPPORTED_CHAPTERS}
        actions = [UserAction.CHANGE_CHAPTER]
        if state.get("global_search_offered") is True:
            facts["global_search_offered"] = True
            actions.append(UserAction.GLOBAL_SEARCH)
        return _draft(
            "search.chapter.required",
            "WAIT_CHAPTER",
            facts=facts,
            actions=actions,
        )
    if protocol.code in _RETRYABLE_SEARCH_CODES:
        return _draft(
            "search.failed.retryable",
            "ERROR",
            facts={"active_image_preserved": bool(state.get("current_image_path") or state.get("image_path") or state.get("has_active_image"))},
            actions=(UserAction.RETRY_SEARCH,),
        )

    if intent_key == "show_candidates":
        return _candidate_draft(state, protocol, recalled=True)
    if intent_key == "reject_candidates":
        continuation = state.get("continuation_available") is True
        if continuation:
            return _draft(
                "search.candidates.rejected_more",
                "WAIT_CANDIDATE_CHOICE",
                facts={"continuation_available": True},
                actions=(UserAction.CONTINUE_SEARCH, UserAction.CHANGE_CHAPTER),
            )
        return _draft(
            "search.candidates.rejected",
            "NO_MATCH" if phase == "NO_MATCH" else "WAIT_CANDIDATE_CHOICE",
            facts={"continuation_available": False},
            actions=(UserAction.CHANGE_CHAPTER, UserAction.RETRY_UPLOAD),
        )
    if intent_key == "report_answer_mismatch":
        actions: list[UserAction] = [UserAction.SHOW_CANDIDATES]
        facts: dict[str, Any] = {}
        if state.get("continuation_available") is True:
            facts["continuation_available"] = True
            actions.append(UserAction.CONTINUE_SEARCH)
        if _count(state, "candidate_count", "candidates"):
            actions.append(UserAction.SELECT_CANDIDATE)
        return _draft(
            "search.answer.mismatch", "ANSWERED", facts=facts, actions=actions
        )
    if intent_key == "resend_answer":
        return _answer_draft(state, resent=True)
    if phase == "ANSWERED" and (
        intent_key == "select_candidate"
        or protocol.code in {"ANSWER_FILES_FOUND", "REQUEST_SUCCEEDED"}
    ):
        return _answer_draft(state)

    candidate_count = _count(state, "candidate_count", "candidates")
    if phase == "WAIT_CANDIDATE_CHOICE" and (
        candidate_count > 0
        or protocol.code in _CANDIDATE_CODES
        or intent_key in {"search_image", "global_search", "select_question", "continue_search"}
    ):
        return _candidate_draft(
            state,
            protocol,
            force_global=intent_key == "global_search",
        )
    if phase == "WAIT_QUESTION_CHOICE" or protocol.code in {
        "QUESTION_UNITS_PREPARED",
        "MULTI_CROPS_UNAVAILABLE",
    }:
        count = _count(state, "question_count", "questions")
        actions = [UserAction.SELECT_QUESTION]
        if protocol.retryable:
            actions.append(UserAction.RETRY_SEARCH)
        return _draft(
            "search.questions.ready",
            "WAIT_QUESTION_CHOICE",
            facts={"question_count": count},
            actions=actions,
            notices=_notice_keys(protocol),
        )
    if protocol.status is RequestStatus.NO_MATCH or phase == "NO_MATCH":
        return _no_match_draft(
            state,
            protocol,
            bool(author_contact_available),
            force_global=intent_key == "global_search",
        )

    if protocol.code == "REQUEST_OUT_OF_SCOPE" or intent_key == "out_of_scope":
        chapter = _chapter_from_state(state)
        if chapter and phase == "WAIT_CHAPTER":
            return _draft(
                "search.chapter.unsupported",
                "WAIT_CHAPTER",
                facts={"chapter_name": chapter, "supported_chapters": _SUPPORTED_CHAPTERS},
                actions=(UserAction.CHANGE_CHAPTER,),
            )
        return _draft(
            "conversation.out_of_scope",
            phase,
            actions=(UserAction.UPLOAD_IMAGE,),
        )
    if phase == "WAIT_CHAPTER":
        facts: dict[str, Any] = {"supported_chapters": _SUPPORTED_CHAPTERS}
        actions = [UserAction.CHANGE_CHAPTER]
        if state.get("global_search_offered") is True:
            facts["global_search_offered"] = True
            actions.append(UserAction.GLOBAL_SEARCH)
        return _draft(
            "search.chapter.required",
            "WAIT_CHAPTER",
            facts=facts,
            actions=actions,
        )
    return _draft("conversation.general", phase)


_UNIT_BINDING_FIELDS = frozenset({"display_label", "question_label", "page_index"})


def _unit_binding_label(unit: Mapping[str, Any]) -> str:
    return str(unit.get("display_label") or unit.get("question_label") or "").strip()


def _has_unit_binding_conflict(a3: Mapping[str, Any]) -> bool:
    canonical_by_id = {
        str(unit.get("unit_id") or "").strip(): unit
        for unit in _sequence(a3.get("units"))
        if isinstance(unit, Mapping) and str(unit.get("unit_id") or "").strip()
    }
    for key in ("selected_unit", "stopped_unit", "previous_selected_unit"):
        selected = a3.get(key)
        if not isinstance(selected, Mapping) or not selected:
            continue
        unit_id = str(selected.get("unit_id") or "").strip()
        canonical = canonical_by_id.get(unit_id)
        if canonical is None:
            continue
        selected_label = _unit_binding_label(selected)
        canonical_label = _unit_binding_label(canonical)
        if selected_label and canonical_label and selected_label != canonical_label:
            return True
        if (
            "page_index" in selected
            and selected.get("page_index") is not None
            and "page_index" in canonical
            and canonical.get("page_index") is not None
            and selected.get("page_index") != canonical.get("page_index")
        ):
            return True
    return False


def _unit_record(a3: Mapping[str, Any], *, stopped: bool = False) -> Mapping[str, Any]:
    keys = (
        ("stopped_unit", "previous_selected_unit", "selected_unit")
        if stopped
        else ("selected_unit",)
    )
    selected: Mapping[str, Any] = {}
    for key in keys:
        value = a3.get(key)
        if isinstance(value, Mapping) and value:
            selected = value
            break
    unit_id = str(selected.get("unit_id") or "").strip()
    for unit in _sequence(a3.get("units")):
        if not isinstance(unit, Mapping):
            continue
        if unit_id and str(unit.get("unit_id") or "").strip() == unit_id:
            merged = dict(unit)
            merged.update(dict(selected))
            for field in _UNIT_BINDING_FIELDS:
                if (
                    field in unit
                    and unit.get(field) is not None
                    and unit.get(field) != ""
                ):
                    merged[field] = unit[field]
            return merged
        if not unit_id and unit.get("selected") is True:
            return unit
    return selected


def _unit_facts(a3: Mapping[str, Any], *, stopped: bool = False) -> dict[str, Any]:
    unit = _unit_record(a3, stopped=stopped)
    page_index = _positive_int(unit.get("page_index"))
    label = str(unit.get("display_label") or unit.get("question_label") or "").strip()
    facts: dict[str, Any] = {}
    if label:
        facts["question_label"] = label
    elif page_index:
        facts["question_label"] = f"图片第 {page_index} 题"
    if page_index:
        facts["page_index"] = page_index
    return facts


def _previous_unit_facts(a3: Mapping[str, Any]) -> dict[str, Any]:
    current = _unit_facts(a3)
    previous = _unit_facts(a3, stopped=True)
    previous_label = previous.get("question_label")
    if (
        isinstance(previous_label, str)
        and previous_label
        and previous_label != current.get("question_label")
    ):
        return {"previous_question_label": previous_label}
    return {}


def _a3_counts(a3: Mapping[str, Any]) -> tuple[int, int, int, int]:
    units = [item for item in _sequence(a3.get("units")) if isinstance(item, Mapping)]
    searchable = [
        item
        for item in units
        if str(item.get("searchability") or "searchable_candidate") == "searchable_candidate"
    ]
    question_count = _nonnegative_int(a3.get("question_count"))
    explicit_ready = _nonnegative_int(a3.get("ready_count"))
    explicit_manual = _nonnegative_int(a3.get("manual_count"))
    if question_count is None:
        question_count = len(searchable)
        if not searchable and explicit_ready is not None and explicit_manual is not None:
            question_count = explicit_ready + explicit_manual
    remaining = _nonnegative_int(a3.get("remaining_count"))
    if remaining is None:
        remaining = sum(
            item.get("completed") is not True and item.get("searched") is not True
            for item in searchable
        )
    ready = explicit_ready
    if ready is None:
        ready = sum(
            str(item.get("validation_status") or "") == "auto_ready"
            or item.get("crop_available") is True
            for item in searchable
        )
    manual = explicit_manual
    if manual is None:
        manual = max(0, question_count - ready)
    return question_count, remaining, ready, manual


def _selection_facts(question_count: int, remaining: int) -> dict[str, int]:
    facts: dict[str, int] = {}
    if question_count > 0:
        facts["question_count"] = question_count
    if remaining > 0:
        facts["remaining_count"] = remaining
    return facts


def _a3_guidance(a3: Mapping[str, Any]) -> tuple[str, dict[str, bool], tuple[UserAction, ...]]:
    phase = _phase(a3, "WAIT_UNIT_SELECTION")
    if phase not in _A3_GUIDANCE_PHASES:
        phase = "WAIT_UNIT_SELECTION"
    facts: dict[str, bool] = {}
    if phase == "WAIT_UNIT_SELECTION":
        actions = (UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE)
    elif phase == "CROP_REQUIRED":
        actions = (
            UserAction.CROP_QUESTION,
            UserAction.CANCEL_CURRENT_QUESTION,
            UserAction.FINISH_PAGE,
            UserAction.CONTINUE_CURRENT,
        )
    elif phase == "A2_ACTIVE":
        actions = (
            UserAction.SELECT_CANDIDATE,
            UserAction.CANCEL_CURRENT_QUESTION,
            UserAction.FINISH_PAGE,
            UserAction.CONTINUE_CURRENT,
        )
    elif phase == "COMPLETE":
        actions = (UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT)
    else:
        actions = (UserAction.UPLOAD_IMAGE,)
    return phase, facts, actions


def _combine_a3_child(
    a3: Mapping[str, Any], protocol: RequestProtocol, child: OutputDraftV1
) -> OutputDraftV1 | None:
    unit_facts = {**_unit_facts(a3), **_previous_unit_facts(a3)}
    if "question_label" not in unit_facts:
        return None
    _question_count, remaining, _ready, _manual = _a3_counts(a3)
    if child.media_policy == "candidate_set":
        count = _positive_int(child.facts.get("candidate_count"))
        facts = {
            **unit_facts,
            "candidate_count": count,
            "has_usable_result": count > 0,
        }
        sources = child.facts.get("source_chapters")
        if isinstance(sources, Sequence) and not isinstance(sources, str) and sources:
            facts["source_chapters"] = tuple(sources)
        actions = [UserAction.SELECT_CANDIDATE]
        if protocol.retryable:
            actions.append(UserAction.RETRY_SEARCH)
        return _draft(
            "page.unit.candidates.ready",
            "A2_ACTIVE",
            facts=facts,
            actions=actions,
            notices=_notice_keys(protocol),
            media_policy="candidate_set",
        )
    if child.media_policy == "delivery":
        delivered = _positive_int(child.facts.get("delivered_image_count"))
        facts = {
            **unit_facts,
            "delivered_image_count": delivered,
            "has_usable_result": delivered > 0,
        }
        if remaining > 0:
            facts["remaining_count"] = remaining
            return _draft(
                "page.unit.answer.delivered_remaining",
                "WAIT_UNIT_SELECTION",
                facts=facts,
                actions=(UserAction.SELECT_QUESTION,),
                media_policy="delivery",
            )
        return _draft(
            "page.unit.answer.delivered_complete",
            "COMPLETE",
            facts=facts,
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
            media_policy="delivery",
        )
    return None


def build_a3_output_draft(
    intent: str,
    a3: Mapping[str, Any],
    protocol: RequestProtocol,
    child_draft: OutputDraftV1 | None = None,
    variant: str = "",
) -> OutputDraftV1:
    """Build an A3 output draft, combining only structured parent/child facts."""

    if not isinstance(a3, Mapping):
        raise TypeError("a3 must be a mapping")
    if not isinstance(protocol, RequestProtocol):
        raise TypeError("protocol must be RequestProtocol")
    if child_draft is not None and not isinstance(child_draft, OutputDraftV1):
        raise TypeError("child_draft must be OutputDraftV1")
    if _has_unit_binding_conflict(a3):
        return _draft(
            "system.service.unavailable",
            "ERROR",
            actions=(UserAction.RETRY_REQUEST,),
            kind=FinalOutputKind.TRANSPORT_ERROR,
        )
    intent_key = _clean_intent(intent)
    variant_key = _clean_intent(variant)
    phase = _phase(a3)
    question_count, remaining, ready, manual = _a3_counts(a3)

    if child_draft is not None:
        combined = _combine_a3_child(a3, protocol, child_draft)
        if combined is not None:
            return combined
        if child_draft.media_policy in {"candidate_set", "delivery"}:
            return _draft(
                "system.service.unavailable",
                "ERROR",
                actions=(UserAction.RETRY_REQUEST,),
                kind=FinalOutputKind.TRANSPORT_ERROR,
            )
        return child_draft

    if protocol.code == "EXTERNAL_LOAD_NOT_FOUND":
        return _draft(
            "triage.a1.no_external_load",
            "COMPLETE" if phase == "COMPLETE" else "PROCESSING",
            actions=(UserAction.RETRY_UPLOAD,),
        )
    if protocol.code == "TRIAGE_A1_STOPPED" or intent_key in {
        "image_triage_stop",
        "triage_a1_stop",
    }:
        triage_phase = "COMPLETE" if phase == "COMPLETE" else "PROCESSING"
        if variant_key == "no_external_load":
            return _draft(
                "triage.a1.no_external_load",
                triage_phase,
                actions=(UserAction.RETRY_UPLOAD,),
            )
        if variant_key in _A1_REASONS:
            return _draft(
                "triage.a1.reasoned",
                triage_phase,
                facts={"a1_reason": variant_key},
                actions=(UserAction.RETRY_UPLOAD,),
            )
        return _draft(
            "triage.a1.fallback",
            triage_phase,
            actions=(UserAction.RETRY_UPLOAD,),
        )

    if intent_key == "a3_crop_error" and protocol.code == "SERVICE_UNAVAILABLE":
        crop = a3.get("crop_draft")
        preserved = a3.get("crop_draft_preserved") is True or (
            isinstance(crop, Mapping) and crop.get("available") is True
        )
        return _draft(
            "page.crop.verification_failed",
            "CROP_REQUIRED",
            facts={"crop_draft_preserved": preserved},
            actions=(UserAction.RETRY_REQUEST,),
        )
    if intent_key == "a3_page_error" and protocol.code == "SERVICE_UNAVAILABLE":
        return _draft(
            "page.failed.retryable",
            "ERROR",
            actions=(UserAction.RETRY_REQUEST,),
        )

    system = _system_draft(a3, protocol)
    if system is not None:
        return system

    if intent_key == "greeting":
        return _draft(
            "conversation.greeting", phase, actions=(UserAction.UPLOAD_IMAGE,)
        )
    if intent_key in {"small_talk", "courtesy"}:
        return _draft("conversation.courtesy", phase)
    if intent_key in {"capability_help", "capability"}:
        return _draft("conversation.capability", phase)
    if intent_key == "safe_answer":
        key = _CONVERSATION_VARIANTS.get(variant_key, "conversation.general")
        actions = (UserAction.UPLOAD_IMAGE,) if key == "conversation.greeting" else ()
        return _draft(key, phase, actions=actions)

    if intent_key == "a3_session_reset":
        return _draft(
            "page.session.reset", "IDLE", actions=(UserAction.UPLOAD_IMAGE,)
        )
    if intent_key == "a3_page_finished":
        return _draft(
            "page.ended",
            "COMPLETE",
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        )
    if intent_key in {"a3_current_unit_cancelled", "a3_reselect"}:
        facts = _unit_facts(a3, stopped=True)
        if "question_label" in facts:
            facts["remaining_count"] = remaining
            if remaining:
                return _draft(
                    "page.unit.stopped_remaining",
                    "WAIT_UNIT_SELECTION",
                    facts=facts,
                    actions=(UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE),
                )
            return _draft(
                "page.unit.stopped_complete",
                "COMPLETE",
                facts=facts,
                actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
            )
    if intent_key == "a3_complete":
        facts = _unit_facts(a3, stopped=True)
        if "question_label" in facts:
            facts["remaining_count"] = 0
            return _draft(
                "page.unit.stopped_complete",
                "COMPLETE",
                facts=facts,
                actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
            )
        return _draft(
            "page.completed",
            "COMPLETE",
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        )

    if intent_key == "a3_page_ready" and phase == "COMPLETE":
        if question_count == 0:
            return _draft(
                "page.no_units",
                "COMPLETE",
                facts={"question_count": 0},
                actions=(UserAction.RETRY_UPLOAD,),
            )
        return _draft(
            "page.completed",
            "COMPLETE",
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        )
    if intent_key == "a3_page_ready" and phase == "COMPLETE":
        return _draft(
            "page.completed",
            "COMPLETE",
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        )
    if intent_key == "a3_units_prepared":
        actions = [UserAction.SELECT_QUESTION]
        if protocol.retryable:
            actions.append(UserAction.RETRY_CURRENT_STAGE)
        return _draft(
            "page.units.prepared",
            "WAIT_UNIT_SELECTION",
            facts={
                "question_count": question_count,
                "ready_count": ready,
                "manual_count": manual,
            },
            actions=actions,
            notices=_notice_keys(protocol),
        )
    if intent_key == "a3_page_ready" and phase == "CROP_REQUIRED":
        facts = _unit_facts(a3)
        if "question_label" in facts:
            return _draft(
                "page.crop.required",
                "CROP_REQUIRED",
                facts=facts,
                actions=(
                    UserAction.CROP_QUESTION,
                    UserAction.CANCEL_CURRENT_QUESTION,
                    UserAction.FINISH_PAGE,
                    UserAction.CONTINUE_CURRENT,
                ),
            )
    if intent_key == "a3_prepare_required" and question_count == 0 and remaining == 0:
        return _draft(
            "page.current.guidance",
            "WAIT_UNIT_SELECTION",
            actions=(UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE),
        )
    if intent_key in {"a3_page_ready", "a3_auto_crops_ready", "a3_prepare_required", "a3_unit_clarification", "a3_unit_unavailable"}:
        selection_facts = _selection_facts(question_count, remaining)
        if not selection_facts:
            return _draft(
                "page.current.guidance",
                "WAIT_UNIT_SELECTION",
                actions=(UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE),
            )
        return _draft(
            "page.selection.required",
            "WAIT_UNIT_SELECTION",
            facts=selection_facts,
            actions=(UserAction.SELECT_QUESTION, UserAction.FINISH_PAGE),
        )
    if intent_key in {"a3_crop_required", "a3_unit_selected"}:
        facts = {**_unit_facts(a3), **_previous_unit_facts(a3)}
        if "question_label" in facts:
            return _draft(
                "page.crop.required",
                "CROP_REQUIRED",
                facts=facts,
                actions=(
                    UserAction.CROP_QUESTION,
                    UserAction.CANCEL_CURRENT_QUESTION,
                    UserAction.FINISH_PAGE,
                    UserAction.CONTINUE_CURRENT,
                ),
            )
    if intent_key == "a3_crop_review_required":
        facts = _unit_facts(a3)
        facts["crop_reason"] = variant_key if variant_key in _CROP_REASONS else "unconfirmed"
        return _draft(
            "page.crop.rejected",
            "CROP_REQUIRED",
            facts=facts,
            actions=(
                UserAction.CROP_QUESTION,
                UserAction.CANCEL_CURRENT_QUESTION,
                UserAction.FINISH_PAGE,
                UserAction.CONTINUE_CURRENT,
            ),
        )
    if intent_key == "a3_namespace_clarification":
        return _draft(
            "page.namespace.clarification",
            "A2_ACTIVE",
            actions=(UserAction.SELECT_QUESTION, UserAction.SELECT_CANDIDATE),
        )
    if intent_key == "a3_cancel_scope_clarification":
        if phase == "WAIT_UNIT_SELECTION":
            return _draft(
                "page.cancel.scope_required.page",
                phase,
                actions=(UserAction.FINISH_PAGE, UserAction.CONTINUE_CURRENT),
            )
        return _draft(
            "page.cancel.scope_required.current",
            phase if phase in {"CROP_REQUIRED", "A2_ACTIVE"} else "A2_ACTIVE",
            actions=(
                UserAction.CANCEL_CURRENT_QUESTION,
                UserAction.FINISH_PAGE,
                UserAction.CONTINUE_CURRENT,
            ),
        )
    if intent_key in {"stale_action", "stale_selection"}:
        return _draft(
            "page.stale.selection",
            "WAIT_UNIT_SELECTION",
            facts={"remaining_count": remaining} if remaining else {},
            actions=(UserAction.SELECT_QUESTION,),
        )
    if intent_key == "stale_candidate":
        return _draft(
            "page.stale.candidate",
            "A2_ACTIVE" if phase == "A2_ACTIVE" else "WAIT_UNIT_SELECTION",
            actions=(UserAction.RETRY_UPLOAD,),
        )
    if intent_key in {"a3_continue_current", "a3_unit_already_selected", "clarification"}:
        guidance_phase, facts, actions = _a3_guidance(a3)
        return _draft(
            "page.current.guidance",
            guidance_phase,
            facts=facts,
            actions=actions,
        )
    if phase == "COMPLETE" or a3.get("page_finished") is True:
        return _draft(
            "page.completed",
            "COMPLETE",
            actions=(UserAction.UPLOAD_IMAGE, UserAction.NEW_CHAT),
        )
    return _draft("conversation.general", phase)


def _protocol_with_code(protocol: RequestProtocol, code: str) -> RequestProtocol:
    return RequestProtocol.from_code(
        code,
        request_id=protocol.request_id,
        search_id=protocol.search_id,
    )


def _normalized_protocol(draft: OutputDraftV1, protocol: RequestProtocol) -> RequestProtocol:
    if (
        draft.message_key in _SAFE_CONVERSATION_KEYS
        and protocol.code in _STATE_DERIVED_CONVERSATION_CODES
    ):
        return _protocol_with_code(protocol, "REQUEST_SUCCEEDED")
    if (
        draft.message_key.startswith("triage.a1.")
        and protocol.code == "TRIAGE_A3_REQUIRES_REUPLOAD"
    ):
        return _protocol_with_code(protocol, "TRIAGE_A1_STOPPED")
    if (
        draft.message_key == "system.service.unavailable"
        and protocol.code != "SERVICE_UNAVAILABLE"
    ):
        return _protocol_with_code(protocol, "SERVICE_UNAVAILABLE")
    if protocol.code == "LOAD_ROUTE_NEEDS_REVIEW":
        return _protocol_with_code(protocol, "CLARIFICATION_REQUIRED")
    if (
        draft.message_key == "search.candidates.rejected"
        and protocol.code == "REQUEST_SUCCEEDED"
    ):
        return _protocol_with_code(protocol, "NO_MORE_CANDIDATES")
    return protocol


def _media_failure(protocol: RequestProtocol) -> PublicMessageV1:
    media_protocol = _protocol_with_code(protocol, "MEDIA_NOT_FOUND")
    return render_final_output(
        FinalOutputRequestV1(
            schema_version=USER_OUTPUT_SCHEMA_VERSION,
            kind=FinalOutputKind.TRANSPORT_ERROR,
            message_key="system.media.not_found",
            protocol=media_protocol,
            phase="ERROR",
            facts={},
            allowed_actions=(UserAction.RETRY_REQUEST,),
        )
    )


def _contract_failure(protocol: RequestProtocol) -> PublicMessageV1:
    service_protocol = _protocol_with_code(protocol, "SERVICE_UNAVAILABLE")
    return render_final_output(
        FinalOutputRequestV1(
            schema_version=USER_OUTPUT_SCHEMA_VERSION,
            kind=FinalOutputKind.TRANSPORT_ERROR,
            message_key="system.service.unavailable",
            protocol=service_protocol,
            phase="ERROR",
            facts={},
            allowed_actions=(UserAction.RETRY_REQUEST,),
        )
    )


def finalize_output_draft(
    draft: OutputDraftV1,
    protocol: RequestProtocol,
    delivered_count: int,
    expected_media_count: int,
    contact: PublicContactV1 | None = None,
) -> PublicMessageV1:
    """Bind media evidence and render one canonical public message.

    Candidate media is atomic: any missing item fails the whole candidate set.
    Answer delivery may be partial, but reports only the actual delivered count.
    """

    if not isinstance(draft, OutputDraftV1) or not isinstance(protocol, RequestProtocol):
        raise TypeError("draft and protocol types are required")
    if type(delivered_count) is not int or type(expected_media_count) is not int:
        raise TypeError("media counts must be integers")
    if delivered_count < 0 or expected_media_count < 0:
        raise ValueError("media counts must not be negative")
    if contact is not None and not isinstance(contact, PublicContactV1):
        raise TypeError("contact must be PublicContactV1")

    protocol = _normalized_protocol(draft, protocol)
    facts = dict(draft.facts)
    actions = list(draft.allowed_actions)
    notices = _final_notice_keys(draft, protocol)

    if draft.media_policy == "none":
        if delivered_count or expected_media_count:
            return _contract_failure(protocol)
    elif draft.media_policy == "candidate_set":
        candidate_count = _positive_int(facts.get("candidate_count"))
        if (
            expected_media_count <= 0
            or delivered_count != expected_media_count
            or candidate_count != expected_media_count
        ):
            return _media_failure(protocol)
    elif draft.media_policy == "delivery":
        if expected_media_count <= 0 or delivered_count > expected_media_count:
            return _media_failure(protocol)
        if delivered_count == 0:
            return _media_failure(protocol)
        facts["delivered_image_count"] = delivered_count
        facts["has_usable_result"] = True
        if delivered_count < expected_media_count:
            protocol = _protocol_with_code(protocol, "MEDIA_PERSIST_FAILED")
            notices = _final_notice_keys(draft, protocol)
            if UserAction.RETRY_REQUEST not in actions:
                actions.append(UserAction.RETRY_REQUEST)
        else:
            actions = [item for item in actions if item is not UserAction.RETRY_REQUEST]
            notices = _final_notice_keys(draft, protocol)
    else:  # pragma: no cover - OutputDraftV1 rejects this before finalization.
        return _contract_failure(protocol)

    return render_final_output(
        FinalOutputRequestV1(
            schema_version=USER_OUTPUT_SCHEMA_VERSION,
            kind=draft.kind,
            message_key=draft.message_key,
            protocol=protocol,
            phase=draft.phase,
            facts=facts,
            allowed_actions=tuple(actions),
            notice_keys=notices,
            contact=contact,
        )
    )


__all__ = [
    "OutputDraftV1",
    "build_a2_output_draft",
    "build_a3_output_draft",
    "finalize_output_draft",
]

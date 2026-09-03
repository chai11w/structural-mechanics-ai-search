"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial, wraps
from io import BytesIO
import inspect
import json
import logging
import math
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from tiku_agent.a3_runtime import A3_CROP_REVIEW_CODES
from tiku_agent.agent import AgentResponse
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.invite_access import InviteAccess, InviteIdentity
from tiku_agent.output_watchdog import observe_output, observe_public_output
from tiku_agent.session_artifacts import session_key
from tiku_agent.session_runtime import (
    AgentBudgetExceededError,
    AgentProtocolError,
    AgentRuntimeBusyError,
    AgentSessionRuntime,
    SessionResponseSnapshotError,
    SessionResponseSnapshotV1,
    _ExecutionCancelled,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent import task_state_contract as task_state_contract
from tiku_agent.task_state_public import PUBLIC_TASK_STATE_FIELD, with_public_task_state
from tiku_agent.task_state_public import public_task_state_snapshot
from tiku_agent.task_state_contract import TaskStateSnapshotV1, empty_task_state_snapshot
from tiku_agent.task_state_runtime import TaskStateEntryCapabilities
from tiku_agent.tool_result import is_public_tool_code
from tiku_shared.model_costs import SQLiteModelCostLedger
from tiku_shared.request_protocol import (
    PROTOCOL_REASONS,
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
    new_request_id,
)
from tiku_shared.response_store import (
    ResponseFinalizationCancelled,
    ResponseOwnershipError,
    ResponseProjection,
    ResponseRecord,
    ResponseStoreError,
    ResponseValidationError,
    SQLiteResponseStore,
    is_valid_response_id,
)
from tiku_shared.trace_context import (
    TraceContext,
    current_request_id,
    current_trace_id,
    trace_context_scope,
)
from tiku_shared.trace_events import (
    TraceEventRecorder,
    TraceEventSession,
    bind_trace_event_dimensions,
    current_trace_event_session,
    record_public_terminal,
    record_trace_event,
    trace_event_session_scope,
    trace_event_scope,
)
from tiku_agent.tools import DEFAULT_RUNTIME_DIR
from tiku_shared.http_security import (
    FailureRateLimiter,
    RequestBodyError,
    read_bounded_body,
    validated_client_key,
)


SESSION_COOKIE = "tiku_agent_session"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
INCOMING_DIR = DEFAULT_RUNTIME_DIR / "incoming"
WEB_DIR = Path(__file__).with_name("demo_web")
SUPPORTED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
    "BMP": ("image/bmp", ".bmp"),
}
GENERIC_CONTENT_TYPES = {"", "application/octet-stream"}
_SESSION_TASK_STATE_CAPABILITIES = TaskStateEntryCapabilities(
    trusted_image_event=False,
    reset_session_available=True,
)
_JSON_TASK_STATE_CAPABILITIES = _SESSION_TASK_STATE_CAPABILITIES
_STREAM_TASK_STATE_CAPABILITIES = _SESSION_TASK_STATE_CAPABILITIES
_IMAGE_JSON_TASK_STATE_CAPABILITIES = TaskStateEntryCapabilities(
    trusted_image_event=True,
    reset_session_available=True,
)
_IMAGE_STREAM_TASK_STATE_CAPABILITIES = _IMAGE_JSON_TASK_STATE_CAPABILITIES
FEEDBACK_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_FEEDBACK_BYTES = 128 * 1024
SESSION_COORDINATION_VERSION = "6"
SESSION_COORDINATION_VERSION_HEADER = "X-Session-Coordination-Version"
SESSION_REQUEST_FENCE_HEADER = "X-Session-Request-Fence"
SESSION_RECONCILE_FENCES_HEADER = "X-Session-Reconcile-Fences"
SESSION_COORDINATION_ACK_FIELD = "session_coordination"
_SESSION_COORDINATION_MAX_RECONCILE_FENCES = 64
_SESSION_COORDINATION_RETENTION_SECONDS = 2 * 60 * 60
_SESSION_COORDINATION_FUTURE_SKEW_SECONDS = 5 * 60
_SESSION_REQUEST_FENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
FEEDBACK_TAGS = {
    "positive": {
        "found_answer", "relevant_results", "clear_reply", "fast", "other",
    },
    "negative": {
        "not_found", "irrelevant_results", "ranking_issue", "wrong_answer",
        "too_slow", "system_error", "other",
    },
}

# API error details are a public contract. Keep provider/runtime text out of
# this boundary even when an exception was constructed with an arbitrary
# message.
_PUBLIC_PROTOCOL_MESSAGES = {
    "LOGIN_REQUIRED": "请先使用有效邀请码登录。",
    "INVITE_INVALID": "邀请码无效或已停用，请检查后重试。",
    "LOGIN_REQUEST_INVALID": "登录请求无效，请刷新页面后重试。",
    "LOGIN_RATE_LIMITED": "登录尝试过多，请稍后再试。",
    "LOGIN_EXPIRED": "登录状态已失效，请重新登录。",
    "MESSAGE_INVALID": "请求内容无效，请重新提交。",
    "STALE_ACTION": "这个操作已经失效，请使用当前页面中的操作。",
    "STALE_CANDIDATE": "这个候选已经失效，请重新上传题图。",
    "UPLOAD_REQUIRED": "请先上传题图。",
    "UPLOAD_TOO_LARGE": "题图文件过大，请压缩后重新上传。",
    "UPLOAD_UNSUPPORTED_FORMAT": "暂不支持这种图片格式，请重新上传 JPG、PNG 或 WEBP。",
    "UPLOAD_DECODE_FAILED": "这张图片无法正常读取，请重新上传。",
    "UPLOAD_PERSIST_FAILED": "题图暂时无法保存，请稍后重试。",
    "MEDIA_NOT_FOUND": "请求的图片已失效，请重新上传题图。",
    "MEDIA_PERSIST_FAILED": "图片暂时无法保存，请稍后重试。",
    "MEDIA_CANDIDATES_INCOMPLETE": "候选图片暂时无法完整发送，请回复“重试”。",
    "MEDIA_ANSWERS_UNAVAILABLE": "答案暂时无法发送，请回复“重试”。",
    "MEDIA_ANSWERS_PARTIAL": "答案只发送了一部分，请回复“重试”补发。",
    "QUEUE_FULL": "当前请求较多，请稍后再试。",
    "QUEUE_TIMEOUT": "请求等待超时，请稍后重试。",
    "GLOBAL_DAILY_QUOTA_EXCEEDED": "今日服务额度已用完，请明天再试。",
    "INVITE_DAILY_QUOTA_EXCEEDED": "该邀请码今日额度已用完，请明天再试。",
    "INVITE_IDENTITY_MISSING": "当前请求缺少有效邀请码，请重新登录。",
    "FEEDBACK_INVALID": "反馈内容无效，请检查后重试。",
    "FEEDBACK_TOO_LARGE": "反馈内容过大，请缩短后重试。",
    "FEEDBACK_SAVE_FAILED": "反馈暂时无法保存，请稍后重试。",
    "SERVICE_UNAVAILABLE": "服务暂时异常，请稍后重试。",
    "AGENT_FAILED": "这次处理没有完成，请稍后重试。",
    "AGENT_FAILED_NO_IMAGE": "这次处理没有完成，请重新上传题图。",
    "TOOL_FAILED": "这次处理没有完成，请稍后重试。",
    "TOOL_INPUT_REQUIRED": "还需要补充信息后才能继续。",
}
_PUBLIC_PROGRESS_EXACT = {
    "queued": frozenset({"前面有任务正在处理，已进入队列…"}),
    "dequeued": frozenset({"轮到你的题目了，正在开始处理…"}),
    "triage": frozenset({"正在检查图片并决定处理路线…"}),
    "searching": frozenset({
        "正在搜索题目…",
        "图片适合直接检索，正在识别题目信息…",
        "自动裁图已通过校验，正在进入题库检索…",
    }),
    "global_searching": frozenset({"正在全局搜索题目，可能需要一点时间…"}),
    "a3_understanding": frozenset({
        "正在理解整页题目和图形关系…",
        "正在重新理解整页题目…",
    }),
    "a3_auto_grounding": frozenset({"正在一次定位整页所有可检索结构图…"}),
    "a3_auto_validating": frozenset({"所选题目需要人工裁剪，正在准备…"}),
    "a3_verifying": frozenset({"正在核对裁剪图和所选题目…"}),
    "a3_analyzing_unit": frozenset({"校验通过，正在结合题干识别章节和荷载…"}),
}
_PUBLIC_PROGRESS_CHAPTER_RE = re.compile(r"^正在按「(.{1,64})」搜索题目…$")
_PUBLIC_PROGRESS_AUTO_START_RE = re.compile(r"^正在并发校验 ([1-9][0-9]{0,2}) 张自动裁图…$")
_PUBLIC_PROGRESS_AUTO_DONE_RE = re.compile(
    r"^已完成 ([1-9][0-9]{0,2})/([1-9][0-9]{0,2}) 张自动裁图校验…$"
)


@dataclass(frozen=True)
class _AuthoritativeResponseDraft:
    snapshot: dict[str, object]
    workflow_search_id: str
    search_id: str
    unit_id: str
    image_route: str
    intent: str


@dataclass(frozen=True)
class _QueuedStreamEvent:
    body: str
    response_record: ResponseRecord | None = None
    terminal_recorder: Callable[[], None] | None = None


@dataclass(frozen=True)
class _SessionCoordinationHeaders:
    versioned: bool
    request_fence: str = ""
    reconcile_fences: tuple[str, ...] = ()


class _SessionCoordinationGate:
    """Serialize one session's v6 admission and reconciliation boundary."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._busy = False
        self._completed_fences: dict[str, float] = {}
        self._last_used = time.monotonic()

    def _prune_completed_locked(self, now: float) -> None:
        expired = [
            fence_id
            for fence_id, expires_at in self._completed_fences.items()
            if expires_at <= now
        ]
        for fence_id in expired:
            self._completed_fences.pop(fence_id, None)

    def begin_task(self, fence_id: str) -> bool:
        clean_fence = str(fence_id or "")
        with self._condition:
            contended = self._busy
            while self._busy:
                self._condition.wait()
            now = time.monotonic()
            self._prune_completed_locked(now)
            self._busy = True
            self._last_used = now
            admitted = (
                not contended
                and (
                    not clean_fence
                    or clean_fence not in self._completed_fences
                )
            )
            # Every request that reaches this serialized boundary is terminal
            # for its exact fence, including a contender that must fail closed.
            # Otherwise that contender could retry after the winner finishes
            # and unexpectedly execute as a fresh task.
            if clean_fence:
                self._completed_fences[clean_fence] = (
                    now + _SESSION_COORDINATION_RETENTION_SECONDS
                )
            return admitted

    def finish_task(self, fence_id: str, admitted: bool) -> None:
        clean_fence = str(fence_id or "")
        with self._condition:
            now = time.monotonic()
            if clean_fence:
                self._completed_fences[clean_fence] = (
                    now + _SESSION_COORDINATION_RETENTION_SECONDS
                )
            self._last_used = now
            self._busy = False
            self._condition.notify_all()

    @contextmanager
    def task(self, fence_id: str):
        admitted = self.begin_task(fence_id)
        try:
            yield admitted
        finally:
            self.finish_task(fence_id, admitted)

    @contextmanager
    def reconcile(
        self,
        fence_ids: tuple[str, ...],
        request_fence: str = "",
    ):
        clean_request_fence = str(request_fence or "")
        with self._condition:
            while self._busy:
                self._condition.wait()
            now = time.monotonic()
            self._prune_completed_locked(now)
            self._busy = True
            self._last_used = now
            for fence_id in fence_ids:
                self._completed_fences[fence_id] = (
                    now + _SESSION_COORDINATION_RETENTION_SECONDS
                )
            admitted = (
                not clean_request_fence
                or clean_request_fence not in self._completed_fences
            )
            if admitted and clean_request_fence:
                self._completed_fences[clean_request_fence] = (
                    now + _SESSION_COORDINATION_RETENTION_SECONDS
                )
        try:
            yield admitted
        finally:
            with self._condition:
                now = time.monotonic()
                if admitted and clean_request_fence:
                    self._completed_fences[clean_request_fence] = (
                        now + _SESSION_COORDINATION_RETENTION_SECONDS
                    )
                self._last_used = now
                self._busy = False
                self._condition.notify_all()

    def expired(self, now: float) -> bool:
        with self._condition:
            self._prune_completed_locked(now)
            return (
                not self._busy
                and not self._completed_fences
                and now - self._last_used >= _SESSION_COORDINATION_RETENTION_SECONDS
            )


class _SessionCoordinationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: dict[str, _SessionCoordinationGate] = {}
        self._pins: dict[str, int] = {}
        self._last_sweep = time.monotonic()

    def _pin(self, session_id: str) -> tuple[str, _SessionCoordinationGate]:
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            raise ValueError("session_id is required for session coordination")
        with self._lock:
            now = time.monotonic()
            if now - self._last_sweep >= 60:
                self._gates = {
                    key: candidate
                    for key, candidate in self._gates.items()
                    if self._pins.get(key, 0) or not candidate.expired(now)
                }
                self._pins = {
                    key: count
                    for key, count in self._pins.items()
                    if key in self._gates and count > 0
                }
                self._last_sweep = now
            gate = self._gates.get(clean_session_id)
            if gate is None:
                gate = _SessionCoordinationGate()
                self._gates[clean_session_id] = gate
            self._pins[clean_session_id] = self._pins.get(clean_session_id, 0) + 1
            return clean_session_id, gate

    def _unpin(self, session_id: str, gate: _SessionCoordinationGate) -> None:
        with self._lock:
            if self._gates.get(session_id) is not gate:
                raise RuntimeError("session coordination gate identity changed while pinned")
            pins = self._pins.get(session_id, 0)
            if pins <= 0:
                raise RuntimeError("session coordination gate pin underflow")
            if pins == 1:
                self._pins.pop(session_id, None)
            else:
                self._pins[session_id] = pins - 1

    @contextmanager
    def task(self, session_id: str, fence_id: str):
        clean_session_id, gate = self._pin(session_id)
        try:
            with gate.task(fence_id) as admitted:
                yield admitted
        finally:
            self._unpin(clean_session_id, gate)

    @contextmanager
    def reconcile(
        self,
        session_id: str,
        fence_ids: tuple[str, ...],
        request_fence: str = "",
    ):
        clean_session_id, gate = self._pin(session_id)
        try:
            with gate.reconcile(fence_ids, request_fence) as admitted:
                yield admitted
        finally:
            self._unpin(clean_session_id, gate)

def _same_origin_browser_request(request: Request) -> bool:
    return str(request.headers.get("sec-fetch-site") or "").strip().lower() == "same-origin"


def _valid_session_request_fence(value: object) -> bool:
    return isinstance(value, str) and bool(_SESSION_REQUEST_FENCE_RE.fullmatch(value.strip()))


def _current_session_request_fence(value: object) -> bool:
    """Accept only a recent v6 task/read fence minted from browser Date.now()."""

    if not _valid_session_request_fence(value):
        return False
    timestamp_text = str(value).strip().split(":", 1)[0]
    if not timestamp_text.isdigit() or len(timestamp_text) != 13:
        return False
    timestamp_ms = int(timestamp_text)
    now_ms = int(time.time() * 1000)
    oldest_ms = now_ms - (_SESSION_COORDINATION_RETENTION_SECONDS * 1000)
    newest_ms = now_ms + (_SESSION_COORDINATION_FUTURE_SKEW_SECONDS * 1000)
    return oldest_ms <= timestamp_ms <= newest_ms


def _parse_session_coordination_headers(
    request: Request,
    *,
    task_request: bool,
) -> tuple[_SessionCoordinationHeaders | None, str]:
    header_names = (
        SESSION_COORDINATION_VERSION_HEADER,
        SESSION_REQUEST_FENCE_HEADER,
        SESSION_RECONCILE_FENCES_HEADER,
    )
    supplied = any(name in request.headers for name in header_names)
    if not supplied:
        if task_request and _same_origin_browser_request(request):
            return None, "same-origin browser task is missing v6 coordination headers"
        return _SessionCoordinationHeaders(versioned=False), ""

    version = str(request.headers.get(SESSION_COORDINATION_VERSION_HEADER) or "").strip()
    if version != SESSION_COORDINATION_VERSION:
        return None, "unsupported session coordination version"

    raw_reconcile = request.headers.get(SESSION_RECONCILE_FENCES_HEADER)
    reconcile_fences: tuple[str, ...] = ()
    if raw_reconcile is not None:
        try:
            decoded = json.loads(raw_reconcile)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "invalid reconciliation fence list"
        if (
            not isinstance(decoded, list)
            or len(decoded) > _SESSION_COORDINATION_MAX_RECONCILE_FENCES
            or any(not _valid_session_request_fence(value) for value in decoded)
        ):
            return None, "invalid reconciliation fence list"
        normalized = tuple(str(value).strip() for value in decoded)
        if len(set(normalized)) != len(normalized):
            return None, "duplicate reconciliation fence"
        reconcile_fences = normalized

    raw_request_fence = request.headers.get(SESSION_REQUEST_FENCE_HEADER)
    if task_request:
        if not _current_session_request_fence(raw_request_fence):
            return None, "v6 task is missing a current request fence"
        if reconcile_fences:
            return None, "task request cannot reconcile pending fences"
        return _SessionCoordinationHeaders(
            versioned=True,
            request_fence=str(raw_request_fence).strip(),
        ), ""
    if raw_request_fence is not None and not _current_session_request_fence(
        raw_request_fence
    ):
        return None, "reconciliation request has a non-current request fence"
    clean_request_fence = str(raw_request_fence or "").strip()
    if clean_request_fence and clean_request_fence in reconcile_fences:
        return None, "request fence cannot also be reconciled"
    return _SessionCoordinationHeaders(
        versioned=True,
        request_fence=clean_request_fence,
        reconcile_fences=reconcile_fences,
    ), ""


def _coordination_completed_fences(
    coordination: _SessionCoordinationHeaders,
) -> tuple[str, ...]:
    values = (*coordination.reconcile_fences, coordination.request_fence)
    return tuple(dict.fromkeys(value for value in values if value))


def _with_session_coordination_ack(
    payload: dict[str, object],
    fence_ids: tuple[str, ...],
) -> dict[str, object]:
    payload[SESSION_COORDINATION_ACK_FIELD] = {
        "version": SESSION_COORDINATION_VERSION,
        "completed_fences": list(fence_ids),
    }
    return payload


def _mark_session_coordination_ack(
    request: Request,
    fence_ids: tuple[str, ...] = (),
) -> None:
    request.state.session_coordination_versioned = True
    existing = tuple(
        str(value)
        for value in getattr(
            request.state,
            "session_coordination_completed_fences",
            (),
        )
        if value
    )
    request.state.session_coordination_completed_fences = tuple(
        dict.fromkeys((*existing, *fence_ids))
    )


def _attach_session_coordination_ack(
    response: Response,
    request: Request,
) -> Response:
    if not getattr(request.state, "session_coordination_versioned", False):
        return response
    if not isinstance(response, JSONResponse):
        return response
    try:
        payload = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return response
    if not isinstance(payload, dict):
        return response
    fence_ids = tuple(
        str(value)
        for value in getattr(
            request.state,
            "session_coordination_completed_fences",
            (),
        )
        if value
    )
    _with_session_coordination_ack(payload, fence_ids)
    response.body = response.render(payload)
    response.headers["content-length"] = str(len(response.body))
    return response


class _AgentPayload(dict[str, object]):
    """Public payload with server-only finalization data kept off the wire."""

    def __init__(self, values: Mapping[str, object]) -> None:
        super().__init__(values)
        self.authoritative_draft: _AuthoritativeResponseDraft | None = None
        self.response_task_state_snapshot: TaskStateSnapshotV1 | None = None

_PUBLIC_STATE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_PUBLIC_STATE_PHASE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_PUBLIC_STATE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PUBLIC_STATE_SENSITIVE_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)", re.IGNORECASE),
    re.compile(
        r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|cookie)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:traceback|raw_model_output|reasoning|confidence|debug|prompt)\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]{0,80}(?:Error|Exception)\s*:", re.IGNORECASE),
    re.compile(
        r"(?:^|[^A-Za-z0-9])(?:token|secret|password|api[_-]?key)[_:= -][A-Za-z0-9_-]{4,}",
        re.IGNORECASE,
    ),
)
_PUBLIC_STATE_SENSITIVE_ID_RE = re.compile(
    r"(?:bearer|token|secret|password|api[_-]?key|sk[-_](?:proj[-_])?)",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)
_ACTIVE_PUBLIC_TRACE_META: ContextVar[dict[str, object] | None] = ContextVar(
    "active_public_trace_meta", default=None
)
_STYLE = (WEB_DIR / "demo.css").read_text(encoding="utf-8")
_SCRIPT = (WEB_DIR / "demo.js").read_text(encoding="utf-8")
_INVITE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>结构力学搜题 · 邀请验证</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f5f2;color:#252522;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:min(420px,calc(100% - 32px));padding:32px;border:1px solid #e2e2dc;border-radius:18px;background:#fff;box-shadow:0 18px 50px rgba(0,0,0,.07)}
h1{margin:0 0 10px;font-size:22px}p{margin:0 0 24px;color:#6b6b65;line-height:1.65}.error{padding:10px 12px;border-radius:9px;background:#fff1f0;color:#b42318}
label{display:block;margin-bottom:8px;font-size:13px;font-weight:600}input,button{width:100%;height:46px;border-radius:10px;font:inherit}input{padding:0 13px;border:1px solid #d6d6d0}input:focus{outline:2px solid #292925;outline-offset:1px}button{margin-top:14px;border:0;background:#292925;color:#fff;font-weight:650;cursor:pointer}
small{display:block;margin-top:18px;color:#8a8a83;line-height:1.55}
</style></head><body><main><h1>邀请码验证</h1><p>请输入内测邀请码。验证后，本浏览器 30 天内无需重复输入。</p>{error}
<form method="post" action="/api/invite/login"><label for="invite-code">邀请码</label><input id="invite-code" name="code" type="password" autocomplete="one-time-code" required maxlength="128"><button type="submit">进入搜题</button></form>
<small>每个邀请码拥有独立的每日使用额度，请勿转发。</small></main></body></html>"""


def _read_demo_page() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def create_app(
    *,
    runtime: AgentSessionRuntime | None = None,
    incoming_dir: str | Path = INCOMING_DIR,
    session_cookie: str = SESSION_COOKIE,
    cleanup_interval_seconds: float = 300.0,
    invite_access: InviteAccess | None = None,
    feedback_store: SQLiteFeedbackStore | None = None,
    response_store: SQLiteResponseStore | None = None,
    feedback_retention_days_provider: Callable[[], int] | None = None,
    output_watchdog: object | None = None,
    trace_event_recorder: TraceEventRecorder | None = None,
    media_cache_seconds: float = 0.0,
) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    session_cookie = str(session_cookie).strip()
    if not session_cookie:
        raise ValueError("session_cookie is required")
    media_cache_seconds = max(0.0, float(media_cache_seconds))
    media_cache_header = (
        f"private, max-age={int(media_cache_seconds)}"
        if media_cache_seconds > 0
        else "private, no-store"
    )
    runtime = runtime or AgentSessionRuntime(
        SQLiteSessionStore(DEFAULT_RUNTIME_DIR / "session.db"),
        cost_ledger=SQLiteModelCostLedger(DEFAULT_RUNTIME_DIR / "model_costs.sqlite3"),
    )
    if output_watchdog is not None:
        try:
            setattr(runtime, "output_watchdog", output_watchdog)
        except Exception:  # noqa: BLE001 - an observer is strictly optional.
            pass
    cleaner = getattr(runtime, "purge_expired", None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        cleanup_task = None
        if callable(cleaner) and cleanup_interval_seconds > 0:
            cleanup_task = asyncio.create_task(
                _periodic_session_cleanup(cleaner, cleanup_interval_seconds)
            )
        try:
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task
            if trace_event_recorder is not None:
                await asyncio.to_thread(trace_event_recorder.close)

    app = FastAPI(
        title="结构力学搜题 Agent",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    feedback_store = feedback_store or SQLiteFeedbackStore(
        DEFAULT_RUNTIME_DIR / "feedback.sqlite3"
    )
    response_store = response_store or SQLiteResponseStore(
        feedback_store.path.with_name("responses.sqlite3")
    )
    invite_login_limiter = FailureRateLimiter(
        attempts=10,
        window_seconds=600,
        max_keys=4096,
    )
    app.state.response_store = response_store
    app.state.trace_event_recorder = trace_event_recorder
    session_coordination = _SessionCoordinationRegistry()
    app.state.session_coordination = session_coordination
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    def invite_login_rejection(
        message: str,
        protocol: RequestProtocol,
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        _record_protocol_terminal(protocol, http_status=status_code)
        response_headers = {"Cache-Control": "no-store", **dict(headers or {})}
        error = f'<p class="error">{message}</p>'
        return HTMLResponse(
            _INVITE_PAGE.replace("{error}", error),
            status_code=status_code,
            headers=response_headers,
        )

    def request_protocol_response(
        request: Request,
        detail: str,
        protocol: RequestProtocol,
        *,
        status_code: int,
        headers: dict[str, str] | None = None,
        error_kind: str = "ProtocolFailure",
        response_snapshot: object = None,
        response_task_state_snapshot: object = None,
        response_snapshot_attempted: bool = False,
        capture_missing_task_state: bool = True,
        persist_authoritative: bool = True,
    ) -> JSONResponse:
        clean_path = str(request.url.path or "").split("?", 1)[0]
        targetable = _is_authoritative_reply_path(clean_path)
        task_state_allowed = _is_http_error_task_state_path(clean_path)
        if invite_access is not None and not isinstance(
            getattr(request.state, "invite_identity", None), InviteIdentity
        ):
            targetable = False
            task_state_allowed = False
        session_id = str(
            getattr(request.state, "session_id", "")
            or request.cookies.get(session_cookie)
            or ""
        ).strip()
        snapshot = response_snapshot if isinstance(response_snapshot, Mapping) else {}
        has_exact_task_state_pair = _has_exact_response_task_state_pair(
            snapshot,
            response_task_state_snapshot,
        )
        if (
            task_state_allowed
            and session_id
            and capture_missing_task_state
            and not has_exact_task_state_pair
        ):
            if (
                snapshot
                or response_snapshot_attempted
                or type(response_task_state_snapshot) is TaskStateSnapshotV1
            ):
                # Any carried half proves that execution already established
                # or attempted its response-time read-set. Never repair an
                # incomplete pair with a later live read.
                if not snapshot:
                    snapshot = {"session_valid": False}
                response_task_state_snapshot = _unreadable_http_error_task_state(
                    runtime
                )
            else:
                snapshot, response_task_state_snapshot = _safe_error_snapshot(
                    runtime,
                    session_id,
                    capabilities=_JSON_TASK_STATE_CAPABILITIES,
                )
        protocol = _with_protocol_search_id(
            protocol,
            str(snapshot.get("search_id") or "").strip(),
        )
        # HTTP-error task-state and Response Store persistence are separate
        # gates.  A task-state-capable route must consume the carried legacy
        # snapshot (if any) and never repair it with a live read here.
        if (
            targetable
            and session_id
            and not snapshot
            and not task_state_allowed
            and type(response_task_state_snapshot) is not TaskStateSnapshotV1
        ):
            snapshot = _safe_runtime_snapshot(runtime, session_id)
        result = _protocol_json_response(
            detail,
            protocol,
            status_code=status_code,
            headers=headers,
            output_watchdog=output_watchdog,
            error_kind=error_kind,
            response_store=(
                response_store
                if persist_authoritative and targetable and session_id
                else None
            ),
            identity_key=_identity_key(request) or "local",
            session_id=session_id,
            response_mode=_trace_response_mode(clean_path),
            response_snapshot=snapshot,
            response_task_state_snapshot=(
                response_task_state_snapshot
                if task_state_allowed and session_id
                else None
            ),
            response_task_state_fallback=(
                _unreadable_http_error_task_state(runtime)
                if task_state_allowed and session_id
                else None
            ),
            trace_id=_request_trace_context(request).trace_id,
        )
        if session_id and not request.cookies.get(session_cookie):
            _set_session_cookie(
                result,
                session_id,
                secure_cookie=_is_secure_request(request),
                cookie_name=session_cookie,
            )
        # Exception-handler responses bypass the normal middleware response
        # decoration path. Preserve the same request correlation header while
        # respecting any explicitly supplied value.
        result.headers.setdefault("X-Request-ID", _request_id(request))
        return _attach_session_coordination_ack(result, request)

    @app.exception_handler(HTTPException)
    async def public_http_error(request: Request, exc: HTTPException) -> Response:
        if not request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        clean_path = str(request.url.path or "").split("?", 1)[0]
        session_id = str(
            getattr(request.state, "session_id", "")
            or request.cookies.get(session_cookie)
            or ""
        ).strip()
        task_state_allowed = _is_http_error_task_state_path(clean_path) and (
            invite_access is None
            or isinstance(getattr(request.state, "invite_identity", None), InviteIdentity)
        )
        snapshot: Mapping[str, object] = _exception_response_snapshot(exc)
        response_task_state_snapshot: object = _exception_task_state_snapshot(exc)
        response_snapshot_attempted = _exception_response_snapshot_attempted(exc)
        carries_response_snapshot = bool(snapshot) or (
            type(response_task_state_snapshot) is TaskStateSnapshotV1
        )

        # A validation error can happen before an endpoint calls _session_id.
        # Only an already-established session may trigger one controlled,
        # response-frozen capture; never mint a session merely to decorate an
        # error response.
        if task_state_allowed:
            if (
                session_id
                and not carries_response_snapshot
                and not response_snapshot_attempted
            ):
                snapshot, response_task_state_snapshot = _safe_error_snapshot(
                    runtime,
                    session_id,
                    # An HTTP input rejection is not a successfully accepted
                    # image event, including on /api/image.
                    capabilities=_JSON_TASK_STATE_CAPABILITIES,
                )
        elif _is_authoritative_reply_path(clean_path) and session_id:
            # Stream and other legacy authoritative errors still retain their
            # existing Response Store projection, but never gain task_state.
            snapshot = _safe_runtime_snapshot(runtime, session_id)

        search_id = str(snapshot.get("search_id") or "")
        protocol = _http_error_protocol(request, exc, search_id=search_id)
        if clean_path == "/api/invite/login":
            if exc.status_code == 413:
                message = "登录请求过大，请刷新页面后重试。"
            elif exc.status_code >= 500:
                message = "邀请码登录暂不可用，请稍后再试。"
            else:
                message = "登录请求无效，请刷新页面后重试。"
            return invite_login_rejection(
                message,
                protocol,
                status_code=exc.status_code,
                headers=exc.headers,
            )
        if (
            session_id
            and protocol.layer is not RequestLayer.LOGIN
            and not task_state_allowed
        ):
            # Controlled task-state errors have already consumed their one
            # authoritative frozen read-set.  The legacy task logger obtains
            # state with another store.load(), so leave these boundary errors
            # to the request/terminal trace instead of observing a later state.
            _record_protocol_event(
                runtime,
                session_id,
                kind=_event_kind_for_layer(protocol.layer),
                identity_key=_identity_key(request),
                protocol=protocol,
                error_kind="HTTPException",
            )
        return request_protocol_response(
            request,
            str(exc.detail),
            protocol,
            status_code=exc.status_code,
            headers=exc.headers,
            error_kind=type(exc).__name__,
            response_snapshot=snapshot,
            response_task_state_snapshot=response_task_state_snapshot,
            response_snapshot_attempted=response_snapshot_attempted,
        )

    @app.exception_handler(AgentRuntimeBusyError)
    async def runtime_busy(request: Request, exc: AgentRuntimeBusyError) -> JSONResponse:
        response_snapshot = _exception_response_snapshot(exc)
        response_search_id = str(
            response_snapshot.get("search_id") or exc.search_id
        ).strip()
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=response_search_id,
        )
        return request_protocol_response(
            request,
            str(exc),
            protocol,
            status_code=429,
            headers={"Retry-After": "15", "Cache-Control": "no-store"},
            error_kind=type(exc).__name__,
            response_snapshot=response_snapshot,
            # Admission rejection has no authoritative session read-set. Even
            # a malformed/custom Busy exception must not smuggle typed state
            # into the controlled-path response.
            response_task_state_snapshot=None,
            # Queue rejection occurs before the session lock/read-set exists.
            # It must neither read live state nor manufacture a typed view.
            capture_missing_task_state=False,
        )

    @app.exception_handler(AgentBudgetExceededError)
    async def runtime_budget(request: Request, exc: AgentBudgetExceededError) -> JSONResponse:
        response_snapshot = _exception_response_snapshot(exc)
        response_task_state_snapshot = _exception_task_state_snapshot(exc)
        response_search_id = str(
            response_snapshot.get("search_id") or exc.search_id
        ).strip()
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=response_search_id,
        )
        return request_protocol_response(
            request,
            str(exc),
            protocol,
            status_code=503,
            headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            error_kind=type(exc).__name__,
            response_snapshot=response_snapshot,
            response_task_state_snapshot=response_task_state_snapshot,
            response_snapshot_attempted=_exception_response_snapshot_attempted(exc),
        )

    @app.exception_handler(AgentProtocolError)
    async def protocol_error(request: Request, exc: AgentProtocolError) -> JSONResponse:
        response_snapshot = _exception_response_snapshot(exc)
        response_task_state_snapshot = _exception_task_state_snapshot(exc)
        response_search_id = str(
            response_snapshot.get("search_id") or exc.search_id
        ).strip()
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=response_search_id,
        )
        return request_protocol_response(
            request,
            str(exc),
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
            error_kind=type(exc).__name__,
            response_snapshot=response_snapshot,
            response_task_state_snapshot=response_task_state_snapshot,
            response_snapshot_attempted=_exception_response_snapshot_attempted(exc),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled public Agent request failure")
        clean_path = str(request.url.path or "").split("?", 1)[0]
        session_id = str(
            getattr(request.state, "session_id", "")
            or request.cookies.get(session_cookie)
            or ""
        ).strip()
        snapshot = _exception_response_snapshot(exc)
        response_task_state_snapshot = _exception_task_state_snapshot(exc)
        search_id = str(snapshot.get("search_id") or "").strip()
        protocol = RequestProtocol.from_code(
            "SERVICE_UNAVAILABLE",
            request_id=_request_id(request),
            search_id=search_id,
        )
        if (
            session_id
            and not _is_http_error_task_state_path(clean_path)
            and not isinstance(exc, SessionResponseSnapshotError)
        ):
            # Runtime work errors already carry their response-time snapshot
            # and task log.  Re-reading here would both duplicate that log and
            # let error rendering observe a newer store value.
            _record_protocol_event(
                runtime,
                session_id,
                kind="image" if request.url.path.startswith("/api/image") else "text",
                identity_key=_identity_key(request),
                protocol=protocol,
                error_kind="UnhandledException",
            )
        return request_protocol_response(
            request,
            "服务端处理失败，请稍后重试。",
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
            error_kind=type(exc).__name__,
            response_snapshot=snapshot,
            response_task_state_snapshot=response_task_state_snapshot,
            response_snapshot_attempted=_exception_response_snapshot_attempted(exc),
            persist_authoritative=not isinstance(exc, ResponseStoreError),
        )

    @app.middleware("http")
    async def secure_public_requests(request: Request, call_next):
        request_id = _incoming_request_id(request)
        trace_context = TraceContext.create(request_id=request_id)
        trace_meta: dict[str, object] = {
            "endpoint": _normalized_trace_endpoint(request.url.path),
            "response_mode": _trace_response_mode(request.url.path),
            "started_perf": time.perf_counter(),
        }
        request.state.request_id = request_id
        request.state.trace_context = trace_context
        with trace_context_scope(trace_context), trace_event_scope(
            trace_event_recorder,
            trace_id=trace_context.trace_id,
            request_id=request_id,
        ) as event_session, _public_trace_meta_scope(trace_meta):
            existing_session = str(request.cookies.get(session_cookie) or "").strip()
            if existing_session:
                bind_trace_event_dimensions(session_key=session_key(existing_session))
            record_trace_event(
                "request_received",
                stage="http_request",
                outcome="started",
                safe_attributes={
                    "method": request.method,
                    "endpoint": trace_meta["endpoint"],
                    "response_mode": trace_meta["response_mode"],
                },
            )
            try:
                if _forwarded_proto(request) == "http":
                    result = RedirectResponse(
                        str(request.url.replace(scheme="https")), status_code=308
                    )
                    _record_generic_terminal(result.status_code)
                    return result
                if invite_access is not None:
                    cookie_value = str(request.cookies.get(invite_access.cookie_name) or "")
                    identity = invite_access.verify_cookie(cookie_value)
                    request.state.invite_identity = identity
                    if isinstance(identity, InviteIdentity):
                        bind_trace_event_dimensions(identity_key=identity.invite_id)
                    public_path = (
                        request.url.path == "/health"
                        or request.url.path == "/invite"
                        or request.url.path == "/api/invite/login"
                        or request.url.path.startswith("/assets/")
                    )
                    if identity is None and not public_path:
                        if request.url.path.startswith("/api/"):
                            # Authentication failed before business execution.
                            # Do not acknowledge client fences here: a request
                            # that passed authentication concurrently may not
                            # have reached its per-session gate yet. The browser
                            # keeps those fences and reconciles them after login.
                            result = request_protocol_response(
                                request,
                                "请先使用有效邀请码登录。",
                                RequestProtocol.from_code(
                                    "LOGIN_REQUIRED", request_id=_request_id(request)
                                ),
                                status_code=401,
                                headers={"Cache-Control": "no-store"},
                            )
                            result.headers["X-Request-ID"] = _request_id(request)
                            return result
                        target = "/invite?reason=session_expired" if cookie_value else "/invite"
                        result = RedirectResponse(target, status_code=303)
                        _record_generic_terminal(result.status_code)
                        return result
                result = await call_next(request)
                result.headers["Content-Security-Policy"] = (
                    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
                    "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'"
                )
                result.headers["X-Content-Type-Options"] = "nosniff"
                result.headers["X-Frame-Options"] = "DENY"
                result.headers["Referrer-Policy"] = "no-referrer"
                result.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if _is_secure_request(request):
                    result.headers["Strict-Transport-Security"] = "max-age=31536000"
                if request.url.path.startswith("/api/"):
                    result.headers.setdefault("Cache-Control", "private, no-store")
                    result.headers["X-Request-ID"] = _request_id(request)
                if trace_meta["response_mode"] != "stream":
                    _record_generic_terminal(result.status_code)
                return result
            except asyncio.CancelledError:
                if not event_session.terminal_attempted:
                    record_public_terminal(
                        stage="http_request",
                        outcome="cancelled",
                        failed=True,
                        duration_ms=_trace_duration_ms(),
                        safe_attributes={
                            "endpoint": trace_meta["endpoint"],
                            "response_mode": trace_meta["response_mode"],
                            "http_status": 499,
                            "error_kind": "CancelledError",
                        },
                    )
                raise
            except BaseException as exc:
                if not event_session.terminal_attempted:
                    _record_protocol_terminal(
                        RequestProtocol.from_code(
                            "SERVICE_UNAVAILABLE",
                            request_id=_request_id(request),
                        ),
                        http_status=500,
                        error_kind=type(exc).__name__,
                    )
                raise

    @app.get("/invite", response_class=HTMLResponse)
    def invite_page(request: Request) -> Response:
        if invite_access is None:
            return RedirectResponse("/", status_code=303)
        identity = getattr(request.state, "invite_identity", None)
        if isinstance(identity, InviteIdentity):
            return RedirectResponse("/", status_code=303)
        error = ""
        if request.query_params.get("reason") == "session_expired":
            error = '<p class="error">登录状态已失效，请重新输入邀请码。</p>'
        return HTMLResponse(
            _INVITE_PAGE.replace("{error}", error), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/invite/login")
    async def invite_login(request: Request) -> Response:
        if invite_access is None:
            raise HTTPException(status_code=404, detail="invitation access is disabled")
        client_key = validated_client_key(
            str(request.headers.get("cf-connecting-ip") or ""),
            request.client.host if request.client else "unknown",
        )
        reservation, retry_after = invite_login_limiter.reserve_attempt(client_key)
        if reservation is None:
            protocol = RequestProtocol.from_code(
                "LOGIN_RATE_LIMITED", request_id=_request_id(request)
            )
            return invite_login_rejection(
                "登录尝试过多，请稍后再试。",
                protocol,
                status_code=429,
                headers={
                    "Retry-After": str(retry_after),
                },
            )
        reservation_completed = False
        try:
            try:
                body = await read_bounded_body(request, max_bytes=4096)
            except RequestBodyError as exc:
                detail = (
                    "invitation request is too large"
                    if exc.status_code == 413
                    else exc.detail
                )
                raise HTTPException(status_code=exc.status_code, detail=detail) from exc
            content_type = str(request.headers.get("content-type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/x-www-form-urlencoded":
                raise HTTPException(
                    status_code=415,
                    detail="invitation request must use form encoding",
                )
            try:
                form = parse_qs(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=8,
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="invalid invitation request",
                ) from exc
            identity = invite_access.authenticate_code(
                str((form.get("code") or [""])[0])
            )
            if identity is None:
                invite_login_limiter.complete_failure(reservation)
                reservation_completed = True
                return invite_login_rejection(
                    "邀请码无效或已停用，请检查后重试。",
                    RequestProtocol.from_code(
                        "INVITE_INVALID", request_id=_request_id(request)
                    ),
                    status_code=401,
                )
            result = RedirectResponse("/", status_code=303)
            result.set_cookie(
                invite_access.cookie_name,
                invite_access.issue_cookie(identity),
                max_age=invite_access.auth_max_age_seconds,
                httponly=True,
                secure=_is_secure_request(request),
                samesite="lax",
            )
            invite_login_limiter.complete_success(reservation)
            reservation_completed = True
            return result
        finally:
            if not reservation_completed:
                invite_login_limiter.cancel_attempt(reservation)

    @app.post("/api/invite/logout")
    def invite_logout(request: Request) -> Response:
        result = RedirectResponse("/invite", status_code=303)
        if invite_access is not None:
            result.delete_cookie(
                invite_access.cookie_name,
                secure=_is_secure_request(request),
                httponly=True,
                samesite="lax",
            )
        return result

    @app.post("/api/feedback")
    async def submit_feedback(request: Request) -> JSONResponse:
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if content_length > MAX_FEEDBACK_BYTES:
            raise HTTPException(status_code=413, detail="feedback is too large")
        try:
            raw_body = await request.body()
            if len(raw_body) > MAX_FEEDBACK_BYTES:
                raise HTTPException(status_code=413, detail="feedback is too large")
            payload = json.loads(raw_body)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed external input.
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object is required")
        message_id = str(payload.get("message_id") or "").strip()
        rated_response_id = str(payload.get("rated_response_id") or "").strip()
        rating = str(payload.get("rating") or "").strip().lower()
        raw_tags = payload.get("tags")
        detail = str(payload.get("detail") or "").strip()
        conversation = payload.get("conversation")
        if not FEEDBACK_MESSAGE_ID_RE.fullmatch(message_id):
            raise HTTPException(status_code=400, detail="invalid message id")
        if not rated_response_id:
            raise HTTPException(status_code=400, detail="rated response id is required")
        if not is_valid_response_id(rated_response_id):
            raise HTTPException(status_code=400, detail="invalid rated response id")
        if rating not in FEEDBACK_TAGS:
            raise HTTPException(status_code=400, detail="invalid rating")
        if not isinstance(raw_tags, list) or len(raw_tags) > 8:
            raise HTTPException(status_code=400, detail="invalid feedback tags")
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip()))
        if any(tag not in FEEDBACK_TAGS[rating] for tag in tags):
            raise HTTPException(status_code=400, detail="invalid feedback tag")
        if len(detail) > 300:
            raise HTTPException(status_code=400, detail="feedback detail is too long")
        if conversation is None:
            raise HTTPException(status_code=400, detail="feedback target is missing")
        if not isinstance(conversation, list):
            raise HTTPException(status_code=400, detail="invalid feedback conversation")
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session is required")
        identity_key = _identity_key(request) or "local"
        clean_session_key = session_key(session_id)
        try:
            rated_response = response_store.require_owned(
                rated_response_id,
                identity_key=identity_key,
                session_key=clean_session_key,
            )
        except ResponseValidationError as exc:
            raise HTTPException(status_code=400, detail="invalid rated response id") from exc
        except ResponseOwnershipError as exc:
            raise HTTPException(status_code=404, detail="rated response not found") from exc
        target_message = _feedback_target_message(conversation, message_id)
        if not target_message:
            raise HTTPException(status_code=400, detail="feedback target is missing")
        conversation_response_id = str(
            target_message.get("responseId")
            or target_message.get("response_id")
            or ""
        ).strip()
        if conversation_response_id != rated_response_id:
            raise HTTPException(
                status_code=400,
                detail="feedback target response does not match",
            )
        try:
            rated_protocol = RequestProtocol(
                status=rated_response.status,
                layer=rated_response.layer,
                code=rated_response.code,
                retryable=rated_response.retryable,
                action=rated_response.action,
                request_id=rated_response.request_id,
                search_id=rated_response.search_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="stored response protocol is invalid") from exc
        search_key = rated_response.search_id or rated_response.workflow_search_id
        search_duration_ms = _feedback_search_duration_ms(
            conversation,
            message_id,
            task_revision=rated_response.task_revision,
        )
        try:
            saved = feedback_store.upsert(
                message_id=message_id,
                rated_response_id=rated_response.response_id,
                identity_key=identity_key,
                session_key=clean_session_key,
                rating=rating,
                tags=tags,
                detail=detail,
                task_revision=rated_response.task_revision,
                phase=rated_response.phase,
                candidate_count=rated_response.candidate_count,
                search_duration_ms=search_duration_ms,
                search_key=search_key,
                request_id=rated_protocol.request_id,
                search_id=rated_protocol.search_id,
                status=rated_protocol.status.value,
                layer=rated_protocol.layer.value,
                code=rated_protocol.code,
                chapter=rated_response.chapter,
                image_route=rated_response.image_route,
                workflow_search_id=rated_response.workflow_search_id,
                intent=rated_response.intent,
                conversation=conversation,
                media_resolver=lambda url: _resolve_feedback_media(
                    runtime, session_id, url
                ),
                retention_days=(
                    int(feedback_retention_days_provider())
                    if feedback_retention_days_provider is not None
                    else 30
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        protocol = RequestProtocol(
            status=RequestStatus.SUCCESS,
            layer=RequestLayer.FEEDBACK,
            code="FEEDBACK_RECORDED",
            request_id=_request_id(request),
            search_id=rated_response.search_id,
        )
        _record_protocol_event(
            runtime,
            session_id,
            kind="feedback",
            identity_key=identity_key,
            protocol=protocol,
        )
        bind_trace_event_dimensions(
            feedback_id=saved.feedback_id,
            session_key=clean_session_key,
            identity_key=identity_key,
            workflow_search_id=rated_response.workflow_search_id,
            search_id=rated_response.search_id,
            rated_response_id=rated_response.response_id,
        )
        record_trace_event(
            "feedback_recorded",
            stage="feedback_store",
            outcome="success",
            protocol=protocol,
            safe_attributes={
                "rating": saved.rating,
                "feedback_scope": saved.feedback_scope,
            },
        )
        _record_protocol_terminal(protocol, http_status=200)
        return JSONResponse({
            "ok": True,
            "feedback": {
                "message_id": saved.message_id,
                "rated_response_id": saved.rated_response_id,
                "rating": saved.rating,
                "tags": list(saved.tags),
                "feedback_scope": saved.feedback_scope,
                "updated_at": saved.updated_at,
            },
            **protocol.to_dict(),
        })

    @app.delete("/api/feedback/{rated_response_id}")
    def delete_feedback(rated_response_id: str, request: Request) -> JSONResponse:
        rated_response_id = str(rated_response_id or "").strip()
        if not is_valid_response_id(rated_response_id):
            raise HTTPException(status_code=400, detail="invalid rated response id")
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session is required")
        identity_key = _identity_key(request) or "local"
        clean_session_key = session_key(session_id)
        try:
            rated_response = response_store.get_owned(
                rated_response_id,
                identity_key=identity_key,
                session_key=clean_session_key,
                include_expired=True,
            )
        except ResponseValidationError as exc:
            raise HTTPException(status_code=400, detail="invalid rated response id") from exc
        if rated_response is None:
            raise HTTPException(status_code=404, detail="rated response not found")
        removed = feedback_store.delete_by_response(
            rated_response_id=rated_response.response_id,
            identity_key=identity_key,
            session_key=clean_session_key,
        )
        protocol = RequestProtocol(
            status=RequestStatus.SUCCESS,
            layer=RequestLayer.FEEDBACK,
            code="FEEDBACK_REMOVED",
            request_id=_request_id(request),
            search_id=rated_response.search_id,
        )
        _record_protocol_event(
            runtime,
            session_id,
            kind="feedback",
            identity_key=identity_key,
            protocol=protocol,
        )
        bind_trace_event_dimensions(
            rated_response_id=rated_response.response_id,
            session_key=clean_session_key,
            identity_key=identity_key,
            workflow_search_id=rated_response.workflow_search_id,
            search_id=rated_response.search_id,
        )
        _record_protocol_terminal(protocol, http_status=200)
        return JSONResponse({
            "ok": True,
            "rated_response_id": rated_response.response_id,
            "removed": removed,
            **protocol.to_dict(),
        })

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        session_id = _session_id(request, cookie_name=session_cookie)
        result = HTMLResponse(_read_demo_page(), headers={"Cache-Control": "no-store"})
        _set_session_cookie(
            result,
            session_id,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )
        return result

    @app.get("/health")
    def health() -> dict[str, object]:
        trace_health = (
            trace_event_recorder.health()
            if trace_event_recorder is not None
            else {
                "status": "disabled",
                "written": 0,
                "dropped": 0,
                "write_failures": 0,
                "validation_rejections": 0,
                "duplicate_terminals": 0,
                "pending": 0,
                "queue_capacity": 0,
                "accepting": False,
                "last_failure_kind": "",
                "last_failure_at": "",
            }
        )
        return {"status": "ok", "trace_events": trace_health}

    def coordination_stale_response(
        session_id: str,
        *,
        request_id: str,
        capabilities: TaskStateEntryCapabilities,
    ) -> AgentResponse:
        try:
            captured = runtime.session_response_snapshot_v1(
                session_id,
                capabilities=capabilities,
                response_frozen=True,
            )
        except Exception as exc:
            setattr(exc, "_response_snapshot_attempted", True)
            _carry_frozen_task_state_failure(
                exc,
                runtime,
                legacy_snapshot=_exception_response_snapshot(exc),
                task_state=_exception_task_state_snapshot(exc),
            )
            raise
        if not _has_exact_response_task_state_pair(
            captured.legacy_session if type(captured) is SessionResponseSnapshotV1 else None,
            captured.task_state if type(captured) is SessionResponseSnapshotV1 else None,
        ):
            exc = RuntimeError("session coordination rejection snapshot is incomplete")
            setattr(exc, "_response_snapshot_attempted", True)
            _carry_frozen_task_state_failure(
                exc,
                runtime,
                legacy_snapshot=(
                    captured.legacy_session
                    if type(captured) is SessionResponseSnapshotV1
                    else None
                ),
                task_state=(
                    captured.task_state
                    if type(captured) is SessionResponseSnapshotV1
                    else None
                ),
            )
            raise exc
        snapshot = dict(captured.legacy_session)
        response = AgentResponse(
            text="这个请求已经失效，请重新连接后再试。",
            intent="stale_action",
            protocol=RequestProtocol.from_code(
                "STALE_ACTION",
                request_id=request_id,
                search_id=str(snapshot.get("search_id") or ""),
            ).to_dict(),
        )
        response.response_snapshot = snapshot
        response.response_projection_snapshot = dict(snapshot)
        response.response_task_state_snapshot = captured.task_state
        response.response_media_snapshot_captured = True
        response.uploaded_image_path = captured.uploaded_image_path
        return response

    def coordination_http_rejection(
        request: Request,
        session_id: str,
        reason: str,
    ) -> JSONResponse:
        snapshot: Mapping[str, object] = {}
        task_state: object = None
        if session_id:
            stale = coordination_stale_response(
                session_id,
                request_id=_request_id(request),
                capabilities=_JSON_TASK_STATE_CAPABILITIES,
            )
            snapshot = stale.response_snapshot
            task_state = stale.response_task_state_snapshot
        return request_protocol_response(
            request,
            reason,
            RequestProtocol.from_code(
                "STALE_ACTION",
                request_id=_request_id(request),
                search_id=str(snapshot.get("search_id") or ""),
            ),
            status_code=409,
            headers={"Cache-Control": "private, no-store"},
            response_snapshot=snapshot,
            response_task_state_snapshot=task_state,
            response_snapshot_attempted=bool(session_id),
        )

    async def coordinated_task_preflight(
        request: Request,
    ) -> tuple[_SessionCoordinationHeaders | None, Response | None]:
        """Validate browser task coordination before consuming request input."""

        coordination, rejection = _parse_session_coordination_headers(
            request,
            task_request=True,
        )
        if rejection:
            session_id = _session_id(request, cookie_name=session_cookie)

            def reject() -> JSONResponse:
                # Serialize the authoritative rejection snapshot with any
                # already-running task, but do not acknowledge an invalid or
                # absent fence as completed.
                with session_coordination.task(session_id, ""):
                    return coordination_http_rejection(
                        request,
                        session_id,
                        rejection,
                    )

            return None, await asyncio.to_thread(reject)
        assert coordination is not None
        if coordination.versioned:
            # Input validation still precedes the admission boundary.  An
            # error raised before the gate therefore carries an empty ACK and
            # leaves the exact browser fence available for reconciliation.
            _mark_session_coordination_ack(request)
        return coordination, None

    def coordination_stale_json_response(
        request: Request,
        session_id: str,
        *,
        capabilities: TaskStateEntryCapabilities,
    ) -> JSONResponse:
        stale = coordination_stale_response(
            session_id,
            request_id=_request_id(request),
            capabilities=capabilities,
        )
        return _agent_json(
            stale,
            runtime,
            session_id,
            uploaded_image=_task_state_uploaded_image(stale, runtime),
            response_store=response_store,
            identity_key=_identity_key(request) or "local",
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
            task_state_capabilities=capabilities,
        )

    async def run_coordinated_json_task(
        request: Request,
        coordination: _SessionCoordinationHeaders,
        session_id: str,
        execute: Callable[[], Response],
        *,
        capabilities: TaskStateEntryCapabilities,
    ) -> Response:
        """Hold the per-session gate for the complete synchronous task path."""

        if not coordination.versioned:
            # Headerless non-browser callers predate browser coordination and
            # retain their existing runtime admission/queue semantics.
            return await asyncio.to_thread(execute)

        def coordinated_execute() -> Response:
            with session_coordination.task(
                session_id,
                coordination.request_fence,
            ) as admitted:
                _mark_session_coordination_ack(
                    request,
                    _coordination_completed_fences(coordination),
                )
                if admitted:
                    return execute()
                return coordination_stale_json_response(
                    request,
                    session_id,
                    capabilities=capabilities,
                )

        result = await asyncio.to_thread(coordinated_execute)
        return _attach_session_coordination_ack(result, request)

    def coordinated_stream_response(
        request: Request,
        coordination: _SessionCoordinationHeaders,
        session_id: str,
        execute: Callable[[Callable[[str, str], None]], dict[str, object]],
        *,
        capabilities: TaskStateEntryCapabilities,
    ) -> StreamingResponse:
        """Bind a stream's gate to the real worker, including cancellation."""

        request_id = _request_id(request)
        identity_key = _identity_key(request)
        completed_fences = _coordination_completed_fences(coordination)
        crossed_boundary = threading.Event()

        def coordinated_execute(
            progress: Callable[[str, str], None],
        ) -> dict[str, object]:
            with session_coordination.task(
                session_id,
                coordination.request_fence,
            ) as admitted:
                if coordination.versioned:
                    _mark_session_coordination_ack(
                        request,
                        completed_fences,
                    )
                    crossed_boundary.set()
                if admitted:
                    return execute(progress)
                stale = coordination_stale_response(
                    session_id,
                    request_id=request_id,
                    capabilities=capabilities,
                )
                return _agent_payload(
                    stale,
                    runtime,
                    session_id,
                    uploaded_image=_task_state_uploaded_image(stale, runtime),
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=capabilities,
                )

        def completed_ack() -> tuple[str, ...] | None:
            if not coordination.versioned or not crossed_boundary.is_set():
                return None
            return completed_fences

        worker_execute = coordinated_execute if coordination.versioned else execute
        acknowledgement = completed_ack if coordination.versioned else None

        result = StreamingResponse(
            _stream_agent_events(
                worker_execute,
                request_id=request_id,
                trace_context=_request_trace_context(request),
                trace_event_session=current_trace_event_session(),
                trace_meta=_current_public_trace_meta(),
                runtime=runtime,
                response_store=response_store,
                session_id=session_id,
                identity_key=identity_key or "local",
                task_state_capabilities=capabilities,
                session_coordination_ack=acknowledgement,
            ),
            media_type="application/x-ndjson",
        )
        _set_session_cookie(
            result,
            session_id,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )
        return result

    def coordinated_reconciliation_endpoint(*, mint_session: bool):
        def decorate(endpoint):
            @wraps(endpoint)
            def wrapped(request: Request):
                if mint_session:
                    session_id = _session_id(request, cookie_name=session_cookie)
                else:
                    session_id = str(request.cookies.get(session_cookie) or "").strip()
                    if session_id:
                        session_id = _session_id(request, cookie_name=session_cookie)
                coordination, rejection = _parse_session_coordination_headers(
                    request,
                    task_request=False,
                )
                if coordination is not None and coordination.versioned:
                    _mark_session_coordination_ack(request)
                if not session_id:
                    if rejection:
                        return coordination_http_rejection(request, "", rejection)
                    assert coordination is not None
                    if coordination.versioned:
                        _mark_session_coordination_ack(
                            request,
                            _coordination_completed_fences(coordination),
                        )
                    return _attach_session_coordination_ack(endpoint(request), request)
                if rejection:
                    with session_coordination.reconcile(session_id, ()):
                        return coordination_http_rejection(request, session_id, rejection)
                assert coordination is not None
                with session_coordination.reconcile(
                    session_id,
                    coordination.reconcile_fences,
                    coordination.request_fence,
                ) as admitted:
                    if coordination.versioned:
                        _mark_session_coordination_ack(
                            request,
                            _coordination_completed_fences(coordination),
                        )
                    if not admitted:
                        return coordination_http_rejection(
                            request,
                            session_id,
                            "session coordination request fence was already completed",
                        )
                    return _attach_session_coordination_ack(endpoint(request), request)

            return wrapped

        return decorate

    @app.get("/api/session")
    @coordinated_reconciliation_endpoint(mint_session=True)
    def session(request: Request) -> JSONResponse:
        session_id = _session_id(request, cookie_name=session_cookie)
        try:
            captured = runtime.session_response_snapshot_v1(
                session_id,
                capabilities=_SESSION_TASK_STATE_CAPABILITIES,
            )
        except Exception as exc:
            setattr(exc, "_response_snapshot_attempted", True)
            _carry_frozen_task_state_failure(
                exc,
                runtime,
                legacy_snapshot=_exception_response_snapshot(exc),
                task_state=_exception_task_state_snapshot(exc),
            )
            raise
        captured_legacy = (
            captured.legacy_session
            if type(captured) is SessionResponseSnapshotV1
            else None
        )
        captured_task_state = (
            captured.task_state
            if type(captured) is SessionResponseSnapshotV1
            else None
        )
        if not _has_exact_response_task_state_pair(
            captured_legacy,
            captured_task_state,
        ):
            exc = RuntimeError("session endpoint returned an incomplete response snapshot")
            setattr(exc, "_response_snapshot_attempted", True)
            _carry_frozen_task_state_failure(
                exc,
                runtime,
                legacy_snapshot=captured_legacy,
                task_state=captured_task_state,
            )
            raise exc
        path = captured.uploaded_image_path
        try:
            payload = with_public_task_state(
                {
                    "uploaded_image": (
                        f"/api/upload/{path.name}" if path is not None else ""
                    ),
                    "session": _public_session_snapshot(captured.legacy_session),
                },
                captured.task_state,
            )
        except Exception as exc:
            # Mapping is part of the controlled response boundary. Preserve
            # the already-frozen pair so the 500 handler neither rereads live
            # state nor loses task_state when the mapper itself fails.
            setattr(exc, "response_snapshot", dict(captured.legacy_session))
            setattr(exc, "response_task_state_snapshot", captured.task_state)
            raise
        result = JSONResponse(payload)
        _set_session_cookie(
            result,
            session_id,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )
        return result

    @app.post("/api/message")
    async def message(request: Request) -> Response:
        coordination, coordination_rejection = await coordinated_task_preflight(request)
        if coordination_rejection is not None:
            return coordination_rejection
        assert coordination is not None
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed external input.
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object is required")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        session_id = _session_id(request, cookie_name=session_cookie)
        request_id = _request_id(request)
        identity_key = _identity_key(request)
        secure_cookie = _is_secure_request(request)

        def execute() -> Response:
            action_context = payload.get("action_context")
            stale = _validate_action_context(
                runtime,
                session_id,
                action_context,
                request_id=request_id,
                task_state_capabilities=_JSON_TASK_STATE_CAPABILITIES,
            )
            if stale is not None:
                return _agent_json(
                    stale,
                    runtime,
                    session_id,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    secure_cookie=secure_cookie,
                    cookie_name=session_cookie,
                    task_state_capabilities=_JSON_TASK_STATE_CAPABILITIES,
                )
            authorized_text = _authorized_action_text(action_context) or text
            response = _handle_text(
                runtime,
                session_id,
                authorized_text,
                action_context=action_context,
                request_id=request_id,
                identity_key=identity_key,
                task_state_capabilities=_JSON_TASK_STATE_CAPABILITIES,
            )
            return _agent_json(
                response,
                runtime,
                session_id,
                response_store=response_store,
                identity_key=identity_key or "local",
                secure_cookie=secure_cookie,
                cookie_name=session_cookie,
                task_state_capabilities=_JSON_TASK_STATE_CAPABILITIES,
            )

        # The JSON compatibility endpoint runs the same synchronous model and
        # search pipeline as the streaming endpoint. Keep it off the ASGI event
        # loop so /health remains responsive during a long provider call.
        return await run_coordinated_json_task(
            request,
            coordination,
            session_id,
            execute,
            capabilities=_JSON_TASK_STATE_CAPABILITIES,
        )

    @app.post("/api/message/stream")
    async def message_stream(request: Request) -> Response:
        coordination, coordination_rejection = await coordinated_task_preflight(request)
        if coordination_rejection is not None:
            return coordination_rejection
        assert coordination is not None
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed external input.
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="json object is required")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        session_id = _session_id(request, cookie_name=session_cookie)
        request_id = _request_id(request)
        identity_key = _identity_key(request)

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            action_context = payload.get("action_context")
            stale = _validate_action_context(
                runtime,
                session_id,
                action_context,
                request_id=request_id,
                task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )
            if stale is not None:
                return _agent_payload(
                    stale,
                    runtime,
                    session_id,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
                )
            authorized_text = _authorized_action_text(action_context) or text
            response = _handle_text(
                runtime,
                session_id,
                authorized_text,
                action_context=action_context,
                request_id=request_id,
                identity_key=identity_key,
                progress=progress,
                task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )
            return _agent_payload(
                response,
                runtime,
                session_id,
                response_store=response_store,
                identity_key=identity_key or "local",
                response_mode="stream",
                defer_authoritative=True,
                include_task_state=True,
                task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )

        return coordinated_stream_response(
            request,
            coordination,
            session_id,
            execute,
            capabilities=_STREAM_TASK_STATE_CAPABILITIES,
        )

    @app.post("/api/reset")
    @coordinated_reconciliation_endpoint(mint_session=False)
    def reset(request: Request) -> JSONResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        search_id = ""
        if session_id:
            try:
                captured = runtime.session_response_snapshot_v1(
                    session_id,
                    capabilities=_JSON_TASK_STATE_CAPABILITIES,
                    response_frozen=True,
                )
            except Exception as exc:
                setattr(exc, "_response_snapshot_attempted", True)
                _carry_frozen_task_state_failure(
                    exc,
                    runtime,
                    legacy_snapshot=_exception_response_snapshot(exc),
                    task_state=_exception_task_state_snapshot(exc),
                )
                raise
            captured_legacy = (
                captured.legacy_session
                if type(captured) is SessionResponseSnapshotV1
                else None
            )
            captured_task_state = (
                captured.task_state
                if type(captured) is SessionResponseSnapshotV1
                else None
            )
            if not _has_exact_response_task_state_pair(
                captured_legacy,
                captured_task_state,
            ):
                exc = RuntimeError(
                    "reset preflight returned an incomplete response snapshot"
                )
                setattr(exc, "_response_snapshot_attempted", True)
                _carry_frozen_task_state_failure(
                    exc,
                    runtime,
                    legacy_snapshot=captured_legacy,
                    task_state=captured_task_state,
                )
                raise exc
            search_id = str(captured.legacy_session.get("search_id") or "")
            clear_kwargs: dict[str, object] = {}
            try:
                if _accepts_keyword(runtime.clear, "task_state_capabilities"):
                    clear_kwargs["task_state_capabilities"] = (
                        _JSON_TASK_STATE_CAPABILITIES
                    )
            except Exception:  # noqa: BLE001 - compatibility runtime; fallback below.
                clear_kwargs = {}
            try:
                runtime.clear(session_id, **clear_kwargs)
            except Exception as exc:
                # Production runtimes capture while their clear lock is still
                # held.  A compatibility runtime without that hook gets one
                # immediate post-attempt capture here. Never reuse the
                # pre-clear pair: a parent or child store may already be gone.
                error_legacy = _exception_response_snapshot(exc)
                error_task_state = _exception_task_state_snapshot(exc)
                if not _has_exact_response_task_state_pair(
                    error_legacy,
                    error_task_state,
                ):
                    if (
                        error_legacy
                        or type(error_task_state) is TaskStateSnapshotV1
                        or _exception_response_snapshot_attempted(exc)
                    ):
                        error_legacy = error_legacy or {"session_valid": False}
                        error_task_state = _unreadable_http_error_task_state(runtime)
                    else:
                        error_legacy, error_task_state = _safe_error_snapshot(
                            runtime,
                            session_id,
                            capabilities=_JSON_TASK_STATE_CAPABILITIES,
                        )
                if error_legacy:
                    try:
                        setattr(exc, "response_snapshot", dict(error_legacy))
                    except Exception:  # noqa: BLE001 - preserve the clear failure.
                        pass
                if type(error_task_state) is TaskStateSnapshotV1:
                    try:
                        setattr(
                            exc,
                            "response_task_state_snapshot",
                            error_task_state,
                        )
                    except Exception:  # noqa: BLE001 - preserve the clear failure.
                        pass
                raise
        protocol = RequestProtocol(
            status=RequestStatus.SUCCESS,
            layer=RequestLayer.SESSION,
            code="SESSION_RESET",
            request_id=_request_id(request),
            search_id=search_id,
        )
        reset_task_state = empty_task_state_snapshot()
        try:
            reset_payload = with_public_task_state(
                {"ok": True, **protocol.to_dict()},
                reset_task_state,
            )
        except Exception as exc:
            setattr(exc, "response_snapshot", {"session_valid": False})
            setattr(exc, "response_task_state_snapshot", reset_task_state)
            raise
        result = JSONResponse(reset_payload)
        result.delete_cookie(session_cookie, secure=_is_secure_request(request), httponly=True, samesite="lax")
        return result

    @app.post("/api/image")
    async def image(request: Request) -> Response:
        coordination, coordination_rejection = await coordinated_task_preflight(request)
        if coordination_rejection is not None:
            return coordination_rejection
        assert coordination is not None
        content, filename, content_type = await _read_image_upload(request)
        incoming_suffix = _validated_incoming_image_suffix(
            content,
            filename,
            content_type,
        )
        # Missing, oversized, and undecodable input is rejected before a new
        # session exists. A failed upload must not mint a Cookie merely so the
        # HTTP error can carry task state.
        session_id = _session_id(request, cookie_name=session_cookie)
        request_id = _request_id(request)
        identity_key = _identity_key(request)
        secure_cookie = _is_secure_request(request)

        def execute() -> Response:
            incoming = _write_validated_incoming_image(
                content,
                incoming_suffix,
                incoming_dir=incoming_dir,
            )
            try:
                response = _handle_image(
                    runtime,
                    session_id,
                    incoming,
                    request_id=request_id,
                    identity_key=identity_key,
                    task_state_capabilities=_IMAGE_JSON_TASK_STATE_CAPABILITIES,
                )
                uploaded_image = _task_state_uploaded_image(response, runtime)
                return _agent_json(
                    response,
                    runtime,
                    session_id,
                    uploaded_image=uploaded_image,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    secure_cookie=secure_cookie,
                    cookie_name=session_cookie,
                    task_state_capabilities=_IMAGE_JSON_TASK_STATE_CAPABILITIES,
                )
            finally:
                incoming.unlink(missing_ok=True)

        return await run_coordinated_json_task(
            request,
            coordination,
            session_id,
            execute,
            capabilities=_IMAGE_JSON_TASK_STATE_CAPABILITIES,
        )

    @app.post("/api/image/stream")
    async def image_stream(request: Request) -> Response:
        coordination, coordination_rejection = await coordinated_task_preflight(request)
        if coordination_rejection is not None:
            return coordination_rejection
        assert coordination is not None
        content, filename, content_type = await _read_image_upload(request)
        session_id = _session_id(request, cookie_name=session_cookie)
        incoming_suffix = _validated_incoming_image_suffix(
            content,
            filename,
            content_type,
        )
        request_id = _request_id(request)
        identity_key = _identity_key(request)

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            incoming = _write_validated_incoming_image(
                content,
                incoming_suffix,
                incoming_dir=incoming_dir,
            )
            try:
                response = _handle_image(
                    runtime,
                    session_id,
                    incoming,
                    request_id=request_id,
                    identity_key=identity_key,
                    progress=progress,
                    task_state_capabilities=_IMAGE_STREAM_TASK_STATE_CAPABILITIES,
                )
                uploaded_image = _task_state_uploaded_image(response, runtime)
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    uploaded_image=uploaded_image,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=_IMAGE_STREAM_TASK_STATE_CAPABILITIES,
                )
            finally:
                incoming.unlink(missing_ok=True)

        return coordinated_stream_response(
            request,
            coordination,
            session_id,
            execute,
            capabilities=_IMAGE_STREAM_TASK_STATE_CAPABILITIES,
        )

    @app.get("/api/upload/{filename}")
    def get_upload(filename: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        path = runtime.resolve_upload(session_id, filename) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="upload not found")
        return FileResponse(path, headers={"Cache-Control": media_cache_header})

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        path = runtime.resolve_media(session_id, media_id) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(path, headers={"Cache-Control": media_cache_header})

    if bool(getattr(runtime, "a3_enabled", False)):

        def required_a3_task_revision(payload: Mapping[str, object]) -> int:
            value = payload.get("task_revision")
            if type(value) is not int or value <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="task_revision is required",
                )
            return value

        @app.post("/api/a3/select")
        async def a3_select(request: Request) -> Response:
            coordination, coordination_rejection = await coordinated_task_preflight(
                request
            )
            if coordination_rejection is not None:
                return coordination_rejection
            assert coordination is not None
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="json object is required")
            unit_id = str(payload.get("unit_id") or "").strip()
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            workflow_search_id = str(payload.get("workflow_id") or "").strip()
            if not workflow_search_id:
                raise HTTPException(status_code=400, detail="workflow_id is required")
            task_revision = required_a3_task_revision(payload)
            session_id = _session_id(request, cookie_name=session_cookie)
            identity_key = _identity_key(request)
            kwargs: dict[str, object] = {
                "task_revision": task_revision,
                "workflow_search_id": workflow_search_id,
                "request_id": _request_id(request),
                "task_state_capabilities": _JSON_TASK_STATE_CAPABILITIES,
            }
            if identity_key:
                kwargs["identity_key"] = identity_key

            def execute() -> Response:
                response = runtime.select_unit(  # type: ignore[attr-defined]
                    session_id,
                    unit_id,
                    **kwargs,
                )
                return _agent_json(
                    response,
                    runtime,
                    session_id,
                    submitted_crop=response.submitted_crop_path,
                    response_store=response_store,
                    identity_key=_identity_key(request) or "local",
                    secure_cookie=_is_secure_request(request),
                    cookie_name=session_cookie,
                    task_state_capabilities=_JSON_TASK_STATE_CAPABILITIES,
                )

            return await run_coordinated_json_task(
                request,
                coordination,
                session_id,
                execute,
                capabilities=_JSON_TASK_STATE_CAPABILITIES,
            )

        @app.post("/api/a3/select/stream")
        async def a3_select_stream(request: Request) -> Response:
            coordination, coordination_rejection = await coordinated_task_preflight(
                request
            )
            if coordination_rejection is not None:
                return coordination_rejection
            assert coordination is not None
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="json object is required")
            unit_id = str(payload.get("unit_id") or "").strip()
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            workflow_search_id = str(payload.get("workflow_id") or "").strip()
            if not workflow_search_id:
                raise HTTPException(status_code=400, detail="workflow_id is required")
            task_revision = required_a3_task_revision(payload)
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "workflow_search_id": workflow_search_id,
                    "progress": progress,
                    "request_id": request_id,
                    "task_state_capabilities": _STREAM_TASK_STATE_CAPABILITIES,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.select_unit(  # type: ignore[attr-defined]
                    session_id,
                    unit_id,
                    **kwargs,
                )
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    submitted_crop=response.submitted_crop_path,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
                )

            return coordinated_stream_response(
                request,
                coordination,
                session_id,
                execute,
                capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )

        @app.post("/api/a3/prepare/stream")
        async def a3_prepare_stream(request: Request) -> Response:
            coordination, coordination_rejection = await coordinated_task_preflight(
                request
            )
            if coordination_rejection is not None:
                return coordination_rejection
            assert coordination is not None
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("unit_ids"), list):
                raise HTTPException(status_code=400, detail="unit_ids array is required")
            unit_ids = [str(value or "").strip() for value in payload["unit_ids"]]
            if not unit_ids or any(not value for value in unit_ids):
                raise HTTPException(status_code=400, detail="unit_ids are required")
            workflow_search_id = str(payload.get("workflow_id") or "").strip()
            if not workflow_search_id:
                raise HTTPException(status_code=400, detail="workflow_id is required")
            task_revision = required_a3_task_revision(payload)
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "workflow_search_id": workflow_search_id,
                    "progress": progress,
                    "request_id": request_id,
                    "task_state_capabilities": _STREAM_TASK_STATE_CAPABILITIES,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.prepare_units(  # type: ignore[attr-defined]
                    session_id,
                    unit_ids,
                    **kwargs,
                )
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
                )

            return coordinated_stream_response(
                request,
                coordination,
                session_id,
                execute,
                capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )

        @app.post("/api/a3/crop/stream")
        async def a3_crop_stream(request: Request) -> Response:
            coordination, coordination_rejection = await coordinated_task_preflight(
                request
            )
            if coordination_rejection is not None:
                return coordination_rejection
            assert coordination is not None
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("bounds"), dict):
                raise HTTPException(status_code=400, detail="crop bounds are required")
            unit_id = str(payload.get("unit_id") or "").strip()
            workflow_search_id = str(payload.get("workflow_id") or "").strip()
            if not workflow_search_id:
                raise HTTPException(status_code=400, detail="workflow_id is required")
            task_revision = required_a3_task_revision(payload)
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "progress": progress,
                    "request_id": request_id,
                    "task_state_capabilities": _STREAM_TASK_STATE_CAPABILITIES,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.handle_crop(  # type: ignore[attr-defined]
                    session_id,
                    payload["bounds"],
                    unit_id=unit_id,
                    task_revision=task_revision,
                    workflow_search_id=workflow_search_id,
                    **kwargs,
                )
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    submitted_crop=response.submitted_crop_path,
                    response_store=response_store,
                    identity_key=identity_key or "local",
                    response_mode="stream",
                    defer_authoritative=True,
                    include_task_state=True,
                    task_state_capabilities=_STREAM_TASK_STATE_CAPABILITIES,
                )

            return coordinated_stream_response(
                request,
                coordination,
                session_id,
                execute,
                capabilities=_STREAM_TASK_STATE_CAPABILITIES,
            )

        @app.get("/api/a3/crop/{unit_id}")
        def get_a3_crop(
            unit_id: str,
            request: Request,
            workflow_id: str,
            task_revision: int,
        ) -> FileResponse:
            session_id = str(request.cookies.get(session_cookie) or "").strip()
            clean_workflow_id = str(workflow_id or "").strip()
            if not clean_workflow_id or type(task_revision) is not int or task_revision <= 0:
                raise HTTPException(status_code=400, detail="invalid media identity")
            path = (
                runtime.current_crop_path(  # type: ignore[attr-defined]
                    session_id,
                    unit_id,
                    expected_workflow_id=clean_workflow_id,
                    expected_task_revision=task_revision,
                )
                if session_id
                else None
            )
            if path is None:
                raise HTTPException(status_code=404, detail="crop not found")
            return FileResponse(path, headers={"Cache-Control": "private, no-store"})

        @app.get("/api/a3/overlay")
        def get_a3_overlay(
            request: Request,
            workflow_id: str,
            task_revision: int,
        ) -> FileResponse:
            session_id = str(request.cookies.get(session_cookie) or "").strip()
            clean_workflow_id = str(workflow_id or "").strip()
            if not clean_workflow_id or type(task_revision) is not int or task_revision <= 0:
                raise HTTPException(status_code=400, detail="invalid media identity")
            path = (
                runtime.current_auto_crop_overlay_path(  # type: ignore[attr-defined]
                    session_id,
                    expected_workflow_id=clean_workflow_id,
                    expected_task_revision=task_revision,
                )
                if session_id
                else None
            )
            if path is None:
                raise HTTPException(status_code=404, detail="overlay not found")
            return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    return app


@contextmanager
def _public_trace_meta_scope(meta: dict[str, object]):
    token = _ACTIVE_PUBLIC_TRACE_META.set(meta)
    try:
        yield meta
    finally:
        _ACTIVE_PUBLIC_TRACE_META.reset(token)


def _current_public_trace_meta() -> dict[str, object]:
    return dict(_ACTIVE_PUBLIC_TRACE_META.get() or {})


def _normalized_trace_endpoint(path: object) -> str:
    clean = str(path or "/").split("?", 1)[0]
    for prefix, template in (
        ("/api/media/", "/api/media/:id"),
        ("/api/upload/", "/api/upload/:id"),
        ("/api/a3/crop/", "/api/a3/crop/:unit_id"),
        ("/api/feedback/", "/api/feedback/:id"),
        ("/assets/", "/assets/:id"),
    ):
        if clean.startswith(prefix):
            return template
    return clean if re.fullmatch(r"/[A-Za-z0-9_/:.-]{0,127}", clean) else "/unknown"


def _trace_response_mode(path: object) -> str:
    clean = str(path or "").split("?", 1)[0]
    if clean.endswith("/stream"):
        return "stream"
    if clean == "/api/invite/login":
        return "html"
    return "json" if clean.startswith("/api/") or clean == "/health" else "html"


def _is_authoritative_reply_path(path: object) -> bool:
    return str(path or "").split("?", 1)[0] in {
        "/api/message",
        "/api/message/stream",
        "/api/image",
        "/api/image/stream",
        "/api/a3/select",
        "/api/a3/select/stream",
        "/api/a3/prepare/stream",
        "/api/a3/crop/stream",
    }


def _is_http_error_task_state_path(path: object) -> bool:
    """Return whether a non-stream Agent API may expose frozen task state."""

    return str(path or "").split("?", 1)[0] in {
        "/api/session",
        "/api/message",
        "/api/image",
        "/api/a3/select",
        "/api/reset",
    }


def _trace_duration_ms() -> int:
    started = _current_public_trace_meta().get("started_perf")
    return max(0, round((time.perf_counter() - float(started)) * 1000)) if started else 0


def _protocol_event_outcome(protocol: RequestProtocol) -> str:
    return {
        RequestStatus.SUCCESS: "success",
        RequestStatus.NO_MATCH: "no_match",
        RequestStatus.NEEDS_INPUT: "needs_input",
        RequestStatus.PARTIAL: "partial",
        RequestStatus.ERROR: "error",
    }[protocol.status]


def _record_protocol_terminal(
    protocol: RequestProtocol,
    *,
    http_status: int,
    error_kind: str = "ProtocolFailure",
    extra_attributes: Mapping[str, object] | None = None,
) -> None:
    meta = _current_public_trace_meta()
    attributes: dict[str, object] = {
        "endpoint": str(meta.get("endpoint") or "/unknown"),
        "response_mode": str(meta.get("response_mode") or "json"),
        "http_status": int(http_status),
    }
    if protocol.status is RequestStatus.ERROR:
        attributes["error_kind"] = error_kind
    else:
        attributes.update(dict(extra_attributes or {}))
    dimensions = {
        key: value
        for key, value in {
            "request_id": protocol.request_id,
            "search_id": protocol.search_id,
        }.items()
        if value
    }
    record_public_terminal(
        stage="public_response",
        outcome=_protocol_event_outcome(protocol),
        failed=protocol.status is RequestStatus.ERROR,
        protocol=protocol,
        duration_ms=_trace_duration_ms(),
        safe_attributes=attributes,
        **dimensions,
    )


def _record_generic_terminal(http_status: int) -> None:
    session = current_trace_event_session()
    if session is None or session.terminal_attempted:
        return
    meta = _current_public_trace_meta()
    failed = int(http_status) >= 400
    attributes: dict[str, object] = {
        "endpoint": str(meta.get("endpoint") or "/unknown"),
        "response_mode": str(meta.get("response_mode") or "html"),
        "http_status": int(http_status),
    }
    if failed:
        attributes["error_kind"] = "HttpFailure" if int(http_status) >= 500 else "HttpRejection"
    record_public_terminal(
        stage="http_response",
        outcome="error" if int(http_status) >= 500 else "rejected" if failed else "success",
        failed=failed,
        duration_ms=_trace_duration_ms(),
        safe_attributes=attributes,
    )


def _incoming_request_id(request: Request) -> str:
    value = str(request.headers.get("x-request-id") or "").strip()
    if re.fullmatch(r"req_[A-Fa-f0-9]{32}", value):
        return value.lower()
    return new_request_id()


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or new_request_id())


def _request_trace_context(request: Request) -> TraceContext:
    context = getattr(request.state, "trace_context", None)
    if not isinstance(context, TraceContext):
        raise RuntimeError("request trace context is unavailable")
    return context


def _protocol_json_response(
    detail: str,
    protocol: RequestProtocol,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
    output_watchdog: object | None = None,
    error_kind: str = "ProtocolFailure",
    response_store: SQLiteResponseStore | None = None,
    identity_key: str = "local",
    session_id: str = "",
    response_mode: str = "json",
    response_snapshot: Mapping[str, object] | None = None,
    response_task_state_snapshot: object = None,
    response_task_state_fallback: object = None,
    trace_id: str = "",
) -> JSONResponse:
    protocol = _public_response_protocol(protocol)
    message = _public_protocol_message(protocol)
    observe_output(
        output_watchdog,
        message,
        intent="http_error",
        protocol_code=protocol.code,
        media_status="",
        endpoint="http_error",
        session_id="",
    )
    payload: dict[str, object] = {"detail": message, **protocol.to_dict()}
    if type(response_task_state_snapshot) is TaskStateSnapshotV1:
        try:
            payload = with_public_task_state(payload, response_task_state_snapshot)
        except Exception:  # noqa: BLE001 - fail closed with a stable typed sentinel.
            logger.exception("public task-state mapping failed during HTTP error handling")
            fallback = (
                response_task_state_fallback
                if type(response_task_state_fallback) is TaskStateSnapshotV1
                else _unreadable_task_state_like(response_task_state_snapshot)
            )
            # Do not silently drop the field: mapping failure is an invariant
            # breach, so publish a canonical INCONSISTENT value through the
            # lower-level exact mapper.  If this defensive mapping itself ever
            # fails, let the response fail closed instead of emitting a
            # plausible error envelope without authoritative state.
            payload[PUBLIC_TASK_STATE_FIELD] = public_task_state_snapshot(fallback)
    if response_store is not None and session_id:
        snapshot = response_snapshot if isinstance(response_snapshot, Mapping) else {}
        raw_a3 = snapshot.get("a3") if isinstance(snapshot, Mapping) else None
        selected = raw_a3.get("selected_unit") if isinstance(raw_a3, Mapping) else None
        intent = (
            "a3_page_error"
            if str(snapshot.get("image_route") or "").strip().upper() == "A3"
            and not (
                isinstance(selected, Mapping)
                and str(selected.get("unit_id") or "").strip()
            )
            else "request_error"
        )
        try:
            _finalize_authoritative_response(
                payload,
                response_store=response_store,
                identity_key=identity_key,
                session_id=session_id,
                response_mode=response_mode,
                snapshot=snapshot,
                intent=intent,
                stream_event_type="",
                trace_id=trace_id,
            )
        except Exception:  # noqa: BLE001 - preserve the original safe failure.
            logger.exception("authoritative error response persistence failed")
    _record_protocol_terminal(
        protocol,
        http_status=status_code,
        error_kind=error_kind,
    )
    return JSONResponse(
        payload,
        status_code=status_code,
        headers=headers,
    )


def _public_protocol_message(protocol: RequestProtocol) -> str:
    """Return catalog text for an API-bound protocol error."""

    message = _PUBLIC_PROTOCOL_MESSAGES.get(protocol.code)
    if message:
        return message
    if protocol.status is RequestStatus.ERROR:
        return "服务暂时异常，请稍后重试。"
    if protocol.status is RequestStatus.NEEDS_INPUT:
        return "请求信息不完整，请按提示重新提交。"
    if protocol.status is RequestStatus.PARTIAL:
        return "这次处理未完全完成，请稍后重试。"
    if protocol.status is RequestStatus.NO_MATCH:
        return "暂时没有找到足够可靠的结果。"
    return "请求已完成。"


def _public_response_protocol(protocol: RequestProtocol) -> RequestProtocol:
    """Project arbitrary internal protocol metadata onto the public registry."""

    code = protocol.code
    registered = PROTOCOL_REASONS.get(code)
    registered_match = (
        registered is not None
        and registered.status is protocol.status
        and registered.layer is protocol.layer
    )
    tool_match = (
        protocol.layer is RequestLayer.TOOL
        and is_public_tool_code(code, protocol.status)
    )
    request_id = _public_state_id(protocol.request_id, prefix="req_")
    search_id = _public_state_id(protocol.search_id, prefix="search_")
    if registered_match:
        return RequestProtocol.from_code(
            code,
            request_id=request_id,
            search_id=search_id,
        )

    fallback_code = {
        RequestStatus.SUCCESS: "REQUEST_SUCCEEDED",
        RequestStatus.NO_MATCH: "NO_MATCH",
        RequestStatus.NEEDS_INPUT: "TOOL_INPUT_REQUIRED",
        RequestStatus.PARTIAL: "PARTIAL_RESULT",
        RequestStatus.ERROR: "TOOL_FAILED",
    }[protocol.status]
    fallback = RequestProtocol.from_code(
        fallback_code,
        request_id=request_id,
        search_id=search_id,
    )
    if tool_match:
        return RequestProtocol(
            status=protocol.status,
            layer=RequestLayer.TOOL,
            code=code,
            retryable=fallback.retryable,
            action=fallback.action,
            request_id=request_id,
            search_id=search_id,
        )
    return fallback


def _with_protocol_request_id(
    protocol: RequestProtocol,
    request_id: str,
) -> RequestProtocol:
    if not request_id or protocol.request_id == request_id:
        return protocol
    return RequestProtocol(
        status=protocol.status,
        layer=protocol.layer,
        code=protocol.code,
        retryable=protocol.retryable,
        action=protocol.action,
        request_id=request_id,
        search_id=protocol.search_id,
        schema_version=protocol.schema_version,
    )


def _with_protocol_search_id(
    protocol: RequestProtocol,
    search_id: str,
) -> RequestProtocol:
    """Fill an absent protocol search id from the exact frozen snapshot."""

    clean = str(search_id or "").strip()
    if not clean or protocol.search_id:
        return protocol
    return RequestProtocol(
        status=protocol.status,
        layer=protocol.layer,
        code=protocol.code,
        retryable=protocol.retryable,
        action=protocol.action,
        request_id=protocol.request_id,
        search_id=clean,
        schema_version=protocol.schema_version,
    )


def _http_error_protocol(
    request: Request,
    exc: HTTPException,
    *,
    search_id: str = "",
) -> RequestProtocol:
    path = request.url.path
    detail = str(exc.detail or "").lower()
    if path.startswith("/api/invite/"):
        if exc.status_code == 401:
            code = "INVITE_INVALID"
        elif exc.status_code == 429:
            code = "LOGIN_RATE_LIMITED"
        elif exc.status_code >= 500:
            code = "SERVICE_UNAVAILABLE"
        else:
            code = "LOGIN_REQUEST_INVALID"
    elif path.startswith("/api/feedback"):
        code = "FEEDBACK_TOO_LARGE" if exc.status_code == 413 else "FEEDBACK_INVALID"
    elif path.startswith("/api/image"):
        if exc.status_code == 413:
            code = "UPLOAD_TOO_LARGE"
        elif exc.status_code == 415:
            code = "UPLOAD_UNSUPPORTED_FORMAT"
        elif "missing" in detail or "required" in detail:
            code = "UPLOAD_REQUIRED"
        else:
            code = "UPLOAD_DECODE_FAILED"
    elif path.startswith("/api/media/") or path.startswith("/api/upload/"):
        code = "MEDIA_NOT_FOUND"
    elif path.startswith("/api/message"):
        code = "MESSAGE_INVALID"
    else:
        code = "SERVICE_UNAVAILABLE" if exc.status_code >= 500 else "MESSAGE_INVALID"
    return RequestProtocol.from_code(
        code,
        request_id=_request_id(request),
        search_id=search_id,
    )


def _event_kind_for_layer(layer: RequestLayer) -> str:
    if layer is RequestLayer.FEEDBACK:
        return "feedback"
    if layer is RequestLayer.MEDIA:
        return "media"
    if layer is RequestLayer.LOGIN:
        return "login"
    if layer is RequestLayer.SESSION:
        return "session"
    return "image" if layer is RequestLayer.UPLOAD else "text"


def _session_id(request: Request, *, cookie_name: str = SESSION_COOKIE) -> str:
    cached = str(getattr(request.state, "session_id", "") or "").strip()
    value = str(request.cookies.get(cookie_name) or "").strip()
    resolved = cached or value or secrets.token_urlsafe(24)
    bind_trace_event_dimensions(session_key=session_key(resolved))
    # Publish the new session to exception handlers only after request
    # initialization has completed. Otherwise a trace-binding failure could
    # mint a Cookie for a request that never established a usable session.
    request.state.session_id = resolved
    return resolved


def _identity_key(request: Request) -> str:
    identity = getattr(request.state, "invite_identity", None)
    return identity.invite_id if isinstance(identity, InviteIdentity) else ""


def _resolve_feedback_media(
    runtime: AgentSessionRuntime, session_id: str, url: str
) -> Path | None:
    clean = str(url or "").split("?", 1)[0]
    for prefix, resolver in (
        ("/api/upload/", runtime.resolve_upload),
        ("/api/media/", runtime.resolve_media),
    ):
        if not clean.startswith(prefix):
            continue
        name = clean[len(prefix):]
        if Path(name).name != name or not name:
            return None
        return resolver(session_id, name)
    return None


def _feedback_target_message(
    conversation: object, message_id: str
) -> dict[str, object]:
    if not isinstance(conversation, list):
        return {}
    clean_target = str(message_id or "").strip()
    for raw in reversed(conversation):
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("messageId") or raw.get("message_id") or "").strip()
        if raw_id == clean_target:
            return raw
    return {}


def _feedback_search_duration_ms(
    conversation: object,
    message_id: str,
    *,
    task_revision: int,
) -> int:
    """Rebuild upload-to-first-candidate latency from the bounded UI timeline."""

    if not isinstance(conversation, list):
        return 0
    target_index = -1
    for index in range(len(conversation) - 1, -1, -1):
        raw = conversation[index]
        if not isinstance(raw, Mapping):
            continue
        item_id = str(raw.get("messageId") or raw.get("message_id") or "").strip()
        if item_id == message_id:
            target_index = index
            break
    if target_index < 0:
        return 0

    revision = max(0, int(task_revision))
    visible = conversation[: target_index + 1]

    def item_revision(item: Mapping[str, object]) -> int:
        try:
            return max(
                0,
                int(item.get("taskRevision") or item.get("task_revision") or 0),
            )
        except (TypeError, ValueError):
            return 0

    def is_user(item: Mapping[str, object]) -> bool:
        return bool(item.get("me")) or str(item.get("role") or "").lower() == "user"

    matching_users = [
        item
        for item in visible
        if isinstance(item, Mapping)
        and is_user(item)
        and item_revision(item) == revision
    ]
    if not matching_users:
        return 0
    upload = next(
        (
            item
            for item in reversed(matching_users)
            if isinstance(item.get("images"), list) and bool(item.get("images"))
        ),
        matching_users[-1],
    )
    candidate = next(
        (
            item
            for item in visible
            if isinstance(item, Mapping)
            and not is_user(item)
            and item_revision(item) == revision
            and _bounded_feedback_int(item.get("candidateCount") or item.get("candidate_count"))
            > 0
        ),
        None,
    )
    if candidate is None:
        return 0
    started_at = _bounded_feedback_int(
        upload.get("createdAt") or upload.get("created_at")
    )
    finished_at = _bounded_feedback_int(
        candidate.get("createdAt") or candidate.get("created_at")
    )
    duration = finished_at - started_at
    return duration if 0 <= duration <= 86_400_000 and started_at > 0 else 0


def _bounded_feedback_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _handle_text(
    runtime: AgentSessionRuntime,
    session_id: str,
    text: str,
    *,
    action_context: object | None = None,
    request: Request | None = None,
    request_id: str = "",
    identity_key: str = "",
    progress: Callable[[str, str], None] | None = None,
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
) -> AgentResponse:
    if request is not None:
        request_id = request_id or _request_id(request)
        identity_key = identity_key or _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_text, "request_id"):
        kwargs["request_id"] = request_id
    if action_context is not None and _accepts_keyword(
        runtime.handle_text,
        "action_context",
    ):
        kwargs["action_context"] = action_context
    if identity_key:
        kwargs["identity_key"] = identity_key
    if task_state_capabilities is not None and _accepts_keyword(
        runtime.handle_text,
        "task_state_capabilities",
    ):
        kwargs["task_state_capabilities"] = task_state_capabilities
    return runtime.handle_text(session_id, text, **kwargs)


def _handle_image(
    runtime: AgentSessionRuntime,
    session_id: str,
    image_path: Path,
    *,
    request: Request | None = None,
    request_id: str = "",
    identity_key: str = "",
    progress: Callable[[str, str], None] | None = None,
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
) -> AgentResponse:
    if request is not None:
        request_id = request_id or _request_id(request)
        identity_key = identity_key or _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_image, "request_id"):
        kwargs["request_id"] = request_id
    if identity_key:
        kwargs["identity_key"] = identity_key
    if task_state_capabilities is not None and _accepts_keyword(
        runtime.handle_image,
        "task_state_capabilities",
    ):
        kwargs["task_state_capabilities"] = task_state_capabilities
    return runtime.handle_image(session_id, image_path, **kwargs)


def _accepts_keyword(function: Callable, name: str) -> bool:
    parameters = inspect.signature(function).parameters.values()
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _record_protocol_event(
    runtime: AgentSessionRuntime,
    session_id: str,
    **kwargs: object,
) -> None:
    recorder = getattr(runtime, "record_protocol_event", None)
    if callable(recorder):
        try:
            recorder(session_id, **kwargs)
        except Exception:  # noqa: BLE001 - observability must not replace the public reply.
            logger.warning("runtime protocol event recording failed")


def _forwarded_proto(request: Request) -> str:
    return str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()


def _is_secure_request(request: Request) -> bool:
    forwarded = _forwarded_proto(request)
    return forwarded == "https" or (not forwarded and request.url.scheme == "https")


def _set_session_cookie(
    response: Response,
    session_id: str,
    *,
    secure_cookie: bool,
    cookie_name: str = SESSION_COOKIE,
) -> None:
    response.set_cookie(
        cookie_name,
        session_id,
        max_age=2 * 60 * 60,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
    )


async def _read_image_upload(request: Request) -> tuple[bytes, str, str]:
    """Read the new multipart `file` field while retaining the legacy raw-body API."""
    request_type = str(request.headers.get("content-type") or "")
    if request_type.lower().startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - malformed external multipart input.
            raise HTTPException(status_code=400, detail="invalid multipart upload") from exc
        try:
            upload = form.get("file")
            if upload is None or not callable(getattr(upload, "read", None)):
                raise HTTPException(status_code=400, detail="image file field is required")
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            filename = str(getattr(upload, "filename", "") or "cropped.jpg")
            content_type = str(getattr(upload, "content_type", "") or "")
        finally:
            close = getattr(form, "close", None)
            if callable(close):
                await close()
    else:
        content = await request.body()
        filename = str(request.headers.get("x-filename") or "question.jpg")
        content_type = request_type.split(";", 1)[0].strip()
    if not content:
        raise HTTPException(status_code=400, detail="image is missing")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image is too large")
    return content, filename, content_type


def _write_incoming_image(
    content: bytes,
    filename: str,
    content_type: str = "",
    *,
    incoming_dir: str | Path = INCOMING_DIR,
) -> Path:
    """Verify image bytes and choose the temporary suffix from the detected format."""
    suffix = _validated_incoming_image_suffix(content, filename, content_type)
    target_dir = Path(incoming_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / f"{uuid4().hex}{suffix}"
    output.write_bytes(content)
    return output


def _validated_incoming_image_suffix(
    content: bytes,
    filename: str,
    content_type: str = "",
) -> str:
    """Validate image bytes without creating an incoming runtime file."""

    try:
        with Image.open(BytesIO(content)) as image:
            detected_format = str(image.format or "").upper()
            image.verify()
    except Exception as exc:  # noqa: BLE001 - external input boundary.
        raise HTTPException(status_code=400, detail="invalid image") from exc
    detected = SUPPORTED_IMAGE_FORMATS.get(detected_format)
    if detected is None:
        raise HTTPException(status_code=415, detail="unsupported image format")
    detected_type, suffix = detected
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized_type not in GENERIC_CONTENT_TYPES and normalized_type != detected_type:
        logger.debug(
            "image upload metadata mismatch: filename=%r declared=%r detected=%r",
            filename,
            normalized_type,
            detected_type,
        )
    return suffix


def _write_validated_incoming_image(
    content: bytes,
    suffix: str,
    *,
    incoming_dir: str | Path = INCOMING_DIR,
) -> Path:
    target_dir = Path(incoming_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / f"{uuid4().hex}{suffix}"
    output.write_bytes(content)
    return output


def _agent_json(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    submitted_crop: Path | None = None,
    response_store: SQLiteResponseStore | None = None,
    identity_key: str = "local",
    secure_cookie: bool = False,
    cookie_name: str = SESSION_COOKIE,
    task_state_capabilities: TaskStateEntryCapabilities,
) -> JSONResponse:
    payload = _agent_payload(
        response,
        runtime,
        session_id,
        uploaded_image=uploaded_image,
        submitted_crop=submitted_crop,
        response_store=response_store,
        identity_key=identity_key,
        response_mode="json",
        include_task_state=True,
        task_state_capabilities=task_state_capabilities,
    )
    result = JSONResponse(payload)
    _set_session_cookie(
        result,
        session_id,
        secure_cookie=secure_cookie,
        cookie_name=cookie_name,
    )
    _record_agent_payload_terminal(payload)
    return result


def _task_state_uploaded_image(
    response: AgentResponse,
    runtime: object,
) -> Path | None:
    """Return only the media path frozen with a typed task-state response."""

    if response.response_media_snapshot_captured:
        return response.uploaded_image_path
    exc = RuntimeError("task-state response is missing its frozen media snapshot")
    legacy = response.response_snapshot
    task_state = response.response_task_state_snapshot
    _carry_frozen_task_state_failure(
        exc,
        runtime,
        legacy_snapshot=legacy,
        task_state=task_state,
    )
    raise exc


def _has_exact_response_task_state_pair(
    legacy_snapshot: object,
    task_state: object,
) -> bool:
    """Return whether both public views came from one complete frozen read-set."""

    return (
        isinstance(legacy_snapshot, Mapping)
        and bool(legacy_snapshot)
        and type(task_state) is TaskStateSnapshotV1
    )


def _carry_frozen_task_state_failure(
    exc: Exception,
    runtime: object,
    *,
    legacy_snapshot: object = None,
    task_state: object = None,
) -> None:
    """Keep a task-state error on its established read-set without new I/O."""

    try:
        setattr(exc, "_response_snapshot_attempted", True)
        carried_legacy = _exception_response_snapshot(exc)
        carried_task_state = _exception_task_state_snapshot(exc)
        if not carried_legacy and isinstance(legacy_snapshot, Mapping):
            carried_legacy = dict(legacy_snapshot)
        if type(carried_task_state) is not TaskStateSnapshotV1:
            carried_task_state = task_state
        if not _has_exact_response_task_state_pair(
            carried_legacy,
            carried_task_state,
        ):
            carried_task_state = _unreadable_http_error_task_state(runtime)
        setattr(
            exc,
            "response_snapshot",
            carried_legacy or {"session_valid": False},
        )
        setattr(exc, "response_task_state_snapshot", carried_task_state)
    except Exception:  # noqa: BLE001 - preserve the original response failure.
        pass


def _build_agent_payload(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    submitted_crop: Path | None = None,
    response_store: SQLiteResponseStore | None = None,
    identity_key: str = "local",
    response_mode: str = "json",
    defer_authoritative: bool = False,
    include_task_state: bool = False,
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
) -> dict[str, object]:
    attached_public_snapshot = (
        dict(response.response_snapshot)
        if isinstance(response.response_snapshot, Mapping)
        and response.response_snapshot
        else {}
    )
    attached_projection_snapshot = (
        dict(response.response_projection_snapshot)
        if isinstance(response.response_projection_snapshot, Mapping)
        and response.response_projection_snapshot
        else {}
    )
    if type(include_task_state) is not bool:
        raise ValueError("include_task_state must be boolean")
    if include_task_state and type(task_state_capabilities) is not TaskStateEntryCapabilities:
        raise ValueError("task-state capabilities are required")
    response_task_state = response.response_task_state_snapshot
    if include_task_state and (
        not attached_public_snapshot or not attached_projection_snapshot
    ):
        raise RuntimeError("task-state response is missing its frozen session snapshot")
    if include_task_state and type(response_task_state) is not TaskStateSnapshotV1:
        raise RuntimeError("task-state response is missing its exact frozen task-state snapshot")
    if not attached_projection_snapshot:
        # Preserve the legacy internal fallback. Typed task-state exits
        # were validated above and may never synthesize a missing projection.
        attached_projection_snapshot = attached_public_snapshot
    live_snapshot: Mapping[str, object] = {}
    if not attached_public_snapshot or not attached_projection_snapshot:
        live_snapshot = runtime.session_snapshot(session_id)
    public_snapshot_source = attached_public_snapshot or live_snapshot
    media_snapshot = attached_projection_snapshot or public_snapshot_source
    media_guard = _media_delivery_guard(response, media_snapshot)
    image_urls, media = _persist_response_media(response, runtime, session_id)
    text = response.text
    media_failure_snapshot: dict[str, object] = {}
    if media and media.get("status") in {
        "unavailable",
        "partial",
        "incomplete",
    }:
        if include_task_state:
            reopen_v1 = getattr(runtime, "mark_media_delivery_failed_v1", None)
            legacy_reopen = getattr(runtime, "mark_media_delivery_failed", None)
            if callable(reopen_v1):
                try:
                    captured = reopen_v1(
                        session_id,
                        expected_unit_id=media_guard["expected_unit_id"],
                        expected_task_revision=media_guard["expected_task_revision"],
                        expected_candidate_generation=media_guard[
                            "expected_candidate_generation"
                        ],
                        kind=str(media.get("kind") or "answer"),
                        capabilities=task_state_capabilities,
                    )
                except Exception as exc:
                    _carry_frozen_task_state_failure(
                        exc,
                        runtime,
                    )
                    raise
                if captured is not None:
                    if (
                        type(captured) is not SessionResponseSnapshotV1
                        or type(captured.legacy_session) is not dict
                        or not captured.legacy_session
                        or type(captured.task_state) is not TaskStateSnapshotV1
                    ):
                        exc = RuntimeError(
                            "media reopen returned an invalid response snapshot"
                        )
                        _carry_frozen_task_state_failure(
                            exc,
                            runtime,
                        )
                        raise exc
                    media_failure_snapshot.update(captured.legacy_session)
                    response_task_state = captured.task_state
                    response.response_snapshot = dict(captured.legacy_session)
                    response.response_projection_snapshot = dict(
                        captured.legacy_session
                    )
                    response.response_task_state_snapshot = captured.task_state
            elif callable(legacy_reopen):
                raise RuntimeError(
                    "runtime cannot freeze task state after reopening media delivery"
                )
        else:
            reopen = getattr(runtime, "mark_media_delivery_failed", None)
            if callable(reopen):
                try:
                    reopen(
                        session_id,
                        expected_unit_id=media_guard["expected_unit_id"],
                        expected_task_revision=media_guard["expected_task_revision"],
                        expected_candidate_generation=media_guard[
                            "expected_candidate_generation"
                        ],
                        kind=str(media.get("kind") or "answer"),
                        snapshot_target=media_failure_snapshot,
                    )
                except Exception:  # noqa: BLE001 - legacy stream behavior stays best effort.
                    logger.warning("failed to reopen A3 unit after media failure")
    uploaded_image_url = f"/api/upload/{uploaded_image.name}" if uploaded_image is not None else ""
    submitted_crop_url = ""
    if submitted_crop is not None and submitted_crop.is_file():
        persisted_crop = _persist_public_media(runtime, session_id, submitted_crop)
        if persisted_crop is not None:
            submitted_crop_url = f"/api/media/{persisted_crop.name}"
    snapshot = _public_session_snapshot(
        media_failure_snapshot or public_snapshot_source
    )
    feedback_images: list[dict[str, str]] = []
    if response.intent == "a3_units_prepared":
        try:
            if response.response_media_snapshot_captured:
                overlay_path = response.feedback_overlay_path
            else:
                overlay_resolver = getattr(runtime, "current_auto_crop_overlay_path", None)
                overlay_path = (
                    overlay_resolver(session_id) if callable(overlay_resolver) else None
                )
            if overlay_path is not None and Path(overlay_path).is_file():
                persisted_overlay = _persist_public_media(
                    runtime, session_id, Path(overlay_path)
                )
                if persisted_overlay is not None:
                    feedback_images.append({
                        "kind": "a3_overlay",
                        "url": f"/api/media/{persisted_overlay.name}",
                        "label": "整页框选结果",
                    })
        except Exception:  # noqa: BLE001 - feedback-only media must not fail the reply.
            feedback_images = []
    active_request_id = current_request_id()
    fallback_request_id = active_request_id or new_request_id()
    if response.protocol:
        protocol = _with_protocol_request_id(
            _public_response_protocol(RequestProtocol.from_dict(response.protocol)),
            active_request_id,
        )
    elif snapshot.get("phase") == "ERROR":
        protocol = RequestProtocol.from_code(
            (
                "AGENT_FAILED"
                if snapshot.get("has_active_image") is True
                else "AGENT_FAILED_NO_IMAGE"
            ),
            request_id=fallback_request_id,
            search_id=str(snapshot.get("search_id") or ""),
        )
    elif snapshot.get("phase") == "NO_MATCH":
        protocol = RequestProtocol.from_code(
            "NO_MATCH",
            request_id=fallback_request_id,
            search_id=str(snapshot.get("search_id") or ""),
        )
    else:
        protocol = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED",
            request_id=fallback_request_id,
            search_id=str(snapshot.get("search_id") or ""),
        )
    if media and media.get("protocol_code"):
        protocol = RequestProtocol.from_code(
            str(media["protocol_code"]),
            request_id=protocol.request_id or fallback_request_id,
            search_id=protocol.search_id or str(snapshot.get("search_id") or ""),
        )
        text = str(media.get("text") or text)
    observe_public_output(
        runtime,
        text,
        intent=response.intent,
        protocol_code=protocol.code,
        media_status=str(media.get("status") or "") if media else "",
        endpoint="web_a3",
        session_id=session_id,
    )
    failure = None
    if protocol.status is RequestStatus.ERROR:
        failure = {
            "kind": "business_error",
            "recovery_action": protocol.action.value or (
                "retry_search" if snapshot.get("has_active_image") is True else "new_chat"
            ),
        }
    payload = _AgentPayload({
        "text": text,
        "images": image_urls,
        "media": media,
        "uploaded_image": uploaded_image_url,
        "submitted_crop": submitted_crop_url,
        "feedback_images": feedback_images,
        "intent": response.intent,
        "author_contact": dict(response.author_contact),
        "session": snapshot,
        "failure": failure,
        **protocol.to_dict(),
    })
    if include_task_state:
        try:
            mapped = with_public_task_state(payload, response_task_state)
            payload[PUBLIC_TASK_STATE_FIELD] = mapped[PUBLIC_TASK_STATE_FIELD]
        except Exception as exc:
            # Keep the response-time legacy/typed pair on mapping failures.
            # The controlled 500 handler can then publish its safe fallback
            # without observing a newer store value.
            setattr(
                exc,
                "response_snapshot",
                dict(media_failure_snapshot or public_snapshot_source),
            )
            setattr(exc, "response_task_state_snapshot", response_task_state)
            raise
        payload.response_task_state_snapshot = response_task_state
    image_route = str(media_snapshot.get("image_route") or "").strip().upper()
    if image_route not in {"A1", "A2", "A3"}:
        image_route = ""
    workflow_search_id = str(
        media_snapshot.get("workflow_search_id") or ""
    ).strip()
    response_search_id = str(
        protocol.search_id or media_snapshot.get("search_id") or ""
    ).strip()
    if image_route == "A2" and not workflow_search_id:
        workflow_search_id = response_search_id
    if (
        image_route in {"A1", "A3"}
        and workflow_search_id
        and response_search_id == workflow_search_id
    ):
        response_search_id = ""
    unit_id = str(media_guard.get("expected_unit_id") or "").strip()
    if not unit_id:
        raw_a3 = media_snapshot.get("a3")
        if isinstance(raw_a3, Mapping):
            selected = raw_a3.get("selected_unit")
            if isinstance(selected, Mapping):
                unit_id = str(selected.get("unit_id") or "").strip()
    dimensions: dict[str, str] = {
        "session_key": session_key(session_id),
        "identity_key": str(identity_key or "local").strip(),
    }
    for key, value in (
        ("workflow_search_id", workflow_search_id),
        ("search_id", response_search_id),
        ("unit_id", unit_id),
    ):
        clean = str(value or "").strip()
        if clean:
            dimensions[key] = clean

    finalization_snapshot = dict(media_failure_snapshot or media_snapshot)
    payload.authoritative_draft = _AuthoritativeResponseDraft(
        snapshot=finalization_snapshot,
        workflow_search_id=workflow_search_id,
        search_id=response_search_id,
        unit_id=unit_id,
        image_route=image_route,
        intent=str(payload.get("intent") or "public_response"),
    )
    if response_store is not None and not defer_authoritative:
        try:
            record = _finalize_authoritative_response(
                payload,
                response_store=response_store,
                identity_key=identity_key,
                session_id=session_id,
                response_mode=response_mode,
                snapshot=payload.authoritative_draft.snapshot,
                workflow_search_id=payload.authoritative_draft.workflow_search_id,
                search_id=payload.authoritative_draft.search_id,
                unit_id=payload.authoritative_draft.unit_id,
                image_route=payload.authoritative_draft.image_route,
                intent=payload.authoritative_draft.intent,
            )
        except Exception as exc:
            try:
                setattr(exc, "response_snapshot", dict(finalization_snapshot))
            except Exception:  # noqa: BLE001 - preserve the persistence failure.
                pass
            if include_task_state and type(response_task_state) is TaskStateSnapshotV1:
                try:
                    setattr(
                        exc,
                        "response_task_state_snapshot",
                        response_task_state,
                    )
                except Exception:  # noqa: BLE001 - preserve the persistence failure.
                    pass
            raise
        dimensions["response_id"] = record.response_id
    bind_trace_event_dimensions(**dimensions)

    return payload


def _agent_payload(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    submitted_crop: Path | None = None,
    response_store: SQLiteResponseStore | None = None,
    identity_key: str = "local",
    response_mode: str = "json",
    defer_authoritative: bool = False,
    include_task_state: bool = False,
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
) -> dict[str, object]:
    try:
        return _build_agent_payload(
            response,
            runtime,
            session_id,
            uploaded_image=uploaded_image,
            submitted_crop=submitted_crop,
            response_store=response_store,
            identity_key=identity_key,
            response_mode=response_mode,
            defer_authoritative=defer_authoritative,
            include_task_state=include_task_state,
            task_state_capabilities=task_state_capabilities,
        )
    except Exception as exc:
        if include_task_state:
            _carry_frozen_task_state_failure(
                exc,
                runtime,
                legacy_snapshot=response.response_snapshot,
                task_state=response.response_task_state_snapshot,
            )
        raise


def _finalize_authoritative_response(
    payload: dict[str, object],
    *,
    response_store: SQLiteResponseStore,
    identity_key: str,
    session_id: str,
    response_mode: str,
    snapshot: Mapping[str, object] | None = None,
    workflow_search_id: str | None = None,
    search_id: str | None = None,
    unit_id: str | None = None,
    image_route: str | None = None,
    intent: str = "",
    stream_event_type: str | None = None,
    trace_id: str = "",
    cancelled: Callable[[], bool] | None = None,
    bind_dimensions: bool = True,
):
    """Commit the exact safe payload before exposing its response identity."""

    raw_snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    public_snapshot = _public_session_snapshot(raw_snapshot)
    protocol = RequestProtocol.from_dict(payload)
    resolved_route = (
        str(
            image_route
            if image_route is not None
            else raw_snapshot.get("image_route") or ""
        )
        .strip()
        .upper()
    )
    if resolved_route not in {"A1", "A2", "A3"}:
        resolved_route = ""
    resolved_workflow = str(
        workflow_search_id
        if workflow_search_id is not None
        else raw_snapshot.get("workflow_search_id") or ""
    ).strip()
    resolved_search = str(
        search_id
        if search_id is not None
        else protocol.search_id or raw_snapshot.get("search_id") or ""
    ).strip()
    if resolved_route == "A2" and not resolved_workflow:
        resolved_workflow = resolved_search
    if (
        resolved_route in {"A1", "A3"}
        and resolved_workflow
        and resolved_search == resolved_workflow
    ):
        resolved_search = ""
    resolved_unit = str(unit_id or "").strip() if unit_id is not None else ""
    if unit_id is None:
        raw_a3 = raw_snapshot.get("a3")
        if isinstance(raw_a3, Mapping):
            selected = raw_a3.get("selected_unit")
            if isinstance(selected, Mapping):
                resolved_unit = str(selected.get("unit_id") or "").strip()
    resolved_intent = str(intent or payload.get("intent") or "public_response").strip()
    if not resolved_intent:
        resolved_intent = "public_response"
    resolved_event_type = (
        "result" if stream_event_type is None and response_mode == "stream"
        else str(stream_event_type or "")
    )
    delivery_envelope: object = payload
    if resolved_event_type == "result":
        delivery_envelope = {"type": "result", "data": payload}
    elif resolved_event_type == "error":
        delivery_envelope = {"type": "error", **payload}
    json.dumps(
        delivery_envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    images = payload.get("images")
    media_payload = payload.get("media")
    media_status = (
        str(media_payload.get("status") or "").strip()
        if isinstance(media_payload, Mapping)
        else ""
    )
    text = payload.get("text") or payload.get("detail") or payload.get("message") or ""
    projection = ResponseProjection(
            trace_id=str(trace_id or current_trace_id()).strip(),
            identity_key=str(identity_key or "local").strip(),
            session_key=session_key(session_id),
            request_id=protocol.request_id,
            status=protocol.status.value,
            layer=protocol.layer.value,
            code=protocol.code,
            retryable=protocol.retryable,
            action=protocol.action.value,
            workflow_search_id=resolved_workflow,
            search_id=resolved_search,
            unit_id=resolved_unit,
            phase=str(public_snapshot.get("phase") or "IDLE"),
            task_revision=int(public_snapshot.get("task_revision") or 0),
            candidate_count=int(public_snapshot.get("candidate_count") or 0),
            chapter=str(public_snapshot.get("chapter") or "").strip(),
            image_route=resolved_route,
            intent=resolved_intent,
            response_mode=response_mode,
            media_status=media_status,
            image_count=len(images) if isinstance(images, list) else 0,
            text_length=len(str(text)),
            duration_ms=_trace_duration_ms(),
    )
    record = (
        response_store.finalize(projection, cancelled=cancelled)
        if cancelled is not None
        else response_store.finalize(projection)
    )
    payload["response_id"] = record.response_id
    if bind_dimensions:
        bind_trace_event_dimensions(
            response_id=record.response_id,
            identity_key=record.identity_key,
            session_key=record.session_key,
            workflow_search_id=record.workflow_search_id,
            search_id=record.search_id,
            unit_id=record.unit_id,
        )
    return record


def _record_agent_payload_terminal(payload: Mapping[str, object]) -> None:
    response_id = str(payload.get("response_id") or "").strip()
    if response_id:
        bind_trace_event_dimensions(response_id=response_id)
    protocol = RequestProtocol.from_dict(dict(payload))
    images = payload.get("images")
    terminal_attributes: dict[str, object] = {
        "intent": str(payload.get("intent") or "public_response"),
        "image_count": len(images) if isinstance(images, list) else 0,
        "text_length": len(str(payload.get("text") or "")),
    }
    media = payload.get("media")
    if isinstance(media, Mapping) and str(media.get("status") or "").strip():
        terminal_attributes["media_status"] = str(media["status"]).strip()
    snapshot = payload.get("session")
    if isinstance(snapshot, Mapping):
        candidate_count = snapshot.get("candidate_count")
        if type(candidate_count) is int:
            terminal_attributes["candidate_count"] = candidate_count
        a3_snapshot = snapshot.get("a3")
        if isinstance(a3_snapshot, Mapping) and isinstance(a3_snapshot.get("units"), list):
            terminal_attributes["unit_count"] = len(a3_snapshot["units"])
    _record_protocol_terminal(
        protocol,
        http_status=200,
        error_kind="BusinessProtocolError",
        extra_attributes=terminal_attributes,
    )


def _persist_response_media(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
) -> tuple[list[str], dict[str, object] | None]:
    """Persist response media and make success depend on resolvable URLs.

    Candidate images are an atomic group: a partial group is withheld rather
    than shown with a misleading candidate list. Answer images may be sent
    progressively, but the public text and protocol distinguish zero,
    partial, and complete delivery. Persisted session media never implies that
    the current response promised another delivery.
    """

    intent = str(response.intent or "").strip()
    state = response.state if isinstance(response.state, Mapping) else {}
    kind = str(getattr(response, "media_kind", "") or "").strip().lower()
    if kind not in {"candidates", "answer"}:
        if not response.images:
            return [], None
        kind = (
            "answer"
            if intent in {"select_candidate", "resend_answer"}
            else "candidates"
        )

    state_items = state.get("last_answer_paths" if kind == "answer" else "candidates")
    state_count = len(state_items) if isinstance(state_items, (list, tuple)) else 0
    expected = max(state_count, len(response.images))
    if expected <= 0:
        if kind == "answer":
            return [], {
                "kind": kind,
                "requested_count": 0,
                "delivered_count": 0,
                "status": "unavailable",
                "protocol_code": "MEDIA_ANSWERS_UNAVAILABLE",
                "text": "答案暂时无法发送，请回复“重试”。",
            }
        return [], None

    urls: list[str] = []
    for raw_image in response.images:
        try:
            persisted_path = _persist_public_media(runtime, session_id, Path(raw_image))
            if persisted_path is not None:
                urls.append(f"/api/media/{persisted_path.name}")
        except Exception:  # noqa: BLE001 - one bad media must not leak details.
            logger.warning("response media persistence failed")

    delivered = len(urls)
    if kind == "candidates" and delivered != expected:
        return [], {
            "kind": kind,
            "requested_count": expected,
            "delivered_count": 0,
            "status": "incomplete",
            "protocol_code": "MEDIA_CANDIDATES_INCOMPLETE",
            "text": "候选图片暂时无法完整发送，请回复“重试”。",
        }
    if kind == "answer" and delivered == 0:
        return [], {
            "kind": kind,
            "requested_count": expected,
            "delivered_count": 0,
            "status": "unavailable",
            "protocol_code": "MEDIA_ANSWERS_UNAVAILABLE",
            "text": "答案暂时无法发送，请回复“重试”。",
        }
    if kind == "answer" and delivered < expected:
        return urls, {
            "kind": kind,
            "requested_count": expected,
            "delivered_count": delivered,
            "status": "partial",
            "protocol_code": "MEDIA_ANSWERS_PARTIAL",
            "text": f"答案已发送 {delivered}/{expected} 张，剩余暂时无法发送，请回复“重试”。",
        }
    return urls, {
        "kind": kind,
        "requested_count": expected,
        "delivered_count": delivered,
        "status": "complete",
    }


def _media_delivery_guard(
    response: AgentResponse,
    snapshot: object,
) -> dict[str, object]:
    """Use the response-time A3 identity before falling back to a live snapshot."""

    response_state = response.state if isinstance(response.state, Mapping) else {}
    embedded = response_state.get("_a3_media_guard")
    if isinstance(embedded, Mapping):
        try:
            revision = int(embedded.get("task_revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        return {
            "expected_unit_id": str(embedded.get("unit_id") or ""),
            "expected_task_revision": revision,
            "expected_candidate_generation": str(
                embedded.get("candidate_generation") or ""
            ),
        }

    if not isinstance(snapshot, Mapping):
        return {
            "expected_unit_id": "",
            "expected_task_revision": 0,
            "expected_candidate_generation": "",
        }
    a3 = snapshot.get("a3")
    a3_snapshot = a3 if isinstance(a3, Mapping) else {}
    selected = a3_snapshot.get("selected_unit")
    selected_snapshot = selected if isinstance(selected, Mapping) else {}
    raw_revision = a3_snapshot.get("task_revision", snapshot.get("task_revision", 0))
    try:
        revision = int(raw_revision or 0)
    except (TypeError, ValueError):
        revision = 0
    return {
        "expected_unit_id": str(selected_snapshot.get("unit_id") or ""),
        "expected_task_revision": revision,
        "expected_candidate_generation": str(
            snapshot.get("candidate_generation") or ""
        ),
    }


def _persist_public_media(
    runtime: AgentSessionRuntime,
    session_id: str,
    source: Path,
) -> Path | None:
    """Persist one media artifact and verify that the public URL can resolve."""

    if not source.is_file():
        return None
    try:
        persisted = runtime.persist_media(session_id, source)
        if persisted is None:
            return None
        persisted_path = Path(persisted)
        if not persisted_path.is_file():
            return None
        resolver = getattr(runtime, "resolve_media", None)
        if callable(resolver):
            resolved = resolver(session_id, persisted_path.name)
            if resolved is None or not Path(resolved).is_file():
                return None
        return persisted_path
    except Exception:  # noqa: BLE001 - media failures become structured state.
        logger.warning("public media persistence failed")
        return None


def _public_session_snapshot(value: object) -> dict[str, object]:
    """Expose only state required by the current browser workflow."""

    snapshot = value if isinstance(value, Mapping) else {}
    result: dict[str, object] = {
        "session_valid": snapshot.get("session_valid") is True,
        "phase": _public_phase(snapshot.get("phase")),
        "has_active_image": snapshot.get("has_active_image") is True,
        "task_revision": _public_nonnegative_int(snapshot.get("task_revision")),
        "candidate_generation": _public_state_id(snapshot.get("candidate_generation")),
        "candidate_count": _public_nonnegative_int(snapshot.get("candidate_count")),
        "chapter": _public_state_text(snapshot.get("chapter"), 64),
        "search_id": _public_state_id(snapshot.get("search_id"), prefix="search_"),
    }
    a3 = _public_a3_snapshot(snapshot.get("a3"))
    # An explicit null clears stale A3 state when a later upload routes to A1/A2.
    result["a3"] = a3
    return result


def _safe_runtime_snapshot(
    runtime: AgentSessionRuntime,
    session_id: str,
) -> Mapping[str, object]:
    try:
        snapshot = runtime.session_snapshot(session_id)
    except Exception:  # noqa: BLE001 - error rendering must preserve the original failure.
        logger.warning("runtime session snapshot unavailable during error handling")
        return {}
    return snapshot if isinstance(snapshot, Mapping) else {}


def _exception_response_snapshot(exc: object) -> dict[str, object]:
    """Return only the legacy projection explicitly carried by an exception."""

    snapshot = getattr(exc, "response_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return {}
    return dict(snapshot)


def _exception_task_state_snapshot(
    exc: object,
) -> TaskStateSnapshotV1 | None:
    """Return an exception's exact frozen V1 snapshot, never a legacy mapping."""

    snapshot = getattr(exc, "response_task_state_snapshot", None)
    if type(snapshot) is TaskStateSnapshotV1:
        return snapshot
    # SessionResponseSnapshotError predates the public error attribute and
    # carries its exact frozen value under ``task_state``.
    if isinstance(exc, SessionResponseSnapshotError):
        snapshot = getattr(exc, "task_state", None)
        if type(snapshot) is TaskStateSnapshotV1:
            return snapshot
    return None


def _exception_response_snapshot_attempted(exc: object) -> bool:
    """Return whether runtime already consumed the authoritative read-set."""

    try:
        return getattr(exc, "_response_snapshot_attempted", False) is True
    except Exception:  # noqa: BLE001 - malformed exceptions grant no evidence.
        return False


def _unreadable_task_state(*, a3_wrapper: bool) -> TaskStateSnapshotV1:
    """Return a no-I/O fail-closed V1 for an unavailable response read-set."""

    if a3_wrapper:
        return task_state_contract.TaskStateSnapshotV1(
            workflow=task_state_contract.WorkflowStateView(
                exists=True,
                workflow_id="",
                kind=task_state_contract.WORKFLOW_KIND_IMAGE_SEARCH,
                route=task_state_contract.WORKFLOW_ROUTE_PENDING,
                task_revision=0,
                phase=task_state_contract.PHASE_UNKNOWN,
                status=task_state_contract.STATUS_INCONSISTENT,
                completed_steps=(),
                allowed_actions=(),
                next_stage=task_state_contract.NEXT_RETRY,
            ),
            consistency=task_state_contract.ConsistencyView(
                status=task_state_contract.CONSISTENCY_INCONSISTENT,
                codes=(
                    task_state_contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE,
                    task_state_contract.CONSISTENCY_CHILD_STATE_UNREADABLE,
                ),
            ),
        )
    return task_state_contract.TaskStateSnapshotV1(
        workflow=empty_task_state_snapshot().workflow,
        active_child_task=task_state_contract.ChildTaskStateView(
            task_id="",
            kind=task_state_contract.CHILD_KIND_A2_QUESTION,
            unit_id="",
            task_revision=0,
            phase=task_state_contract.PHASE_UNKNOWN,
            status=task_state_contract.STATUS_INCONSISTENT,
            completed_steps=(),
            allowed_actions=(),
            next_stage=task_state_contract.NEXT_RETRY,
            chapter="",
            candidate_count=0,
            candidate_generation="",
        ),
        consistency=task_state_contract.ConsistencyView(
            status=task_state_contract.CONSISTENCY_INCONSISTENT,
            codes=(task_state_contract.CONSISTENCY_CHILD_STATE_UNREADABLE,),
        ),
    )


def _unreadable_http_error_task_state(runtime: object) -> TaskStateSnapshotV1:
    """Select the stable unreadable sentinel from trusted runtime topology."""

    try:
        a3_wrapper = bool(getattr(runtime, "a3_enabled", False))
    except Exception:  # noqa: BLE001 - topology lookup must not break error rendering.
        a3_wrapper = False
    return _unreadable_task_state(a3_wrapper=a3_wrapper)


def _unreadable_task_state_like(snapshot: object) -> TaskStateSnapshotV1:
    """Choose a defensive sentinel when only a typed source is available."""

    try:
        workflow = getattr(snapshot, "workflow", None)
        a3_wrapper = bool(
            type(workflow) is task_state_contract.WorkflowStateView
            and workflow.exists is True
        )
    except Exception:  # noqa: BLE001 - a corrupt source grants no topology facts.
        a3_wrapper = False
    return _unreadable_task_state(a3_wrapper=a3_wrapper)


def _safe_error_snapshot(
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    capabilities: TaskStateEntryCapabilities,
) -> tuple[dict[str, object], TaskStateSnapshotV1 | None]:
    """Capture one response-frozen legacy/V1 pair for a controlled error."""

    fallback_task_state = _unreadable_http_error_task_state(runtime)
    capture = getattr(runtime, "session_response_snapshot_v1", None)
    if not callable(capture) or not session_id:
        return (
            ({"session_valid": False}, fallback_task_state)
            if session_id
            else ({}, None)
        )
    kwargs: dict[str, object] = {"capabilities": capabilities}
    try:
        if _accepts_keyword(capture, "response_frozen"):
            kwargs["response_frozen"] = True
    except Exception:  # noqa: BLE001 - an unusual test/runtime object is not fatal.
        pass
    try:
        captured = capture(session_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - preserve the original HTTP error.
        legacy = _exception_response_snapshot(exc)
        task_state = _exception_task_state_snapshot(exc)
        if _has_exact_response_task_state_pair(legacy, task_state):
            return legacy, task_state
        logger.warning("runtime response snapshot unavailable during HTTP error handling")
        return legacy or {"session_valid": False}, fallback_task_state
    if type(captured) is not SessionResponseSnapshotV1:
        logger.warning("runtime returned an invalid response snapshot during HTTP error handling")
        return {"session_valid": False}, fallback_task_state
    legacy = (
        dict(captured.legacy_session)
        if isinstance(captured.legacy_session, Mapping)
        else {}
    )
    task_state = (
        captured.task_state
        if type(captured.task_state) is TaskStateSnapshotV1
        else None
    )
    if not _has_exact_response_task_state_pair(legacy, task_state):
        logger.warning("runtime response snapshot omitted a complete task-state pair")
        return legacy or {"session_valid": False}, fallback_task_state
    return legacy, task_state


def _public_a3_snapshot(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        return None
    units: list[dict[str, object]] = []
    raw_units = value.get("units")
    if isinstance(raw_units, (list, tuple)):
        for raw_unit in raw_units[:100]:
            if not isinstance(raw_unit, Mapping):
                continue
            unit_id = _public_state_id(raw_unit.get("unit_id"))
            if not unit_id:
                continue
            page_index = _public_positive_int(raw_unit.get("page_index"))
            display_label = _public_state_text(raw_unit.get("display_label"), 64)
            if not display_label and page_index:
                display_label = f"图片第 {page_index} 题"
            validation_status = str(raw_unit.get("validation_status") or "")
            grounding_status = str(raw_unit.get("grounding_status") or "")
            crop_available = raw_unit.get("crop_available") is True
            requested = raw_unit.get("requested") is True
            if validation_status == "auto_ready":
                preparation_status = "ready"
            elif requested:
                preparation_status = "manual"
            elif grounding_status == "auto_ready":
                preparation_status = "located"
            elif crop_available:
                preparation_status = "manual"
            else:
                preparation_status = "pending"
            units.append({
                "unit_id": unit_id,
                "page_index": page_index,
                "display_label": display_label,
                "title_text": _public_state_text(raw_unit.get("title_text"), 160),
                "completed": raw_unit.get("completed") is True,
                "searched": raw_unit.get("searched") is True,
                "selected": raw_unit.get("selected") is True,
                "requested": requested,
                "crop_available": crop_available,
                "preparation_status": preparation_status,
            })

    selected_raw = value.get("selected_unit")
    selected = selected_raw if isinstance(selected_raw, Mapping) else {}
    selected_id = _public_state_id(selected.get("unit_id"))
    selected_label = _public_state_text(selected.get("display_label"), 64)
    selected_unit = next(
        (item for item in units if item["unit_id"] == selected_id),
        None,
    )
    if not selected_label and selected_unit is not None:
        selected_label = str(selected_unit["display_label"])

    crop_draft_raw = value.get("crop_draft")
    crop_draft = crop_draft_raw if isinstance(crop_draft_raw, Mapping) else {}
    crop_review_required = value.get("crop_review_required") is True
    crop_review_code = str(value.get("crop_review_code") or "").strip().upper()
    if not crop_review_required or crop_review_code not in A3_CROP_REVIEW_CODES:
        crop_review_code = ""
    return {
        "enabled": True,
        "auto_crop_enabled": value.get("auto_crop_enabled") is True,
        "auto_prepare_all_enabled": value.get("auto_prepare_all_enabled") is True,
        "auto_prepare_all_units": value.get("auto_prepare_all_units") is True,
        "phase": _public_phase(value.get("phase")),
        "page_finished": value.get("page_finished") is True,
        "units": units,
        "selected_unit": {
            "unit_id": selected_id,
            "display_label": selected_label,
            "context_text": _public_state_text(selected.get("context_text"), 480),
        },
        "auto_crop_overlay_available": value.get("auto_crop_overlay_available") is True,
        "crop_review_required": crop_review_required,
        "crop_review_code": crop_review_code,
        "crop_draft": {
            "bounds": _public_crop_bounds(crop_draft.get("bounds")),
            "available": crop_draft.get("available") is True,
        },
        "task_revision": _public_nonnegative_int(value.get("task_revision")),
    }


def _public_crop_bounds(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    bounds: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return {}
        number = float(number)
        if not math.isfinite(number) or not 0 <= number <= 1:
            return {}
        bounds[key] = round(number, 6)
    if bounds["width"] < 0.02 or bounds["height"] < 0.02:
        return {}
    if bounds["x"] + bounds["width"] > 1.000001:
        return {}
    if bounds["y"] + bounds["height"] > 1.000001:
        return {}
    return bounds


def _public_state_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = value.replace("\r\n", "\n").replace("\r", "\n")
    clean = _PUBLIC_STATE_CONTROL_RE.sub("", clean).strip()
    if not clean or any(pattern.search(clean) for pattern in _PUBLIC_STATE_SENSITIVE_PATTERNS):
        return ""
    return clean[:max_chars]


def _public_state_id(value: object, *, prefix: str = "") -> str:
    clean = value.strip() if isinstance(value, str) else ""
    if not _PUBLIC_STATE_ID_RE.fullmatch(clean):
        return ""
    if prefix and not clean.startswith(prefix):
        return ""
    if _PUBLIC_STATE_SENSITIVE_ID_RE.search(clean):
        return ""
    if any(pattern.search(clean) for pattern in _PUBLIC_STATE_SENSITIVE_PATTERNS):
        return ""
    return clean


def _public_phase(value: object) -> str:
    clean = value.strip().upper() if isinstance(value, str) else "IDLE"
    return clean if _PUBLIC_STATE_PHASE_RE.fullmatch(clean) else "IDLE"


def _public_nonnegative_int(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000 else 0


def _public_positive_int(value: object) -> int:
    return value if type(value) is int and 0 < value <= 10_000 else 0


def _authorized_action_text(raw_context: object) -> str:
    if type(raw_context) is not dict:
        return ""
    action = raw_context.get("type")
    if action == task_state_contract.ACTION_SELECT_CANDIDATE:
        rank = raw_context.get("rank")
        return f"选择候选 {rank}" if type(rank) is int and rank > 0 else ""
    if action == task_state_contract.ACTION_RETRY_SEARCH:
        return "重试搜索"
    return ""


def _validate_action_context(
    runtime: AgentSessionRuntime,
    session_id: str,
    raw_context: object,
    *,
    request_id: str = "",
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
) -> AgentResponse | None:
    """Reject a button action unless the frozen V1 child explicitly authorizes it."""
    if raw_context is None:
        return None
    captured = (
        runtime.session_response_snapshot_v1(
            session_id,
            capabilities=task_state_capabilities,
            response_frozen=True,
        )
        if task_state_capabilities is not None
        else None
    )
    snapshot = (
        dict(captured.legacy_session)
        if captured is not None
        else runtime.session_snapshot(session_id)
    )
    task_state = (
        captured.task_state
        if captured is not None and type(captured.task_state) is TaskStateSnapshotV1
        else None
    )
    child = (
        task_state.active_child_task
        if task_state is not None
        and task_state.consistency.status == task_state_contract.CONSISTENCY_OK
        else None
    )

    def stale_response(text: str, *, intent: str, code: str) -> AgentResponse:
        response = AgentResponse(
            text=text,
            intent=intent,
            protocol=RequestProtocol.from_code(
                code,
                request_id=request_id,
                search_id=str(snapshot.get("search_id") or ""),
            ).to_dict(),
        )
        if captured is not None:
            response.response_snapshot = dict(captured.legacy_session)
            response.response_projection_snapshot = dict(captured.legacy_session)
            response.response_task_state_snapshot = captured.task_state
            response.response_media_snapshot_captured = True
            response.uploaded_image_path = captured.uploaded_image_path
        return response

    try:
        supports_atomic_context = _accepts_keyword(
            runtime.handle_text,
            "action_context",
        )
    except (TypeError, ValueError):
        supports_atomic_context = False
    if not supports_atomic_context:
        return stale_response(
            "这个操作已经失效，请使用当前页面中的操作。",
            intent="stale_action",
            code="STALE_ACTION",
        )

    if type(raw_context) is not dict or raw_context.get("type") not in {
        "select_candidate",
        "retry_search",
    }:
        return stale_response(
            "这个操作已经失效，请使用当前页面中的操作。",
            intent="stale_action",
            code="STALE_ACTION",
        )
    action = str(raw_context["type"])
    task_id = raw_context.get("task_id")
    task_revision = raw_context.get("task_revision")
    expected_keys = {"type", "task_id", "task_revision"}
    common_valid = (
        child is not None
        and type(task_id) is str
        and task_id == child.task_id
        and type(task_revision) is int
        and task_revision == child.task_revision
        and action in child.allowed_actions
    )
    if action == "retry_search":
        valid = set(raw_context) == expected_keys and common_valid
    else:
        expected_keys.update({"rank", "candidate_generation"})
        rank = raw_context.get("rank")
        generation = raw_context.get("candidate_generation")
        valid = (
            set(raw_context) == expected_keys
            and common_valid
            and type(rank) is int
            and 1 <= rank <= child.candidate_count
            and type(generation) is str
            and bool(generation)
            and generation == child.candidate_generation
        )
    if valid:
        return None
    if action == "retry_search":
        return stale_response(
            "这个重试操作已经失效，请使用当前页面中的操作。",
            intent="stale_action",
            code="STALE_ACTION",
        )
    has_image = snapshot.get("has_active_image") is True
    message = (
        "这是上一道题或上一轮搜索的候选，已经不能选择。请继续完成当前题目的章节确认和搜索。"
        if has_image
        else "这是已失效的候选，当前会话没有可选择的候选题，请重新上传题图。"
    )
    return stale_response(
        message,
        intent="stale_candidate",
        code="STALE_CANDIDATE",
    )


async def _stream_agent_events(
    execute: Callable[[Callable[[str, str], None]], dict[str, object]],
    *,
    request_id: str = "",
    search_id: str = "",
    trace_context: TraceContext,
    trace_event_session: TraceEventSession | None = None,
    trace_meta: Mapping[str, object] | None = None,
    runtime: AgentSessionRuntime | None = None,
    response_store: SQLiteResponseStore | None = None,
    session_id: str = "",
    identity_key: str = "local",
    task_state_capabilities: TaskStateEntryCapabilities | None = None,
    session_coordination_ack: (
        tuple[str, ...]
        | Callable[[], tuple[str, ...] | None]
        | None
    ) = None,
):
    if (
        task_state_capabilities is not None
        and type(task_state_capabilities) is not TaskStateEntryCapabilities
    ):
        raise ValueError("stream task-state capabilities are invalid")
    include_task_state = task_state_capabilities is not None
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[_QueuedStreamEvent | None] = asyncio.Queue()
    delivery_cancelled = threading.Event()
    unexposed_responses: dict[str, ResponseRecord] = {}

    def current_coordination_ack() -> tuple[str, ...] | None:
        value = (
            session_coordination_ack()
            if callable(session_coordination_ack)
            else session_coordination_ack
        )
        return value if isinstance(value, tuple) else None

    def serialized_event(event: Mapping[str, object]) -> str:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    def progress(stage: str, message: str) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            _QueuedStreamEvent(
                serialized_event(_public_progress_event(stage, message))
            ),
        )

    # asyncio.to_thread cannot stop work that is already waiting in a worker
    # thread. Carry this thread-safe probe with the existing progress callable
    # so the runtime gate can withdraw the ticket before business execution.
    progress.cancelled = delivery_cancelled.is_set  # type: ignore[attr-defined]

    def error_snapshot(attached: object = None) -> Mapping[str, object]:
        if isinstance(attached, Mapping) and attached:
            return attached
        if runtime is None or not session_id:
            return {}
        return _safe_runtime_snapshot(runtime, session_id)

    def error_response_state(
        exc: Exception,
        attached: object = None,
    ) -> tuple[dict[str, object], TaskStateSnapshotV1 | None]:
        """Resolve one stream error read-set without observing later live state."""

        if not include_task_state:
            return dict(error_snapshot(attached)), None

        carried_legacy = _exception_response_snapshot(exc)
        if not carried_legacy and isinstance(attached, Mapping) and attached:
            carried_legacy = dict(attached)

        # Admission rejection precedes the authoritative session read-set.
        # Preserve the existing zero-I/O exception and never manufacture state.
        if isinstance(exc, AgentRuntimeBusyError):
            return carried_legacy, None

        carried_task_state = _exception_task_state_snapshot(exc)
        if _has_exact_response_task_state_pair(
            carried_legacy,
            carried_task_state,
        ):
            return carried_legacy, carried_task_state

        if (
            carried_legacy
            or type(carried_task_state) is TaskStateSnapshotV1
            or _exception_response_snapshot_attempted(exc)
        ):
            return (
                carried_legacy or {"session_valid": False},
                _unreadable_http_error_task_state(runtime),
            )

        if runtime is None or not session_id:
            return {}, None
        return _safe_error_snapshot(
            runtime,
            session_id,
            capabilities=task_state_capabilities,
        )

    def with_stream_error_task_state(
        payload: dict[str, object],
        task_state: TaskStateSnapshotV1 | None,
    ) -> dict[str, object]:
        """Attach an exact error snapshot, falling closed without another read."""

        if type(task_state) is not TaskStateSnapshotV1:
            return payload
        try:
            return with_public_task_state(payload, task_state)
        except Exception:
            fallback = (
                _unreadable_http_error_task_state(runtime)
                if runtime is not None
                else _unreadable_task_state_like(task_state)
            )
            payload[PUBLIC_TASK_STATE_FIELD] = public_task_state_snapshot(fallback)
            return payload

    async def finalize_stream_response(
        payload: dict[str, object],
        **kwargs: object,
    ) -> ResponseRecord | None:
        if response_store is None or not session_id:
            return None
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                _finalize_authoritative_response,
                payload,
                response_store=response_store,
                identity_key=identity_key,
                session_id=session_id,
                response_mode="stream",
                trace_id=trace_context.trace_id,
                cancelled=delivery_cancelled.is_set,
                bind_dimensions=False,
                **kwargs,
            )
        )

        async def discard_committed(record: object) -> None:
            response_id = str(getattr(record, "response_id", "") or "")
            try:
                await asyncio.to_thread(
                    response_store.discard_unexposed,
                    response_id,
                    trace_id=trace_context.trace_id,
                )
            except Exception:  # noqa: BLE001 - cancellation remains authoritative.
                logger.exception("unexposed stream response cleanup failed")
            finally:
                unexposed_responses.pop(response_id, None)
                payload.pop("response_id", None)

        try:
            record = await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            delivery_cancelled.set()
            record = None
            try:
                record = await commit_task
            except Exception:
                pass
            if record is not None:
                await discard_committed(record)
            raise
        except ResponseFinalizationCancelled as exc:
            raise asyncio.CancelledError() from exc
        if delivery_cancelled.is_set():
            await discard_committed(record)
            raise asyncio.CancelledError()
        unexposed_responses[record.response_id] = record
        return record

    async def finalize_error_event(
        payload: dict[str, object],
        *,
        snapshot: Mapping[str, object],
        persist_authoritative: bool = True,
    ) -> ResponseRecord | None:
        if not persist_authoritative or response_store is None or not session_id:
            return None
        raw_a3 = snapshot.get("a3") if isinstance(snapshot, Mapping) else None
        selected = raw_a3.get("selected_unit") if isinstance(raw_a3, Mapping) else None
        intent = (
            "a3_page_error"
            if str(snapshot.get("image_route") or "").strip().upper() == "A3"
            and not (
                isinstance(selected, Mapping)
                and str(selected.get("unit_id") or "").strip()
            )
            else "request_error"
        )
        try:
            return await finalize_stream_response(
                payload,
                snapshot=snapshot,
                intent=intent,
                stream_event_type="error",
            )
        except Exception:  # noqa: BLE001 - preserve the original safe failure.
            logger.exception("authoritative stream error persistence failed")
            return None

    def expose_stream_event(event: _QueuedStreamEvent) -> None:
        event_scope = (
            trace_event_session_scope(trace_event_session)
            if trace_event_session is not None
            else suppress()
        )
        with trace_context_scope(trace_context), event_scope, _public_trace_meta_scope(
            dict(trace_meta or {})
        ):
            record = event.response_record
            if record is not None:
                bind_trace_event_dimensions(
                    response_id=record.response_id,
                    identity_key=record.identity_key,
                    session_key=record.session_key,
                    workflow_search_id=record.workflow_search_id,
                    search_id=record.search_id,
                    unit_id=record.unit_id,
                )
            if event.terminal_recorder is not None:
                event.terminal_recorder()
        if event.response_record is not None:
            unexposed_responses.pop(event.response_record.response_id, None)

    async def run() -> None:
        event_scope = (
            trace_event_session_scope(trace_event_session)
            if trace_event_session is not None
            else suppress()
        )
        with trace_context_scope(trace_context), event_scope, _public_trace_meta_scope(
            dict(trace_meta or {})
        ):
            try:
                payload = await asyncio.to_thread(execute, progress)
                coordination_ack = current_coordination_ack()
                if coordination_ack is not None:
                    _with_session_coordination_ack(
                        payload,
                        coordination_ack,
                    )
                draft = getattr(payload, "authoritative_draft", None)
                response_task_state: object = None
                try:
                    response_task_state = getattr(
                        payload,
                        "response_task_state_snapshot",
                        None,
                    )
                    if include_task_state and (
                        type(response_task_state) is not TaskStateSnapshotV1
                        or PUBLIC_TASK_STATE_FIELD not in payload
                    ):
                        raise RuntimeError(
                            "stream result is missing its exact frozen task-state snapshot"
                        )
                    response_record = None
                    if (
                        response_store is not None
                        and not str(payload.get("response_id") or "").strip()
                        and isinstance(draft, _AuthoritativeResponseDraft)
                    ):
                        response_record = await finalize_stream_response(
                            payload,
                            snapshot=draft.snapshot,
                            workflow_search_id=draft.workflow_search_id,
                            search_id=draft.search_id,
                            unit_id=draft.unit_id,
                            image_route=draft.image_route,
                            intent=draft.intent,
                        )
                    line = serialized_event({"type": "result", "data": payload})
                except Exception as exc:
                    # The business result already owns its response-time pair.
                    # Carry it into a terminal stream error instead of reading
                    # a newer state after persistence or serialization fails.
                    setattr(exc, "_response_snapshot_attempted", True)
                    if isinstance(draft, _AuthoritativeResponseDraft):
                        setattr(exc, "response_snapshot", dict(draft.snapshot))
                    if type(response_task_state) is TaskStateSnapshotV1:
                        setattr(
                            exc,
                            "response_task_state_snapshot",
                            response_task_state,
                        )
                    raise
                await queue.put(
                    _QueuedStreamEvent(
                        line,
                        response_record=response_record,
                        terminal_recorder=partial(
                            _record_agent_payload_terminal,
                            payload,
                        ),
                    )
                )
            except _ExecutionCancelled as exc:
                raise asyncio.CancelledError() from exc
            except AgentProtocolError as exc:
                snapshot, error_task_state = error_response_state(
                    exc,
                    exc.response_snapshot,
                )
                protocol = _public_response_protocol(
                    exc.bind(
                        request_id=request_id or exc.request_id or new_request_id(),
                        search_id=str(
                            snapshot.get("search_id") or exc.search_id or search_id
                        ).strip(),
                    )
                )
                error_payload: dict[str, object] = {
                    "type": "error",
                    "message": _public_protocol_message(protocol),
                    **protocol.to_dict(),
                }
                error_payload = with_stream_error_task_state(
                    error_payload,
                    error_task_state,
                )
                coordination_ack = current_coordination_ack()
                if coordination_ack is not None:
                    _with_session_coordination_ack(
                        error_payload,
                        coordination_ack,
                    )
                response_record = await finalize_error_event(
                    error_payload,
                    snapshot=snapshot,
                )
                line = serialized_event(error_payload)
                error_kind = type(exc).__name__
                await queue.put(
                    _QueuedStreamEvent(
                        line,
                        response_record=response_record,
                        terminal_recorder=partial(
                            _record_protocol_terminal,
                            protocol,
                            http_status=200,
                            error_kind=error_kind,
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - keep internal failures out of the public stream.
                logger.exception("streamed Agent request failed")
                snapshot, error_task_state = error_response_state(
                    exc,
                    getattr(exc, "response_snapshot", None),
                )
                protocol = _public_response_protocol(
                    RequestProtocol.from_code(
                        "SERVICE_UNAVAILABLE",
                        request_id=request_id or new_request_id(),
                        search_id=str(snapshot.get("search_id") or search_id).strip(),
                    )
                )
                error_payload = {
                    "type": "error",
                    "message": "服务端处理失败，请稍后重试。",
                    **protocol.to_dict(),
                }
                error_payload = with_stream_error_task_state(
                    error_payload,
                    error_task_state,
                )
                coordination_ack = current_coordination_ack()
                if coordination_ack is not None:
                    _with_session_coordination_ack(
                        error_payload,
                        coordination_ack,
                    )
                response_record = await finalize_error_event(
                    error_payload,
                    snapshot=snapshot,
                    persist_authoritative=not isinstance(exc, ResponseStoreError),
                )
                line = serialized_event(error_payload)
                error_kind = type(exc).__name__
                await queue.put(
                    _QueuedStreamEvent(
                        line,
                        response_record=response_record,
                        terminal_recorder=partial(
                            _record_protocol_terminal,
                            protocol,
                            http_status=200,
                            error_kind=error_kind,
                        ),
                    )
                )
            finally:
                await queue.put(None)

    task = asyncio.create_task(run())

    def record_cancelled() -> None:
        event_scope = (
            trace_event_session_scope(trace_event_session)
            if trace_event_session is not None
            else suppress()
        )
        with trace_context_scope(trace_context), event_scope, _public_trace_meta_scope(
            dict(trace_meta or {})
        ):
            session = current_trace_event_session()
            if session is None or not session.terminal_attempted:
                record_public_terminal(
                    stage="stream_delivery",
                    outcome="cancelled",
                    failed=True,
                    duration_ms=_trace_duration_ms(),
                    safe_attributes={
                        "endpoint": str((trace_meta or {}).get("endpoint") or "/unknown"),
                        "response_mode": "stream",
                        "http_status": 499,
                        "error_kind": "ClientDisconnected",
                    },
                )

    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            expose_stream_event(event)
            yield event.body
        await task
    except (asyncio.CancelledError, GeneratorExit):
        delivery_cancelled.set()
        record_cancelled()
        raise
    finally:
        if not task.done():
            delivery_cancelled.set()
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for record in tuple(unexposed_responses.values()):
            try:
                await asyncio.to_thread(
                    response_store.discard_unexposed,
                    record.response_id,
                    trace_id=trace_context.trace_id,
                )
            except Exception:  # noqa: BLE001 - cancellation remains authoritative.
                logger.exception("unexposed queued stream response cleanup failed")
            finally:
                unexposed_responses.pop(record.response_id, None)


def _public_progress_event(stage: object, message: object) -> dict[str, str]:
    public_stage = str(stage or "").strip().lower()
    public_message = str(message or "").strip()
    if public_message in _PUBLIC_PROGRESS_EXACT.get(public_stage, ()):
        return {"type": "progress", "stage": public_stage, "message": public_message}

    chapter_match = (
        _PUBLIC_PROGRESS_CHAPTER_RE.fullmatch(public_message)
        if public_stage == "searching"
        else None
    )
    if chapter_match and chapter_match.group(1) in CHAPTERS:
        return {"type": "progress", "stage": public_stage, "message": public_message}

    if public_stage == "a3_auto_validating":
        started = _PUBLIC_PROGRESS_AUTO_START_RE.fullmatch(public_message)
        if started and int(started.group(1)) <= 100:
            return {"type": "progress", "stage": public_stage, "message": public_message}
        completed = _PUBLIC_PROGRESS_AUTO_DONE_RE.fullmatch(public_message)
        if completed:
            done, total = map(int, completed.groups())
            if done <= total <= 100:
                return {"type": "progress", "stage": public_stage, "message": public_message}

    return {
        "type": "progress",
        "stage": "working",
        "message": "正在处理当前请求…",
    }


async def _periodic_session_cleanup(cleaner: Callable[[], None], interval_seconds: float) -> None:
    interval = max(0.01, float(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cleaner)
        except Exception:  # noqa: BLE001 - cleanup failure must not stop the web service.
            logger.exception("periodic session cleanup failed")

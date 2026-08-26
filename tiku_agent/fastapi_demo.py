"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from io import BytesIO
import inspect
import json
import logging
import math
import re
import secrets
import time
from pathlib import Path
from typing import Callable
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
)
from tiku_agent.session_store import SQLiteSessionStore
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
from tiku_shared.trace_context import (
    TraceContext,
    current_request_id,
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
FEEDBACK_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_FEEDBACK_BYTES = 128 * 1024
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
_PAGE = (WEB_DIR / "index.html").read_text(encoding="utf-8")
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


def create_app(
    *,
    runtime: AgentSessionRuntime | None = None,
    incoming_dir: str | Path = INCOMING_DIR,
    session_cookie: str = SESSION_COOKIE,
    cleanup_interval_seconds: float = 300.0,
    invite_access: InviteAccess | None = None,
    feedback_store: SQLiteFeedbackStore | None = None,
    feedback_retention_days_provider: Callable[[], int] | None = None,
    output_watchdog: object | None = None,
    trace_event_recorder: TraceEventRecorder | None = None,
) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    session_cookie = str(session_cookie).strip()
    if not session_cookie:
        raise ValueError("session_cookie is required")
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
    app.state.trace_event_recorder = trace_event_recorder
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.exception_handler(HTTPException)
    async def public_http_error(request: Request, exc: HTTPException) -> Response:
        if not request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        search_id = ""
        if session_id:
            search_id = str(runtime.session_snapshot(session_id).get("search_id") or "")
        protocol = _http_error_protocol(request, exc, search_id=search_id)
        if session_id and protocol.layer is not RequestLayer.LOGIN:
            _record_protocol_event(
                runtime,
                session_id,
                kind=_event_kind_for_layer(protocol.layer),
                identity_key=_identity_key(request),
                protocol=protocol,
                error_kind="HTTPException",
            )
        return _protocol_json_response(
            str(exc.detail),
            protocol,
            status_code=exc.status_code,
            headers=exc.headers,
            output_watchdog=output_watchdog,
            error_kind=type(exc).__name__,
        )

    @app.exception_handler(AgentRuntimeBusyError)
    async def runtime_busy(request: Request, exc: AgentRuntimeBusyError) -> JSONResponse:
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            str(exc),
            protocol,
            status_code=429,
            headers={"Retry-After": "15", "Cache-Control": "no-store"},
            output_watchdog=output_watchdog,
            error_kind=type(exc).__name__,
        )

    @app.exception_handler(AgentBudgetExceededError)
    async def runtime_budget(request: Request, exc: AgentBudgetExceededError) -> JSONResponse:
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            str(exc),
            protocol,
            status_code=503,
            headers={"Retry-After": "3600", "Cache-Control": "no-store"},
            output_watchdog=output_watchdog,
            error_kind=type(exc).__name__,
        )

    @app.exception_handler(AgentProtocolError)
    async def protocol_error(request: Request, exc: AgentProtocolError) -> JSONResponse:
        protocol = exc.bind(
            request_id=_request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            str(exc),
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
            output_watchdog=output_watchdog,
            error_kind=type(exc).__name__,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled public Agent request failure")
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        search_id = ""
        if session_id:
            search_id = str(runtime.session_snapshot(session_id).get("search_id") or "")
        protocol = RequestProtocol.from_code(
            "SERVICE_UNAVAILABLE",
            request_id=_request_id(request),
            search_id=search_id,
        )
        if session_id:
            _record_protocol_event(
                runtime,
                session_id,
                kind="image" if request.url.path.startswith("/api/image") else "text",
                identity_key=_identity_key(request),
                protocol=protocol,
                error_kind="UnhandledException",
            )
        return _protocol_json_response(
            "服务端处理失败，请稍后重试。",
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
            output_watchdog=output_watchdog,
            error_kind=type(exc).__name__,
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
                            result = _protocol_json_response(
                                "请先使用有效邀请码登录。",
                                RequestProtocol.from_code(
                                    "LOGIN_REQUIRED", request_id=_request_id(request)
                                ),
                                status_code=401,
                                headers={"Cache-Control": "no-store"},
                                output_watchdog=output_watchdog,
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
        try:
            content_length = int(request.headers.get("content-length") or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if content_length > 4096:
            raise HTTPException(status_code=413, detail="invitation request is too large")
        form = await request.form()
        identity = invite_access.authenticate_code(str(form.get("code") or ""))
        if identity is None:
            _record_protocol_terminal(
                RequestProtocol.from_code(
                    "INVITE_INVALID", request_id=_request_id(request)
                ),
                http_status=401,
            )
            error = '<p class="error">邀请码无效或已停用，请检查后重试。</p>'
            return HTMLResponse(
                _INVITE_PAGE.replace("{error}", error),
                status_code=401,
                headers={"Cache-Control": "no-store"},
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
        return result

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
        rating = str(payload.get("rating") or "").strip().lower()
        raw_tags = payload.get("tags")
        detail = str(payload.get("detail") or "").strip()
        conversation = payload.get("conversation")
        try:
            search_duration_ms = int(payload.get("search_duration_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid search duration") from exc
        if not 0 <= search_duration_ms <= 86_400_000:
            raise HTTPException(status_code=400, detail="invalid search duration")
        if not FEEDBACK_MESSAGE_ID_RE.fullmatch(message_id):
            raise HTTPException(status_code=400, detail="invalid message id")
        if rating not in FEEDBACK_TAGS:
            raise HTTPException(status_code=400, detail="invalid rating")
        if not isinstance(raw_tags, list) or len(raw_tags) > 8:
            raise HTTPException(status_code=400, detail="invalid feedback tags")
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip()))
        if any(tag not in FEEDBACK_TAGS[rating] for tag in tags):
            raise HTTPException(status_code=400, detail="invalid feedback tag")
        if len(detail) > 300:
            raise HTTPException(status_code=400, detail="feedback detail is too long")
        if conversation is not None and not isinstance(conversation, list):
            raise HTTPException(status_code=400, detail="invalid feedback conversation")
        target_message = _feedback_target_message(conversation, message_id)
        if conversation is not None and not target_message:
            raise HTTPException(status_code=400, detail="feedback target is missing")
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session is required")
        snapshot = runtime.session_snapshot(session_id)
        identity_key = _identity_key(request) or "local"
        try:
            target_revision = int(
                target_message.get("taskRevision")
                or target_message.get("task_revision")
                or 0
            )
        except (TypeError, ValueError):
            target_revision = 0
        revision = max(0, target_revision or int(snapshot.get("task_revision") or 0))
        try:
            target_candidate_count = int(
                target_message.get("candidateCount")
                if "candidateCount" in target_message
                else target_message.get("candidate_count", snapshot.get("candidate_count") or 0)
            )
        except (TypeError, ValueError):
            target_candidate_count = int(snapshot.get("candidate_count") or 0)
        clean_session_key = session_key(session_id)
        search_id = str(
            payload.get("search_id")
            or snapshot.get("search_id")
            or (f"{clean_session_key}:{revision}" if revision > 0 else "")
        ).strip()
        try:
            rated_protocol = RequestProtocol(
                status=(
                    payload.get("status")
                    or ("ERROR" if snapshot.get("phase") == "ERROR" else "SUCCESS")
                ),
                layer=payload.get("layer") or "tool",
                code=payload.get("code") or (
                    "AGENT_FAILED"
                    if snapshot.get("phase") == "ERROR"
                    else "REQUEST_SUCCEEDED"
                ),
                retryable=bool(payload.get("retryable")),
                action=payload.get("action") or "",
                request_id=payload.get("request_id") or "",
                search_id=search_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid feedback protocol") from exc
        try:
            saved = feedback_store.upsert(
                message_id=message_id,
                identity_key=identity_key,
                session_key=clean_session_key,
                rating=rating,
                tags=tags,
                detail=detail,
                task_revision=revision,
                phase=str(snapshot.get("phase") or ""),
                candidate_count=max(0, target_candidate_count),
                search_duration_ms=search_duration_ms,
                search_key=search_id,
                request_id=rated_protocol.request_id,
                search_id=rated_protocol.search_id,
                status=rated_protocol.status.value,
                layer=rated_protocol.layer.value,
                code=rated_protocol.code,
                chapter=str(snapshot.get("chapter") or ""),
                image_route=str(snapshot.get("image_route") or ""),
                workflow_search_id=str(snapshot.get("workflow_search_id") or search_id),
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
            search_id=str(snapshot.get("search_id") or ""),
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
            workflow_search_id=str(snapshot.get("workflow_search_id") or search_id),
            search_id=str(snapshot.get("search_id") or search_id),
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
                "rating": saved.rating,
                "tags": list(saved.tags),
                "feedback_scope": saved.feedback_scope,
                "updated_at": saved.updated_at,
            },
            **protocol.to_dict(),
        })

    @app.delete("/api/feedback/{message_id}")
    def delete_feedback(message_id: str, request: Request) -> JSONResponse:
        message_id = str(message_id or "").strip()
        if not FEEDBACK_MESSAGE_ID_RE.fullmatch(message_id):
            raise HTTPException(status_code=400, detail="invalid message id")
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session is required")
        removed = feedback_store.delete(
            message_id=message_id,
            identity_key=_identity_key(request) or "local",
            session_key=session_key(session_id),
        )
        snapshot = runtime.session_snapshot(session_id)
        protocol = RequestProtocol(
            status=RequestStatus.SUCCESS,
            layer=RequestLayer.FEEDBACK,
            code="FEEDBACK_REMOVED",
            request_id=_request_id(request),
            search_id=str(snapshot.get("search_id") or ""),
        )
        _record_protocol_event(
            runtime,
            session_id,
            kind="feedback",
            identity_key=_identity_key(request) or "local",
            protocol=protocol,
        )
        _record_protocol_terminal(protocol, http_status=200)
        return JSONResponse({
            "ok": True,
            "message_id": message_id,
            "removed": removed,
            **protocol.to_dict(),
        })

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        session_id = _session_id(request, cookie_name=session_cookie)
        result = HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})
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

    @app.get("/api/session")
    def session(request: Request) -> JSONResponse:
        session_id = _session_id(request, cookie_name=session_cookie)
        path = runtime.current_image_path(session_id)
        snapshot = _public_session_snapshot(runtime.session_snapshot(session_id))
        result = JSONResponse({
            "uploaded_image": f"/api/upload/{path.name}" if path is not None else "",
            "session": snapshot,
        })
        _set_session_cookie(
            result,
            session_id,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )
        return result

    @app.post("/api/message")
    async def message(request: Request) -> Response:
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
        stale = _validate_action_context(
            runtime,
            session_id,
            payload.get("action_context"),
            request_id=_request_id(request),
        )
        if stale is not None:
            return _agent_json(
                stale,
                runtime,
                session_id,
                secure_cookie=_is_secure_request(request),
                cookie_name=session_cookie,
            )
        response = _handle_text(runtime, session_id, text, request=request)
        return _agent_json(
            response,
            runtime,
            session_id,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )

    @app.post("/api/message/stream")
    async def message_stream(request: Request) -> StreamingResponse:
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
        trace_context = _request_trace_context(request)

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            stale = _validate_action_context(
                runtime,
                session_id,
                payload.get("action_context"),
                request_id=request_id,
            )
            if stale is not None:
                return _agent_payload(stale, runtime, session_id)
            response = _handle_text(
                runtime,
                session_id,
                text,
                request_id=request_id,
                identity_key=identity_key,
                progress=progress,
            )
            return _agent_payload(response, runtime, session_id)

        result = StreamingResponse(
            _stream_agent_events(
                execute,
                request_id=request_id,
                search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
                trace_context=trace_context,
                trace_event_session=current_trace_event_session(),
                trace_meta=_current_public_trace_meta(),
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

    @app.post("/api/reset")
    def reset(request: Request) -> JSONResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        search_id = ""
        if session_id:
            search_id = str(runtime.session_snapshot(session_id).get("search_id") or "")
            runtime.clear(session_id)
        protocol = RequestProtocol(
            status=RequestStatus.SUCCESS,
            layer=RequestLayer.SESSION,
            code="SESSION_RESET",
            request_id=_request_id(request),
            search_id=search_id,
        )
        result = JSONResponse({"ok": True, **protocol.to_dict()})
        result.delete_cookie(session_cookie, secure=_is_secure_request(request), httponly=True, samesite="lax")
        return result

    @app.post("/api/image")
    async def image(request: Request) -> Response:
        content, filename, content_type = await _read_image_upload(request)
        session_id = _session_id(request, cookie_name=session_cookie)
        incoming = _write_incoming_image(
            content,
            filename,
            content_type,
            incoming_dir=incoming_dir,
        )
        try:
            response = _handle_image(runtime, session_id, incoming, request=request)
            uploaded_image = runtime.current_image_path(session_id)
        finally:
            incoming.unlink(missing_ok=True)
        return _agent_json(
            response,
            runtime,
            session_id,
            uploaded_image=uploaded_image,
            secure_cookie=_is_secure_request(request),
            cookie_name=session_cookie,
        )

    @app.post("/api/image/stream")
    async def image_stream(request: Request) -> StreamingResponse:
        content, filename, content_type = await _read_image_upload(request)
        session_id = _session_id(request, cookie_name=session_cookie)
        incoming = _write_incoming_image(
            content,
            filename,
            content_type,
            incoming_dir=incoming_dir,
        )
        request_id = _request_id(request)
        identity_key = _identity_key(request)
        trace_context = _request_trace_context(request)

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            try:
                response = _handle_image(
                    runtime,
                    session_id,
                    incoming,
                    request_id=request_id,
                    identity_key=identity_key,
                    progress=progress,
                )
                uploaded_image = runtime.current_image_path(session_id)
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    uploaded_image=uploaded_image,
                )
            finally:
                incoming.unlink(missing_ok=True)

        result = StreamingResponse(
            _stream_agent_events(
                execute,
                request_id=request_id,
                trace_context=trace_context,
                trace_event_session=current_trace_event_session(),
                trace_meta=_current_public_trace_meta(),
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

    @app.get("/api/upload/{filename}")
    def get_upload(filename: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        path = runtime.resolve_upload(session_id, filename) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="upload not found")
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        path = runtime.resolve_media(session_id, media_id) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    if bool(getattr(runtime, "a3_enabled", False)):

        @app.post("/api/a3/select")
        async def a3_select(request: Request) -> Response:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="json object is required")
            unit_id = str(payload.get("unit_id") or "").strip()
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            try:
                task_revision = int(payload.get("task_revision"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="task_revision is required") from exc
            session_id = _session_id(request, cookie_name=session_cookie)
            response = runtime.select_unit(  # type: ignore[attr-defined]
                session_id,
                unit_id,
                task_revision=task_revision,
                request_id=_request_id(request),
            )
            return _agent_json(
                response,
                runtime,
                session_id,
                secure_cookie=_is_secure_request(request),
                cookie_name=session_cookie,
            )

        @app.post("/api/a3/select/stream")
        async def a3_select_stream(request: Request) -> StreamingResponse:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="json object is required")
            unit_id = str(payload.get("unit_id") or "").strip()
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            try:
                task_revision = int(payload.get("task_revision"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="task_revision is required") from exc
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)
            trace_context = _request_trace_context(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "progress": progress,
                    "request_id": request_id,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.select_unit(  # type: ignore[attr-defined]
                    session_id,
                    unit_id,
                    **kwargs,
                )
                submitted_crop = (
                    runtime.current_crop_path(session_id, unit_id)  # type: ignore[attr-defined]
                    if runtime.session_snapshot(session_id).get("a3", {}).get("phase") == "A2_ACTIVE"
                    else None
                )
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    submitted_crop=submitted_crop,
                )

            result = StreamingResponse(
                _stream_agent_events(
                    execute,
                    request_id=request_id,
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
                    trace_context=trace_context,
                    trace_event_session=current_trace_event_session(),
                    trace_meta=_current_public_trace_meta(),
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

        @app.post("/api/a3/prepare/stream")
        async def a3_prepare_stream(request: Request) -> StreamingResponse:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("unit_ids"), list):
                raise HTTPException(status_code=400, detail="unit_ids array is required")
            unit_ids = [str(value or "").strip() for value in payload["unit_ids"]]
            if not unit_ids or any(not value for value in unit_ids):
                raise HTTPException(status_code=400, detail="unit_ids are required")
            try:
                task_revision = int(payload.get("task_revision"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="task_revision is required") from exc
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)
            trace_context = _request_trace_context(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "progress": progress,
                    "request_id": request_id,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.prepare_units(  # type: ignore[attr-defined]
                    session_id,
                    unit_ids,
                    **kwargs,
                )
                return _agent_payload(response, runtime, session_id)

            result = StreamingResponse(
                _stream_agent_events(
                    execute,
                    request_id=request_id,
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
                    trace_context=trace_context,
                    trace_event_session=current_trace_event_session(),
                    trace_meta=_current_public_trace_meta(),
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

        @app.post("/api/a3/crop/stream")
        async def a3_crop_stream(request: Request) -> StreamingResponse:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - malformed external input.
                raise HTTPException(status_code=400, detail="invalid json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("bounds"), dict):
                raise HTTPException(status_code=400, detail="crop bounds are required")
            unit_id = str(payload.get("unit_id") or "").strip()
            try:
                task_revision = int(payload.get("task_revision"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="task_revision is required") from exc
            if not unit_id:
                raise HTTPException(status_code=400, detail="unit_id is required")
            session_id = _session_id(request, cookie_name=session_cookie)
            request_id = _request_id(request)
            identity_key = _identity_key(request)
            trace_context = _request_trace_context(request)

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "progress": progress,
                    "request_id": request_id,
                }
                if identity_key:
                    kwargs["identity_key"] = identity_key
                response = runtime.handle_crop(  # type: ignore[attr-defined]
                    session_id,
                    payload["bounds"],
                    unit_id=unit_id,
                    task_revision=task_revision,
                    **kwargs,
                )
                a3_snapshot = runtime.session_snapshot(session_id).get("a3") or {}
                submitted_crop = (
                    runtime.current_crop_path(session_id, unit_id)  # type: ignore[attr-defined]
                    if a3_snapshot.get("phase") == "A2_ACTIVE"
                    else None
                )
                return _agent_payload(
                    response,
                    runtime,
                    session_id,
                    submitted_crop=submitted_crop,
                )

            result = StreamingResponse(
                _stream_agent_events(
                    execute,
                    request_id=request_id,
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
                    trace_context=trace_context,
                    trace_event_session=current_trace_event_session(),
                    trace_meta=_current_public_trace_meta(),
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

        @app.get("/api/a3/crop/{unit_id}")
        def get_a3_crop(unit_id: str, request: Request) -> FileResponse:
            session_id = str(request.cookies.get(session_cookie) or "").strip()
            path = (
                runtime.current_crop_path(session_id, unit_id)  # type: ignore[attr-defined]
                if session_id
                else None
            )
            if path is None:
                raise HTTPException(status_code=404, detail="crop not found")
            return FileResponse(path, headers={"Cache-Control": "private, no-store"})

        @app.get("/api/a3/overlay")
        def get_a3_overlay(request: Request) -> FileResponse:
            session_id = str(request.cookies.get(session_cookie) or "").strip()
            path = (
                runtime.current_auto_crop_overlay_path(session_id)  # type: ignore[attr-defined]
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
    clean = str(path or "")
    if clean.endswith("/stream"):
        return "stream"
    return "json" if clean.startswith("/api/") or clean == "/health" else "html"


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
    _record_protocol_terminal(
        protocol,
        http_status=status_code,
        error_kind=error_kind,
    )
    return JSONResponse(
        {"detail": message, **protocol.to_dict()},
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


def _http_error_protocol(
    request: Request,
    exc: HTTPException,
    *,
    search_id: str = "",
) -> RequestProtocol:
    path = request.url.path
    detail = str(exc.detail or "").lower()
    if path.startswith("/api/invite/"):
        code = "INVITE_INVALID" if exc.status_code == 401 else "LOGIN_REQUIRED"
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
    value = str(request.cookies.get(cookie_name) or "").strip()
    resolved = value or secrets.token_urlsafe(24)
    bind_trace_event_dimensions(session_key=session_key(resolved))
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


def _handle_text(
    runtime: AgentSessionRuntime,
    session_id: str,
    text: str,
    *,
    request: Request | None = None,
    request_id: str = "",
    identity_key: str = "",
    progress: Callable[[str, str], None] | None = None,
) -> AgentResponse:
    if request is not None:
        request_id = request_id or _request_id(request)
        identity_key = identity_key or _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_text, "request_id"):
        kwargs["request_id"] = request_id
    if identity_key:
        kwargs["identity_key"] = identity_key
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
) -> AgentResponse:
    if request is not None:
        request_id = request_id or _request_id(request)
        identity_key = identity_key or _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_image, "request_id"):
        kwargs["request_id"] = request_id
    if identity_key:
        kwargs["identity_key"] = identity_key
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
        recorder(session_id, **kwargs)


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
    target_dir = Path(incoming_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
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
    output = target_dir / f"{uuid4().hex}{suffix}"
    output.write_bytes(content)
    return output


def _agent_json(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    secure_cookie: bool = False,
    cookie_name: str = SESSION_COOKIE,
) -> JSONResponse:
    payload = _agent_payload(
        response,
        runtime,
        session_id,
        uploaded_image=uploaded_image,
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


def _agent_payload(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    submitted_crop: Path | None = None,
) -> dict[str, object]:
    media_snapshot = runtime.session_snapshot(session_id)
    media_guard = _media_delivery_guard(response, media_snapshot)
    image_urls, media = _persist_response_media(response, runtime, session_id)
    text = response.text
    if media and media.get("status") in {
        "unavailable",
        "partial",
        "incomplete",
    }:
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
                )
            except Exception:  # noqa: BLE001 - delivery state must not break the reply.
                logger.warning("failed to reopen A3 unit after media failure")
    uploaded_image_url = f"/api/upload/{uploaded_image.name}" if uploaded_image is not None else ""
    submitted_crop_url = ""
    if submitted_crop is not None and submitted_crop.is_file():
        persisted_crop = _persist_public_media(runtime, session_id, submitted_crop)
        if persisted_crop is not None:
            submitted_crop_url = f"/api/media/{persisted_crop.name}"
    snapshot = _public_session_snapshot(runtime.session_snapshot(session_id))
    feedback_images: list[dict[str, str]] = []
    if response.intent == "a3_units_prepared":
        try:
            overlay_resolver = getattr(runtime, "current_auto_crop_overlay_path", None)
            overlay_path = overlay_resolver(session_id) if callable(overlay_resolver) else None
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
    payload: dict[str, object] = {
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
    }
    dimensions: dict[str, str] = {
        "session_key": session_key(session_id),
    }
    for key, value in (
        ("workflow_search_id", media_snapshot.get("workflow_search_id")),
        ("search_id", protocol.search_id or media_snapshot.get("search_id")),
    ):
        clean = str(value or "").strip()
        if clean:
            dimensions[key] = clean
    raw_a3 = media_snapshot.get("a3")
    if isinstance(raw_a3, Mapping):
        selected = raw_a3.get("selected_unit")
        if isinstance(selected, Mapping) and str(selected.get("unit_id") or "").strip():
            dimensions["unit_id"] = str(selected["unit_id"]).strip()
    bind_trace_event_dimensions(**dimensions)

    return payload


def _record_agent_payload_terminal(payload: Mapping[str, object]) -> None:
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


def _validate_action_context(
    runtime: AgentSessionRuntime,
    session_id: str,
    raw_context: object,
    *,
    request_id: str = "",
) -> AgentResponse | None:
    """Reject a button action when it belongs to an older question/candidate list."""
    if raw_context is None:
        return None
    if not isinstance(raw_context, dict) or raw_context.get("type") != "select_candidate":
        snapshot = runtime.session_snapshot(session_id)
        return AgentResponse(
            text="这个操作已经失效，请使用当前页面中的操作。",
            intent="stale_action",
            protocol=RequestProtocol.from_code(
                "STALE_ACTION",
                request_id=request_id,
                search_id=str(snapshot.get("search_id") or ""),
            ).to_dict(),
        )
    snapshot = runtime.session_snapshot(session_id)
    try:
        rank = int(raw_context.get("rank") or 0)
        task_revision = int(raw_context.get("task_revision") or 0)
    except (TypeError, ValueError):
        rank = 0
        task_revision = -1
    generation = str(raw_context.get("candidate_generation") or "")
    valid = (
        snapshot.get("session_valid") is True
        and snapshot.get("phase") in {"WAIT_CANDIDATE_CHOICE", "ANSWERED"}
        and task_revision == snapshot.get("task_revision")
        and generation
        and generation == snapshot.get("candidate_generation")
        and 1 <= rank <= int(snapshot.get("candidate_count") or 0)
    )
    if valid:
        return None
    has_image = snapshot.get("has_active_image") is True
    message = (
        "这是上一道题或上一轮搜索的候选，已经不能选择。请继续完成当前题目的章节确认和搜索。"
        if has_image
        else "这是已失效的候选，当前会话没有可选择的候选题，请重新上传题图。"
    )
    return AgentResponse(
        text=message,
        intent="stale_candidate",
        protocol=RequestProtocol.from_code(
            "STALE_CANDIDATE",
            request_id=request_id,
            search_id=str(snapshot.get("search_id") or ""),
        ).to_dict(),
    )


async def _stream_agent_events(
    execute: Callable[[Callable[[str, str], None]], dict[str, object]],
    *,
    request_id: str = "",
    search_id: str = "",
    trace_context: TraceContext,
    trace_event_session: TraceEventSession | None = None,
    trace_meta: Mapping[str, object] | None = None,
):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def serialized_event(event: Mapping[str, object]) -> str:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    def progress(stage: str, message: str) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            serialized_event(_public_progress_event(stage, message)),
        )

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
                line = serialized_event({"type": "result", "data": payload})
                _record_agent_payload_terminal(payload)
                await queue.put(line)
            except AgentProtocolError as exc:
                protocol = _public_response_protocol(
                    exc.bind(
                        request_id=request_id or exc.request_id or new_request_id(),
                        search_id=search_id or exc.search_id,
                    )
                )
                line = serialized_event({
                    "type": "error",
                    "message": _public_protocol_message(protocol),
                    **protocol.to_dict(),
                })
                _record_protocol_terminal(
                    protocol,
                    http_status=200,
                    error_kind=type(exc).__name__,
                )
                await queue.put(line)
            except Exception as exc:  # noqa: BLE001 - keep internal failures out of the public stream.
                logger.exception("streamed Agent request failed")
                protocol = _public_response_protocol(
                    RequestProtocol.from_code(
                        "SERVICE_UNAVAILABLE",
                        request_id=request_id or new_request_id(),
                        search_id=search_id,
                    )
                )
                line = serialized_event({
                    "type": "error",
                    "message": "服务端处理失败，请稍后重试。",
                    **protocol.to_dict(),
                })
                _record_protocol_terminal(
                    protocol,
                    http_status=200,
                    error_kind=type(exc).__name__,
                )
                await queue.put(line)
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
            yield event
        await task
    except (asyncio.CancelledError, GeneratorExit):
        record_cancelled()
        raise
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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

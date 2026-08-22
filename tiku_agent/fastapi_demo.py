"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from io import BytesIO
import inspect
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess, InviteIdentity
from tiku_agent.session_artifacts import session_key
from tiku_agent.session_runtime import (
    AgentBudgetExceededError,
    AgentProtocolError,
    AgentRuntimeBusyError,
    AgentSessionRuntime,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.user_output import (
    USER_OUTPUT_SCHEMA_VERSION,
    ProgressOutputRequestV1,
    PublicContactV1,
    PublicMessageV1,
    UserAction,
    render_progress_output,
)
from tiku_agent.user_output_integration import (
    build_a2_output_draft,
    build_a3_output_draft,
    finalize_output_draft,
)
from tiku_shared.model_costs import SQLiteModelCostLedger
from tiku_shared.request_protocol import (
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
    new_request_id,
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
logger = logging.getLogger(__name__)
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_PUBLIC_PROTOCOL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{7,127}$")
_PUBLIC_STATE_PHASE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_PUBLIC_STATE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PUBLIC_STATE_SENSITIVE_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(
        r"/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|cookie)\b", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9]{6,}|AKIA[A-Z0-9]{8,})\b"),
    re.compile(
        r"(?:^|[^A-Za-z0-9])(?:token|secret|password|api[_-]?key)[_:= -][A-Za-z0-9_-]{4,}",
        re.IGNORECASE,
    ),
    re.compile(r"\btraceback\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]{0,80}(?:Error|Exception)\s*:", re.IGNORECASE),
    re.compile(
        r"\b(?:invalid_observation_schema|schema_error|raw_model_output|reasoning|"
        r"route[_ -]?code|reason[_ -]?code|confidence|debug|prompt)\b",
        re.IGNORECASE,
    ),
)
_PROGRESS_KEY_BY_STAGE = {
    "queued": "progress.queue.waiting",
    "dequeued": "progress.queue.started",
    "triage": "progress.image.triage",
    "searching": "progress.image.analysis",
    "global_searching": "progress.search.global",
    "a3_understanding": "progress.page.understanding",
    "a3_reunderstanding": "progress.page.reunderstanding",
    "a3_auto_grounding": "progress.page.auto_grounding",
    # These callbacks do not yet carry a structured question label.  A fixed,
    # generic registered message is safer than reflecting their raw text.
    "a3_auto_validating": "progress.image.analysis",
    "a3_verifying": "progress.image.analysis",
    "a3_analyzing_unit": "progress.image.analysis",
}
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
) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    session_cookie = str(session_cookie).strip()
    if not session_cookie:
        raise ValueError("session_cookie is required")
    runtime = runtime or AgentSessionRuntime(
        SQLiteSessionStore(DEFAULT_RUNTIME_DIR / "session.db"),
        cost_ledger=SQLiteModelCostLedger(DEFAULT_RUNTIME_DIR / "model_costs.sqlite3"),
    )
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
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.exception_handler(HTTPException)
    async def public_http_error(request: Request, exc: HTTPException) -> Response:
        if not request.url.path.startswith("/api/"):
            return HTMLResponse("页面不可用。", status_code=exc.status_code)
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
            protocol,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(AgentRuntimeBusyError)
    async def runtime_busy(request: Request, exc: AgentRuntimeBusyError) -> JSONResponse:
        protocol = exc.bind(
            request_id=exc.request_id or _request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            protocol,
            status_code=429,
            headers={"Retry-After": "15", "Cache-Control": "no-store"},
        )

    @app.exception_handler(AgentBudgetExceededError)
    async def runtime_budget(request: Request, exc: AgentBudgetExceededError) -> JSONResponse:
        protocol = exc.bind(
            request_id=exc.request_id or _request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            protocol,
            status_code=503,
            headers={"Retry-After": "3600", "Cache-Control": "no-store"},
        )

    @app.exception_handler(AgentProtocolError)
    async def protocol_error(request: Request, exc: AgentProtocolError) -> JSONResponse:
        protocol = exc.bind(
            request_id=exc.request_id or _request_id(request),
            search_id=exc.search_id,
        )
        return _protocol_json_response(
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
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
            protocol,
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def secure_public_requests(request: Request, call_next):
        request.state.request_id = _incoming_request_id(request)
        if _forwarded_proto(request) == "http":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        if invite_access is not None:
            cookie_value = str(request.cookies.get(invite_access.cookie_name) or "")
            identity = invite_access.verify_cookie(cookie_value)
            request.state.invite_identity = identity
            public_path = (
                request.url.path == "/health"
                or request.url.path == "/invite"
                or request.url.path == "/api/invite/login"
                or request.url.path.startswith("/assets/")
            )
            if identity is None and not public_path:
                if request.url.path.startswith("/api/"):
                    result = _protocol_json_response(
                        RequestProtocol.from_code(
                            "LOGIN_REQUIRED", request_id=_request_id(request)
                        ),
                        status_code=401,
                        headers={"Cache-Control": "no-store"},
                    )
                    result.headers["X-Request-ID"] = _request_id(request)
                    return result
                target = "/invite?reason=session_expired" if cookie_value else "/invite"
                return RedirectResponse(target, status_code=303)
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
        return result

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
            raise HTTPException(status_code=400, detail="invalid feedback payload") from exc
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
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request) -> JSONResponse:
        session_id = _session_id(request, cookie_name=session_cookie)
        path = runtime.current_image_path(session_id)
        snapshot = runtime.session_snapshot(session_id)
        result = JSONResponse({
            "uploaded_image": f"/api/upload/{path.name}" if path is not None else "",
            "session": _public_session_snapshot(snapshot),
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

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            stale = _validate_action_context(
                runtime,
                session_id,
                payload.get("action_context"),
                request_id=_request_id(request),
            )
            if stale is not None:
                return _agent_payload(stale, runtime, session_id)
            response = _handle_text(
                runtime, session_id, text, request=request, progress=progress
            )
            return _agent_payload(response, runtime, session_id)

        result = StreamingResponse(
            _stream_agent_events(
                execute,
                request_id=_request_id(request),
                search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
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
        protocol = RequestProtocol.from_code(
            "SESSION_RESET",
            request_id=_request_id(request),
            search_id=search_id,
        )
        message = finalize_output_draft(
            build_a3_output_draft(
                "a3_session_reset",
                {"phase": "IDLE"},
                protocol,
            ),
            protocol,
            delivered_count=0,
            expected_media_count=0,
        )
        result = JSONResponse({"ok": True, **message.to_dict()})
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

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            try:
                response = _handle_image(
                    runtime, session_id, incoming, request=request, progress=progress
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
            _stream_agent_events(execute, request_id=_request_id(request)),
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

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "progress": progress,
                    "request_id": _request_id(request),
                }
                identity_key = _identity_key(request)
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
                    request_id=_request_id(request),
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
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

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "task_revision": task_revision,
                    "progress": progress,
                    "request_id": _request_id(request),
                }
                identity_key = _identity_key(request)
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
                    request_id=_request_id(request),
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
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

            def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
                kwargs: dict[str, object] = {
                    "progress": progress,
                    "request_id": _request_id(request),
                }
                identity_key = _identity_key(request)
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
                    request_id=_request_id(request),
                    search_id=str(runtime.session_snapshot(session_id).get("search_id") or ""),
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


def _incoming_request_id(request: Request) -> str:
    value = str(request.headers.get("x-request-id") or "").strip()
    if re.fullmatch(r"req_[A-Fa-f0-9]{32}", value):
        return value.lower()
    return new_request_id()


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or new_request_id())


def _protocol_json_response(
    protocol: RequestProtocol,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    message = _public_message_for_protocol(protocol)
    return JSONResponse(
        message.to_dict(),
        status_code=status_code,
        headers=headers,
    )


def _public_message_for_protocol(protocol: RequestProtocol) -> PublicMessageV1:
    """Render public transport/session errors without reflecting exception text."""

    draft = build_a2_output_draft(
        "",
        {"phase": "IDLE"},
        protocol,
    )
    return finalize_output_draft(
        draft,
        protocol,
        delivered_count=0,
        expected_media_count=0,
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
    return value or secrets.token_urlsafe(24)


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
    request: Request,
    progress: Callable[[str, str], None] | None = None,
) -> AgentResponse:
    identity_key = _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_text, "request_id"):
        kwargs["request_id"] = _request_id(request)
    if identity_key:
        kwargs["identity_key"] = identity_key
    return runtime.handle_text(session_id, text, **kwargs)


def _handle_image(
    runtime: AgentSessionRuntime,
    session_id: str,
    image_path: Path,
    *,
    request: Request,
    progress: Callable[[str, str], None] | None = None,
) -> AgentResponse:
    identity_key = _identity_key(request)
    kwargs: dict[str, object] = {"progress": progress}
    if _accepts_keyword(runtime.handle_image, "request_id"):
        kwargs["request_id"] = _request_id(request)
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
    result = JSONResponse(
        _agent_payload(
            response,
            runtime,
            session_id,
            uploaded_image=uploaded_image,
        )
    )
    _set_session_cookie(
        result,
        session_id,
        secure_cookie=secure_cookie,
        cookie_name=cookie_name,
    )
    return result


def _agent_payload(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
    submitted_crop: Path | None = None,
) -> dict[str, object]:
    image_urls: list[str] = []
    for image in response.images:
        try:
            path = Path(image)
            if not path.is_file():
                continue
            persisted = runtime.persist_media(session_id, path)
            if persisted is not None:
                image_urls.append(f"/api/media/{persisted.name}")
        except Exception:  # noqa: BLE001 - media evidence is finalized below.
            logger.warning("agent response media persistence failed")
    uploaded_image_url = f"/api/upload/{uploaded_image.name}" if uploaded_image is not None else ""
    submitted_crop_url = ""
    if submitted_crop is not None and submitted_crop.is_file():
        try:
            persisted_crop = runtime.persist_media(session_id, submitted_crop)
            if persisted_crop is not None:
                submitted_crop_url = f"/api/media/{persisted_crop.name}"
        except Exception:  # noqa: BLE001 - submitted crop is feedback-only media.
            logger.warning("submitted crop media persistence failed")
    snapshot = runtime.session_snapshot(session_id)
    public_snapshot = _public_session_snapshot(snapshot)
    feedback_images: list[dict[str, str]] = []
    if response.intent == "a3_units_prepared":
        try:
            overlay_resolver = getattr(runtime, "current_auto_crop_overlay_path", None)
            overlay_path = overlay_resolver(session_id) if callable(overlay_resolver) else None
            if overlay_path is not None and Path(overlay_path).is_file():
                persisted_overlay = runtime.persist_media(session_id, Path(overlay_path))
                if persisted_overlay is not None:
                    feedback_images.append({
                        "kind": "a3_overlay",
                        "url": f"/api/media/{persisted_overlay.name}",
                        "label": "整页框选结果",
                    })
        except Exception:  # noqa: BLE001 - feedback-only media must not fail the reply.
            feedback_images = []
    fallback_request_id, fallback_search_id = _structured_output_ids(
        response.protocol,
        snapshot,
    )
    output = response.output
    if output is None:
        logger.warning("agent response missing structured output")
        media_policy = ""
        expected_media_count = 0
        candidate_shape_mismatch = False
        public_message = _structured_output_failure(
            request_id=fallback_request_id,
            search_id=fallback_search_id,
        )
    else:
        media_policy = getattr(output, "media_policy", "")
        expected_media_count = _expected_media_count(
            output,
            response_image_count=len(response.images),
        )
        candidate_shape_mismatch = (
            media_policy == "candidate_set"
            and len(response.images) != expected_media_count
        )
        delivered_media_count = 0 if candidate_shape_mismatch else len(image_urls)
        try:
            protocol = RequestProtocol.from_dict(response.protocol)
            public_message = finalize_output_draft(
                output,
                protocol,
                delivered_count=delivered_media_count,
                expected_media_count=expected_media_count,
                contact=_public_contact(response.author_contact),
            )
        except Exception:  # noqa: BLE001 - fail closed at the public boundary.
            logger.warning("structured agent output failed validation")
            public_message = _structured_output_failure(
                request_id=fallback_request_id,
                search_id=fallback_search_id,
            )

    final_protocol = public_message.protocol
    if final_protocol is None:  # Defensive: final drafts must never render progress.
        public_message = _structured_output_failure(
            request_id=fallback_request_id,
            search_id=fallback_search_id,
        )
        final_protocol = public_message.protocol
    assert final_protocol is not None

    # Candidate numbering is meaningful only when the whole ranked set was
    # delivered. Contract failures must not leak otherwise-valid media.
    candidate_message_keys = {
        "search.candidates.ready",
        "search.global.candidates.ready",
        "search.candidates.recalled",
        "page.unit.candidates.ready",
    }
    delivery_message_keys = {
        "search.answer.ready",
        "search.answer.resent",
        "page.unit.answer.delivered_remaining",
        "page.unit.answer.delivered_complete",
    }
    if (
        media_policy == "candidate_set"
        and (
            candidate_shape_mismatch
            or len(image_urls) != expected_media_count
            or public_message.message_key not in candidate_message_keys
        )
    ) or (
        media_policy == "delivery"
        and public_message.message_key not in delivery_message_keys
    ) or final_protocol.code in {"MEDIA_NOT_FOUND", "SERVICE_UNAVAILABLE"}:
        image_urls = []
    if final_protocol.code == "SERVICE_UNAVAILABLE":
        feedback_images = []

    failure = _failure_payload(final_protocol, snapshot)
    payload = public_message.to_dict()
    contact = public_message.contact
    public_intent = (
        ""
        if final_protocol.code == "SERVICE_UNAVAILABLE"
        else _public_state_id(response.intent)
    )
    payload.update({
        "images": image_urls,
        "uploaded_image": uploaded_image_url,
        "submitted_crop": submitted_crop_url,
        "feedback_images": feedback_images,
        "intent": public_intent,
        "author_contact": (
            {
                "label": contact.label,
                "channel": contact.channel,
                "value": contact.value,
            }
            if contact is not None
            else {}
        ),
        "session": public_snapshot,
        "failure": failure,
    })
    return payload


def _public_session_snapshot(value: object) -> dict[str, object]:
    """Expose only state required by the current browser workflow."""

    snapshot = value if isinstance(value, Mapping) else {}
    phase = str(snapshot.get("phase") or "IDLE").strip().upper()
    if not _PUBLIC_STATE_PHASE_RE.fullmatch(phase):
        phase = "IDLE"
    task_revision = _public_nonnegative_int(snapshot.get("task_revision"))
    candidate_count = _public_nonnegative_int(snapshot.get("candidate_count"))
    result: dict[str, object] = {
        "session_valid": snapshot.get("session_valid") is True,
        "phase": phase,
        "has_active_image": snapshot.get("has_active_image") is True,
        "task_revision": task_revision,
        "candidate_generation": _public_state_id(snapshot.get("candidate_generation")),
        "candidate_count": candidate_count,
        "search_id": _public_state_id(snapshot.get("search_id")),
    }
    a3 = _public_a3_snapshot(snapshot.get("a3"))
    if a3 is not None:
        result["a3"] = a3
    return result


def _public_a3_snapshot(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("enabled") is not True:
        return None
    phase = str(value.get("phase") or "IDLE").strip().upper()
    if not _PUBLIC_STATE_PHASE_RE.fullmatch(phase):
        phase = "IDLE"
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
    bounds = _public_crop_bounds(crop_draft.get("bounds"))
    return {
        "enabled": True,
        "auto_crop_enabled": value.get("auto_crop_enabled") is True,
        "auto_prepare_all_enabled": value.get("auto_prepare_all_enabled") is True,
        "auto_prepare_all_units": value.get("auto_prepare_all_units") is True,
        "phase": phase,
        "page_finished": value.get("page_finished") is True,
        "units": units,
        "selected_unit": {
            "unit_id": selected_id,
            "display_label": selected_label,
            "context_text": _public_state_text(selected.get("context_text"), 480),
        },
        "auto_crop_overlay_available": value.get("auto_crop_overlay_available") is True,
        "crop_review_required": value.get("crop_review_required") is True,
        "crop_draft": {
            "bounds": bounds,
            "available": crop_draft.get("available") is True,
        },
        "task_revision": _public_nonnegative_int(value.get("task_revision")),
    }


def _public_crop_bounds(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    bounds: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return {}
        bounds[name] = round(float(raw), 6)
    if (
        any(not 0 <= item <= 1 for item in bounds.values())
        or bounds["width"] < 0.02
        or bounds["height"] < 0.02
        or bounds["x"] + bounds["width"] > 1.000001
        or bounds["y"] + bounds["height"] > 1.000001
    ):
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


def _public_state_id(value: object) -> str:
    clean = value.strip() if isinstance(value, str) else ""
    return clean if _PUBLIC_ID_RE.fullmatch(clean) else ""


def _public_nonnegative_int(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000 else 0


def _public_positive_int(value: object) -> int:
    return value if type(value) is int and 0 < value <= 10_000 else 0


def _public_contact(value: object) -> PublicContactV1 | None:
    """Accept only the one bounded legacy contact shape reviewed for public use."""

    if not isinstance(value, dict) or set(value) != {"label", "channel", "value"}:
        return None
    label = value.get("label")
    channel = value.get("channel")
    contact_value = value.get("value")
    if (
        type(label) is not str
        or type(channel) is not str
        or type(contact_value) is not str
    ):
        return None
    clean_label = label.strip()
    clean_channel = channel.strip()
    clean_value = contact_value.strip()
    if clean_label != "联系作者" or clean_channel not in {"微信", "邮箱"}:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,64}", clean_value):
        return None
    return PublicContactV1(clean_label, clean_channel, clean_value)


def _expected_media_count(output: object, *, response_image_count: int) -> int:
    """Keep internal media expectations when response paths are incomplete."""

    media_policy = getattr(output, "media_policy", "")
    facts = getattr(output, "facts", None)
    fact_count = 0
    if isinstance(facts, Mapping):
        key = (
            "candidate_count"
            if media_policy == "candidate_set"
            else "delivered_image_count"
        )
        value = facts.get(key)
        if type(value) is int and value >= 0:
            fact_count = value
    if media_policy == "candidate_set":
        return fact_count
    if media_policy == "delivery":
        return max(response_image_count, fact_count)
    return response_image_count


def _structured_output_ids(
    protocol_payload: object,
    snapshot: Mapping[str, object],
) -> tuple[str, str]:
    """Preserve correlation IDs when an otherwise-valid response loses its draft."""

    if isinstance(protocol_payload, Mapping):
        try:
            protocol = RequestProtocol.from_dict(protocol_payload)
        except (TypeError, ValueError):
            protocol = None
        if protocol is not None:
            return protocol.request_id or new_request_id(), protocol.search_id
    raw_search_id = snapshot.get("search_id")
    search_id = raw_search_id.strip() if isinstance(raw_search_id, str) else ""
    if search_id and not _PUBLIC_PROTOCOL_ID_RE.fullmatch(search_id):
        search_id = ""
    return new_request_id(), search_id


def _structured_output_failure(
    *, request_id: str = "", search_id: str = ""
) -> PublicMessageV1:
    """Render a deterministic response without consulting legacy response text."""

    protocol = RequestProtocol.from_code(
        "SERVICE_UNAVAILABLE",
        request_id=request_id or new_request_id(),
        search_id=search_id,
    )
    draft = build_a2_output_draft(
        "",
        {"phase": "ERROR"},
        protocol,
    )
    return finalize_output_draft(
        draft,
        protocol,
        delivered_count=0,
        expected_media_count=0,
    )


def _failure_payload(
    protocol: RequestProtocol,
    snapshot: dict[str, object],
) -> dict[str, str] | None:
    if protocol.status is not RequestStatus.ERROR:
        return None
    return {
        "kind": "business_error",
        "recovery_action": protocol.action.value or (
            "retry_search" if snapshot.get("has_active_image") is True else "new_chat"
        ),
    }


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
        protocol = RequestProtocol.from_code(
            "STALE_ACTION",
            request_id=request_id or new_request_id(),
            search_id=str(snapshot.get("search_id") or ""),
        )
        return AgentResponse(
            text="这个操作已经失效，请使用当前页面中的操作。",
            state=dict(snapshot),
            intent="stale_action",
            protocol=protocol.to_dict(),
            output=build_a3_output_draft(
                "stale_action",
                _a3_output_snapshot(snapshot),
                protocol,
            ),
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
    protocol = RequestProtocol.from_code(
        "STALE_CANDIDATE",
        request_id=request_id or new_request_id(),
        search_id=str(snapshot.get("search_id") or ""),
    )
    return AgentResponse(
        text=message,
        state=dict(snapshot),
        intent="stale_candidate",
        protocol=protocol.to_dict(),
        output=build_a3_output_draft(
            "stale_candidate",
            _a3_output_snapshot(snapshot),
            protocol,
        ),
    )


def _a3_output_snapshot(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    nested = snapshot.get("a3")
    return nested if isinstance(nested, Mapping) else snapshot


async def _stream_agent_events(
    execute: Callable[[Callable[[str, str], None]], dict[str, object]],
    *,
    request_id: str = "",
    search_id: str = "",
):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    stream_request_id = (
        request_id.strip()
        if isinstance(request_id, str) and _PUBLIC_PROTOCOL_ID_RE.fullmatch(request_id.strip())
        else new_request_id()
    )
    stream_search_id = (
        search_id.strip()
        if isinstance(search_id, str)
        and (not search_id.strip() or _PUBLIC_PROTOCOL_ID_RE.fullmatch(search_id.strip()))
        else ""
    )
    progress_sequence = 0

    def publish_progress(stage: str) -> None:
        nonlocal progress_sequence
        progress_sequence += 1
        key = _PROGRESS_KEY_BY_STAGE.get(stage.strip().lower(), "progress.image.analysis")
        output = render_progress_output(
            ProgressOutputRequestV1(
                schema_version=USER_OUTPUT_SCHEMA_VERSION,
                progress_key=key,
                request_id=stream_request_id,
                search_id=stream_search_id,
                sequence=progress_sequence,
                facts={},
            )
        )
        queue.put_nowait(output.to_stream_event())

    def progress(stage: str, message: str) -> None:
        del message
        loop.call_soon_threadsafe(publish_progress, str(stage or ""))

    async def run() -> None:
        try:
            payload = await asyncio.to_thread(execute, progress)
            await asyncio.sleep(0)
            await queue.put(
                _canonical_final_stream_event(
                    payload,
                    request_id=stream_request_id,
                    search_id=stream_search_id,
                )
            )
        except (AgentRuntimeBusyError, AgentBudgetExceededError) as exc:
            protocol = exc.bind(
                request_id=stream_request_id,
                search_id=stream_search_id,
            )
            await queue.put(_public_message_for_protocol(protocol).to_stream_event())
        except Exception:  # noqa: BLE001 - keep internal failures out of the public stream.
            logger.exception("streamed Agent request failed")
            protocol = RequestProtocol.from_code(
                "SERVICE_UNAVAILABLE",
                request_id=stream_request_id,
                search_id=stream_search_id,
            )
            await queue.put(_public_message_for_protocol(protocol).to_stream_event())
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    while True:
        event = await queue.get()
        if event is None:
            break
        yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    await task


def _canonical_final_stream_event(
    payload: object,
    *,
    request_id: str,
    search_id: str,
) -> dict[str, object]:
    """Validate the rendered final payload before placing it on the stream."""

    try:
        if not isinstance(payload, dict):
            raise ValueError("payload type")
        if payload.get("schema_version") != USER_OUTPUT_SCHEMA_VERSION:
            raise ValueError("schema")
        kind = payload.get("kind")
        if kind not in {"result", "transport_error", "client_error"}:
            raise ValueError("kind")
        message_key = payload.get("message_key")
        if not isinstance(message_key, str) or not re.fullmatch(
            r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+", message_key
        ):
            raise ValueError("message key")
        text = payload.get("text")
        if (
            not isinstance(text, str)
            or not text
            or _public_state_text(text, 320) != text
        ):
            raise ValueError("text")
        actions = payload.get("allowed_actions")
        if (
            not isinstance(actions, list)
            or len(actions) != len(set(actions))
            or any(not isinstance(item, str) or item not in {action.value for action in UserAction} for item in actions)
        ):
            raise ValueError("actions")
        protocol = RequestProtocol.from_dict({
            "schema_version": payload["schema_version"],
            "status": payload["status"],
            "layer": payload["layer"],
            "code": payload["code"],
            "retryable": payload["retryable"],
            "action": payload["action"],
            "request_id": payload["request_id"],
            "search_id": payload["search_id"],
        })
        if protocol.request_id != request_id:
            raise ValueError("request id")
        if search_id and protocol.search_id != search_id:
            raise ValueError("search id")
        event_type = (
            "error"
            if kind in {"transport_error", "client_error"}
            or protocol.status is RequestStatus.ERROR
            else "result"
        )
        return {"type": event_type, "data": payload}
    except (KeyError, TypeError, ValueError):
        protocol = RequestProtocol.from_code(
            "SERVICE_UNAVAILABLE",
            request_id=request_id,
            search_id=search_id,
        )
        return _public_message_for_protocol(protocol).to_stream_event()


async def _periodic_session_cleanup(cleaner: Callable[[], None], interval_seconds: float) -> None:
    interval = max(0.01, float(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cleaner)
        except Exception:  # noqa: BLE001 - cleanup failure must not stop the web service.
            logger.exception("periodic session cleanup failed")

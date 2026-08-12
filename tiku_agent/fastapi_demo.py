"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from io import BytesIO
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
    AgentRuntimeBusyError,
    AgentSessionRuntime,
)
from tiku_agent.session_store import SQLiteSessionStore
from tiku_shared.model_costs import SQLiteModelCostLedger
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

    @app.exception_handler(AgentRuntimeBusyError)
    async def runtime_busy(_request: Request, exc: AgentRuntimeBusyError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=429,
            headers={"Retry-After": "15", "Cache-Control": "no-store"},
        )

    @app.exception_handler(AgentBudgetExceededError)
    async def runtime_budget(_request: Request, exc: AgentBudgetExceededError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=503,
            headers={"Retry-After": "3600", "Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def secure_public_requests(request: Request, call_next):
        if _forwarded_proto(request) == "http":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        if invite_access is not None:
            identity = invite_access.verify_cookie(
                str(request.cookies.get(invite_access.cookie_name) or "")
            )
            request.state.invite_identity = identity
            public_path = (
                request.url.path == "/health"
                or request.url.path == "/invite"
                or request.url.path == "/api/invite/login"
                or request.url.path.startswith("/assets/")
            )
            if identity is None and not public_path:
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": "请先使用有效邀请码登录。"},
                        status_code=401,
                        headers={"Cache-Control": "no-store"},
                    )
                return RedirectResponse("/invite", status_code=303)
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
        return result

    @app.get("/invite", response_class=HTMLResponse)
    def invite_page(request: Request) -> Response:
        if invite_access is None:
            return RedirectResponse("/", status_code=303)
        identity = getattr(request.state, "invite_identity", None)
        if isinstance(identity, InviteIdentity):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(
            _INVITE_PAGE.replace("{error}", ""), headers={"Cache-Control": "no-store"}
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
        session_id = str(request.cookies.get(session_cookie) or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session is required")
        snapshot = runtime.session_snapshot(session_id)
        identity_key = _identity_key(request) or "local"
        revision = int(snapshot.get("task_revision") or 0)
        clean_session_key = session_key(session_id)
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
                candidate_count=int(snapshot.get("candidate_count") or 0),
                search_duration_ms=search_duration_ms,
                search_key=f"{clean_session_key}:{revision}" if revision > 0 else "",
                chapter=str(snapshot.get("chapter") or ""),
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
        return JSONResponse({
            "ok": True,
            "feedback": {
                "message_id": saved.message_id,
                "rating": saved.rating,
                "tags": list(saved.tags),
                "updated_at": saved.updated_at,
            },
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
        return JSONResponse({"ok": True, "message_id": message_id, "removed": removed})

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
        stale = _validate_action_context(runtime, session_id, payload.get("action_context"))
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
            stale = _validate_action_context(runtime, session_id, payload.get("action_context"))
            if stale is not None:
                return _agent_payload(stale, runtime, session_id)
            response = _handle_text(
                runtime, session_id, text, request=request, progress=progress
            )
            return _agent_payload(response, runtime, session_id)

        result = StreamingResponse(_stream_agent_events(execute), media_type="application/x-ndjson")
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
        if session_id:
            runtime.clear(session_id)
        result = JSONResponse({"ok": True})
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

        result = StreamingResponse(_stream_agent_events(execute), media_type="application/x-ndjson")
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

    return app


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


def _handle_text(
    runtime: AgentSessionRuntime,
    session_id: str,
    text: str,
    *,
    request: Request,
    progress: Callable[[str, str], None] | None = None,
) -> AgentResponse:
    identity_key = _identity_key(request)
    if identity_key:
        return runtime.handle_text(
            session_id, text, identity_key=identity_key, progress=progress
        )
    return runtime.handle_text(session_id, text, progress=progress)


def _handle_image(
    runtime: AgentSessionRuntime,
    session_id: str,
    image_path: Path,
    *,
    request: Request,
    progress: Callable[[str, str], None] | None = None,
) -> AgentResponse:
    identity_key = _identity_key(request)
    if identity_key:
        return runtime.handle_image(
            session_id, image_path, identity_key=identity_key, progress=progress
        )
    return runtime.handle_image(session_id, image_path, progress=progress)


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
) -> dict[str, object]:
    image_urls = []
    for image in response.images:
        path = Path(image)
        if not path.is_file():
            continue
        persisted = runtime.persist_media(session_id, path)
        if persisted is not None:
            image_urls.append(f"/api/media/{persisted.name}")
    uploaded_image_url = f"/api/upload/{uploaded_image.name}" if uploaded_image is not None else ""
    return {
        "text": response.text,
        "images": image_urls,
        "uploaded_image": uploaded_image_url,
        "intent": response.intent,
        "session": runtime.session_snapshot(session_id),
    }


def _validate_action_context(
    runtime: AgentSessionRuntime,
    session_id: str,
    raw_context: object,
) -> AgentResponse | None:
    """Reject a button action when it belongs to an older question/candidate list."""
    if raw_context is None:
        return None
    if not isinstance(raw_context, dict) or raw_context.get("type") != "select_candidate":
        return AgentResponse(text="这个操作已经失效，请使用当前页面中的操作。", intent="stale_action")
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
    return AgentResponse(text=message, intent="stale_candidate")


async def _stream_agent_events(
    execute: Callable[[Callable[[str, str], None]], dict[str, object]],
):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

    def progress(stage: str, message: str) -> None:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"type": "progress", "stage": stage, "message": message},
        )

    async def run() -> None:
        try:
            payload = await asyncio.to_thread(execute, progress)
            await queue.put({"type": "result", "data": payload})
        except (AgentRuntimeBusyError, AgentBudgetExceededError) as exc:
            await queue.put({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 - keep internal failures out of the public stream.
            logger.exception("streamed Agent request failed")
            await queue.put({"type": "error", "message": "服务端处理失败，请稍后重试。"})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    while True:
        event = await queue.get()
        if event is None:
            break
        yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    await task


async def _periodic_session_cleanup(cleaner: Callable[[], None], interval_seconds: float) -> None:
    interval = max(0.01, float(interval_seconds))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(cleaner)
        except Exception:  # noqa: BLE001 - cleanup failure must not stop the web service.
            logger.exception("periodic session cleanup failed")

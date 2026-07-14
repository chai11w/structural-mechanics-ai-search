"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import logging
import secrets
from pathlib import Path
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
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
logger = logging.getLogger(__name__)
_PAGE = (WEB_DIR / "index.html").read_text(encoding="utf-8")
_STYLE = (WEB_DIR / "demo.css").read_text(encoding="utf-8")
_SCRIPT = (WEB_DIR / "demo.js").read_text(encoding="utf-8")


def create_app(
    *,
    runtime: AgentSessionRuntime | None = None,
    incoming_dir: str | Path = INCOMING_DIR,
) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    runtime = runtime or AgentSessionRuntime(SQLiteSessionStore(DEFAULT_RUNTIME_DIR / "session.db"))
    app = FastAPI(title="结构力学搜题 Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.middleware("http")
    async def secure_public_requests(request: Request, call_next):
        if _forwarded_proto(request) == "http":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
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
        return result

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        session_id = _session_id(request)
        result = HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})
        _set_session_cookie(result, session_id, secure_cookie=_is_secure_request(request))
        return result

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request) -> JSONResponse:
        session_id = _session_id(request)
        path = runtime.current_image_path(session_id)
        result = JSONResponse({"uploaded_image": f"/api/upload/{path.name}" if path is not None else ""})
        _set_session_cookie(result, session_id, secure_cookie=_is_secure_request(request))
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
        session_id = _session_id(request)
        response = runtime.handle_text(session_id, text)
        return _agent_json(response, runtime, session_id, secure_cookie=_is_secure_request(request))

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
        session_id = _session_id(request)

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            response = runtime.handle_text(session_id, text, progress=progress)
            return _agent_payload(response, runtime, session_id)

        result = StreamingResponse(_stream_agent_events(execute), media_type="application/x-ndjson")
        _set_session_cookie(result, session_id, secure_cookie=_is_secure_request(request))
        return result

    @app.post("/api/reset")
    def reset(request: Request) -> JSONResponse:
        session_id = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        if session_id:
            runtime.clear(session_id)
        result = JSONResponse({"ok": True})
        result.delete_cookie(SESSION_COOKIE, secure=_is_secure_request(request), httponly=True, samesite="lax")
        return result

    @app.post("/api/image")
    async def image(request: Request) -> Response:
        content, filename, content_type = await _read_image_upload(request)
        session_id = _session_id(request)
        incoming = _write_incoming_image(
            content,
            filename,
            content_type,
            incoming_dir=incoming_dir,
        )
        try:
            response = runtime.handle_image(session_id, incoming)
            uploaded_image = runtime.current_image_path(session_id)
        finally:
            incoming.unlink(missing_ok=True)
        return _agent_json(
            response,
            runtime,
            session_id,
            uploaded_image=uploaded_image,
            secure_cookie=_is_secure_request(request),
        )

    @app.post("/api/image/stream")
    async def image_stream(request: Request) -> StreamingResponse:
        content, filename, content_type = await _read_image_upload(request)
        session_id = _session_id(request)
        incoming = _write_incoming_image(
            content,
            filename,
            content_type,
            incoming_dir=incoming_dir,
        )

        def execute(progress: Callable[[str, str], None]) -> dict[str, object]:
            try:
                response = runtime.handle_image(session_id, incoming, progress=progress)
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
        _set_session_cookie(result, session_id, secure_cookie=_is_secure_request(request))
        return result

    @app.get("/api/upload/{filename}")
    def get_upload(filename: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        path = runtime.resolve_upload(session_id, filename) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="upload not found")
        return FileResponse(path)

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str, request: Request) -> FileResponse:
        session_id = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        path = runtime.resolve_media(session_id, media_id) if session_id else None
        if path is None:
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(path)

    return app


def _session_id(request: Request) -> str:
    value = str(request.cookies.get(SESSION_COOKIE) or "").strip()
    return value or secrets.token_urlsafe(24)


def _forwarded_proto(request: Request) -> str:
    return str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()


def _is_secure_request(request: Request) -> bool:
    forwarded = _forwarded_proto(request)
    return forwarded == "https" or (not forwarded and request.url.scheme == "https")


def _set_session_cookie(response: Response, session_id: str, *, secure_cookie: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
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
) -> JSONResponse:
    result = JSONResponse(
        _agent_payload(
            response,
            runtime,
            session_id,
            uploaded_image=uploaded_image,
        )
    )
    _set_session_cookie(result, session_id, secure_cookie=secure_cookie)
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
    }


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

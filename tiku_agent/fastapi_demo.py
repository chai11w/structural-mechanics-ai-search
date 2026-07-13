"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
_PAGE = (WEB_DIR / "index.html").read_text(encoding="utf-8")
_STYLE = (WEB_DIR / "demo.css").read_text(encoding="utf-8")
_SCRIPT = (WEB_DIR / "demo.js").read_text(encoding="utf-8")


def create_app(*, runtime: AgentSessionRuntime | None = None) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    runtime = runtime or AgentSessionRuntime(SQLiteSessionStore(DEFAULT_RUNTIME_DIR / "session.db"))
    app = FastAPI(title="结构力学搜题 Agent", docs_url=None, redoc_url=None)
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session")
    def session(request: Request) -> dict[str, str]:
        session_id = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        path = runtime.current_image_path(session_id) if session_id else None
        return {"uploaded_image": f"/api/upload/{path.name}" if path is not None else ""}

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
        return _agent_json(response, runtime, session_id)

    @app.post("/api/reset")
    def reset(request: Request) -> JSONResponse:
        session_id = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        if session_id:
            runtime.clear(session_id)
        result = JSONResponse({"ok": True})
        result.delete_cookie(SESSION_COOKIE)
        return result

    @app.post("/api/image")
    async def image(request: Request) -> Response:
        content = await request.body()
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image is missing or too large")
        session_id = _session_id(request)
        incoming = _write_incoming_image(content, request.headers.get("x-filename", "question.jpg"))
        try:
            response = runtime.handle_image(session_id, incoming)
            uploaded_image = runtime.current_image_path(session_id)
        finally:
            incoming.unlink(missing_ok=True)
        return _agent_json(response, runtime, session_id, uploaded_image=uploaded_image)

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


def _write_incoming_image(content: bytes, filename: str) -> Path:
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".jpg"
    output = INCOMING_DIR / f"{uuid4().hex}{suffix}"
    output.write_bytes(content)
    try:
        with Image.open(output) as image:
            image.verify()
    except Exception as exc:  # noqa: BLE001 - external input boundary.
        output.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="invalid image") from exc
    return output


def _agent_json(
    response: AgentResponse,
    runtime: AgentSessionRuntime,
    session_id: str,
    *,
    uploaded_image: Path | None = None,
) -> JSONResponse:
    image_urls = []
    for image in response.images:
        path = Path(image)
        if not path.is_file():
            continue
        persisted = runtime.persist_media(session_id, path)
        if persisted is not None:
            image_urls.append(f"/api/media/{persisted.name}")
    uploaded_image_url = f"/api/upload/{uploaded_image.name}" if uploaded_image is not None else ""
    result = JSONResponse(
        {"text": response.text, "images": image_urls, "uploaded_image": uploaded_image_url, "intent": response.intent}
    )
    result.set_cookie(SESSION_COOKIE, session_id, max_age=2 * 60 * 60, httponly=True, samesite="lax")
    return result

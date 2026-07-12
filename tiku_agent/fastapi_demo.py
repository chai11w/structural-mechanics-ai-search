"""Local FastAPI demo for the isolated question-bank Agent (default port: 8790)."""

from __future__ import annotations

import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image

from tiku_agent.agent import AgentResponse
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.tools import DEFAULT_RUNTIME_DIR


SESSION_COOKIE = "tiku_agent_session"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
INCOMING_DIR = DEFAULT_RUNTIME_DIR / "incoming"


def create_app(*, runtime: AgentSessionRuntime | None = None) -> FastAPI:
    """Create a local-only demo app without any existing Feishu configuration."""
    runtime = runtime or AgentSessionRuntime(SQLiteSessionStore(DEFAULT_RUNTIME_DIR / "session.db"))
    media: dict[str, Path] = {}
    app = FastAPI(title="结构力学搜题 Agent", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/message")
    async def message(request: Request) -> Response:
        payload = await request.json()
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        session_id = _session_id(request)
        response = runtime.handle_text(session_id, text)
        return _agent_json(response, media, session_id)

    @app.post("/api/image")
    async def image(request: Request) -> Response:
        content = await request.body()
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image is missing or too large")
        session_id = _session_id(request)
        incoming = _write_incoming_image(content, request.headers.get("x-filename", "question.jpg"))
        try:
            response = runtime.handle_image(session_id, incoming)
        finally:
            incoming.unlink(missing_ok=True)
        return _agent_json(response, media, session_id)

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str) -> FileResponse:
        path = media.get(media_id)
        if path is None or not path.is_file():
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


def _agent_json(response: AgentResponse, media: dict[str, Path], session_id: str) -> JSONResponse:
    image_urls = []
    for image in response.images:
        path = Path(image)
        if not path.is_file():
            continue
        media_id = uuid4().hex
        media[media_id] = path
        image_urls.append(f"/api/media/{media_id}")
    result = JSONResponse({"text": response.text, "images": image_urls, "intent": response.intent})
    result.set_cookie(SESSION_COOKIE, session_id, max_age=2 * 60 * 60, httponly=True, samesite="lax")
    return result


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>结构力学搜题 Agent</title>
<style>
body{margin:0;background:#f5f6f8;color:#202124;font:16px system-ui,"Microsoft YaHei",sans-serif}main{max-width:720px;margin:auto;padding:28px 16px 110px}h1{font-size:20px;margin:0 0 22px}.bubble{background:white;border-radius:14px;padding:12px 15px;margin:10px 0;line-height:1.55;box-shadow:0 1px 3px #0000000b}.me{background:#dff3e4;margin-left:60px}.images{display:flex;gap:8px;flex-wrap:wrap}.images img{max-width:220px;max-height:240px;border-radius:10px;background:white}.bar{position:fixed;bottom:0;left:0;right:0;background:white;border-top:1px solid #e5e7eb;padding:12px}.inner{max-width:720px;margin:auto;display:flex;gap:8px}input{flex:1;padding:11px;border:1px solid #d1d5db;border-radius:9px;font:inherit}button{border:0;border-radius:9px;background:#267a44;color:white;padding:0 16px;font:inherit;cursor:pointer}label{display:inline-flex;align-items:center;padding:0 12px;border:1px solid #d1d5db;border-radius:9px;cursor:pointer}label input{display:none}</style>
 </head><body>
<main><h1>结构力学搜题 Agent</h1><div id="chat"><div class="bubble">发一张题图，或者直接告诉我你想做什么。</div></div></main>
<div class="bar"><div class="inner"><label>图片<input id="file" type="file" accept="image/*"></label><input id="text" placeholder="说点什么…"><button id="send">发送</button></div></div>
<script>
const chat=document.querySelector('#chat'), text=document.querySelector('#text'), file=document.querySelector('#file');
function add(message,me=false,images=[]){const box=document.createElement('div');box.className='bubble'+(me?' me':'');box.textContent=message;chat.append(box);if(images.length){const row=document.createElement('div');row.className='images';images.forEach(url=>{const img=document.createElement('img');img.src=url;row.append(img)});chat.append(row)}window.scrollTo(0,document.body.scrollHeight)}
async function sendText(){const value=text.value.trim();if(!value)return;add(value,true);text.value='';const r=await fetch('/api/message',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:value})});const d=await r.json();add(d.text,false,d.images||[])}
async function sendImage(){const selected=file.files[0];if(!selected)return;add('我发了一张题图。',true);file.value='';const r=await fetch('/api/image',{method:'POST',headers:{'x-filename':selected.name,'content-type':selected.type},body:selected});const d=await r.json();add(d.text,false,d.images||[])}
document.querySelector('#send').onclick=sendText;text.onkeydown=e=>{if(e.key==='Enter')sendText()};file.onchange=sendImage;
</script></body></html>"""

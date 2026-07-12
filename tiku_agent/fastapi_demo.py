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
    def index() -> HTMLResponse:
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})

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
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>结构力学搜题</title>
<style>
:root{font-family:Inter,"Microsoft YaHei",system-ui,sans-serif;color:#202123;background:#fff}*{box-sizing:border-box}body{margin:0;min-width:320px;background:#fff}.app{min-height:100vh;display:flex}.side{width:252px;background:#f7f7f8;border-right:1px solid #ececee;padding:15px 12px;display:flex;flex-direction:column;gap:20px}.brand{display:flex;align-items:center;gap:9px;padding:7px 9px;font-weight:600;font-size:15px;letter-spacing:-.2px}.brand-mark{width:25px;height:25px;display:grid;place-items:center;border-radius:7px;background:#202123;color:#fff;font-size:12px}.new-chat{height:40px;border:1px solid #dedee3;border-radius:9px;background:#fff;display:flex;align-items:center;gap:9px;padding:0 12px;color:#303035;font:inherit;font-size:14px;cursor:pointer}.new-chat:hover{background:#f2f2f3}.side-label{font-size:11px;color:#8a8a91;padding:0 10px;text-transform:uppercase;letter-spacing:.08em}.session-note{font-size:13px;line-height:1.55;color:#66666e;padding:0 10px}.side-footer{margin-top:auto;padding:10px;font-size:12px;color:#85858d;border-top:1px solid #e5e5e8}.main{flex:1;min-width:0;display:flex;flex-direction:column;background:#fff}.topbar{height:58px;display:flex;align-items:center;justify-content:space-between;padding:0 28px;border-bottom:1px solid #f1f1f2}.topbar-title{font-size:14px;font-weight:550;color:#303035}.status{display:flex;align-items:center;gap:7px;color:#777780;font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:#30a46c}.conversation{width:min(100%,840px);margin:0 auto;padding:28px 24px 150px;flex:1}.empty{min-height:calc(100vh - 250px);display:flex;align-items:center;justify-content:center;text-align:center}.empty-inner{max-width:470px}.empty-icon{width:44px;height:44px;margin:0 auto 18px;display:grid;place-items:center;border:1px solid #e5e5e8;border-radius:13px;color:#33343a}.empty h1{font-size:28px;letter-spacing:-.7px;line-height:1.2;margin:0 0 10px;font-weight:600}.empty p{margin:0;color:#777780;font-size:15px;line-height:1.65}.message{display:flex;gap:12px;margin:0 0 22px;align-items:flex-start}.avatar{width:28px;height:28px;border-radius:8px;flex:0 0 28px;display:grid;place-items:center;background:#202123;color:#fff;font-size:11px;font-weight:600}.message-body{min-width:0;max-width:76%;line-height:1.7;color:#29292e;white-space:pre-wrap}.message.user{justify-content:flex-end}.message.user .message-body{background:#f2f2f3;border-radius:16px;padding:10px 14px}.message.user .avatar{display:none}.images{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:3px 0 24px 40px}.images img{width:100%;max-height:310px;object-fit:contain;border:1px solid #e8e8eb;border-radius:11px;background:#fff}.composer-wrap{position:fixed;left:252px;right:0;bottom:0;padding:18px 24px 22px;background:linear-gradient(transparent,#fff 30%)}.composer{width:min(100%,800px);margin:auto;display:flex;align-items:flex-end;gap:8px;padding:8px 9px 8px 12px;border:1px solid #dbdbe0;border-radius:15px;background:#fff;box-shadow:0 7px 24px #0000000d}.attach{width:34px;height:34px;border:0;border-radius:9px;background:transparent;color:#66666e;display:grid;place-items:center;cursor:pointer}.attach:hover{background:#f0f0f2}.attach input{display:none}.composer textarea{flex:1;resize:none;border:0;outline:0;background:transparent;font:inherit;font-size:15px;line-height:1.45;min-height:34px;max-height:140px;padding:7px 2px;color:#242429}.composer textarea::placeholder{color:#9a9aa2}.send{width:34px;height:34px;border:0;border-radius:9px;background:#202123;color:#fff;display:grid;place-items:center;cursor:pointer}.send:disabled{background:#d8d8dc;cursor:default}.notice{font-size:12px;text-align:center;color:#9a9aa2;margin-top:8px}@media(max-width:720px){.side{display:none}.topbar{padding:0 18px}.conversation{padding:22px 16px 138px}.composer-wrap{left:0;padding:15px 12px 18px}.message-body{max-width:88%}.images{margin-left:0}.empty{min-height:calc(100vh - 220px)}.empty h1{font-size:25px}}</style></head>
<body><div class="app"><aside class="side"><div class="brand"><span class="brand-mark">力</span><span>结构力学搜题</span></div><button class="new-chat" id="new-chat"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 5v14M5 12h14"/></svg>新对话</button><div><div class="side-label">当前会话</div><p class="session-note">发题图后，我会帮你在题库里找最相似的题。</p></div><div class="side-footer">会话会在 2 小时后自动清理</div></aside>
<main class="main"><header class="topbar"><div class="topbar-title">结构力学题库</div><div class="status"><span class="dot"></span>Agent 已就绪</div></header><section class="conversation"><div class="empty" id="empty"><div class="empty-inner"><div class="empty-icon"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 19.5V4.75A1.75 1.75 0 0 1 5.75 3h12.5A1.75 1.75 0 0 1 20 4.75V20l-4-2.5-4 2.5-4-2.5-4 2.5Z"/></svg></div><h1>今天想查哪道题？</h1><p>上传结构力学题图，或直接告诉我你想继续查哪一题。</p></div></div><div id="chat"></div></section></main></div>
<div class="composer-wrap"><form class="composer" id="composer"><label class="attach" title="上传题图"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 16V4m0 0L8 8m4-4 4 4M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/></svg><input id="file" type="file" accept="image/*"></label><textarea id="text" rows="1" placeholder="发题图，或说点什么…"></textarea><button class="send" id="send" title="发送" type="submit"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="m5 12 14-7-4 14-3-5-5-2Z"/><path d="m12 14 3-3"/></svg></button></form><div class="notice">题图与会话将在 2 小时后自动清理</div></div>
<script>
const chat=document.querySelector('#chat'),empty=document.querySelector('#empty'),text=document.querySelector('#text'),file=document.querySelector('#file'),form=document.querySelector('#composer'),send=document.querySelector('#send');
function add(message,me=false,images=[]){empty.hidden=true;const row=document.createElement('article');row.className='message'+(me?' user':'');if(!me){const avatar=document.createElement('div');avatar.className='avatar';avatar.textContent='力';row.append(avatar)}const body=document.createElement('div');body.className='message-body';body.textContent=message;row.append(body);chat.append(row);if(images.length){const grid=document.createElement('div');grid.className='images';images.forEach(url=>{const img=document.createElement('img');img.src=url;img.alt='题库候选题';grid.append(img)});chat.append(grid)}window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})}
function busy(value){send.disabled=value;text.disabled=value;file.disabled=value}
async function request(url,options){const response=await fetch(url,options);const data=await response.json();if(!response.ok)throw new Error(data.detail||'暂时无法处理，请再试一次。');return data}
async function sendText(){const value=text.value.trim();if(!value)return;add(value,true);text.value='';busy(true);try{const data=await request('/api/message',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:value})});add(data.text,false,data.images||[])}catch(error){add(error.message||'暂时无法处理，请再试一次。')}finally{busy(false);text.focus()}}
async function sendImage(){const selected=file.files[0];if(!selected)return;add('我发了一张题图。',true);file.value='';busy(true);try{const data=await request('/api/image',{method:'POST',headers:{'x-filename':selected.name,'content-type':selected.type},body:selected});add(data.text,false,data.images||[])}catch(error){add(error.message||'图片暂时无法处理，请再试一次。')}finally{busy(false);text.focus()}}
form.addEventListener('submit',event=>{event.preventDefault();sendText()});text.addEventListener('input',()=>{text.style.height='auto';text.style.height=Math.min(text.scrollHeight,140)+'px'});file.addEventListener('change',sendImage);document.querySelector('#new-chat').addEventListener('click',async()=>{await fetch('/api/reset',{method:'POST'});chat.replaceChildren();empty.hidden=false;text.focus()});
</script></body></html>"""

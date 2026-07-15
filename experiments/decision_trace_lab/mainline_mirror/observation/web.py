from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from mainline_mirror.integrity import activate_verified_source
from .core import HookManager, ObservedAgent, ObservedToolbox
from .storage import ObservationStore, trace_key


LAB_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = LAB_ROOT / "runtime" / "mainline_web"
DEFAULT_DATA = LAB_ROOT / "data" / "mainline_observed"
OBSERVER_WEB = Path(__file__).with_name("web_static")
EXTERNAL_COOKIE = "decision_trace_mainline_session"
INTERNAL_COOKIE = "tiku_agent_session"
INJECT_MARKER_START = "<!-- decision-trace-observer:start -->"
INJECT_MARKER_END = "<!-- decision-trace-observer:end -->"


def create_observed_app(
    *,
    runtime: Any | None = None,
    store: ObservationStore | None = None,
    runtime_root: str | Path = DEFAULT_RUNTIME,
    agent_factory: Callable[[Any], Any] | None = None,
) -> Any:
    """Create 8793 from the verified mainline App, adding only a sidecar."""

    manifest = activate_verified_source()
    from tiku_agent.agent import AgentToolbox, TikuSearchAgent
    from tiku_agent.fastapi_demo import create_app as create_mainline_app
    from tiku_agent.session_artifacts import SessionArtifacts
    from tiku_agent.session_runtime import AgentSessionRuntime
    from tiku_agent.session_store import SQLiteSessionStore
    from tiku_agent.task_log import JsonlTaskLogger
    from tiku_agent.tools import AgentToolConfig

    root = Path(runtime_root).resolve()
    store = store or ObservationStore(DEFAULT_DATA)
    hooks = HookManager()
    hooks.install()

    if runtime is None:
        def observed_factory(state: Any) -> Any:
            if agent_factory is not None:
                base = agent_factory(state)
            else:
                config = AgentToolConfig(
                    runtime_dir=root,
                    session_dir=root / "sessions" / state.session_id,
                )
                base = TikuSearchAgent(state=state, tools=ObservedToolbox(AgentToolbox()), config=config)
            if not isinstance(getattr(base, "tools", None), ObservedToolbox):
                base.tools = ObservedToolbox(base.tools)
            return ObservedAgent(base, store)

        runtime = AgentSessionRuntime(
            SQLiteSessionStore(root / "session.db"),
            artifacts=SessionArtifacts(root / "sessions"),
            task_logger=JsonlTaskLogger(root / "task_logs.jsonl"),
            agent_factory=observed_factory,
        )

    app = create_mainline_app(runtime=runtime, incoming_dir=root / "incoming")
    app.state.mainline_manifest = manifest
    app.state.observation_store = store
    app.state.hook_manager = hooks
    app.mount("/observer-assets", StaticFiles(directory=OBSERVER_WEB), name="observer-assets")

    @app.get("/api/observation/source")
    def source_info() -> dict[str, Any]:
        return {
            "source_branch": manifest["source_branch"],
            "source_commit": manifest["source_commit"],
            "verified_files": len(manifest["files"]),
        }

    @app.get("/api/observation/turns")
    def turns(request: Request) -> dict[str, Any]:
        session_id = str(request.cookies.get(INTERNAL_COOKIE) or "").strip()
        return {"turns": store.turns(trace_key(session_id)) if session_id else []}

    @app.get("/api/observation/turns/{turn_id}")
    def turn(turn_id: str, request: Request) -> dict[str, Any]:
        session_id = str(request.cookies.get(INTERNAL_COOKIE) or "").strip()
        events = store.events(trace_id=trace_key(session_id), turn_id=turn_id) if session_id else []
        if not events:
            raise HTTPException(status_code=404, detail="turn not found")
        event_ids = {str(event.get("event_id") or "") for event in events}
        return {
            "turn_id": turn_id,
            "events": events,
            "latest_labels": store.latest_labels(event_ids),
            "issues": store.scan(trace_key(session_id)),
        }

    @app.post("/api/observation/labels")
    async def label(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            row = store.append_label(dict(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(row)

    @app.get("/api/observation/summary")
    def summary(request: Request) -> dict[str, Any]:
        session_id = str(request.cookies.get(INTERNAL_COOKIE) or "").strip()
        return store.summary(trace_key(session_id)) if session_id else store.summary("missing")

    @app.get("/api/observation/scan")
    def scan(request: Request) -> dict[str, Any]:
        session_id = str(request.cookies.get(INTERNAL_COOKIE) or "").strip()
        issues = store.scan(trace_key(session_id)) if session_id else []
        return {"issue_count": len(issues), "issues": issues}

    app.add_middleware(ObserverWebMiddleware)
    return app


class ObserverWebMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        _translate_request_cookie(request)
        response = await call_next(request)
        _translate_response_cookie(response)
        if request.method == "GET" and request.url.path == "/" and response.status_code == 200:
            body = b"".join([chunk async for chunk in response.body_iterator])
            html = body.decode("utf-8")
            injected = html.replace("</body>", _observer_markup() + "</body>")
            headers = dict(response.headers)
            headers.pop("content-length", None)
            response = Response(
                injected,
                status_code=response.status_code,
                headers=headers,
                media_type="text/html",
                background=response.background,
            )
            _translate_response_cookie(response)
        return response


def _translate_request_cookie(request: Request) -> None:
    headers = list(request.scope.get("headers") or [])
    cookie_headers = [value for key, value in headers if key.lower() == b"cookie"]
    if not cookie_headers:
        request.__dict__.pop("_cookies", None)
        return

    external_name = EXTERNAL_COOKIE.encode()
    internal_name = INTERNAL_COOKIE.encode()
    external_value: bytes | None = None
    passthrough: list[bytes] = []
    for value in cookie_headers:
        for raw_part in value.split(b";"):
            part = raw_part.strip()
            if not part:
                continue
            name, separator, cookie_value = part.partition(b"=")
            normalized_name = name.strip()
            if separator and normalized_name == external_name:
                external_value = cookie_value.strip()
            elif separator and normalized_name == internal_name:
                # A legacy mainline cookie must never compete with the isolated
                # observer session after external -> internal translation.
                continue
            else:
                passthrough.append(part)

    if external_value is not None:
        passthrough.append(internal_name + b"=" + external_value)

    translated = [(key, value) for key, value in headers if key.lower() != b"cookie"]
    if passthrough:
        translated.append((b"cookie", b"; ".join(passthrough)))
    request.scope["headers"] = translated
    request.__dict__.pop("_cookies", None)


def _translate_response_cookie(response: Response) -> None:
    response.raw_headers = [
        (key, value.replace(INTERNAL_COOKIE.encode() + b"=", EXTERNAL_COOKIE.encode() + b"="))
        if key.lower() == b"set-cookie" else (key, value)
        for key, value in response.raw_headers
    ]


def _observer_markup() -> str:
    return f"""{INJECT_MARKER_START}
<button id="observer-toggle" type="button" aria-controls="observer-panel" aria-expanded="true">评审轨迹</button>
<aside id="observer-panel" aria-label="决策轨迹评审侧栏">
  <header><strong>主线决策轨迹</strong><small id="observer-source">正在校验镜像…</small></header>
  <p class="observer-guide">只标你想核对的关键项，不需要逐条评分；未标记不代表正确或错误。</p>
  <p class="observer-count-note">每个回合的轨迹数量不固定，取决于本轮调用了多少工具、发生了多少次状态变化。</p>
  <div id="observer-alerts" role="alert"></div>
  <section><h2>人工复核队列</h2><p id="observer-review-count">待复核 0 · 已复核 0 · 共 0 个关键项</p><div id="observer-review-items"></div></section>
  <details id="observer-technical"><summary>技术详情（完整机器轨迹）</summary><p id="observer-event-count">事件 0 条</p><div id="observer-events"></div></details>
</aside>
<link rel="stylesheet" href="/observer-assets/observer.css?v=20260715-review-state-2">
<script src="/observer-assets/observer.js?v=20260715-review-state-2" defer></script>
{INJECT_MARKER_END}
"""


def strip_observer_markup(html: str) -> str:
    start = html.find(INJECT_MARKER_START)
    end = html.find(INJECT_MARKER_END)
    if start < 0 or end < 0:
        return html
    end += len(INJECT_MARKER_END)
    if html[end:end + 1] == "\n":
        end += 1
    return html[:start] + html[end:]

"""FastAPI application for the isolated 8795 administration console."""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import asynccontextmanager
import json
from pathlib import Path
from threading import Lock
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from tiku_admin.auth import AdminSession, AdminSessionAuth
from tiku_admin.control_store import SQLiteControlStore, cny_to_micros, micros_to_cny
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import (
    FEEDBACK_SCOPES,
    SQLiteFeedbackStore,
    scope_feedback_conversation,
)


WEB_DIR = Path(__file__).with_name("web")
MAX_ADMIN_REQUEST_BYTES = 128 * 1024


class _LoginLimiter:
    def __init__(self, *, attempts: int = 5, window_seconds: int = 600) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        cutoff = time.monotonic() - self.window_seconds
        with self._lock:
            queue = self._entries[str(key)]
            while queue and queue[0] < cutoff:
                queue.popleft()
            return len(queue) < self.attempts

    def failure(self, key: str) -> None:
        with self._lock:
            self._entries[str(key)].append(time.monotonic())

    def success(self, key: str) -> None:
        with self._lock:
            self._entries.pop(str(key), None)


def create_admin_app(
    *,
    control_store: SQLiteControlStore,
    reporter: AdminReporter,
    feedback_store: SQLiteFeedbackStore,
    allow_local_setup: bool = True,
) -> FastAPI:
    auth = AdminSessionAuth(control_store)
    limiter = _LoginLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        feedback_store.purge_expired_cases()
        yield

    app = FastAPI(
        title="力答管理后台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="admin-assets")

    @app.middleware("http")
    async def protect_admin(request: Request, call_next):
        if _forwarded_proto(request) == "http":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        session = auth.verify_cookie(str(request.cookies.get(auth.cookie_name) or ""))
        request.state.admin_session = session
        path = request.url.path
        public_path = (
            path == "/health"
            or path == "/"
            or path in {"/login", "/setup"}
            or path in {"/api/admin/session", "/api/admin/login", "/api/admin/setup"}
            or path.startswith("/assets/")
        )
        if session is None and not public_path:
            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "管理员登录已失效，请重新登录。"}, status_code=401
                )
            return RedirectResponse("/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and path not in {
            "/api/admin/login", "/api/admin/setup"
        }:
            if not auth.verify_csrf(session, request.headers.get("x-csrf-token", "")):
                return JSONResponse({"detail": "安全校验失败，请刷新后重试。"}, status_code=403)
        result = await call_next(request)
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
        )
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["X-Frame-Options"] = "DENY"
        result.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        result.headers.setdefault("Cache-Control", "private, no-store")
        return result

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/admin/session")
    def session(request: Request) -> dict[str, object]:
        current = _session(request)
        return {
            "authenticated": current is not None,
            "setup_required": not control_store.has_admin(),
            "local_setup_allowed": allow_local_setup and _is_local_request(request),
            "csrf_token": current.csrf_token if current else "",
        }

    @app.post("/api/admin/setup")
    async def setup(request: Request) -> Response:
        if not allow_local_setup or not _is_local_request(request):
            raise HTTPException(status_code=403, detail="管理员只能在本机初始化。")
        if control_store.has_admin():
            raise HTTPException(status_code=409, detail="管理员已经初始化。")
        payload = await _json_body(request, max_bytes=4096)
        password = str(payload.get("password") or "")
        if password != str(payload.get("confirm_password") or ""):
            raise HTTPException(status_code=400, detail="两次输入的密码不一致。")
        try:
            control_store.initialize_admin(password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _login_response(auth, request)

    @app.post("/api/admin/login")
    async def login(request: Request) -> Response:
        client_key = _client_key(request)
        if not limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="登录尝试过多，请稍后再试。")
        payload = await _json_body(request, max_bytes=4096)
        if not control_store.verify_admin_password(str(payload.get("password") or "")):
            limiter.failure(client_key)
            raise HTTPException(status_code=401, detail="管理员密码错误。")
        limiter.success(client_key)
        return _login_response(auth, request)

    @app.post("/api/admin/logout")
    def logout(request: Request) -> Response:
        result = JSONResponse({"ok": True})
        result.delete_cookie(
            auth.cookie_name,
            secure=_is_secure_request(request),
            httponly=True,
            samesite="strict",
        )
        return result

    @app.get("/api/admin/overview")
    def overview() -> dict[str, object]:
        return reporter.overview()

    @app.get("/api/admin/invitations")
    def invitations(include_archived: bool = False) -> dict[str, object]:
        return {"items": reporter.invitation_rows(include_archived=include_archived)}

    @app.post("/api/admin/invitations")
    async def create_invitation(request: Request) -> dict[str, object]:
        payload = await _json_body(request)
        budget_value = payload.get("daily_budget_cny")
        try:
            invitation, code = control_store.create_invitation(
                label=str(payload.get("label") or ""),
                daily_budget_micros=(
                    cny_to_micros(budget_value) if _has_value(budget_value) else None
                ),
                expires_at=str(payload.get("expires_at") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"invitation": invitation.to_dict(), "code": code}

    @app.patch("/api/admin/invitations/{invite_id}")
    async def update_invitation(invite_id: str, request: Request) -> dict[str, object]:
        payload = await _json_body(request)
        kwargs: dict[str, object] = {}
        if "label" in payload:
            kwargs["label"] = str(payload.get("label") or "")
        if "daily_budget_cny" in payload:
            value = payload.get("daily_budget_cny")
            kwargs["daily_budget_micros"] = (
                cny_to_micros(value) if _has_value(value) else None
            )
        if "expires_at" in payload:
            kwargs["expires_at"] = str(payload.get("expires_at") or "")
        try:
            item = control_store.update_invitation(invite_id, **kwargs)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="邀请码不存在。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"invitation": item.to_dict()}

    @app.post("/api/admin/invitations/{invite_id}/status")
    async def invitation_status(invite_id: str, request: Request) -> dict[str, object]:
        payload = await _json_body(request)
        try:
            item = control_store.set_invitation_status(
                invite_id, str(payload.get("status") or "")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="邀请码不存在。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"invitation": item.to_dict()}

    @app.delete("/api/admin/invitations/{invite_id}")
    def delete_invitation(invite_id: str) -> dict[str, object]:
        invitation = control_store.get_invitation(invite_id)
        if invitation is None:
            raise HTTPException(status_code=404, detail="邀请码不存在。")
        if invitation.status != "archived":
            raise HTTPException(status_code=409, detail="请先归档邀请码，再永久删除。")
        blockers = reporter.invitation_delete_blockers(invite_id)
        if blockers["cost_runs"] or blockers["feedback"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "该邀请码已有费用或反馈历史，不能永久删除；可继续保留为已归档。"
                ),
            )
        control_store.delete_archived_invitation(invite_id)
        return {"deleted": True}

    @app.post("/api/admin/invitations/{invite_id}/reset")
    def reset_invitation(invite_id: str) -> dict[str, object]:
        try:
            invitation, code = control_store.reset_invitation_code(invite_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="邀请码不存在。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"invitation": invitation.to_dict(), "code": code}

    @app.post("/api/admin/invitations/{invite_id}/reveal")
    def reveal_invitation(invite_id: str) -> Response:
        try:
            code = control_store.reveal_invitation_code(invite_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="邀请码不存在。") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="邀请码解密失败，请检查后台密钥。") from exc
        if code is None:
            raise HTTPException(
                status_code=409,
                detail="旧邀请码未保存可恢复明文，请重置后再复制。",
            )
        return JSONResponse(
            {"code": code},
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/admin/feedback")
    def feedback_list(
        rating: str = "",
        feedback_scope: str = "",
        identity_key: str = "",
        identity_status: str = "",
        chapter: str = "",
        review_status: str = "",
        include_archived: bool = False,
        date: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        if identity_status and identity_status != "archived":
            raise HTTPException(status_code=400, detail="用户分组筛选无效。")
        if feedback_scope and feedback_scope not in FEEDBACK_SCOPES:
            raise HTTPException(status_code=400, detail="反馈范围筛选无效。")
        try:
            return reporter.feedback_list(
                rating=rating,
                feedback_scope=feedback_scope,
                identity_key=identity_key,
                identity_status=identity_status,
                chapter=chapter,
                review_status=review_status,
                include_archived=include_archived,
                date=date,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="反馈日期格式无效。") from exc

    @app.get("/api/admin/feedback/{feedback_id}")
    def feedback_detail(feedback_id: str) -> dict[str, object]:
        item = feedback_store.get_feedback(feedback_id)
        if item is None:
            raise HTTPException(status_code=404, detail="反馈不存在。")
        if item.archived_at:
            raise HTTPException(status_code=409, detail="请先取消归档反馈，再查看详情。")
        result = reporter.feedback_detail(feedback_id)
        if result is None:
            raise HTTPException(status_code=404, detail="反馈不存在。")
        return result

    @app.patch("/api/admin/feedback/{feedback_id}/review")
    async def review_feedback(feedback_id: str, request: Request) -> dict[str, object]:
        payload = await _json_body(request)
        try:
            item = feedback_store.update_review(
                feedback_id,
                review_status=str(payload.get("review_status") or ""),
                admin_note=str(payload.get("admin_note") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="反馈不存在。") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"feedback": item.to_dict()}

    @app.post("/api/admin/feedback/{feedback_id}/archive")
    def archive_feedback(feedback_id: str) -> dict[str, object]:
        try:
            item = feedback_store.set_archived(feedback_id, archived=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="反馈不存在。") from exc
        return {"feedback": item.to_dict()}

    @app.post("/api/admin/feedback/{feedback_id}/restore")
    def restore_feedback(feedback_id: str) -> dict[str, object]:
        try:
            item = feedback_store.set_archived(feedback_id, archived=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="反馈不存在。") from exc
        return {"feedback": item.to_dict()}

    @app.delete("/api/admin/feedback/{feedback_id}")
    def delete_archived_feedback(feedback_id: str) -> dict[str, object]:
        try:
            removed = feedback_store.delete_archived(feedback_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="请先归档反馈，再永久删除。") from exc
        if not removed:
            raise HTTPException(status_code=404, detail="反馈不存在。")
        return {"deleted": True}

    @app.get("/api/admin/feedback/{feedback_id}/media/{media_name}")
    def feedback_media(feedback_id: str, media_name: str) -> FileResponse:
        item = feedback_store.get_feedback(feedback_id)
        if item is None:
            raise HTTPException(status_code=404, detail="反馈图片不存在或已过期。")
        if item.archived_at:
            raise HTTPException(status_code=409, detail="请先取消归档反馈，再查看详情。")
        conversation = scope_feedback_conversation(item.conversation, item.message_id)
        allowed_media = {
            str(name)
            for message in conversation
            for name in (
                list(message.get("images", []))
                + ([message.get("a3_overlay")] if message.get("a3_overlay") else [])
            )
            if str(name or "").strip()
        }
        if media_name not in allowed_media:
            raise HTTPException(status_code=404, detail="反馈图片不存在或已过期。")
        path = feedback_store.resolve_case_media(feedback_id, media_name)
        if path is None:
            raise HTTPException(status_code=404, detail="反馈图片不存在或已过期。")
        return FileResponse(path, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/admin/settings")
    def settings(audit_limit: int = 10, audit_offset: int = 0) -> dict[str, object]:
        values = control_store.settings()
        safe_limit = min(50, max(1, int(audit_limit)))
        safe_offset = max(0, int(audit_offset))
        return {
            **values,
            "global_daily_budget_cny": micros_to_cny(int(values["global_daily_budget_micros"])),
            "default_invite_daily_budget_cny": micros_to_cny(
                int(values["default_invite_daily_budget_micros"])
            ),
            "audit": control_store.list_audit(limit=safe_limit, offset=safe_offset),
            "audit_total": control_store.count_audit(),
            "audit_limit": safe_limit,
            "audit_offset": safe_offset,
        }

    @app.patch("/api/admin/settings")
    async def update_settings(request: Request) -> dict[str, object]:
        payload = await _json_body(request)
        try:
            values = control_store.update_settings(
                global_daily_budget_micros=cny_to_micros(
                    payload.get("global_daily_budget_cny")
                ),
                default_invite_daily_budget_micros=cny_to_micros(
                    payload.get("default_invite_daily_budget_cny")
                ),
                feedback_retention_days=int(payload.get("feedback_retention_days") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"settings": values}

    @app.patch("/api/admin/password")
    async def change_password(request: Request) -> Response:
        payload = await _json_body(request, max_bytes=4096)
        if not control_store.verify_admin_password(str(payload.get("current_password") or "")):
            raise HTTPException(status_code=401, detail="当前密码错误。")
        password = str(payload.get("new_password") or "")
        if password != str(payload.get("confirm_password") or ""):
            raise HTTPException(status_code=400, detail="两次输入的新密码不一致。")
        try:
            control_store.change_admin_password(password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _login_response(auth, request)

    @app.get("/")
    def root(request: Request) -> Response:
        if not control_store.has_admin() and allow_local_setup and _is_local_request(request):
            return RedirectResponse("/setup", status_code=303)
        if _session(request) is None:
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/overview", status_code=303)

    @app.get("/{page:path}", response_class=HTMLResponse)
    def pages(page: str) -> Response:
        clean = str(page).strip("/")
        if clean in {"login", "setup", "overview", "invitations", "feedback", "settings"} or clean.startswith("feedback/"):
            return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="page not found")

    return app


async def _json_body(request: Request, *, max_bytes: int = MAX_ADMIN_REQUEST_BYTES) -> dict[str, object]:
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid content length") from exc
    if content_length > max_bytes:
        raise HTTPException(status_code=413, detail="request is too large")
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="request is too large")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json object is required")
    return payload


def _login_response(auth: AdminSessionAuth, request: Request) -> JSONResponse:
    result = JSONResponse({"ok": True})
    result.set_cookie(
        auth.cookie_name,
        auth.issue_cookie(),
        max_age=auth.max_age_seconds,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="strict",
    )
    return result


def _session(request: Request) -> AdminSession | None:
    value = getattr(request.state, "admin_session", None)
    return value if isinstance(value, AdminSession) else None


def _client_key(request: Request) -> str:
    forwarded = str(request.headers.get("cf-connecting-ip") or "").strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _has_value(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def _is_local_request(request: Request) -> bool:
    if any(request.headers.get(name) for name in ("forwarded", "x-forwarded-for", "cf-connecting-ip")):
        return False
    return bool(request.client and request.client.host in {"127.0.0.1", "::1", "testclient"})


def _forwarded_proto(request: Request) -> str:
    return str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()


def _is_secure_request(request: Request) -> bool:
    forwarded = _forwarded_proto(request)
    return forwarded == "https" or (not forwarded and request.url.scheme == "https")

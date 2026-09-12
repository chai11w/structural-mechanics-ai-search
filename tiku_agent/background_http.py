"""Version 1 submit/discover/observe protocol for the detached execution kernel."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from tiku_agent.background_auth import BackgroundInviteAccess
from tiku_agent.background_publication import BackgroundPublication
from tiku_agent.background_trace import BackgroundTrace
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import OPERATION_HEADER
from tiku_agent.execution_store import ExecutionError, digest, session_key
from tiku_agent.execution_worker import BackgroundWorker
from tiku_agent.task_state_runtime import TaskStateEntryCapabilities
from tiku_shared.trace_context import trace_context_scope
from tiku_shared.trace_events import current_trace_event_session, record_trace_event, record_public_terminal, trace_event_session_scope


PROTOCOL_HEADER = "X-Tiku-Background"
LEGACY_BUSINESS_PATHS = frozenset({"/api/image", "/api/image/stream", "/api/message", "/api/message/stream",
                                 "/api/a3/select", "/api/a3/select/stream", "/api/a3/prepare/stream", "/api/a3/crop/stream"})


class BackgroundHTTP:
    def __init__(self, runtime, access, responses, feedback, *, session_cookie, trace_recorder=None):
        if (not isinstance(access, BackgroundInviteAccess)
                or session_cookie in {"tiku_agent_session", "tiku_agent_invite", "tiku_admin_session", access.cookie_name}):
            raise ValueError("background mode requires isolated login and session cookies")
        self.dispatch = runtime.execution_dispatch
        self.store = self.dispatch.store
        self.access, self.session_cookie, self.trace_recorder = access, session_cookie, trace_recorder
        root = self.store.path.parent.resolve()
        paths = [responses.path, feedback.path, feedback.cases_root, access.store.path]
        for engine in (runtime, getattr(runtime, "a2_runtime", runtime)):
            paths.append(engine.artifacts.root)
            if getattr(engine, "cost_ledger", None) is not None:
                paths.append(engine.cost_ledger.path)
            checkpoint = getattr(engine, "checkpoint_recorder", None)
            if checkpoint is not None:
                paths.extend([checkpoint.store.path, checkpoint.store.artifact_root])
        if trace_recorder is not None:
            paths.append(trace_recorder.store.path)
        for path in paths:
            path = Path(path).absolute()
            if (not path.resolve().is_relative_to(root) or path.resolve() != path
                    or any(parent.is_symlink() for parent in [path, *path.parents])):
                raise ValueError("background stores and media must belong to the isolated runtime")
        self.dispatch.authorize = lambda identity, version: access.store.active_invitation(identity, version) is not None
        getattr(runtime, "a2_runtime", runtime)._budget_policy = access.store
        self.publication = BackgroundPublication(self.dispatch, responses)
        self.publication.trace = BackgroundTrace(self.dispatch, trace_recorder)
        self.worker = BackgroundWorker(self.dispatch, root / "background-inputs", publication=self.publication,
            trace_recorder=trace_recorder, capabilities=TaskStateEntryCapabilities(trusted_image_event=True, reset_session_available=True))
        self._subscriptions = {}
        self._subscriptions_lock = threading.Lock()
        self.max_subscriptions, self.max_session_subscriptions = 64, 2
        self.poll_seconds, self.stream_seconds = 0.25, 30.0
        self.closing = False
        self.drain_result = None
        self.feedback_exports = root / "background-feedback-exports"
        self.feedback_exports.mkdir(exist_ok=True)
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS execution_http_logins (id TEXT PRIMARY KEY,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)")
            conn.execute("CREATE TABLE IF NOT EXISTS execution_http_bindings (login_id TEXT NOT NULL REFERENCES execution_http_logins(id), session TEXT NOT NULL,grant_id TEXT NOT NULL REFERENCES execution_dispatch_grants(id), PRIMARY KEY(login_id,session))")
        self.access.register_session = self.register_login

    def register_login(self, login_id, expires):
        # Apply the same capacity bound before issuing any login cookie, even
        # when the caller never binds a business session or immediately logs out.
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            conn.execute("DELETE FROM execution_http_bindings WHERE login_id IN (SELECT id FROM execution_http_logins WHERE expires<=?)", (now,))
            conn.execute("DELETE FROM execution_http_logins WHERE expires<=?", (now,))
            if conn.execute("SELECT count(*) FROM execution_http_logins").fetchone()[0] >= self.dispatch.policy.max_grants:
                raise ExecutionError("EXECUTION_CAPACITY")
            conn.execute("INSERT INTO execution_http_logins VALUES (?,?,0)", (login_id, expires))

    def start(self):
        self.publication.start()
        self.worker.start()

    def close(self):
        self.closing = True
        self.drain_result = self.worker.close(drain_seconds=5)
        self.drain_result["publisher_drained"] = self.publication.close(timeout=5)
        return self.drain_result

    def login(self, request):
        login = self.access.verify_session(str(request.cookies.get(self.access.cookie_name) or ""))
        if login is None:
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        with self.store.reading() as conn:
            row = conn.execute("SELECT revoked FROM execution_http_logins WHERE id=?", (login.login_id,)).fetchone()
            if row is None or row[0]:
                raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        return login

    def authenticate(self, request):
        """Called by the common middleware before every protected legacy route."""
        path = request.url.path
        if path in {"/health", "/invite", "/api/invite/login"} or path.startswith("/assets/"):
            return None
        try:
            if request.method in {"POST", "DELETE"}:
                self.require_origin(request)
            self.login(request)
        except Exception:
            return JSONResponse({"code": "EXECUTION_AUTH_REQUIRED", "message": "请重新登录。"}, status_code=401)
        if request.method == "POST" and path in LEGACY_BUSINESS_PATHS:
            return JSONResponse({"code": "BACKGROUND_PROTOCOL_REQUIRED", "message": "此服务使用后台任务协议，请更新页面后提交。", "protocol_version": 1}, status_code=409)
        return None

    def logout(self, request):
        login = self.access.verify_session(str(request.cookies.get(self.access.cookie_name) or ""))
        if login is None:
            return
        with self.store.transaction() as conn:
            conn.execute("UPDATE execution_http_logins SET revoked=1 WHERE id=?", (login.login_id,))
            conn.execute("UPDATE execution_dispatch_grants SET revoked=1 WHERE id IN (SELECT grant_id FROM execution_http_bindings WHERE login_id=?)", (login.login_id,))

    @staticmethod
    def require_origin(request):
        if request.headers.get("sec-fetch-site", "").lower() in {"cross-site", "same-site"}:
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        origin = request.headers.get("origin")
        scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme
        if origin and origin != f"{scheme}://{request.headers.get('host')}":
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")

    @staticmethod
    def require_protocol(request, *, task=False):
        if request.headers.get(PROTOCOL_HEADER) != "1":
            raise ExecutionError("BACKGROUND_PROTOCOL_REQUIRED")
        BackgroundHTTP.require_origin(request)
        if task:
            from tiku_agent.fastapi_demo import _parse_session_coordination_headers
            coordination, error = _parse_session_coordination_headers(request, task_request=True)
            if error or coordination is None or not coordination.versioned:
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")

    def bind_session(self, request):
        login = self.login(request)
        sid = str(request.cookies.get(self.session_cookie) or uuid4().hex)
        context = self.store.context(sid)
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            self.dispatch.operations.verify_owner(sid, login.identity.invite_id, claim=True)
            conn.execute("DELETE FROM execution_http_bindings WHERE login_id IN (SELECT id FROM execution_http_logins WHERE expires<=?)", (now,))
            conn.execute("DELETE FROM execution_http_logins WHERE expires<=?", (now,))
            if conn.execute("SELECT count(*) FROM execution_http_logins").fetchone()[0] >= self.dispatch.policy.max_grants:
                if not conn.execute("SELECT 1 FROM execution_http_logins WHERE id=?", (login.login_id,)).fetchone():
                    raise ExecutionError("EXECUTION_CAPACITY")
            conn.execute("INSERT OR IGNORE INTO execution_http_logins VALUES (?,?,0)", (login.login_id, login.expires_at))
            row = conn.execute("SELECT grant_id FROM execution_http_bindings WHERE login_id=? AND session=?", (login.login_id, session_key(sid))).fetchone()
            if row is None:
                grant = self.dispatch.create_grant(sid, login.identity.invite_id, auth_version=login.identity.auth_version, expires_at=login.expires_at)
                conn.execute("INSERT INTO execution_http_bindings VALUES (?,?,?)", (login.login_id, session_key(sid), grant))
        return sid, context

    def credentials(self, request):
        login = self.login(request)
        sid = str(request.cookies.get(self.session_cookie) or "")
        if not sid:
            raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        with self.store.reading() as conn:
            row = conn.execute("SELECT grant_id FROM execution_http_bindings WHERE login_id=? AND session=?", (login.login_id, session_key(sid))).fetchone()
            if row is None:
                raise ExecutionError("EXECUTION_AUTH_REQUIRED")
        return sid, login.identity.invite_id, row[0]

    def view(self, credentials, *, operation_id=None, key=None, epoch=None):
        sid, identity, grant = credentials
        with self.store.reading() as conn:
            if operation_id is not None:
                if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                    raise ExecutionError("EXECUTION_NOT_FOUND")
                row = conn.execute("SELECT op_key,epoch FROM execution_operations WHERE id=? AND session=? AND identity=?", (operation_id, session_key(sid), digest(identity))).fetchone()
                if row is None:
                    raise ExecutionError("EXECUTION_NOT_FOUND")
                key, epoch = row
        request = OperationRequest.parse({"key": key, "epoch": epoch, "state_version": 0})
        job = self.dispatch.observe(sid, identity, grant, request)
        if job is None:
            raise ExecutionError("EXECUTION_NOT_FOUND")
        job["trace_id"] = "trace_" + job["operation_id"]
        if job["status"] == "SUCCEEDED":
            job["publication"] = self.publication.view(job["operation_id"])
        else:
            job["publication"] = {"status": "NOT_READY", "error_code": ""}
        return {"schema_version": 1, "job": job}

    def feedback_media(self, request, url, exports):
        """Temporary authorized copies consumed synchronously by FeedbackStore."""
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/media/(m\d{1,2})", str(url))
        if not match:
            return None
        self.view(self.credentials(request), operation_id=match[1])
        body, mime = self.publication.read_media(match[1], match[2])
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[mime]
        if self.feedback_exports.resolve() != self.feedback_exports or self.feedback_exports.is_symlink():
            raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
        with self.store.transaction() as conn:
            # Only this service's generated flat files are eligible. Crash
            # remnants are bounded and expire; no arbitrary path is accepted.
            existing = list(self.feedback_exports.glob("feedback-*"))
            if len(existing) >= 1000:
                return None
            for path in existing:
                if not path.is_symlink() and path.is_file() and path.stat().st_mtime < time.time() - 3600:
                    path.unlink()
            if sum(path.stat().st_size for path in existing if path.exists()) + len(body) > self.publication.MAX_TOTAL_MEDIA:
                return None
            self.store.storage_capacity(extra=len(body))
            target = self.feedback_exports / ("feedback-" + uuid4().hex + extension)
            exports.append(target)
            with target.open("xb") as handle:
                handle.write(body)
        return target

    def release_feedback_media(self, exports):
        for path in exports:
            if path.parent == self.feedback_exports and not path.is_symlink():
                path.unlink(missing_ok=True)

    @staticmethod
    def failure(exc):
        code = getattr(exc, "code", "EXECUTION_UNAVAILABLE")
        codes = {"BACKGROUND_PROTOCOL_REQUIRED", "EXECUTION_AUTH_REQUIRED", "EXECUTION_AUTH_UNAVAILABLE",
                 "EXECUTION_CONTEXT_REQUIRED", "EXECUTION_STALE", "EXECUTION_BUSY", "EXECUTION_INPUT_CONFLICT",
                 "EXECUTION_INPUT_INVALID", "EXECUTION_INPUT_TOO_LARGE", "EXECUTION_QUEUE_FULL", "EXECUTION_CAPACITY",
                 "EXECUTION_NOT_FOUND", "EXECUTION_RESULT_UNAVAILABLE", "EXECUTION_COST_PENDING", "EXECUTION_SHUTTING_DOWN",
                 "INVITE_DAILY_QUOTA_EXCEEDED", "GLOBAL_DAILY_QUOTA_EXCEEDED", "EXECUTION_SUBSCRIPTION_LIMIT"}
        code = code if code in codes else "EXECUTION_UNAVAILABLE"
        status = (401 if code.startswith("EXECUTION_AUTH") else 404 if code in {"EXECUTION_NOT_FOUND", "EXECUTION_RESULT_UNAVAILABLE"}
                  else 413 if code == "EXECUTION_INPUT_TOO_LARGE" else 429 if code in {"EXECUTION_QUEUE_FULL", "EXECUTION_SUBSCRIPTION_LIMIT"}
                  else 503 if code in {"EXECUTION_UNAVAILABLE", "EXECUTION_CAPACITY", "EXECUTION_SHUTTING_DOWN"} else 409)
        return JSONResponse({"schema_version": 1, "code": code, "message": "请核对任务状态后再操作。"}, status_code=status,
                            headers={"Cache-Control": "private, no-store"})

    async def body(self, request, limit):
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise ExecutionError("EXECUTION_INPUT_INVALID") from None
        if length < 0 or length > limit:
            raise ExecutionError("EXECUTION_INPUT_TOO_LARGE")
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > limit:
                raise ExecutionError("EXECUTION_INPUT_TOO_LARGE")
            body.extend(part)
        return bytes(body)

    def install(self, app):
        app.state.background = self

        @app.post("/api/jobs/session")
        async def bind(request: Request):
            try:
                self.require_protocol(request)
                sid, context = await asyncio.to_thread(self.bind_session, request)
                result = JSONResponse({"schema_version": 1, "execution": context, "protocol_version": 1})
                from tiku_agent.fastapi_demo import _set_session_cookie, _is_secure_request
                _set_session_cookie(result, sid, cookie_name=self.session_cookie, secure_cookie=_is_secure_request(request))
                return result
            except Exception as exc:
                return self.failure(exc)

        @app.post("/api/jobs")
        @app.post("/api/jobs/image")
        async def submit(request: Request):
            try:
                self.require_protocol(request, task=True)
                credentials = self.credentials(request)
                envelope = request.headers.get(OPERATION_HEADER, "")
                if len(envelope) > 1024:
                    raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
                try:
                    operation = OperationRequest.parse(json.loads(envelope))
                except (ValueError, TypeError):
                    raise ExecutionError("EXECUTION_CONTEXT_REQUIRED") from None
                if request.url.path.endswith("/image"):
                    data = await self.body(request, self.dispatch.policy.max_image_bytes)
                    kind, parameters, image = "handle_image", {}, data
                else:
                    raw = await self.body(request, self.dispatch.policy.max_json_bytes + 1024)
                    try:
                        data = json.loads(raw)
                    except (ValueError, TypeError):
                        raise ExecutionError("EXECUTION_INPUT_INVALID") from None
                    if type(data) is not dict or set(data) != {"kind", "parameters"}:
                        raise ExecutionError("EXECUTION_INPUT_INVALID")
                    kind, parameters, image = data["kind"], data["parameters"], None
                # Copy only immutable command/auth data into the background
                # admission call. Cancellation may lose ACK, never the commit.
                ack = await asyncio.to_thread(self.dispatch.accept, *credentials, operation, kind, parameters, image=image)
                record_trace_event("stage_finished", stage="background_submission", outcome="success",
                                   safe_attributes={"operation": ack["operation_id"], "completed": True})
                ack["trace_id"] = "trace_" + ack["operation_id"]
                return JSONResponse({"schema_version": 1, "job": ack}, status_code=202,
                                    headers={"Location": "/api/jobs/" + ack["operation_id"], "Cache-Control": "private, no-store"})
            except Exception as exc:
                return self.failure(exc)

        @app.get("/api/jobs/lookup")
        async def lookup(request: Request, key: str, epoch: str):
            try:
                value = await asyncio.to_thread(self.view, self.credentials(request), key=key, epoch=epoch)
                return JSONResponse(value)
            except Exception as exc:
                return self.failure(exc)

        @app.get("/api/jobs/{operation_id}")
        async def status(request: Request, operation_id: str):
            try:
                return JSONResponse(await asyncio.to_thread(self.view, self.credentials(request), operation_id=operation_id))
            except Exception as exc:
                return self.failure(exc)

        @app.get("/api/jobs/{operation_id}/media/{media_key}")
        async def media(request: Request, operation_id: str, media_key: str):
            try:
                credentials = self.credentials(request)
                await asyncio.to_thread(self.view, credentials, operation_id=operation_id)
                data, mime = await asyncio.to_thread(self.publication.read_media, operation_id, media_key)
                return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
            except Exception as exc:
                return self.failure(exc)

        @app.get("/api/jobs/{operation_id}/stream")
        async def stream(request: Request, operation_id: str, since: int = 0):
            try:
                if since < 0 or since > 2**53 - 1:
                    raise ExecutionError("EXECUTION_INPUT_INVALID")
                credentials = self.credentials(request)
                await asyncio.to_thread(self.view, credentials, operation_id=operation_id)
                scope = session_key(credentials[0])
                with self._subscriptions_lock:
                    if sum(self._subscriptions.values()) >= self.max_subscriptions or self._subscriptions.get(scope, 0) >= self.max_session_subscriptions:
                        raise ExecutionError("EXECUTION_SUBSCRIPTION_LIMIT")
                    self._subscriptions[scope] = self._subscriptions.get(scope, 0) + 1
            except Exception as exc:
                return self.failure(exc)
            event_session = current_trace_event_session()
            return StreamingResponse(self.events(request, operation_id, credentials, since, scope, event_session), media_type="application/x-ndjson",
                                     headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"})

    async def events(self, request, operation_id, credentials, since, scope, event_session):
        context = request.state.trace_context
        with trace_context_scope(context), trace_event_session_scope(event_session):
            outcome = "success"
            try:
                deadline = time.monotonic() + self.stream_seconds
                last = None
                while not self.closing:
                    self.credentials(request)  # recheck login revocation on every observation
                    value = await asyncio.to_thread(self.view, credentials, operation_id=operation_id)
                    cursor = value["job"]["progress_version"]
                    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    if last != encoded:
                        # No queue: a slow consumer retains at most this single
                        # snapshot. A cursor gap returns the latest authority.
                        yield json.dumps({"type": "snapshot", "cursor": cursor, "resync": last is None and since != cursor, "data": value}, ensure_ascii=False) + "\n"
                        last = encoded
                    job = value["job"]
                    if (job["status"] in {"FAILED", "CANCELLED", "UNKNOWN"}
                            or job["status"] == "SUCCEEDED" and job["publication"]["status"] in {"READY", "FAILED", "UNAVAILABLE"}):
                        break
                    if time.monotonic() >= deadline:
                        yield json.dumps({"type": "reconnect", "cursor": cursor}) + "\n"
                        break
                    await asyncio.sleep(self.poll_seconds)
            except (asyncio.CancelledError, GeneratorExit):
                outcome = "cancelled"
                raise
            except Exception as exc:
                outcome = "error"
                yield json.dumps({"type": "error", "code": "EXECUTION_OBSERVATION_UNAVAILABLE"}) + "\n"
            finally:
                with self._subscriptions_lock:
                    count = self._subscriptions.get(scope, 0) - 1
                    if count > 0:
                        self._subscriptions[scope] = count
                    else:
                        self._subscriptions.pop(scope, None)
                record_trace_event("stage_finished", stage="background_observation", outcome=outcome,
                                   safe_attributes={"operation": operation_id, "completed": outcome == "success"})
                if not event_session.terminal_attempted:
                    record_public_terminal(stage="background_observation", outcome=outcome, failed=outcome != "success")

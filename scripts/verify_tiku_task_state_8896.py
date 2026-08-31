"""Verify the no-model TaskStateSnapshotV1 wiring of the isolated 8896 service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.client import HTTPConnection
from http.cookies import Morsel, SimpleCookie
import json
import re
import sys
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit


BASE_URL = "http://127.0.0.1:8896"
HOST = "127.0.0.1"
PORT = 8896
SESSION_COOKIE = "tiku_agent_8896_session"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_TRACE_WRITTEN_DELTA = 20

_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'"
)

CANONICAL_EMPTY_TASK_STATE: dict[str, object] = {
    "schema_version": 1,
    "workflow": {
        "exists": False,
        "workflow_id": "",
        "kind": "NONE",
        "route": "NONE",
        "task_revision": 0,
        "phase": "IDLE",
        "status": "IDLE",
        "completed_steps": [],
        "allowed_actions": [],
        "next_stage": "UPLOAD_IMAGE",
    },
    "active_child_task": None,
    "current_unit": None,
    "units": [],
    "consistency": {"status": "OK", "codes": []},
}


class SmokeVerificationError(RuntimeError):
    """Raised when live 8896 behavior violates the frozen smoke contract."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header_values(self, name: str) -> tuple[str, ...]:
        clean = name.lower()
        return tuple(value for key, value in self.headers if key.lower() == clean)

    def header(self, name: str) -> str:
        values = self.header_values(name)
        return values[0] if values else ""


Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], HttpResponse]


class Local8896Client:
    """Small fixed-origin client with explicit session-cookie handling."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        transport: Transport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _network_transport
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        _fixed_origin_path(path)
        request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in sorted(self.cookies.items())
            )
        response = self.transport(
            method.upper(),
            path,
            body,
            request_headers,
            self.timeout_seconds,
        )
        self._update_cookies(response)
        return response

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[HttpResponse, dict[str, object]]:
        if payload is not None and raw_body is not None:
            raise ValueError("payload and raw_body are mutually exclusive")
        body = raw_body
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        response = self.request(
            method,
            path,
            body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        _expect_media_type(response, "application/json", path)
        return response, _decode_json_object(response.body, path)

    def _update_cookies(self, response: HttpResponse) -> None:
        for raw_cookie in response.header_values("set-cookie"):
            parsed = SimpleCookie()
            try:
                parsed.load(raw_cookie)
            except Exception as exc:  # pragma: no cover - defensive stdlib boundary.
                raise SmokeVerificationError("8896 returned an invalid Set-Cookie header") from exc
            morsel = parsed.get(SESSION_COOKIE)
            if morsel is None:
                continue
            if str(morsel["max-age"]).strip() == "0" or not morsel.value:
                self.cookies.pop(SESSION_COOKIE, None)
            else:
                self.cookies[SESSION_COOKIE] = morsel.value


def _network_transport(
    method: str,
    path: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> HttpResponse:
    connection = HTTPConnection(HOST, PORT, timeout=timeout_seconds)
    try:
        connection.request(method, path, body=body, headers=dict(headers))
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SmokeVerificationError("8896 response exceeded the smoke-test limit")
        return HttpResponse(
            status=response.status,
            headers=tuple((str(key), str(value)) for key, value in response.getheaders()),
            body=payload,
        )
    finally:
        connection.close()


def _strict_base_url(value: str) -> str:
    clean = str(value or "").strip().rstrip("/")
    if clean != BASE_URL:
        raise ValueError(f"base URL must be exactly {BASE_URL}")
    return clean


def _fixed_origin_path(path: str) -> str:
    parsed = urlsplit(str(path or ""))
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("request path must stay on the fixed 8896 origin")
    return parsed.path


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeVerificationError(message)


def _decode_json_object(body: bytes, path: str) -> dict[str, object]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeVerificationError(f"{path} did not return valid UTF-8 JSON") from exc
    _expect(type(value) is dict, f"{path} JSON root must be an object")
    return value


def _expect_media_type(response: HttpResponse, expected: str, path: str) -> None:
    actual = response.header("content-type").split(";", 1)[0].strip().lower()
    _expect(actual == expected, f"{path} returned an unexpected media type")


def _expect_status(response: HttpResponse, expected: int, path: str) -> None:
    _expect(response.status == expected, f"{path} returned an unexpected HTTP status")


def _expect_security_headers(
    response: HttpResponse,
    path: str,
    *,
    api: bool,
) -> str:
    _expect(response.header("content-security-policy") == _CSP, f"{path} CSP mismatch")
    _expect(response.header("x-content-type-options") == "nosniff", f"{path} nosniff missing")
    _expect(response.header("x-frame-options") == "DENY", f"{path} frame denial missing")
    _expect(response.header("referrer-policy") == "no-referrer", f"{path} referrer policy mismatch")
    _expect(
        response.header("permissions-policy") == "camera=(), microphone=(), geolocation=()",
        f"{path} permissions policy mismatch",
    )
    _expect(not response.header("strict-transport-security"), f"{path} local HTTP must not emit HSTS")
    _expect(not response.header("x-trace-id"), f"{path} exposed an internal trace header")
    _expect(b'"trace_id"' not in response.body, f"{path} exposed an internal trace field")
    request_id = ""
    if api:
        cache_tokens = {
            token.strip().lower()
            for token in response.header("cache-control").split(",")
            if token.strip()
        }
        _expect({"private", "no-store"} <= cache_tokens, f"{path} API cache policy mismatch")
        request_id = response.header("x-request-id")
        _expect(bool(_REQUEST_ID_RE.fullmatch(request_id)), f"{path} request ID is invalid")
    return request_id


def _session_cookie_morsel(response: HttpResponse, path: str) -> Morsel[str]:
    matches: list[Morsel[str]] = []
    for raw_cookie in response.header_values("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        morsel = parsed.get(SESSION_COOKIE)
        if morsel is not None:
            matches.append(morsel)
    _expect(len(matches) == 1, f"{path} must set exactly one 8896 session cookie")
    return matches[0]


def _expect_live_session_cookie(response: HttpResponse, path: str) -> str:
    morsel = _session_cookie_morsel(response, path)
    _expect(bool(morsel.value), f"{path} session cookie value is empty")
    _expect(morsel["path"] == "/", f"{path} session cookie path mismatch")
    _expect(not morsel["domain"], f"{path} session cookie must be host-only")
    _expect(str(morsel["max-age"]) == "7200", f"{path} session cookie lifetime mismatch")
    _expect(bool(morsel["httponly"]), f"{path} session cookie must be HttpOnly")
    _expect(str(morsel["samesite"]).lower() == "lax", f"{path} SameSite mismatch")
    _expect(not bool(morsel["secure"]), f"{path} local HTTP cookie must not be Secure")
    return morsel.value


def _expect_deleted_session_cookie(response: HttpResponse, path: str) -> None:
    morsel = _session_cookie_morsel(response, path)
    _expect(not morsel.value, f"{path} reset cookie must be empty")
    _expect(str(morsel["max-age"]) == "0", f"{path} reset cookie Max-Age mismatch")
    _expect(morsel["path"] == "/", f"{path} reset cookie path mismatch")
    _expect(not morsel["domain"], f"{path} reset cookie must be host-only")
    _expect(bool(morsel["httponly"]), f"{path} reset cookie must be HttpOnly")
    _expect(str(morsel["samesite"]).lower() == "lax", f"{path} reset SameSite mismatch")
    _expect(not bool(morsel["secure"]), f"{path} local reset cookie must not be Secure")


def _expect_empty_task_state(payload: Mapping[str, object], path: str) -> dict[str, object]:
    _expect("task_state" in payload, f"{path} is missing root task_state")
    state = payload["task_state"]
    _expect(type(state) is dict, f"{path} task_state must be an object")
    _expect(state == CANONICAL_EMPTY_TASK_STATE, f"{path} task_state is not canonical empty V1")
    return state


def _expect_protocol_request_id(
    payload: Mapping[str, object],
    header_request_id: str,
    path: str,
) -> None:
    _expect(
        payload.get("request_id") == header_request_id,
        f"{path} payload/header request IDs differ",
    )


def _expect_protocol(
    payload: Mapping[str, object],
    *,
    status: str,
    layer: str,
    code: str,
    request_id: str,
    path: str,
) -> None:
    expected = {
        "status": status,
        "layer": layer,
        "code": code,
        "retryable": False,
        "action": "",
        "request_id": request_id,
        "search_id": "",
        "schema_version": 1,
    }
    for key, value in expected.items():
        _expect(payload.get(key) == value, f"{path} protocol field {key} mismatch")


def _expect_trace_health(payload: Mapping[str, object], path: str) -> tuple[int, int]:
    _expect(payload.get("status") == "ok", f"{path} service health is not ok")
    trace = payload.get("trace_events")
    _expect(type(trace) is dict, f"{path} trace health is missing")
    _expect(trace.get("status") == "ok", f"{path} trace writer is not ok")
    for key in ("dropped", "write_failures", "validation_rejections", "duplicate_terminals"):
        value = trace.get(key)
        _expect(type(value) is int and value == 0, f"{path} trace counter {key} is nonzero")
    _expect(trace.get("accepting") is True, f"{path} trace writer is not accepting")
    written = trace.get("written")
    _expect(type(written) is int and written >= 0, f"{path} trace written counter is invalid")
    pending = trace.get("pending")
    _expect(type(pending) is int and pending >= 0, f"{path} trace pending counter is invalid")
    return written, pending


def _parse_ndjson(response: HttpResponse, path: str) -> list[dict[str, object]]:
    try:
        text = response.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeVerificationError(f"{path} did not return UTF-8 NDJSON") from exc
    events: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SmokeVerificationError(f"{path} returned invalid NDJSON") from exc
        _expect(type(value) is dict, f"{path} NDJSON event must be an object")
        events.append(value)
    _expect(bool(events), f"{path} returned no NDJSON events")
    return events


def verify_task_state_8896(
    *,
    base_url: str = BASE_URL,
    timeout_seconds: float = 5.0,
    transport: Transport | None = None,
) -> dict[str, object]:
    """Run the deterministic, no-image, no-model 8896 contract smoke."""

    _strict_base_url(base_url)
    if not 1.0 <= timeout_seconds <= 30.0:
        raise ValueError("timeout_seconds must be between 1 and 30")
    client = Local8896Client(timeout_seconds=timeout_seconds, transport=transport)
    request_ids: set[str] = set()

    health_response, health_payload = client.request_json("GET", "/health")
    _expect_status(health_response, 200, "/health")
    request_ids.add(_expect_security_headers(health_response, "/health", api=False))
    request_ids.discard("")
    initial_trace_written, _initial_trace_pending = _expect_trace_health(
        health_payload,
        "/health",
    )

    no_cookie_error, no_cookie_payload = client.request_json(
        "POST",
        "/api/message",
        raw_body=b"not-json",
    )
    _expect_status(no_cookie_error, 400, "/api/message no-cookie error")
    request_ids.add(
        _expect_security_headers(no_cookie_error, "/api/message no-cookie error", api=True)
    )
    no_cookie_request_id = no_cookie_error.header("x-request-id")
    _expect_protocol(
        no_cookie_payload,
        status="NEEDS_INPUT",
        layer="session",
        code="MESSAGE_INVALID",
        request_id=no_cookie_request_id,
        path="/api/message no-cookie error",
    )
    _expect(
        no_cookie_payload.get("detail")
        == "\u8bf7\u6c42\u5185\u5bb9\u65e0\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4\u3002",
        "no-cookie validation detail is not the registered public message",
    )
    _expect("task_state" not in no_cookie_payload, "no-cookie validation minted task_state")
    _expect("response_id" not in no_cookie_payload, "no-cookie validation persisted a response")
    _expect(b"not-json" not in no_cookie_error.body, "no-cookie validation echoed request data")
    _expect(not no_cookie_error.header_values("set-cookie"), "no-cookie validation minted a session")
    _expect(SESSION_COOKIE not in client.cookies, "no-cookie validation retained a session")

    session_response, session_payload = client.request_json("GET", "/api/session")
    _expect_status(session_response, 200, "/api/session")
    session_request_id = _expect_security_headers(session_response, "/api/session", api=True)
    request_ids.add(session_request_id)
    first_cookie = _expect_live_session_cookie(session_response, "/api/session")
    _expect(client.cookies.get(SESSION_COOKIE) == first_cookie, "session cookie was not retained")
    session_state = _expect_empty_task_state(session_payload, "/api/session")
    _expect(session_payload.get("uploaded_image") == "", "/api/session unexpectedly exposed an image")
    legacy_session = session_payload.get("session")
    _expect(type(legacy_session) is dict, "/api/session legacy session is missing")
    _expect("task_state" not in legacy_session, "legacy session nested task_state")

    page_response = client.request("GET", "/", headers={"Accept": "text/html"})
    _expect_status(page_response, 200, "/")
    _expect_media_type(page_response, "text/html", "/")
    _expect_security_headers(page_response, "/", api=False)
    try:
        page_text = page_response.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeVerificationError("/ did not return UTF-8 HTML") from exc
    _expect("/assets/task_state.js" in page_text, "/ is missing the task-state client")
    _expect("/assets/demo.js" in page_text, "/ is missing the demo client")
    _expect(
        _expect_live_session_cookie(page_response, "/") == first_cookie,
        "/ changed the established session cookie",
    )

    stale_payload = {"text": "\u7ee7\u7eed", "action_context": {"type": "unsupported_action"}}
    json_response, json_payload = client.request_json(
        "POST",
        "/api/message",
        payload=stale_payload,
    )
    _expect_status(json_response, 200, "/api/message stale action")
    json_request_id = _expect_security_headers(json_response, "/api/message stale action", api=True)
    request_ids.add(json_request_id)
    _expect_protocol(
        json_payload,
        status="NEEDS_INPUT",
        layer="session",
        code="STALE_ACTION",
        request_id=json_request_id,
        path="/api/message stale action",
    )
    _expect(json_payload.get("intent") == "stale_action", "JSON stale action intent mismatch")
    _expect(type(json_payload.get("text")) is str and bool(json_payload["text"]), "JSON stale text missing")
    json_state = _expect_empty_task_state(json_payload, "/api/message stale action")
    json_response_id = str(json_payload.get("response_id") or "")
    _expect(bool(_RESPONSE_ID_RE.fullmatch(json_response_id)), "JSON response_id is invalid")
    _expect(
        _expect_live_session_cookie(json_response, "/api/message stale action") == first_cookie,
        "JSON stale action changed the session cookie",
    )

    stream_response = client.request(
        "POST",
        "/api/message/stream",
        body=json.dumps(stale_payload, ensure_ascii=True, separators=(",", ":")).encode("ascii"),
        headers={"Accept": "application/x-ndjson", "Content-Type": "application/json"},
    )
    _expect_status(stream_response, 200, "/api/message/stream stale action")
    _expect_media_type(stream_response, "application/x-ndjson", "/api/message/stream")
    stream_request_id = _expect_security_headers(
        stream_response,
        "/api/message/stream stale action",
        api=True,
    )
    request_ids.add(stream_request_id)
    events = _parse_ndjson(stream_response, "/api/message/stream")
    terminal_indexes = [
        index for index, event in enumerate(events) if event.get("type") in {"result", "error"}
    ]
    _expect(terminal_indexes == [len(events) - 1], "stream must have one final terminal event")
    for event in events[:-1]:
        _expect(event.get("type") == "progress", "stream contains a non-progress prefix event")
        _expect("task_state" not in event, "stream progress event carried task_state")
    terminal = events[-1]
    _expect(set(terminal) == {"type", "data"}, "stream result envelope shape mismatch")
    _expect(terminal.get("type") == "result", "stream stale action did not return result")
    stream_payload = terminal.get("data")
    _expect(type(stream_payload) is dict, "stream result data must be an object")
    _expect_protocol(
        stream_payload,
        status="NEEDS_INPUT",
        layer="session",
        code="STALE_ACTION",
        request_id=stream_request_id,
        path="/api/message/stream",
    )
    _expect(stream_payload.get("intent") == "stale_action", "stream stale action intent mismatch")
    _expect(type(stream_payload.get("text")) is str and bool(stream_payload["text"]), "stream stale text missing")
    stream_state = _expect_empty_task_state(stream_payload, "/api/message/stream")
    stream_response_id = str(stream_payload.get("response_id") or "")
    _expect(bool(_RESPONSE_ID_RE.fullmatch(stream_response_id)), "stream response_id is invalid")
    _expect(stream_response_id != json_response_id, "JSON and stream reused a response_id")
    _expect(
        _expect_live_session_cookie(stream_response, "/api/message/stream") == first_cookie,
        "stream stale action changed the session cookie",
    )

    cookie_error, cookie_error_payload = client.request_json(
        "POST",
        "/api/message",
        raw_body=b"not-json",
    )
    _expect_status(cookie_error, 400, "/api/message session error")
    cookie_error_request_id = _expect_security_headers(
        cookie_error,
        "/api/message session error",
        api=True,
    )
    request_ids.add(cookie_error_request_id)
    _expect_protocol(
        cookie_error_payload,
        status="NEEDS_INPUT",
        layer="session",
        code="MESSAGE_INVALID",
        request_id=cookie_error_request_id,
        path="/api/message session error",
    )
    _expect(
        cookie_error_payload.get("detail")
        == "\u8bf7\u6c42\u5185\u5bb9\u65e0\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4\u3002",
        "session validation detail is not the registered public message",
    )
    error_state = _expect_empty_task_state(cookie_error_payload, "/api/message session error")
    error_response_id = str(cookie_error_payload.get("response_id") or "")
    _expect(bool(_RESPONSE_ID_RE.fullmatch(error_response_id)), "HTTP error response_id is invalid")
    _expect(
        error_response_id not in {json_response_id, stream_response_id},
        "HTTP error reused an earlier response_id",
    )
    _expect(not cookie_error.header_values("set-cookie"), "session validation rewrote the cookie")
    _expect(client.cookies.get(SESSION_COOKIE) == first_cookie, "session validation lost the cookie")
    _expect(b"not-json" not in cookie_error.body, "session validation echoed request data")

    parity_response, parity_payload = client.request_json("GET", "/api/session")
    _expect_status(parity_response, 200, "/api/session parity")
    request_ids.add(_expect_security_headers(parity_response, "/api/session parity", api=True))
    _expect(
        _expect_live_session_cookie(parity_response, "/api/session parity") == first_cookie,
        "parity session changed the session cookie",
    )
    parity_state = _expect_empty_task_state(parity_payload, "/api/session parity")
    _expect(
        session_state == json_state == stream_state == error_state == parity_state,
        "session/JSON/stream/error task-state parity failed",
    )

    reset_response, reset_payload = client.request_json("POST", "/api/reset", payload={})
    _expect_status(reset_response, 200, "/api/reset")
    reset_request_id = _expect_security_headers(reset_response, "/api/reset", api=True)
    request_ids.add(reset_request_id)
    _expect_protocol(
        reset_payload,
        status="SUCCESS",
        layer="session",
        code="SESSION_RESET",
        request_id=reset_request_id,
        path="/api/reset",
    )
    _expect(reset_payload.get("ok") is True, "/api/reset did not report success")
    _expect("response_id" not in reset_payload, "/api/reset unexpectedly persisted a response")
    _expect_empty_task_state(reset_payload, "/api/reset")
    _expect_deleted_session_cookie(reset_response, "/api/reset")
    _expect(SESSION_COOKIE not in client.cookies, "/api/reset did not clear the session cookie")

    post_reset_response, post_reset_payload = client.request_json("GET", "/api/session")
    _expect_status(post_reset_response, 200, "/api/session after reset")
    request_ids.add(
        _expect_security_headers(post_reset_response, "/api/session after reset", api=True)
    )
    second_cookie = _expect_live_session_cookie(post_reset_response, "/api/session after reset")
    _expect(second_cookie != first_cookie, "/api/reset did not retire the prior session identity")
    _expect_empty_task_state(post_reset_payload, "/api/session after reset")

    cleanup_response, cleanup_payload = client.request_json("POST", "/api/reset", payload={})
    _expect_status(cleanup_response, 200, "/api/reset cleanup")
    cleanup_request_id = _expect_security_headers(cleanup_response, "/api/reset cleanup", api=True)
    request_ids.add(cleanup_request_id)
    _expect_protocol(
        cleanup_payload,
        status="SUCCESS",
        layer="session",
        code="SESSION_RESET",
        request_id=cleanup_request_id,
        path="/api/reset cleanup",
    )
    _expect(cleanup_payload.get("ok") is True, "/api/reset cleanup did not report success")
    _expect("response_id" not in cleanup_payload, "/api/reset cleanup persisted a response")
    _expect_empty_task_state(cleanup_payload, "/api/reset cleanup")
    _expect_deleted_session_cookie(cleanup_response, "/api/reset cleanup")
    _expect(SESSION_COOKIE not in client.cookies, "cleanup reset left a session cookie")

    # Give the asynchronous writer one bounded, network-free drain window.
    # Repeated /health polling would generate trace events of its own and could
    # eventually manufacture the minimum delta even if task requests were lost.
    time.sleep(min(0.2, timeout_seconds / 5.0))
    final_health_response, final_health_payload = client.request_json("GET", "/health")
    _expect_status(final_health_response, 200, "/health final")
    _expect_security_headers(final_health_response, "/health final", api=False)
    final_trace_written, final_trace_pending = _expect_trace_health(
        final_health_payload,
        "/health final",
    )
    _expect(
        final_trace_written - initial_trace_written >= MIN_TRACE_WRITTEN_DELTA,
        "trace writer did not persist the minimum smoke-request evidence",
    )
    _expect(final_trace_pending <= 1, "trace writer did not drain the smoke-request backlog")
    _expect(len(request_ids) == 9, "API request IDs were missing or reused")

    return {
        "base_url": BASE_URL,
        "health": "ok",
        "canonical_empty_v1": "ok",
        "no_session_error_boundary": "ok",
        "session_json_stream_error_parity": "ok",
        "reset_cookie_cleanup": "ok",
        "security_headers": "ok",
        "response_store": "ok",
        "trace_events": "ok",
        "trace_written_delta": final_trace_written - initial_trace_written,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the deterministic TaskStateSnapshotV1 exits on fixed local port 8896"
    )
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    try:
        result = verify_task_state_8896(
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
    except (OSError, ValueError, SmokeVerificationError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

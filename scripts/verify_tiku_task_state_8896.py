"""Verify the no-model TaskStateSnapshotV1 wiring of the isolated 8896 service."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.cookies import Morsel, SimpleCookie
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4


BASE_URL = "http://127.0.0.1:8896"
HOST = "127.0.0.1"
PORT = 8896
SESSION_COOKIE = "tiku_agent_8896_session"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
EVIDENCE_SCHEMA = "tiku-task-state-8896-smoke-evidence-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "tiku_agent" / "demo_web"
TERMINAL_EVENT_TYPES = frozenset({"public_response_finalized", "request_failed"})

_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")
_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SESSION_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
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
    request_id: str = ""

    def header_values(self, name: str) -> tuple[str, ...]:
        clean = name.lower()
        return tuple(value for key, value in self.headers if key.lower() == clean)

    def header(self, name: str) -> str:
        values = self.header_values(name)
        return values[0] if values else ""


Transport = Callable[[str, str, bytes | None, Mapping[str, str], float], HttpResponse]
RequestIdFactory = Callable[[], str]


@dataclass(frozen=True)
class TraceExpectation:
    name: str
    method: str
    endpoint: str
    response_mode: str
    http_status: int
    terminal_stage: str
    terminal_outcome: str
    protocol_status: str = ""
    protocol_layer: str = ""
    protocol_code: str = ""


TRACE_EXPECTATIONS: tuple[TraceExpectation, ...] = (
    TraceExpectation("health_initial", "GET", "/health", "json", 200, "http_response", "success"),
    TraceExpectation(
        "message_invalid_without_session",
        "POST",
        "/api/message",
        "json",
        400,
        "public_response",
        "needs_input",
        "NEEDS_INPUT",
        "session",
        "MESSAGE_INVALID",
    ),
    TraceExpectation("session_initial", "GET", "/api/session", "json", 200, "http_response", "success"),
    TraceExpectation("page", "GET", "/", "html", 200, "http_response", "success"),
    TraceExpectation("task_state_asset", "GET", "/assets/:id", "html", 200, "http_response", "success"),
    TraceExpectation("demo_asset", "GET", "/assets/:id", "html", 200, "http_response", "success"),
    TraceExpectation(
        "stale_action_json",
        "POST",
        "/api/message",
        "json",
        200,
        "public_response",
        "needs_input",
        "NEEDS_INPUT",
        "session",
        "STALE_ACTION",
    ),
    TraceExpectation(
        "stale_action_stream",
        "POST",
        "/api/message/stream",
        "stream",
        200,
        "public_response",
        "needs_input",
        "NEEDS_INPUT",
        "session",
        "STALE_ACTION",
    ),
    TraceExpectation(
        "message_invalid_with_session",
        "POST",
        "/api/message",
        "json",
        400,
        "public_response",
        "needs_input",
        "NEEDS_INPUT",
        "session",
        "MESSAGE_INVALID",
    ),
    TraceExpectation("session_parity", "GET", "/api/session", "json", 200, "http_response", "success"),
    TraceExpectation("reset_primary", "POST", "/api/reset", "json", 200, "http_response", "success"),
    TraceExpectation("session_after_reset", "GET", "/api/session", "json", 200, "http_response", "success"),
    TraceExpectation("reset_cleanup", "POST", "/api/reset", "json", 200, "http_response", "success"),
    TraceExpectation("health_final", "GET", "/health", "json", 200, "http_response", "success"),
)

TRACE_EXPECTATION_BY_NAME = {item.name: item for item in TRACE_EXPECTATIONS}

RESPONSE_EXPECTATIONS: dict[str, dict[str, object]] = {
    "stale_action_json": {
        "status": "NEEDS_INPUT",
        "layer": "session",
        "code": "STALE_ACTION",
        "response_mode": "json",
        "intent": "stale_action",
        "text_length": 21,
    },
    "stale_action_stream": {
        "status": "NEEDS_INPUT",
        "layer": "session",
        "code": "STALE_ACTION",
        "response_mode": "stream",
        "intent": "stale_action",
        "text_length": 21,
    },
    "message_invalid_with_session": {
        "status": "NEEDS_INPUT",
        "layer": "session",
        "code": "MESSAGE_INVALID",
        "response_mode": "json",
        "intent": "request_error",
        "text_length": 13,
    },
}


class _ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        for key, value in attrs:
            if key.lower() == "src" and value is not None:
                self.sources.append(value)
                return


class Local8896Client:
    """Small fixed-origin client with explicit session-cookie handling."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        transport: Transport | None = None,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _network_transport
        self.request_id_factory = request_id_factory or _new_request_id
        self.cookies: dict[str, str] = {}
        self.requests: list[dict[str, str]] = []
        self._request_ids: set[str] = set()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        evidence_name: str,
    ) -> HttpResponse:
        clean_path = _fixed_origin_path(path)
        request_id = str(self.request_id_factory() or "").strip().lower()
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise SmokeVerificationError("smoke request ID factory returned an invalid value")
        if request_id in self._request_ids:
            raise SmokeVerificationError("smoke request ID factory reused a value")
        if not evidence_name or any(item["name"] == evidence_name for item in self.requests):
            raise SmokeVerificationError("smoke request evidence name is missing or reused")
        self._request_ids.add(request_id)
        request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
        request_headers["X-Request-ID"] = request_id
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in sorted(self.cookies.items())
            )
        response = self.transport(
            method.upper(),
            clean_path,
            body,
            request_headers,
            self.timeout_seconds,
        )
        response = HttpResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            request_id=request_id,
        )
        self.requests.append({"name": evidence_name, "request_id": request_id})
        self._update_cookies(response)
        return response

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        raw_body: bytes | None = None,
        evidence_name: str,
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
            evidence_name=evidence_name,
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


def _new_request_id() -> str:
    return f"req_{uuid4().hex}"


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
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("request path must stay on the fixed 8896 origin")
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeVerificationError(message)


def _normalized_commit(value: object) -> str:
    commit = str(value or "").strip().lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError("expected commit must be a full 40-character object ID")
    return commit


def _normalized_runtime_identity(value: str | Path) -> str:
    runtime = Path(value)
    if not runtime.is_absolute():
        raise ValueError("runtime identity must be absolute")
    return str(runtime.resolve())


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
    if api:
        cache_tokens = {
            token.strip().lower()
            for token in response.header("cache-control").split(",")
            if token.strip()
        }
        _expect({"private", "no-store"} <= cache_tokens, f"{path} API cache policy mismatch")
        _expect(
            response.header_values("x-request-id") == (response.request_id,),
            f"{path} did not uniquely echo the submitted request ID",
        )
    else:
        _expect(not response.header_values("x-request-id"), f"{path} exposed a non-API request ID")
    return response.request_id


def _expect_no_store(response: HttpResponse, path: str) -> None:
    cache_tokens = {
        token.strip().lower()
        for token in response.header("cache-control").split(",")
        if token.strip()
    }
    _expect("no-store" in cache_tokens, f"{path} cache policy is not no-store")


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


def _expect_trace_health(payload: Mapping[str, object], path: str) -> dict[str, object]:
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
    _expect(not trace.get("last_failure_kind"), f"{path} trace writer retained a failure kind")
    _expect(not trace.get("last_failure_at"), f"{path} trace writer retained a failure time")
    return {
        "status": "ok",
        "written": written,
        "dropped": 0,
        "write_failures": 0,
        "validation_rejections": 0,
        "duplicate_terminals": 0,
        "pending": pending,
        "accepting": True,
        "last_failure_kind": "",
        "last_failure_at": "",
    }


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


def _script_sources(html: str, path: str) -> tuple[str, ...]:
    parser = _ScriptSourceParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise SmokeVerificationError(f"{path} could not be parsed as HTML") from exc
    return tuple(parser.sources)


def _frontend_asset_sources(html: str, path: str) -> tuple[str, str]:
    sources = _script_sources(html, path)
    matches: dict[str, list[str]] = {"task_state.js": [], "demo.js": []}
    for source in sources:
        parsed = urlsplit(source)
        name = Path(parsed.path).name
        if name in matches:
            matches[name].append(source)
    for name, values in matches.items():
        _expect(len(values) == 1, f"{path} must reference exactly one {name}")
        parsed = urlsplit(values[0])
        _expect(
            parsed.path == f"/assets/{name}" and parsed.query.startswith("v="),
            f"{path} {name} URL is not versioned on the fixed origin",
        )
        _fixed_origin_path(values[0])
    task_source = matches["task_state.js"][0]
    demo_source = matches["demo.js"][0]
    _expect(
        sources.index(task_source) < sources.index(demo_source),
        f"{path} loads demo.js before task_state.js",
    )
    return task_source, demo_source


def _verify_frontend_assets(
    client: Local8896Client,
    page_response: HttpResponse,
) -> None:
    expected_page = (WEB_ROOT / "index.html").read_text(encoding="utf-8").encode("utf-8")
    _expect(page_response.body == expected_page, "/ does not match the fixed checkout HTML")
    try:
        live_page = page_response.body.decode("utf-8")
        checkout_page = expected_page.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeVerificationError("frontend HTML is not valid UTF-8") from exc
    live_sources = _frontend_asset_sources(live_page, "/")
    expected_sources = _frontend_asset_sources(checkout_page, "checkout index.html")
    _expect(live_sources == expected_sources, "/ frontend asset versions do not match checkout")

    bodies: dict[str, bytes] = {}
    for source, evidence_name, filename in (
        (live_sources[0], "task_state_asset", "task_state.js"),
        (live_sources[1], "demo_asset", "demo.js"),
    ):
        response = client.request(
            "GET",
            source,
            headers={"Accept": "text/javascript, application/javascript"},
            evidence_name=evidence_name,
        )
        _expect_status(response, 200, source)
        media_type = response.header("content-type").split(";", 1)[0].strip().lower()
        _expect(
            media_type in {"text/javascript", "application/javascript"},
            f"{source} returned an unexpected media type",
        )
        _expect_security_headers(response, source, api=False)
        _expect(not response.header_values("set-cookie"), f"{source} rewrote the session cookie")
        expected_body = (WEB_ROOT / filename).read_bytes()
        _expect(bool(expected_body), f"checkout {filename} is empty")
        _expect(response.body == expected_body, f"{source} does not match the fixed checkout")
        bodies[filename] = response.body

    try:
        task_state_text = bodies["task_state.js"].decode("utf-8")
        demo_text = bodies["demo.js"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeVerificationError("frontend JavaScript is not valid UTF-8") from exc
    _expect("root.TikuTaskStateV1 = api" in task_state_text, "task_state.js export is missing")
    _expect(
        f"const TASK_STATE_ASSET_URL = {live_sources[0]!r};" in demo_text,
        "demo.js task-state bootstrap version does not match index.html",
    )
    _expect("function startDemo(taskStateV1)" in demo_text, "demo.js task-state bootstrap is missing")


def _verified_reset(
    client: Local8896Client,
    *,
    evidence_name: str,
    path_label: str,
) -> tuple[HttpResponse, dict[str, object]]:
    response, payload = client.request_json(
        "POST",
        "/api/reset",
        payload={},
        evidence_name=evidence_name,
    )
    _expect_status(response, 200, path_label)
    request_id = _expect_security_headers(response, path_label, api=True)
    _expect_protocol(
        payload,
        status="SUCCESS",
        layer="session",
        code="SESSION_RESET",
        request_id=request_id,
        path=path_label,
    )
    _expect(payload.get("ok") is True, f"{path_label} did not report success")
    _expect("response_id" not in payload, f"{path_label} persisted a response")
    _expect_empty_task_state(payload, path_label)
    _expect_deleted_session_cookie(response, path_label)
    _expect(SESSION_COOKIE not in client.cookies, f"{path_label} left a session cookie")
    return response, payload


def _validated_evidence(evidence: Mapping[str, object]) -> tuple[
    tuple[dict[str, str], ...], tuple[dict[str, str], ...], dict[str, object]
]:
    _expect(type(evidence) is dict, "smoke evidence root must be an object")
    _expect(evidence.get("schema") == EVIDENCE_SCHEMA, "smoke evidence schema mismatch")
    _expect(evidence.get("base_url") == BASE_URL, "smoke evidence origin mismatch")
    try:
        _normalized_commit(evidence.get("expected_commit"))
        _normalized_runtime_identity(str(evidence.get("runtime_dir") or ""))
    except ValueError as exc:
        raise SmokeVerificationError(str(exc)) from exc
    for key in (
        "health",
        "frontend_checkout_assets",
        "canonical_empty_v1",
        "no_session_error_boundary",
        "session_json_stream_error_parity",
        "reset_cookie_cleanup",
        "security_headers",
    ):
        _expect(evidence.get(key) == "ok", f"smoke evidence check {key} is incomplete")
    _expect(
        evidence.get("sqlite_evidence") == "pending_offline_verification",
        "smoke evidence SQLite state is invalid",
    )
    raw_requests = evidence.get("requests")
    _expect(type(raw_requests) is list, "smoke evidence requests must be a list")
    _expect(len(raw_requests) == len(TRACE_EXPECTATIONS), "smoke evidence request count mismatch")
    requests: list[dict[str, str]] = []
    for index, raw in enumerate(raw_requests):
        _expect(type(raw) is dict and set(raw) == {"name", "request_id"}, "invalid request evidence")
        name = str(raw.get("name") or "")
        request_id = str(raw.get("request_id") or "")
        _expect(name == TRACE_EXPECTATIONS[index].name, "smoke evidence request order mismatch")
        _expect(bool(_REQUEST_ID_RE.fullmatch(request_id)), "smoke evidence request ID is invalid")
        requests.append({"name": name, "request_id": request_id})
    _expect(
        len({item["request_id"] for item in requests}) == len(requests),
        "smoke evidence request IDs are not unique",
    )

    raw_responses = evidence.get("responses")
    _expect(type(raw_responses) is list, "smoke evidence responses must be a list")
    _expect(len(raw_responses) == len(RESPONSE_EXPECTATIONS), "smoke evidence response count mismatch")
    responses: list[dict[str, str]] = []
    for raw in raw_responses:
        _expect(type(raw) is dict and set(raw) == {"name", "response_id"}, "invalid response evidence")
        name = str(raw.get("name") or "")
        response_id = str(raw.get("response_id") or "")
        _expect(name in RESPONSE_EXPECTATIONS, "smoke evidence response name is invalid")
        _expect(bool(_RESPONSE_ID_RE.fullmatch(response_id)), "smoke evidence response ID is invalid")
        responses.append({"name": name, "response_id": response_id})
    _expect(
        {item["name"] for item in responses} == set(RESPONSE_EXPECTATIONS),
        "smoke evidence response names are incomplete",
    )
    _expect(
        len({item["response_id"] for item in responses}) == len(responses),
        "smoke evidence response IDs are not unique",
    )

    trace_health = evidence.get("trace_health")
    _expect(type(trace_health) is dict, "smoke evidence trace health is missing")
    _expect(trace_health.get("status") == "ok", "smoke evidence trace health is not ok")
    for key in ("dropped", "write_failures", "validation_rejections", "duplicate_terminals"):
        _expect(trace_health.get(key) == 0, f"smoke evidence trace counter {key} is nonzero")
    _expect(trace_health.get("accepting") is True, "smoke evidence trace writer was not accepting")
    pending = trace_health.get("pending")
    _expect(type(pending) is int and pending <= 1, "smoke evidence trace backlog is not drained")
    _expect(not trace_health.get("last_failure_kind"), "smoke evidence retained a trace failure kind")
    _expect(not trace_health.get("last_failure_at"), "smoke evidence retained a trace failure time")
    return tuple(requests), tuple(responses), dict(trace_health)


def write_smoke_evidence(path: str | Path, evidence: Mapping[str, object]) -> Path:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("evidence output path must be absolute")
    _validated_evidence(evidence)
    target = target.resolve()
    if target.exists():
        raise FileExistsError("evidence output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(evidence, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return target


def load_smoke_evidence(path: str | Path) -> dict[str, object]:
    source = Path(path)
    if not source.is_absolute():
        raise ValueError("evidence input path must be absolute")
    source = source.resolve(strict=True)
    _expect(source.is_file(), "smoke evidence input is not a file")
    _expect(source.stat().st_size <= 256 * 1024, "smoke evidence input is too large")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeVerificationError("smoke evidence input is not valid UTF-8 JSON") from exc
    _validated_evidence(value)
    return value


def _open_read_only_sqlite(path: Path) -> sqlite3.Connection:
    _expect(path.is_file(), f"runtime database is missing: {path.name}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        result = connection.execute("PRAGMA quick_check").fetchone()
        _expect(result is not None and result[0] == "ok", f"{path.name} quick_check failed")
        return connection
    except SmokeVerificationError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise SmokeVerificationError(f"runtime database is unreadable: {path.name}") from exc


def _expect_sqlite_index(
    connection: sqlite3.Connection,
    *,
    name: str,
    table: str,
    columns: tuple[str, ...],
    predicate: str = "",
) -> None:
    row = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    _expect(row is not None, f"SQLite index {name} is missing")
    _expect(str(row["tbl_name"]) == table, f"SQLite index {name} table mismatch")
    _expect(
        bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table))
        and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)),
        "SQLite index audit received an invalid identifier",
    )

    index_rows = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
    index_row = next((item for item in index_rows if str(item["name"]) == name), None)
    _expect(index_row is not None, f"SQLite index {name} is not attached to {table}")
    _expect(int(index_row["unique"]) == 1, f"SQLite index {name} is not unique")
    _expect(
        int(index_row["partial"]) == int(bool(predicate)),
        f"SQLite index {name} partial flag mismatch",
    )
    actual_columns = tuple(
        str(item["name"])
        for item in connection.execute(f'PRAGMA index_info("{name}")').fetchall()
    )
    _expect(actual_columns == columns, f"SQLite index {name} columns mismatch")

    normalized_sql = " ".join(str(row["sql"] or "").split())
    actual_predicate = normalized_sql.partition(" WHERE ")[2]
    _expect(
        actual_predicate.casefold() == " ".join(predicate.split()).casefold(),
        f"SQLite index {name} predicate mismatch",
    )


def _sqlite_file_state(path: Path) -> tuple[tuple[str, int, int, str], ...]:
    states: list[tuple[str, int, int, str]] = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            stat = candidate.stat()
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            states.append((candidate.name, stat.st_size, stat.st_mtime_ns, digest.hexdigest()))
    return tuple(states)


def _copy_sqlite_snapshot(source: Path, target_directory: Path) -> Path:
    _expect(source.is_file(), f"runtime database is missing: {source.name}")
    target = target_directory / source.name
    for candidate in (source, Path(f"{source}-wal"), Path(f"{source}-shm")):
        if candidate.exists():
            shutil.copy2(candidate, target_directory / candidate.name)
    return target


def verify_runtime_evidence(
    runtime_dir: str | Path,
    evidence: Mapping[str, object],
    *,
    expected_commit: str,
) -> dict[str, object]:
    runtime = Path(runtime_dir)
    if not runtime.is_absolute():
        raise ValueError("runtime directory must be absolute")
    runtime = runtime.resolve(strict=True)
    _expect(runtime.is_dir(), "runtime directory is not a directory")
    requests, responses, _trace_health = _validated_evidence(evidence)
    _expect(
        _normalized_runtime_identity(str(evidence.get("runtime_dir") or "")) == str(runtime),
        "smoke evidence runtime identity mismatch",
    )
    evidence_commit = _normalized_commit(evidence.get("expected_commit"))
    _expect(
        evidence_commit == _normalized_commit(expected_commit),
        "smoke evidence commit identity mismatch",
    )
    request_by_name = {item["name"]: item["request_id"] for item in requests}
    response_by_name = {item["name"]: item["response_id"] for item in responses}
    audit_now = datetime.now(UTC)

    trace_connection: sqlite3.Connection | None = None
    response_connection: sqlite3.Connection | None = None
    original_trace_path = runtime / "trace_events.sqlite3"
    original_response_path = runtime / "responses.sqlite3"
    before_file_state = {
        "trace": _sqlite_file_state(original_trace_path),
        "response": _sqlite_file_state(original_response_path),
    }
    snapshot_directory = tempfile.TemporaryDirectory(prefix="tiku-8896-sqlite-evidence-")
    snapshot_root = Path(snapshot_directory.name)
    try:
        trace_path = _copy_sqlite_snapshot(original_trace_path, snapshot_root)
        response_path = _copy_sqlite_snapshot(original_response_path, snapshot_root)
    except BaseException:
        snapshot_directory.cleanup()
        raise
    try:
        trace_connection = _open_read_only_sqlite(trace_path)
        response_connection = _open_read_only_sqlite(response_path)
        _expect_sqlite_index(
            trace_connection,
            name="idx_trace_events_one_terminal",
            table="trace_events",
            columns=("trace_id",),
            predicate=(
                "event_type IN ('public_response_finalized', 'request_failed')"
            ),
        )
        _expect_sqlite_index(
            response_connection,
            name="idx_public_responses_trace",
            table="public_responses",
            columns=("trace_id",),
        )
        trace_event_count = 0
        terminals: dict[str, sqlite3.Row] = {}
        terminal_attributes: dict[str, dict[str, object]] = {}
        traces: dict[str, str] = {}
        for request in requests:
            name = request["name"]
            request_id = request["request_id"]
            expected = TRACE_EXPECTATION_BY_NAME[name]
            rows = trace_connection.execute(
                "SELECT rowid, * FROM trace_events WHERE request_id = ? "
                "ORDER BY occurred_at ASC, rowid ASC",
                (request_id,),
            ).fetchall()
            _expect(len(rows) == 2, f"{name} trace does not contain exactly two events")
            _expect(len({str(row["trace_id"]) for row in rows}) == 1, f"{name} trace identity is ambiguous")
            trace_id = str(rows[0]["trace_id"])
            trace_rows = trace_connection.execute(
                "SELECT request_id FROM trace_events WHERE trace_id = ?",
                (trace_id,),
            ).fetchall()
            _expect(
                len(trace_rows) == 2
                and all(str(row["request_id"]) == request_id for row in trace_rows),
                f"{name} trace contains events outside its request identity",
            )
            trace_event_count += len(trace_rows)
            _expect(all(int(row["schema_version"]) == 1 for row in rows), f"{name} trace schema mismatch")
            received = [row for row in rows if row["event_type"] == "request_received"]
            terminal = [row for row in rows if row["event_type"] in TERMINAL_EVENT_TYPES]
            _expect(len(received) == 1 and rows[0]["event_type"] == "request_received", f"{name} request trace is missing")
            _expect(len(terminal) == 1 and rows[-1]["event_type"] in TERMINAL_EVENT_TYPES, f"{name} terminal trace is missing")
            _expect(terminal[0]["event_type"] == "public_response_finalized", f"{name} ended as a failed request")
            _expect(received[0]["stage"] == "http_request" and received[0]["outcome"] == "started", f"{name} request trace fields mismatch")
            try:
                received_attrs = json.loads(str(received[0]["safe_attributes_json"]))
                terminal_attrs = json.loads(str(terminal[0]["safe_attributes_json"]))
            except json.JSONDecodeError as exc:
                raise SmokeVerificationError(f"{name} trace attributes are invalid JSON") from exc
            _expect(
                received_attrs
                == {
                    "method": expected.method,
                    "endpoint": expected.endpoint,
                    "response_mode": expected.response_mode,
                },
                f"{name} request trace attributes mismatch",
            )
            for key, value in {
                "endpoint": expected.endpoint,
                "response_mode": expected.response_mode,
                "http_status": expected.http_status,
            }.items():
                _expect(terminal_attrs.get(key) == value, f"{name} terminal attribute {key} mismatch")
            _expect(terminal[0]["stage"] == expected.terminal_stage, f"{name} terminal stage mismatch")
            _expect(terminal[0]["outcome"] == expected.terminal_outcome, f"{name} terminal outcome mismatch")
            for column, value in {
                "protocol_status": expected.protocol_status,
                "protocol_layer": expected.protocol_layer,
                "protocol_code": expected.protocol_code,
                "protocol_action": "",
            }.items():
                _expect(str(terminal[0][column] or "") == value, f"{name} {column} mismatch")
            expected_retryable = 0 if expected.protocol_status else None
            _expect(terminal[0]["protocol_retryable"] == expected_retryable, f"{name} protocol retryable mismatch")
            expected_response_id = response_by_name.get(name, "")
            _expect(str(terminal[0]["response_id"] or "") == expected_response_id, f"{name} terminal response ID mismatch")
            terminals[name] = terminal[0]
            terminal_attributes[name] = terminal_attrs
            traces[name] = trace_id
        _expect(
            len(set(traces.values())) == len(requests),
            "smoke requests reused a trace identity",
        )

        response_count = int(
            response_connection.execute("SELECT COUNT(*) FROM public_responses").fetchone()[0]
        )
        _expect(response_count == len(RESPONSE_EXPECTATIONS), "fresh runtime contains unexpected response rows")
        response_owners: set[tuple[str, str]] = set()
        for name, response_id in response_by_name.items():
            row = response_connection.execute(
                "SELECT * FROM public_responses WHERE response_id = ?",
                (response_id,),
            ).fetchone()
            _expect(row is not None, f"{name} response row is missing")
            expected = RESPONSE_EXPECTATIONS[name]
            _expect(int(row["schema_version"]) == 1, f"{name} response schema mismatch")
            _expect(str(row["request_id"]) == request_by_name[name], f"{name} response request ID mismatch")
            _expect(str(row["trace_id"]) == traces[name], f"{name} response trace ID mismatch")
            identity_key = str(row["identity_key"] or "")
            session_key = str(row["session_key"] or "")
            _expect(identity_key == "local", f"{name} response identity mismatch")
            _expect(
                bool(_SESSION_KEY_RE.fullmatch(session_key)),
                f"{name} response session key is invalid",
            )
            response_owners.add((identity_key, session_key))
            for column in (
                "status",
                "layer",
                "code",
                "response_mode",
                "intent",
                "text_length",
            ):
                _expect(row[column] == expected[column], f"{name} response {column} mismatch")
            for column, value in {
                "retryable": 0,
                "action": "",
                "workflow_search_id": "",
                "search_id": "",
                "unit_id": "",
                "phase": "IDLE",
                "task_revision": 0,
                "candidate_count": 0,
                "chapter": "",
                "image_route": "",
                "media_status": "",
                "image_count": 0,
            }.items():
                _expect(row[column] == value, f"{name} response {column} mismatch")
            for column, maximum in (("text_length", 10_000_000), ("duration_ms", 86_400_000)):
                value = row[column]
                _expect(
                    type(value) is int and 0 <= value <= maximum,
                    f"{name} response {column} mismatch",
                )
            try:
                created_at = datetime.fromisoformat(str(row["created_at"]))
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
                if (
                    created_at.tzinfo is None
                    or expires_at.tzinfo is None
                    or created_at.utcoffset() != timedelta(0)
                    or expires_at.utcoffset() != timedelta(0)
                ):
                    raise ValueError("response timestamps must use UTC")
                created_at = created_at.astimezone(UTC)
                expires_at = expires_at.astimezone(UTC)
            except (TypeError, ValueError) as exc:
                raise SmokeVerificationError(f"{name} response timestamps are invalid") from exc
            _expect(
                created_at <= audit_now < expires_at
                and expires_at - created_at == timedelta(days=30),
                f"{name} response retention window mismatch",
            )
            terminal = terminals[name]
            _expect(
                str(terminal["identity_key"] or "") == identity_key,
                f"{name} response/trace identity mismatch",
            )
            _expect(
                str(terminal["session_key"] or "") == session_key,
                f"{name} response/trace session mismatch",
            )
            for column in ("workflow_search_id", "search_id", "unit_id"):
                _expect(
                    str(terminal[column] or "") == str(row[column] or ""),
                    f"{name} response/trace {column} mismatch",
                )
            for response_column, trace_column in (
                ("status", "protocol_status"),
                ("layer", "protocol_layer"),
                ("code", "protocol_code"),
                ("retryable", "protocol_retryable"),
                ("action", "protocol_action"),
            ):
                _expect(row[response_column] == terminal[trace_column], f"{name} response/trace protocol mismatch")
            terminal_attrs = terminal_attributes[name]
            if name in {"stale_action_json", "stale_action_stream"}:
                _expect(
                    all(
                        column in terminal_attrs
                        for column in ("intent", "image_count", "text_length")
                    ),
                    f"{name} terminal projection attributes are missing",
                )
            for column, default in (
                ("intent", ""),
                ("media_status", ""),
                ("image_count", 0),
                ("text_length", 0),
            ):
                if column in terminal_attrs:
                    _expect(
                        row[column] == terminal_attrs.get(column, default),
                        f"{name} response/trace {column} mismatch",
                    )
        _expect(len(response_owners) == 1, "smoke responses do not share one owner session")
    finally:
        if trace_connection is not None:
            trace_connection.close()
        if response_connection is not None:
            response_connection.close()
        snapshot_directory.cleanup()

    after_file_state = {
        "trace": _sqlite_file_state(original_trace_path),
        "response": _sqlite_file_state(original_response_path),
    }
    _expect(before_file_state == after_file_state, "runtime databases changed during offline verification")

    return {
        "schema": EVIDENCE_SCHEMA,
        "expected_commit": evidence_commit,
        "runtime_dir": str(runtime),
        "runtime_evidence": "ok",
        "trace_request_count": len(requests),
        "trace_event_count": trace_event_count,
        "response_count": len(responses),
    }


def verify_task_state_8896(
    *,
    base_url: str = BASE_URL,
    timeout_seconds: float = 5.0,
    transport: Transport | None = None,
    request_id_factory: RequestIdFactory | None = None,
    expected_commit: str,
    runtime_dir: str | Path,
) -> dict[str, object]:
    """Run the deterministic, no-image, no-model 8896 contract smoke."""

    _strict_base_url(base_url)
    commit_identity = _normalized_commit(expected_commit)
    runtime_identity = _normalized_runtime_identity(runtime_dir)
    if not 1.0 <= timeout_seconds <= 30.0:
        raise ValueError("timeout_seconds must be between 1 and 30")
    client = Local8896Client(
        timeout_seconds=timeout_seconds,
        transport=transport,
        request_id_factory=request_id_factory,
    )
    primary_error: BaseException | None = None
    try:
        health_response, health_payload = client.request_json(
            "GET",
            "/health",
            evidence_name="health_initial",
        )
        _expect_status(health_response, 200, "/health")
        _expect_security_headers(health_response, "/health", api=False)
        _expect_trace_health(health_payload, "/health")

        no_cookie_error, no_cookie_payload = client.request_json(
            "POST",
            "/api/message",
            raw_body=b"not-json",
            evidence_name="message_invalid_without_session",
        )
        _expect_status(no_cookie_error, 400, "/api/message no-cookie error")
        no_cookie_request_id = _expect_security_headers(
            no_cookie_error,
            "/api/message no-cookie error",
            api=True,
        )
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

        session_response, session_payload = client.request_json(
            "GET",
            "/api/session",
            evidence_name="session_initial",
        )
        _expect_status(session_response, 200, "/api/session")
        _expect_security_headers(session_response, "/api/session", api=True)
        first_cookie = _expect_live_session_cookie(session_response, "/api/session")
        _expect(client.cookies.get(SESSION_COOKIE) == first_cookie, "session cookie was not retained")
        session_state = _expect_empty_task_state(session_payload, "/api/session")
        _expect(session_payload.get("uploaded_image") == "", "/api/session unexpectedly exposed an image")
        legacy_session = session_payload.get("session")
        _expect(type(legacy_session) is dict, "/api/session legacy session is missing")
        _expect("task_state" not in legacy_session, "legacy session nested task_state")

        page_response = client.request(
            "GET",
            "/",
            headers={"Accept": "text/html"},
            evidence_name="page",
        )
        _expect_status(page_response, 200, "/")
        _expect_media_type(page_response, "text/html", "/")
        _expect_security_headers(page_response, "/", api=False)
        _expect_no_store(page_response, "/")
        _expect(
            _expect_live_session_cookie(page_response, "/") == first_cookie,
            "/ changed the established session cookie",
        )
        _verify_frontend_assets(client, page_response)

        stale_payload = {
            "text": "\u7ee7\u7eed",
            "action_context": {"type": "unsupported_action"},
        }
        json_response, json_payload = client.request_json(
            "POST",
            "/api/message",
            payload=stale_payload,
            evidence_name="stale_action_json",
        )
        _expect_status(json_response, 200, "/api/message stale action")
        json_request_id = _expect_security_headers(
            json_response,
            "/api/message stale action",
            api=True,
        )
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
            headers={
                "Accept": "application/x-ndjson",
                "Content-Type": "application/json",
            },
            evidence_name="stale_action_stream",
        )
        _expect_status(stream_response, 200, "/api/message/stream stale action")
        _expect_media_type(stream_response, "application/x-ndjson", "/api/message/stream")
        stream_request_id = _expect_security_headers(
            stream_response,
            "/api/message/stream stale action",
            api=True,
        )
        events = _parse_ndjson(stream_response, "/api/message/stream")
        _expect(len(events) == 1, "stale-action stream must contain exactly one event")
        terminal = events[0]
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
            evidence_name="message_invalid_with_session",
        )
        _expect_status(cookie_error, 400, "/api/message session error")
        cookie_error_request_id = _expect_security_headers(
            cookie_error,
            "/api/message session error",
            api=True,
        )
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

        parity_response, parity_payload = client.request_json(
            "GET",
            "/api/session",
            evidence_name="session_parity",
        )
        _expect_status(parity_response, 200, "/api/session parity")
        _expect_security_headers(parity_response, "/api/session parity", api=True)
        _expect(
            _expect_live_session_cookie(parity_response, "/api/session parity") == first_cookie,
            "parity session changed the session cookie",
        )
        parity_state = _expect_empty_task_state(parity_payload, "/api/session parity")
        _expect(
            session_state == json_state == stream_state == error_state == parity_state,
            "session/JSON/stream/error task-state parity failed",
        )

        _verified_reset(client, evidence_name="reset_primary", path_label="/api/reset")

        post_reset_response, post_reset_payload = client.request_json(
            "GET",
            "/api/session",
            evidence_name="session_after_reset",
        )
        _expect_status(post_reset_response, 200, "/api/session after reset")
        _expect_security_headers(post_reset_response, "/api/session after reset", api=True)
        second_cookie = _expect_live_session_cookie(post_reset_response, "/api/session after reset")
        _expect(second_cookie != first_cookie, "/api/reset did not retire the prior session identity")
        _expect_empty_task_state(post_reset_payload, "/api/session after reset")

        _verified_reset(
            client,
            evidence_name="reset_cleanup",
            path_label="/api/reset cleanup",
        )

        # One bounded, network-free drain window lets all preceding request
        # events reach SQLite without manufacturing evidence through polling.
        time.sleep(min(0.2, timeout_seconds / 5.0))
        final_health_response, final_health_payload = client.request_json(
            "GET",
            "/health",
            evidence_name="health_final",
        )
        _expect_status(final_health_response, 200, "/health final")
        _expect_security_headers(final_health_response, "/health final", api=False)
        final_trace_health = _expect_trace_health(final_health_payload, "/health final")
        _expect(final_trace_health["pending"] <= 1, "trace writer did not drain the smoke backlog")
        _expect(
            tuple(item["name"] for item in client.requests)
            == tuple(item.name for item in TRACE_EXPECTATIONS),
            "smoke request evidence is incomplete",
        )
        evidence: dict[str, object] = {
            "schema": EVIDENCE_SCHEMA,
            "base_url": BASE_URL,
            "expected_commit": commit_identity,
            "runtime_dir": runtime_identity,
            "requests": [dict(item) for item in client.requests],
            "responses": [
                {"name": "stale_action_json", "response_id": json_response_id},
                {"name": "stale_action_stream", "response_id": stream_response_id},
                {
                    "name": "message_invalid_with_session",
                    "response_id": error_response_id,
                },
            ],
            "trace_health": final_trace_health,
            "health": "ok",
            "frontend_checkout_assets": "ok",
            "canonical_empty_v1": "ok",
            "no_session_error_boundary": "ok",
            "session_json_stream_error_parity": "ok",
            "reset_cookie_cleanup": "ok",
            "security_headers": "ok",
            "sqlite_evidence": "pending_offline_verification",
        }
        _validated_evidence(evidence)
        # Give the final health terminal a bounded chance to leave the async
        # queue. The post-stop SQLite verifier still fails if it is absent.
        time.sleep(min(0.2, timeout_seconds / 5.0))
        return evidence
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if SESSION_COOKIE in client.cookies:
            try:
                _verified_reset(
                    client,
                    evidence_name=f"failure_cleanup_{len(client.requests) + 1}",
                    path_label="/api/reset failure cleanup",
                )
            except BaseException:
                if primary_error is None:
                    raise


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the deterministic TaskStateSnapshotV1 exits on fixed local port 8896"
    )
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--expected-commit")
    parser.add_argument("--runtime-identity", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--evidence-input", type=Path)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    try:
        offline_values = (args.runtime_dir, args.evidence_input)
        if any(value is not None for value in offline_values):
            if not all(value is not None for value in offline_values):
                raise ValueError("--runtime-dir and --evidence-input must be used together")
            if args.evidence_output is not None:
                raise ValueError("--evidence-output is only valid for the live HTTP smoke")
            if args.expected_commit is None:
                raise ValueError("--expected-commit is required for offline evidence verification")
            result = verify_runtime_evidence(
                args.runtime_dir,
                load_smoke_evidence(args.evidence_input),
                expected_commit=args.expected_commit,
            )
        else:
            if args.expected_commit is None or args.runtime_identity is None:
                raise ValueError(
                    "--expected-commit and --runtime-identity are required for the live smoke"
                )
            result = verify_task_state_8896(
                base_url=args.base_url,
                timeout_seconds=args.timeout_seconds,
                expected_commit=args.expected_commit,
                runtime_dir=args.runtime_identity,
            )
            if args.evidence_output is not None:
                write_smoke_evidence(args.evidence_output, result)
    except (OSError, ValueError, SmokeVerificationError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

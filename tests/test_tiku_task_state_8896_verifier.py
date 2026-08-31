from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.verify_tiku_task_state_8896 import (
    BASE_URL,
    CANONICAL_EMPTY_TASK_STATE,
    SESSION_COOKIE,
    HttpResponse,
    SmokeVerificationError,
    _strict_base_url,
    verify_task_state_8896,
)


_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'"
)


def _security_headers(
    content_type: str,
    *,
    request_id: str = "",
    set_cookie: str = "",
) -> list[tuple[str, str]]:
    headers = [
        ("Content-Type", content_type),
        ("Content-Security-Policy", _CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ]
    if request_id:
        headers.extend(
            [
                ("Cache-Control", "private, no-store"),
                ("X-Request-ID", request_id),
            ]
        )
    if set_cookie:
        headers.append(("Set-Cookie", set_cookie))
    return headers


def _json_response(
    status: int,
    payload: object,
    *,
    request_id: str = "",
    set_cookie: str = "",
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=tuple(
            _security_headers(
                "application/json",
                request_id=request_id,
                set_cookie=set_cookie,
            )
        ),
        body=json.dumps(payload, ensure_ascii=True).encode("ascii"),
    )


def _request_id(number: int) -> str:
    return f"req_{number:032x}"


def _live_cookie(value: str) -> str:
    return f"{SESSION_COOKIE}={value}; HttpOnly; Max-Age=7200; Path=/; SameSite=lax"


def _deleted_cookie() -> str:
    return (
        f'{SESSION_COOKIE}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; '
        "HttpOnly; Max-Age=0; Path=/; SameSite=lax"
    )


def _health(written: int, **overrides: object) -> dict[str, object]:
    trace = {
        "status": "ok",
        "written": written,
        "dropped": 0,
        "write_failures": 0,
        "validation_rejections": 0,
        "duplicate_terminals": 0,
        "pending": 0,
        "queue_capacity": 1024,
        "accepting": True,
        "last_failure_kind": "",
        "last_failure_at": "",
    }
    trace.update(overrides)
    return {"status": "ok", "trace_events": trace}


def _session_payload() -> dict[str, object]:
    return {
        "uploaded_image": "",
        "session": {"session_valid": False, "phase": "IDLE"},
        "task_state": deepcopy(CANONICAL_EMPTY_TASK_STATE),
    }


def _stale_payload(request_id: str, response_number: int) -> dict[str, object]:
    return {
        "text": "stale",
        "images": [],
        "media": {},
        "intent": "stale_action",
        "status": "NEEDS_INPUT",
        "layer": "session",
        "code": "STALE_ACTION",
        "retryable": False,
        "action": "",
        "request_id": request_id,
        "search_id": "",
        "schema_version": 1,
        "response_id": f"resp_{response_number:032x}",
        "task_state": deepcopy(CANONICAL_EMPTY_TASK_STATE),
    }


def _reset_payload(request_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "status": "SUCCESS",
        "layer": "session",
        "code": "SESSION_RESET",
        "retryable": False,
        "action": "",
        "request_id": request_id,
        "search_id": "",
        "schema_version": 1,
        "task_state": deepcopy(CANONICAL_EMPTY_TASK_STATE),
    }


def _message_error_payload(
    request_id: str,
    *,
    response_number: int | None = None,
    task_state: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "detail": "\u8bf7\u6c42\u5185\u5bb9\u65e0\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4\u3002",
        "status": "NEEDS_INPUT",
        "layer": "session",
        "code": "MESSAGE_INVALID",
        "retryable": False,
        "action": "",
        "request_id": request_id,
        "search_id": "",
        "schema_version": 1,
    }
    if response_number is not None:
        payload["response_id"] = f"resp_{response_number:032x}"
    if task_state:
        payload["task_state"] = deepcopy(CANONICAL_EMPTY_TASK_STATE)
    return payload


class FixtureTransport:
    def __init__(self) -> None:
        cookie_a = "a" * 32
        cookie_b = "b" * 32
        json_payload = _stale_payload(_request_id(3), 1)
        stream_payload = _stale_payload(_request_id(4), 2)
        self.responses = [
            _json_response(200, _health(0)),
            _json_response(
                400,
                _message_error_payload(_request_id(1), task_state=False),
                request_id=_request_id(1),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(2),
                set_cookie=_live_cookie(cookie_a),
            ),
            HttpResponse(
                status=200,
                headers=tuple(
                    _security_headers(
                        "text/html; charset=utf-8",
                        set_cookie=_live_cookie(cookie_a),
                    )
                ),
                body=(
                    b'<html><script src="/assets/task_state.js"></script>'
                    b'<script src="/assets/demo.js"></script></html>'
                ),
            ),
            _json_response(
                200,
                json_payload,
                request_id=_request_id(3),
                set_cookie=_live_cookie(cookie_a),
            ),
            HttpResponse(
                status=200,
                headers=tuple(
                    _security_headers(
                        "application/x-ndjson",
                        request_id=_request_id(4),
                        set_cookie=_live_cookie(cookie_a),
                    )
                ),
                body=(
                    json.dumps(
                        {"type": "result", "data": stream_payload},
                        ensure_ascii=True,
                    ).encode("ascii")
                    + b"\n"
                ),
            ),
            _json_response(
                400,
                _message_error_payload(
                    _request_id(5),
                    response_number=3,
                    task_state=True,
                ),
                request_id=_request_id(5),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(6),
                set_cookie=_live_cookie(cookie_a),
            ),
            _json_response(
                200,
                _reset_payload(_request_id(7)),
                request_id=_request_id(7),
                set_cookie=_deleted_cookie(),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(8),
                set_cookie=_live_cookie(cookie_b),
            ),
            _json_response(
                200,
                _reset_payload(_request_id(9)),
                request_id=_request_id(9),
                set_cookie=_deleted_cookie(),
            ),
            _json_response(200, _health(24)),
        ]
        self.calls: list[tuple[str, str, bytes | None, dict[str, str], float]] = []

    def __call__(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers,
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((method, path, body, dict(headers), timeout_seconds))
        if not self.responses:
            raise AssertionError("verifier made an unexpected extra request")
        return self.responses.pop(0)


class TikuTaskState8896VerifierTest(unittest.TestCase):
    def test_accepts_only_the_exact_8896_origin(self):
        self.assertEqual(_strict_base_url(BASE_URL + "/"), BASE_URL)
        for invalid in (
            "http://localhost:8896",
            "http://127.0.0.1:8895",
            "http://127.0.0.1:8897",
            "https://127.0.0.1:8896",
            "http://127.0.0.1:8896/path",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _strict_base_url(invalid)

    def test_verifies_the_complete_deterministic_contract(self):
        transport = FixtureTransport()

        result = verify_task_state_8896(transport=transport)

        self.assertEqual(result["base_url"], BASE_URL)
        self.assertEqual(result["session_json_stream_error_parity"], "ok")
        self.assertEqual(result["response_store"], "ok")
        self.assertEqual(result["trace_written_delta"], 24)
        self.assertEqual(transport.responses, [])
        self.assertEqual(
            [(method, path) for method, path, *_rest in transport.calls],
            [
                ("GET", "/health"),
                ("POST", "/api/message"),
                ("GET", "/api/session"),
                ("GET", "/"),
                ("POST", "/api/message"),
                ("POST", "/api/message/stream"),
                ("POST", "/api/message"),
                ("GET", "/api/session"),
                ("POST", "/api/reset"),
                ("GET", "/api/session"),
                ("POST", "/api/reset"),
                ("GET", "/health"),
            ],
        )
        cookie_headers = [call[3].get("Cookie", "") for call in transport.calls]
        self.assertEqual(cookie_headers[:3], ["", "", ""])
        self.assertEqual(cookie_headers[3:9], [f"{SESSION_COOKIE}={'a' * 32}"] * 6)
        self.assertEqual(cookie_headers[9], "")
        self.assertEqual(cookie_headers[10], f"{SESSION_COOKIE}={'b' * 32}")
        self.assertEqual(cookie_headers[11], "")

    def test_verifier_matches_the_real_fastapi_contract_without_a_port(self):
        from fastapi.testclient import TestClient
        from PIL import Image

        from tests.test_tiku_agent_fastapi_demo import FakeRuntime
        from tiku_agent.fastapi_demo import create_app
        from tiku_agent.feedback_store import SQLiteFeedbackStore
        from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "unused.jpg"
            Image.new("RGB", (4, 4), "white").save(image_path)
            runtime = FakeRuntime(image_path)
            recorder = TraceEventRecorder(SQLiteTraceEventStore(root / "trace.sqlite3"))
            app = create_app(
                runtime=runtime,
                session_cookie=SESSION_COOKIE,
                feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
                trace_event_recorder=recorder,
            )

            with TestClient(app) as test_client:
                def transport(method, path, body, headers, timeout_seconds):
                    del timeout_seconds
                    test_client.cookies.clear()
                    response = test_client.request(
                        method,
                        path,
                        content=body,
                        headers=dict(headers),
                    )
                    test_client.cookies.clear()
                    return HttpResponse(
                        status=response.status_code,
                        headers=tuple(response.headers.multi_items()),
                        body=response.content,
                    )

                result = verify_task_state_8896(transport=transport)

            self.assertEqual(result["canonical_empty_v1"], "ok")
            self.assertGreaterEqual(result["trace_written_delta"], 20)
            self.assertEqual(len(runtime.calls), 2)
            self.assertTrue(all(call[0] == "clear" for call in runtime.calls))

    def test_rejects_task_state_shape_drift(self):
        transport = FixtureTransport()
        payload = json.loads(transport.responses[4].body)
        payload["task_state"]["workflow"]["allowed_actions"] = ["reset_session"]
        transport.responses[4] = _json_response(
            200,
            payload,
            request_id=_request_id(3),
            set_cookie=_live_cookie("a" * 32),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "canonical empty V1"):
            verify_task_state_8896(transport=transport)

    def test_rejects_task_state_on_stream_progress(self):
        transport = FixtureTransport()
        terminal = json.loads(transport.responses[5].body.decode("ascii").strip())
        events = [
            {
                "type": "progress",
                "stage": "searching",
                "message": "working",
                "task_state": deepcopy(CANONICAL_EMPTY_TASK_STATE),
            },
            terminal,
        ]
        transport.responses[5] = HttpResponse(
            status=200,
            headers=transport.responses[5].headers,
            body=("\n".join(json.dumps(event) for event in events) + "\n").encode("ascii"),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "progress event carried"):
            verify_task_state_8896(transport=transport)

    def test_rejects_missing_security_header(self):
        transport = FixtureTransport()
        response = transport.responses[0]
        transport.responses[0] = HttpResponse(
            status=response.status,
            headers=tuple(
                (key, value)
                for key, value in response.headers
                if key.lower() != "content-security-policy"
            ),
            body=response.body,
        )

        with self.assertRaisesRegex(SmokeVerificationError, "CSP mismatch"):
            verify_task_state_8896(transport=transport)

    def test_rejects_trace_health_failures(self):
        transport = FixtureTransport()
        transport.responses[0] = _json_response(
            200,
            _health(0, write_failures=1),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "write_failures"):
            verify_task_state_8896(transport=transport)

    def test_rejects_weak_session_cookie(self):
        transport = FixtureTransport()
        transport.responses[2] = _json_response(
            200,
            _session_payload(),
            request_id=_request_id(2),
            set_cookie=f"{SESSION_COOKIE}={'a' * 32}; Max-Age=7200; Path=/; SameSite=lax",
        )

        with self.assertRaisesRegex(SmokeVerificationError, "HttpOnly"):
            verify_task_state_8896(transport=transport)

    def test_rejects_wrong_path_reset_cookie(self):
        transport = FixtureTransport()
        transport.responses[8] = _json_response(
            200,
            _reset_payload(_request_id(7)),
            request_id=_request_id(7),
            set_cookie=(
                f'{SESSION_COOKIE}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; '
                "HttpOnly; Max-Age=0; Path=/api; SameSite=lax"
            ),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "reset cookie path"):
            verify_task_state_8896(transport=transport)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_tiku_task_state_8896 import (
    BASE_URL,
    CANONICAL_EMPTY_TASK_STATE,
    EVIDENCE_SCHEMA,
    RESPONSE_EXPECTATIONS,
    SESSION_COOKIE,
    TRACE_EXPECTATIONS,
    HttpResponse,
    SmokeVerificationError,
    _strict_base_url,
    load_smoke_evidence,
    verify_runtime_evidence,
    verify_task_state_8896,
    write_smoke_evidence,
)


_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'"
)
ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "tiku_agent" / "demo_web"


def _security_headers(
    content_type: str,
    *,
    request_id: str = "",
    set_cookie: str = "",
    cache_control: str = "",
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
    elif cache_control:
        headers.append(("Cache-Control", cache_control))
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


def _request_id_factory(count: int = 64):
    values = iter(_request_id(index) for index in range(1, count + 1))
    return values.__next__


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


def _build_runtime_evidence(root: Path, evidence: dict[str, object]) -> dict[str, object]:
    from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore
    from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent

    evidence["runtime_dir"] = str(root.resolve())
    requests = {item["name"]: item["request_id"] for item in evidence["requests"]}
    trace_ids = {
        expectation.name: f"trace_{index:032x}"
        for index, expectation in enumerate(TRACE_EXPECTATIONS, start=1)
    }
    response_store = SQLiteResponseStore(root / "responses.sqlite3")
    response_ids: dict[str, str] = {}
    response_expectations = RESPONSE_EXPECTATIONS
    for name, expectation in response_expectations.items():
        record = response_store.finalize(
            ResponseProjection(
                trace_id=trace_ids[name],
                identity_key="local",
                session_key="a" * 64,
                request_id=requests[name],
                status="NEEDS_INPUT",
                layer="session",
                code=str(expectation["code"]),
                response_mode=str(expectation["response_mode"]),
                intent=str(expectation["intent"]),
                text_length=int(expectation["text_length"]),
            )
        )
        response_ids[name] = record.response_id
    evidence["responses"] = [
        {"name": name, "response_id": response_ids[name]}
        for name in response_expectations
    ]

    trace_store = SQLiteTraceEventStore(root / "trace_events.sqlite3")
    for expectation in TRACE_EXPECTATIONS:
        trace_id = trace_ids[expectation.name]
        request_id = requests[expectation.name]
        trace_store.write(
            # The live terminal carries these stable public projection fields.
            TraceEvent.create(
                trace_id=trace_id,
                event_type="request_received",
                stage="http_request",
                outcome="started",
                request_id=request_id,
                safe_attributes={
                    "method": expectation.method,
                    "endpoint": expectation.endpoint,
                    "response_mode": expectation.response_mode,
                },
            )
        )
        protocol = None
        if expectation.protocol_status:
            protocol = {
                "status": expectation.protocol_status,
                "layer": expectation.protocol_layer,
                "code": expectation.protocol_code,
                "retryable": False,
                "action": "",
            }
        trace_store.write(
            TraceEvent.create(
                trace_id=trace_id,
                event_type="public_response_finalized",
                stage=expectation.terminal_stage,
                outcome=expectation.terminal_outcome,
                request_id=request_id,
                response_id=response_ids.get(expectation.name, ""),
                identity_key="local" if expectation.name in response_ids else "",
                session_key="a" * 64 if expectation.name in response_ids else "",
                protocol=protocol,
                safe_attributes={
                    "endpoint": expectation.endpoint,
                    "response_mode": expectation.response_mode,
                    "http_status": expectation.http_status,
                    **(
                        {
                            "intent": response_expectations[expectation.name]["intent"],
                            "image_count": 0,
                            "text_length": response_expectations[expectation.name][
                                "text_length"
                            ],
                        }
                        if expectation.name in response_expectations
                        else {}
                    ),
                },
            )
        )
    return evidence


class FixtureTransport:
    def __init__(self) -> None:
        cookie_a = "a" * 32
        cookie_b = "b" * 32
        json_payload = _stale_payload(_request_id(7), 1)
        stream_payload = _stale_payload(_request_id(8), 2)
        self.responses = [
            _json_response(200, _health(0)),
            _json_response(
                400,
                _message_error_payload(_request_id(2), task_state=False),
                request_id=_request_id(2),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(3),
                set_cookie=_live_cookie(cookie_a),
            ),
            HttpResponse(
                status=200,
                headers=tuple(
                    _security_headers(
                        "text/html; charset=utf-8",
                        set_cookie=_live_cookie(cookie_a),
                        cache_control="no-store",
                    )
                ),
                body=(
                    (WEB_ROOT / "index.html")
                    .read_text(encoding="utf-8")
                    .encode("utf-8")
                ),
            ),
            HttpResponse(
                status=200,
                headers=tuple(_security_headers("text/javascript; charset=utf-8")),
                body=(WEB_ROOT / "task_state.js").read_bytes(),
            ),
            HttpResponse(
                status=200,
                headers=tuple(_security_headers("text/javascript; charset=utf-8")),
                body=(WEB_ROOT / "demo.js").read_bytes(),
            ),
            _json_response(
                200,
                json_payload,
                request_id=_request_id(7),
                set_cookie=_live_cookie(cookie_a),
            ),
            HttpResponse(
                status=200,
                headers=tuple(
                    _security_headers(
                        "application/x-ndjson",
                        request_id=_request_id(8),
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
                    _request_id(9),
                    response_number=3,
                    task_state=True,
                ),
                request_id=_request_id(9),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(10),
                set_cookie=_live_cookie(cookie_a),
            ),
            _json_response(
                200,
                _reset_payload(_request_id(11)),
                request_id=_request_id(11),
                set_cookie=_deleted_cookie(),
            ),
            _json_response(
                200,
                _session_payload(),
                request_id=_request_id(12),
                set_cookie=_live_cookie(cookie_b),
            ),
            _json_response(
                200,
                _reset_payload(_request_id(13)),
                request_id=_request_id(13),
                set_cookie=_deleted_cookie(),
            ),
            _json_response(200, _health(28)),
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
    @staticmethod
    def _verify_fixture(transport: FixtureTransport):
        return verify_task_state_8896(
            transport=transport,
            request_id_factory=_request_id_factory(),
            expected_commit="a" * 40,
            runtime_dir=ROOT / ".tmp_fixture_task_state_8896",
        )

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

        result = self._verify_fixture(transport)

        self.assertEqual(result["base_url"], BASE_URL)
        self.assertEqual(result["session_json_stream_error_parity"], "ok")
        self.assertEqual(result["frontend_checkout_assets"], "ok")
        self.assertEqual(result["sqlite_evidence"], "pending_offline_verification")
        self.assertEqual(len(result["requests"]), len(TRACE_EXPECTATIONS))
        self.assertEqual(result["trace_health"]["written"], 28)
        self.assertEqual(transport.responses, [])
        self.assertEqual(
            [(method, path) for method, path, *_rest in transport.calls],
            [
                ("GET", "/health"),
                ("POST", "/api/message"),
                ("GET", "/api/session"),
                ("GET", "/"),
                ("GET", "/assets/task_state.js?v=20260830-task-state-3-4-5"),
                ("GET", "/assets/demo.js?v=20260903-answer-session-v9"),
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
        self.assertEqual(cookie_headers[3:11], [f"{SESSION_COOKIE}={'a' * 32}"] * 8)
        self.assertEqual(cookie_headers[11], "")
        self.assertEqual(cookie_headers[12], f"{SESSION_COOKIE}={'b' * 32}")
        self.assertEqual(cookie_headers[13], "")
        self.assertEqual(
            [call[3]["X-Request-ID"] for call in transport.calls],
            [_request_id(index) for index in range(1, 15)],
        )

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
            recorder = TraceEventRecorder(
                SQLiteTraceEventStore(root / "trace_events.sqlite3")
            )
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

                result = verify_task_state_8896(
                    transport=transport,
                    expected_commit="a" * 40,
                    runtime_dir=root,
                )

            self.assertEqual(result["canonical_empty_v1"], "ok")
            runtime_evidence = verify_runtime_evidence(
                root,
                result,
                expected_commit="a" * 40,
            )
            self.assertEqual(runtime_evidence["runtime_evidence"], "ok")
            self.assertEqual(runtime_evidence["trace_request_count"], 14)
            self.assertEqual(runtime_evidence["trace_event_count"], 28)
            self.assertEqual(runtime_evidence["response_count"], 3)
            self.assertEqual(len(runtime.calls), 2)
            self.assertTrue(all(call[0] == "clear" for call in runtime.calls))

    def test_rejects_task_state_shape_drift(self):
        transport = FixtureTransport()
        payload = json.loads(transport.responses[6].body)
        payload["task_state"]["workflow"]["allowed_actions"] = ["reset_session"]
        transport.responses[6] = _json_response(
            200,
            payload,
            request_id=_request_id(7),
            set_cookie=_live_cookie("a" * 32),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "canonical empty V1"):
            self._verify_fixture(transport)

    def test_rejects_task_state_on_stream_progress(self):
        transport = FixtureTransport()
        terminal = json.loads(transport.responses[7].body.decode("ascii").strip())
        events = [
            {
                "type": "progress",
                "stage": "searching",
                "message": "working",
            },
            terminal,
        ]
        transport.responses[7] = HttpResponse(
            status=200,
            headers=transport.responses[7].headers,
            body=("\n".join(json.dumps(event) for event in events) + "\n").encode("ascii"),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "exactly one event"):
            self._verify_fixture(transport)

    def test_rejects_cacheable_root_html(self):
        transport = FixtureTransport()
        page = transport.responses[3]
        transport.responses[3] = HttpResponse(
            status=page.status,
            headers=tuple(
                (key, value)
                for key, value in page.headers
                if key.lower() != "cache-control"
            ),
            body=page.body,
        )

        with self.assertRaisesRegex(SmokeVerificationError, "not no-store"):
            self._verify_fixture(transport)

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
            self._verify_fixture(transport)

    def test_rejects_duplicate_response_request_id_headers(self):
        transport = FixtureTransport()
        response = transport.responses[1]
        transport.responses[1] = HttpResponse(
            status=response.status,
            headers=response.headers + (("X-Request-ID", _request_id(99)),),
            body=response.body,
        )

        with self.assertRaisesRegex(SmokeVerificationError, "uniquely echo"):
            self._verify_fixture(transport)

    def test_rejects_trace_health_failures(self):
        transport = FixtureTransport()
        transport.responses[0] = _json_response(
            200,
            _health(0, write_failures=1),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "write_failures"):
            self._verify_fixture(transport)

    def test_rejects_weak_session_cookie(self):
        transport = FixtureTransport()
        transport.responses[2] = _json_response(
            200,
            _session_payload(),
            request_id=_request_id(3),
            set_cookie=f"{SESSION_COOKIE}={'a' * 32}; Max-Age=7200; Path=/; SameSite=lax",
        )

        with self.assertRaisesRegex(SmokeVerificationError, "HttpOnly"):
            self._verify_fixture(transport)

    def test_rejects_wrong_path_reset_cookie(self):
        transport = FixtureTransport()
        transport.responses[10] = _json_response(
            200,
            _reset_payload(_request_id(11)),
            request_id=_request_id(11),
            set_cookie=(
                f'{SESSION_COOKIE}=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; '
                "HttpOnly; Max-Age=0; Path=/api; SameSite=lax"
            ),
        )

        with self.assertRaisesRegex(SmokeVerificationError, "reset cookie path"):
            self._verify_fixture(transport)

    def test_fetches_and_rejects_frontend_asset_content_drift(self):
        transport = FixtureTransport()
        asset = transport.responses[4]
        transport.responses[4] = HttpResponse(
            status=asset.status,
            headers=asset.headers,
            body=asset.body + b"\n// drift",
        )

        with self.assertRaisesRegex(SmokeVerificationError, "fixed checkout"):
            self._verify_fixture(transport)

        self.assertEqual(transport.calls[-1][:2], ("POST", "/api/reset"))

    def test_failure_cleanup_does_not_mask_the_primary_error(self):
        transport = FixtureTransport()
        page = transport.responses[3]
        transport.responses[3] = HttpResponse(
            status=page.status,
            headers=page.headers,
            body=b"<html>wrong checkout</html>",
        )

        with self.assertRaisesRegex(SmokeVerificationError, "fixed checkout HTML"):
            self._verify_fixture(transport)

        self.assertEqual(transport.calls[-1][:2], ("POST", "/api/reset"))

    def test_rejects_reused_outbound_request_ids(self):
        transport = FixtureTransport()

        with self.assertRaisesRegex(SmokeVerificationError, "reused a value"):
            verify_task_state_8896(
                transport=transport,
                request_id_factory=lambda: _request_id(1),
                expected_commit="a" * 40,
                runtime_dir=ROOT / ".tmp_fixture_task_state_8896",
            )

        self.assertEqual(len(transport.calls), 1)

    def test_runtime_evidence_round_trip_is_request_scoped(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            evidence_path = root / "smoke-evidence.json"

            write_smoke_evidence(evidence_path, evidence)
            loaded = load_smoke_evidence(evidence_path)
            with self.assertRaisesRegex(SmokeVerificationError, "commit identity mismatch"):
                verify_runtime_evidence(
                    root,
                    loaded,
                    expected_commit="b" * 40,
                )
            result = verify_runtime_evidence(
                root,
                loaded,
                expected_commit="a" * 40,
            )

            self.assertEqual(result["schema"], EVIDENCE_SCHEMA)
            self.assertEqual(result["trace_request_count"], 14)
            self.assertEqual(result["trace_event_count"], 28)
            self.assertEqual(result["response_count"], 3)

    def test_runtime_evidence_rejects_a_missing_request_terminal(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            request_id = evidence["requests"][0]["request_id"]
            connection = sqlite3.connect(root / "trace_events.sqlite3")
            try:
                with connection:
                    connection.execute(
                        "DELETE FROM trace_events WHERE request_id = ? "
                        "AND event_type = 'public_response_finalized'",
                        (request_id,),
                    )
            finally:
                connection.close()

            with self.assertRaisesRegex(
                SmokeVerificationError,
                "exactly two events",
            ):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_extra_event_on_expected_trace(self):
        from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent

        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            request_id = evidence["requests"][0]["request_id"]
            with closing(sqlite3.connect(root / "trace_events.sqlite3")) as connection:
                trace_id = str(
                    connection.execute(
                        "SELECT trace_id FROM trace_events WHERE request_id = ? LIMIT 1",
                        (request_id,),
                    ).fetchone()[0]
                )
            SQLiteTraceEventStore(root / "trace_events.sqlite3").write(
                TraceEvent.create(
                    trace_id=trace_id,
                    event_type="stage_started",
                    stage="smoke",
                    outcome="started",
                    request_id="req_" + "f" * 32,
                    safe_attributes={"operation": "smoke", "attempt_count": 1},
                )
            )

            with self.assertRaisesRegex(
                SmokeVerificationError,
                "outside its request identity",
            ):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_an_unexpected_response_row(self):
        from tiku_shared.response_store import ResponseProjection, SQLiteResponseStore

        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            SQLiteResponseStore(root / "responses.sqlite3").finalize(
                ResponseProjection(
                    trace_id="trace_ffffffffffffffffffffffffffffffff",
                    identity_key="local",
                    session_key="b" * 64,
                    request_id="req_ffffffffffffffffffffffffffffffff",
                    status="NEEDS_INPUT",
                    layer="session",
                    code="MESSAGE_INVALID",
                    response_mode="json",
                    intent="request_error",
                )
            )

            with self.assertRaisesRegex(SmokeVerificationError, "unexpected response rows"):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_response_owner_session_drift(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            response_id = evidence["responses"][0]["response_id"]
            with closing(sqlite3.connect(root / "responses.sqlite3")) as connection:
                connection.execute(
                    "UPDATE public_responses SET session_key = ? WHERE response_id = ?",
                    ("b" * 64, response_id),
                )
                connection.commit()
            with closing(sqlite3.connect(root / "trace_events.sqlite3")) as connection:
                connection.execute(
                    "UPDATE trace_events SET session_key = ? WHERE response_id = ?",
                    ("b" * 64, response_id),
                )
                connection.commit()

            with self.assertRaisesRegex(SmokeVerificationError, "share one owner session"):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_response_projection_dimension_drift(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            with closing(sqlite3.connect(root / "responses.sqlite3")) as connection:
                connection.execute(
                    "UPDATE public_responses SET workflow_search_id = ?, search_id = ?, "
                    "unit_id = ?, chapter = ?",
                    ("search_wrong", "search_wrong", "unit_wrong", "4"),
                )
                connection.commit()

            with self.assertRaisesRegex(
                SmokeVerificationError,
                "response workflow_search_id mismatch",
            ):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_expired_response_rows(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            with closing(sqlite3.connect(root / "responses.sqlite3")) as connection:
                connection.execute(
                    "UPDATE public_responses SET created_at = ?, expires_at = ?",
                    (
                        "2000-01-01T00:00:00+00:00",
                        "2000-01-31T00:00:00+00:00",
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(
                SmokeVerificationError,
                "response retention window mismatch",
            ):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_requires_stale_terminal_projection_attributes(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            requests = {
                item["name"]: item["request_id"] for item in evidence["requests"]
            }
            with closing(sqlite3.connect(root / "trace_events.sqlite3")) as connection:
                for name in ("stale_action_json", "stale_action_stream"):
                    row = connection.execute(
                        "SELECT rowid, safe_attributes_json FROM trace_events "
                        "WHERE request_id = ? AND event_type = 'public_response_finalized'",
                        (requests[name],),
                    ).fetchone()
                    attributes = json.loads(str(row[1]))
                    for field in ("intent", "image_count", "text_length"):
                        attributes.pop(field, None)
                    connection.execute(
                        "UPDATE trace_events SET safe_attributes_json = ? WHERE rowid = ?",
                        (json.dumps(attributes, sort_keys=True), int(row[0])),
                    )
                connection.commit()

            with self.assertRaisesRegex(
                SmokeVerificationError,
                "terminal projection attributes are missing",
            ):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_source_database_mutation_during_audit(self):
        from scripts import verify_tiku_task_state_8896 as verifier

        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            real_copy = verifier._copy_sqlite_snapshot
            mutated = False

            def copy_then_mutate(source: Path, target_directory: Path) -> Path:
                nonlocal mutated
                target = real_copy(source, target_directory)
                if source.name == "responses.sqlite3" and not mutated:
                    mutated = True
                    with closing(sqlite3.connect(root / "trace_events.sqlite3")) as connection:
                        connection.execute("PRAGMA user_version = 1")
                        connection.commit()
                return target

            with patch.object(
                verifier,
                "_copy_sqlite_snapshot",
                side_effect=copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    SmokeVerificationError,
                    "runtime databases changed during offline verification",
                ):
                    verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_response_trace_owner_mismatch(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            response_id = evidence["responses"][0]["response_id"]
            with closing(sqlite3.connect(root / "responses.sqlite3")) as connection:
                connection.execute(
                    "UPDATE public_responses SET session_key = ? WHERE response_id = ?",
                    ("b" * 64, response_id),
                )
                connection.commit()

            with self.assertRaisesRegex(SmokeVerificationError, "session mismatch"):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

    def test_runtime_evidence_rejects_wrong_index_definitions(self):
        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            with closing(sqlite3.connect(root / "trace_events.sqlite3")) as connection:
                connection.execute("DROP INDEX idx_trace_events_one_terminal")
                connection.execute(
                    "CREATE UNIQUE INDEX idx_trace_events_one_terminal "
                    "ON trace_events(trace_id) "
                    "WHERE event_type = 'public_response_finalized'"
                )
                connection.commit()

            with self.assertRaisesRegex(SmokeVerificationError, "predicate mismatch"):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)

        transport = FixtureTransport()
        evidence = self._verify_fixture(transport)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            evidence = _build_runtime_evidence(root, evidence)
            with closing(sqlite3.connect(root / "responses.sqlite3")) as connection:
                connection.execute("DROP INDEX idx_public_responses_trace")
                connection.execute(
                    "CREATE UNIQUE INDEX idx_public_responses_trace "
                    "ON public_responses(request_id)"
                )
                connection.commit()

            with self.assertRaisesRegex(SmokeVerificationError, "columns mismatch"):
                verify_runtime_evidence(root, evidence, expected_commit="a" * 40)


if __name__ == "__main__":
    unittest.main()

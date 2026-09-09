"""Concurrent JSON/stream requests across independently constructed ASGI apps."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import asyncio
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.a3_runtime import A3MvpRuntime
from tiku_agent.agent import AgentResponse
from tiku_agent.execution_runtime import OPERATION_HEADER, attach_execution
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_store import ExecutionSessionStore, ExecutionStore
from tiku_agent.fastapi_demo import create_app, _stream_agent_events
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call
from tiku_shared.trace_context import TraceContext
from tests.test_a3_runtime import FakeObserver, FakeVerifier


class ExecutionHttpConcurrencyTests(unittest.TestCase):
    def run_race(self, kind, stream_first):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entered, release = threading.Event(), threading.Event()
            calls = []
            def provider():
                calls.append(kind)
                entered.set()
                if not release.wait(15):
                    raise TimeoutError("isolated provider not released")
                return {"input_tokens":10, "output_tokens":3}
            def invoke():
                timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                                 call_type="http-race", usage_getter=lambda value:value)
            class Agent:
                config = None
                def __init__(self, state):
                    self.state = state
                def handle_text(self, text):
                    invoke()
                    return AgentResponse(text="receipt", state=self.state.to_dict(), intent="greeting")
            class Observer(FakeObserver):
                def observe(self, image):
                    invoke()
                    return super().observe(image)
            def app():
                authority = ExecutionStore(root / "execution.db")
                ledger = SQLiteModelCostLedger(root / "costs.db")
                runtime = AgentSessionRuntime(ExecutionSessionStore(authority),
                    artifacts=SessionArtifacts(root / "a2"), agent_factory=Agent, cost_ledger=ledger)
                if kind == "image":
                    runtime = A3MvpRuntime(store=ExecutionSessionStore(authority, "workflow"),
                        artifacts=SessionArtifacts(root / "a3"), a2_runtime=runtime,
                        page_observer=Observer(), crop_verifier=FakeVerifier(), cost_ledger=ledger)
                attach_execution(runtime, authority, configuration_version="http-race-fixture-v1")
                return create_app(runtime=runtime, incoming_dir=root / "incoming"), authority
            first_app, authority = app()
            second_app, _ = app()
            image = io.BytesIO()
            Image.new("RGB", (1000,800), "white").save(image, format="JPEG")
            body = {"content":image.getvalue()} if kind == "image" else {"json":{"text":"hello"}}
            route = "/api/image" if kind == "image" else "/api/message"
            first_route = route + ("/stream" if stream_first else "")
            second_route = route + ("" if stream_first else "/stream")
            def payload(response, success):
                if response.headers.get("content-type", "").startswith("application/json"):
                    self.assertEqual(response.status_code, 200 if success else 409, response.text)
                    return response.json()
                self.assertEqual(response.status_code, 200, response.text)
                events = [json.loads(line) for line in response.text.splitlines()]
                selected = [event for event in events if event["type"] == ("result" if success else "error")]
                self.assertEqual(len(selected), 1, response.text)
                return selected[0]["data"] if success else selected[0]
            with TestClient(first_app) as first, TestClient(second_app) as second:
                context = first.get("/api/session").json()["execution"]
                second.cookies.update(first.cookies)
                def headers(context):
                    value = {OPERATION_HEADER:json.dumps({"key":uuid4().hex,
                        "epoch":context["epoch"], "state_version":context["state_version"]})}
                    if kind == "image":
                        value.update({"Content-Type":"image/jpeg", "X-Filename":"same.jpg"})
                    return value
                original = headers(context)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(first.post, first_route, headers=original, **body)
                    try:
                        self.assertTrue(entered.wait(10), "first request did not reach provider")
                        loser = payload(second.post(second_route, headers=original, **body), False)
                        self.assertEqual(loser["code"], "EXECUTION_BUSY")
                        self.assertEqual(calls, [kind])
                        with authority.transaction() as conn:
                            self.assertEqual(conn.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 1)
                    finally:
                        release.set()
                    winner = payload(pending.result(timeout=15), True)
                replay = payload(second.post(second_route, headers=original, **body), True)
                self.assertTrue(replay["operation"]["replayed"])
                self.assertEqual(winner["operation"]["operation_id"], replay["operation"]["operation_id"])
                self.assertEqual(calls, [kind])
                for _ in range(2):
                    self.assertEqual(first.get("/health").status_code, 200)
                    self.assertEqual(first.get("/api/session").status_code, 200)
                    if kind == "image":
                        self.assertEqual(first.get(winner["uploaded_image"]).status_code, 200)
                self.assertEqual(calls, [kind])
                if kind == "image":
                    with authority.transaction() as conn:
                        stored = json.loads(conn.execute("SELECT result FROM execution_operations").fetchone()[0])
                        state_before = conn.execute("SELECT kind,version,payload FROM execution_states ORDER BY kind").fetchall()
                        state_before = [tuple(row) for row in state_before]
                    saved_path = Path(stored["response"]["uploaded_image_path"])
                    saved_bytes = saved_path.read_bytes()
                    saved_path.unlink()  # only this test's isolated operation artifact
                    self.assertEqual(first.get(winner["uploaded_image"]).status_code, 404)
                    for endpoint in (route, route + "/stream"):
                        unavailable = payload(second.post(endpoint, headers=original, **body), False)
                        self.assertEqual(unavailable["code"], "EXECUTION_RESULT_UNAVAILABLE")
                    self.assertEqual(calls, [kind])
                    with authority.transaction() as conn:
                        self.assertEqual([tuple(row) for row in conn.execute("SELECT kind,version,payload FROM execution_states ORDER BY kind")], state_before)
                        self.assertEqual(conn.execute("SELECT status FROM execution_operations").fetchone()[0], "SUCCEEDED")
                        self.assertEqual(conn.execute("SELECT count(*) FROM execution_attempts").fetchone()[0], 1)
                    # Exact byte restoration permits redelivery; no search is run.
                    saved_path.write_bytes(saved_bytes)
                    restored = payload(second.post(second_route, headers=original, **body), True)
                    self.assertTrue(restored["operation"]["replayed"])
                    self.assertEqual(calls, [kind])
                # Same content with a new explicit operation is a new paid intent.
                fresh = headers(second.get("/api/session").json()["execution"])
                new = payload(second.post(second_route, headers=fresh, **body), True)
                self.assertNotEqual(new["operation"]["operation_id"], winner["operation"]["operation_id"])
                self.assertEqual(calls, [kind,kind])
                with authority.transaction() as conn:
                    for table in ("execution_operations", "execution_attempts", "execution_effects"):
                        self.assertEqual(conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 2)
                    self.assertEqual(conn.execute("SELECT count(*) FROM execution_operations WHERE status='SUCCEEDED'").fetchone()[0], 2)
                with closing(sqlite3.connect(root / "costs.db")) as conn:
                    self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (2,26))

    def test_image_json_winner_stream_competitor(self):
        self.run_race("image", False)

    def test_image_stream_winner_json_competitor(self):
        self.run_race("image", True)

    def test_text_json_winner_stream_competitor(self):
        self.run_race("text", False)

    def test_text_stream_winner_json_competitor(self):
        self.run_race("text", True)

    def test_enabled_runtime_stream_cancel_withdraws_queue_without_call_and_original_key_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority = ExecutionStore(root / "execution.db")
            entered, release = threading.Event(), threading.Event()
            calls = []
            class Agent:
                config = None
                def __init__(self, state):
                    self.state = state
                def handle_text(self, text):
                    def provider():
                        calls.append(self.state.session_id)
                        if self.state.session_id == "active":
                            entered.set()
                            if not release.wait(15):
                                raise TimeoutError("active fixture not released")
                        return {"input_tokens":10, "output_tokens":3}
                    timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus",
                                     call_type="queue-cancel", usage_getter=lambda value:value)
                    return AgentResponse(text="receipt", state=self.state.to_dict(), intent="greeting")
            runtime = AgentSessionRuntime(ExecutionSessionStore(authority),
                artifacts=SessionArtifacts(root / "media"), agent_factory=Agent,
                cost_ledger=SQLiteModelCostLedger(root / "costs.db"), max_concurrent_tasks=1,
                max_queued_tasks=1, queue_wait_seconds=10)
            attach_execution(runtime, authority, configuration_version="queue-cancel-fixture-v1")
            def request(sid):
                context = authority.context(sid)
                return OperationRequest(uuid4().hex, context["epoch"], context["state_version"])
            active, queued = request("active"), request("queued")
            def execute(progress):
                response = runtime.handle_text("queued", "waiting", progress=progress, operation_request=queued)
                return {"text":response.text}
            async def cancel():
                request_id = "req_" + uuid4().hex
                stream = _stream_agent_events(execute, request_id=request_id,
                    trace_context=TraceContext.create(request_id=request_id))
                try:
                    event = json.loads(await stream.__anext__())
                    self.assertEqual((event["type"],event["stage"]), ("progress","queued"))
                finally:
                    await stream.aclose()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    row = runtime.execution_operations.lookup("queued", "local", queued)
                    if row and row["status"] == "REGISTERED":
                        return row["id"]
                    await asyncio.sleep(0.01)
                self.fail("cancelled operation did not return to known-unsent registration")
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(runtime.handle_text, "active", "running", operation_request=active)
                try:
                    self.assertTrue(entered.wait(10))
                    operation_id = asyncio.run(cancel())
                    self.assertEqual(calls, ["active"])
                    self.assertIsNone(runtime.store.load("queued"))
                    with authority.transaction() as conn:
                        for table in ("execution_effects", "execution_cost_runs"):
                            self.assertEqual(conn.execute("SELECT count(*) FROM " + table + " WHERE operation_id=?", (operation_id,)).fetchone()[0], 0)
                        self.assertEqual(conn.execute("SELECT status FROM execution_attempts WHERE operation_id=?", (operation_id,)).fetchone()[0], "FAILED")
                finally:
                    release.set()
                pending.result(timeout=15)
            response = runtime.handle_text("queued", "waiting", operation_request=queued)
            self.assertEqual(response.execution_receipt["operation_id"], operation_id)
            self.assertEqual(calls, ["active", "queued"])
            with closing(sqlite3.connect(root / "costs.db")) as conn:
                self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (2,26))

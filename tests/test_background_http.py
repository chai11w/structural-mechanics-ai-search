"""HTTP phase 6.3 integration over real private databases and counted providers."""
from contextlib import closing
from dataclasses import replace
import gc
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from tiku_admin.control_store import SQLiteControlStore
from tiku_agent.background_auth import BackgroundInviteAccess
from tiku_agent.execution_runtime import OPERATION_HEADER
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_shared.response_store import SQLiteResponseStore
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder
from tests.test_execution_dispatch import DispatchFixture


class BackgroundHttpFixture:
    def __init__(self, root, *, fixture=None):
        self.f = fixture or DispatchFixture(root)
        self.root = self.f.root
        self.control = SQLiteControlStore(self.root / "control.db")
        self.identity, self.code = self.control.create_invitation(label="isolated background HTTP")
        self.access = BackgroundInviteAccess(self.control)
        self.responses = SQLiteResponseStore(self.root / "responses.db")
        self.feedback = SQLiteFeedbackStore(self.root / "feedback.db")
        self.trace_store = SQLiteTraceEventStore(self.root / "trace.db")
        self.recorder = TraceEventRecorder(self.trace_store)
        self.app = create_app(runtime=self.f.runtime, incoming_dir=self.root / "incoming",
            session_cookie="tiku_phase6_session", invite_access=self.access, feedback_store=self.feedback,
            response_store=self.responses, trace_event_recorder=self.recorder, background_execution=True,
            cleanup_interval_seconds=0)

    def login(self, client):
        response = client.post("/api/invite/login", data={"code": self.code}, follow_redirects=False)
        assert response.status_code == 303, response.status_code
        response = client.post("/api/jobs/session", headers={"X-Tiku-Background": "1"})
        assert response.status_code == 200, response.text
        return response.json()["execution"]

    @staticmethod
    def headers(context, key=None):
        return {"X-Tiku-Background": "1", "X-Session-Coordination-Version": "6",
                "X-Session-Request-Fence": str(int(time.time() * 1000)) + ":" + uuid4().hex,
                OPERATION_HEADER: json.dumps({"key": key or uuid4().hex, "epoch": context["epoch"], "state_version": context["state_version"]})}

    def wait_result(self, client, operation_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = client.get("/api/jobs/" + operation_id)
            assert response.status_code == 200, response.text
            value = response.json()["job"]
            if value["status"] in {"UNKNOWN", "FAILED", "CANCELLED"} or value["publication"]["status"] in {"READY", "FAILED"}:
                return value
            time.sleep(0.02)
        raise AssertionError("background result timeout")


class BackgroundHttpTests(unittest.TestCase):
    def test_numeric_operation_id_keeps_submission_trace_valid(self):
        h = self.h
        with patch("tiku_agent.execution_operations.uuid4", side_effect=lambda: SimpleNamespace(hex="0" + uuid4().hex[1:])):
            with TestClient(h.app) as client:
                context = h.login(client)
                ack = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "numeric trace"}}, headers=h.headers(context))
                self.assertEqual(ack.status_code, 202, ack.text)
                operation_id = ack.json()["job"]["operation_id"]
                self.assertTrue(operation_id.startswith("0"))
                self.assertEqual(h.wait_result(client, operation_id)["status"], "SUCCEEDED")
                self.assertEqual(client.get("/api/jobs/" + operation_id + "/stream").status_code, 200)
        self.assertEqual(h.recorder.health()["validation_rejections"], 0)
        with closing(sqlite3.connect(h.trace_store.path)) as conn:
            rows = conn.execute("SELECT safe_attributes_json FROM trace_events WHERE stage='background_submission'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0][0])["operation"], "op_" + operation_id)
            observations = conn.execute("SELECT safe_attributes_json FROM trace_events WHERE stage='background_observation' AND event_type='stage_finished'").fetchall()
            self.assertTrue(observations)
            self.assertTrue(all(json.loads(row[0])["operation"] == "op_" + operation_id for row in observations))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(gc.collect)  # Legacy feedback SQLite contexts rely on GC to close.
        self.h = BackgroundHttpFixture(temporary.name)
        self.addCleanup(self.h.f.release.set)

    def test_submit_discover_result_and_legacy_rejection(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            headers = h.headers(context)
            for path in ("/api/message", "/api/message/stream"):
                old = client.post(path, json={"text": "old"}, headers=headers)
                self.assertEqual(old.status_code, 409)
                self.assertEqual(old.json()["code"], "BACKGROUND_PROTOCOL_REQUIRED")
            submitted = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=headers)
            self.assertEqual(submitted.status_code, 202, submitted.text)
            operation_id = submitted.json()["job"]["operation_id"]
            result = h.wait_result(client, operation_id)
            self.assertEqual(result["status"], "SUCCEEDED", result)
            self.assertEqual(result["publication"]["status"], "READY", result)
            payload = result["publication"]["result"]
            self.assertEqual(payload["text"], "reply:hello")
            self.assertEqual(payload["snapshot_role"], "historical")
            self.assertEqual(payload["task_state"]["workflow"]["allowed_actions"], [])
            envelope = json.loads(headers[OPERATION_HEADER])
            for _ in range(3):
                found = client.get("/api/jobs/lookup", params={"key": envelope["key"], "epoch": envelope["epoch"]})
                self.assertEqual(found.json()["job"]["publication"]["result"], payload)
            replay = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=headers)
            self.assertEqual(replay.json()["job"]["operation_id"], operation_id)
            self.assertEqual(h.f.calls, ["hello"])
        with closing(sqlite3.connect(h.responses.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM public_responses").fetchone()[0], 1)

    def test_frozen_media_and_response_survive_original_file_removal(self):
        h = self.h
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="PNG")
        with TestClient(h.app) as client:
            context = h.login(client)
            submitted = client.post("/api/jobs/image", content=image.getvalue(), headers=h.headers(context))
            self.assertEqual(submitted.status_code, 202, submitted.text)
            result = h.wait_result(client, submitted.json()["job"]["operation_id"])
            self.assertEqual(result["publication"]["status"], "READY", result)
            payload = result["publication"]["result"]
            url = payload["uploaded_image"]
            self.assertTrue(url.startswith("/api/jobs/"))
            before = client.get(url)
            self.assertEqual(before.status_code, 200)
            with h.f.authority.transaction() as conn:
                receipt = json.loads(conn.execute("SELECT result FROM execution_operations").fetchone()[0])
            for path in receipt["files"]:
                Path(path).unlink(missing_ok=True)
            self.assertEqual(client.get(url).content, before.content)
            self.assertEqual(h.f.calls, ["image"])

    def test_logout_cookie_replay_and_cross_session_reads_are_rejected(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            old_cookies = dict(client.cookies)
            response = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=h.headers(context))
            operation_id = response.json()["job"]["operation_id"]
            h.wait_result(client, operation_id)
            client.cookies.set("tiku_phase6_session", uuid4().hex)
            self.assertEqual(client.get("/api/jobs/" + operation_id).status_code, 401)
            client.cookies.clear()
            client.cookies.update(old_cookies)
            self.assertEqual(client.post("/api/invite/logout", follow_redirects=False).status_code, 303)
            client.cookies.update(old_cookies)
            self.assertEqual(client.get("/api/jobs/" + operation_id).status_code, 401)
            self.assertEqual(client.post("/api/jobs/session", headers={"X-Tiku-Background": "1"}).status_code, 401)

    def test_ready_queries_use_only_read_transactions(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            headers = h.headers(context)
            response = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=headers)
            operation_id = response.json()["job"]["operation_id"]
            h.wait_result(client, operation_id)
            h.app.state.background.worker.close()
            h.app.state.background.publication.close()
            envelope = json.loads(headers[OPERATION_HEADER])
            with patch.object(h.f.authority, "transaction", side_effect=AssertionError("query attempted a write transaction")):
                self.assertEqual(client.get("/api/jobs/" + operation_id).status_code, 200)
                self.assertEqual(client.get("/api/jobs/lookup", params={"key": envelope["key"], "epoch": envelope["epoch"]}).status_code, 200)
                streamed = client.get("/api/jobs/" + operation_id + "/stream")
                self.assertEqual(streamed.status_code, 200)
                self.assertEqual(json.loads(streamed.text.splitlines()[0])["type"], "snapshot")

    def test_feedback_uses_same_response_id_and_owned_frozen_image(self):
        h = self.h
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="PNG")
        with TestClient(h.app) as client:
            context = h.login(client)
            response = client.post("/api/jobs/image", content=image.getvalue(), headers=h.headers(context))
            operation_id = response.json()["job"]["operation_id"]
            result = h.wait_result(client, operation_id)["publication"]["result"]
            feedback = {"message_id": "background-message", "rated_response_id": result["response_id"], "rating": "positive",
                        "tags": [], "detail": "", "conversation": [{"messageId": "background-message", "message": result["text"],
                            "responseId": result["response_id"], "images": [result["uploaded_image"]], "me": False}]}
            first = client.post("/api/feedback", json=feedback)
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post("/api/feedback", json=feedback)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(first.json()["feedback"]["rated_response_id"], result["response_id"])
            self.assertEqual(second.json()["feedback"]["rated_response_id"], result["response_id"])
            self.assertEqual(list(h.app.state.background.feedback_exports.iterdir()), [])
            self.assertEqual(h.f.calls, ["image"])
        self.assertTrue(any(h.feedback.cases_root.rglob("*.png")))
        with closing(sqlite3.connect(h.feedback.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM message_feedback").fetchone()[0], 1)

    def test_invalid_protocol_body_and_cross_origin_submit_do_not_create_jobs(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            cases = [({}, b'{}'), (h.headers(context), b'{bad'),
                     (h.headers(context), json.dumps({"kind": [], "parameters": {}}).encode()),
                     ({**h.headers(context), "Origin": "https://other.example"}, b'{}')]
            for headers, body in cases:
                response = client.post("/api/jobs", headers=headers, content=body)
                self.assertNotEqual(response.status_code, 202)
                self.assertNotEqual(response.status_code, 500)
            self.assertEqual(h.f.rows("execution_dispatch"), [])
            self.assertEqual(h.f.calls, [])

    def test_reset_control_still_works_in_background_mode(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            response = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=h.headers(context))
            operation_id = response.json()["job"]["operation_id"]
            h.wait_result(client, operation_id)
            context = client.get("/api/session").json()["execution"]
            reset = client.post("/api/reset", json={}, headers=h.headers(context))
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertEqual(h.f.calls, ["hello"])
            self.assertEqual(client.get("/api/jobs/" + operation_id).status_code, 409)

    def test_other_identity_cannot_use_operation_id_or_original_key(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            headers = h.headers(context)
            submitted = client.post('/api/jobs', json={'kind': 'handle_text', 'parameters': {'text': 'private result'}}, headers=headers)
            operation_id = submitted.json()['job']['operation_id']
            result = h.wait_result(client, operation_id)['publication']['result']
            _, other_code = h.control.create_invitation(label='second identity')
            client.cookies.clear()
            login = client.post('/api/invite/login', data={'code': other_code}, follow_redirects=False)
            self.assertEqual(login.status_code, 303)
            self.assertEqual(client.post('/api/jobs/session', headers={'X-Tiku-Background': '1'}).status_code, 200)
            self.assertEqual(client.get('/api/jobs/' + operation_id).status_code, 404)
            self.assertEqual(client.get('/api/jobs/' + operation_id + '/stream').status_code, 404)
            envelope = json.loads(headers[OPERATION_HEADER])
            lookup = client.get('/api/jobs/lookup', params={'key': envelope['key'], 'epoch': envelope['epoch']})
            self.assertIn(lookup.status_code, {404, 409})
            feedback = client.post('/api/feedback', json={'message_id': 'x', 'rated_response_id': result['response_id'],
                'rating': 'positive', 'tags': [], 'detail': '', 'conversation': []})
            self.assertNotEqual(feedback.status_code, 200)
            self.assertEqual(h.f.calls, ['private result'])

    def test_oversized_http_body_is_rejected_before_acceptance(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            result = client.post('/api/jobs', content=b'x' * (h.f.dispatch.policy.max_json_bytes + 1025), headers=h.headers(context))
            self.assertEqual(result.status_code, 413)
            self.assertEqual(h.f.rows('execution_dispatch'), [])
            self.assertEqual(h.f.calls, [])

    def test_login_capacity_applies_even_without_binding_business_session(self):
        h = self.h
        with TestClient(h.app) as client, patch.object(h.f.dispatch, 'policy', replace(h.f.dispatch.policy, max_grants=1)):
            first = client.post('/api/invite/login', data={'code': h.code}, follow_redirects=False)
            self.assertEqual(first.status_code, 303)
            second = client.post('/api/invite/login', data={'code': h.code}, follow_redirects=False)
            self.assertEqual(second.status_code, 503, second.text)
            self.assertEqual(second.json()['code'], 'EXECUTION_CAPACITY')
            self.assertEqual(len(h.f.rows('execution_http_logins')), 1)
            self.assertEqual(client.post('/api/invite/logout', follow_redirects=False).status_code, 303)
            self.assertEqual(h.f.calls, [])

    def test_feedback_export_capacity_cannot_admit_one_extra_file(self):
        h = self.h
        image = io.BytesIO()
        Image.new('RGB', (10, 10)).save(image, format='PNG')
        with TestClient(h.app) as client:
            context = h.login(client)
            submitted = client.post('/api/jobs/image', content=image.getvalue(), headers=h.headers(context))
            result = h.wait_result(client, submitted.json()['job']['operation_id'])['publication']['result']
            background = h.app.state.background
            for _ in range(1000):
                (background.feedback_exports / ('feedback-' + uuid4().hex + '.png')).touch()
            exports = []
            request = SimpleNamespace(cookies=dict(client.cookies))
            self.assertIsNone(background.feedback_media(request, result['uploaded_image'], exports))
            self.assertEqual(exports, [])
            self.assertEqual(len(list(background.feedback_exports.iterdir())), 1000)


if __name__ == "__main__":
    unittest.main()

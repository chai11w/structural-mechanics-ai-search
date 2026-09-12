"""Close the real ASGI response consumer, including middleware and NDJSON body."""
import asyncio
from contextlib import closing
import json
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from tests.test_background_http import BackgroundHttpFixture
from tests.test_background_admission import http_scope


async def close_stream(app, path, cookies, *, before_body=False):
    disconnected = asyncio.Event()
    received_request = False
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"since=0", "root_path": "", "client": ("127.0.0.1", 12555),
             "server": ("testserver", 80), "headers": [(b"host", b"testserver"),
                 (b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode())]}
    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        messages.append(message)
        if ((before_body and message["type"] == "http.response.start")
                or message["type"] == "http.response.body" and message.get("body")):
            disconnected.set()
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return messages


class BackgroundStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.h = BackgroundHttpFixture(temporary.name)
        self.addCleanup(self.h.f.release.set)

    def test_running_disconnect_does_not_cancel_worker_and_traces_are_separate(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            h.f.release.clear()
            ack = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=h.headers(context))
            operation_id = ack.json()["job"]["operation_id"]
            try:
                self.assertTrue(h.f.entered.wait(5))
                messages = asyncio.run(close_stream(h.app, "/api/jobs/" + operation_id + "/stream", dict(client.cookies)))
                self.assertEqual(messages[0]["status"], 200)
                snapshots = [json.loads(line) for message in messages for line in message.get("body", b"").splitlines()]
                self.assertTrue(any(item["type"] == "snapshot" for item in snapshots))
                self.assertEqual(h.f.calls, ["hello"])
                self.assertEqual(h.f.rows("execution_operations")[0]["status"], "RUNNING")
                self.assertEqual(h.app.state.background._subscriptions, {})
            finally:
                h.f.release.set()
            result = h.wait_result(client, operation_id)
            self.assertEqual(result["publication"]["status"], "READY", result)
            repeated = client.get("/api/jobs/" + operation_id + "/stream?since=999999")
            snapshot = json.loads(repeated.text.splitlines()[0])
            self.assertTrue(snapshot["resync"])
            self.assertEqual(snapshot["data"]["job"]["publication"]["result"]["response_id"], result["publication"]["result"]["response_id"])
            self.assertEqual(h.f.calls, ["hello"])
        with closing(sqlite3.connect(h.trace_store.path)) as conn:
            rows = conn.execute("SELECT trace_id,event_type,stage,outcome FROM trace_events WHERE event_type IN ('public_response_finalized','request_failed')").fetchall()
            business = [row for row in rows if row[0] == "trace_" + operation_id]
            self.assertEqual(len(business), 1, rows)
            self.assertEqual(business[0][2:], ("background_execution", "success"))
            self.assertTrue(any(row[2] == "background_observation" and row[3] == "cancelled" for row in rows), rows)
        self.assertEqual(h.recorder.health()["duplicate_terminals"], 0)

    def test_disconnect_after_headers_releases_subscription(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            h.f.release.clear()
            ack = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "hello"}}, headers=h.headers(context))
            operation_id = ack.json()["job"]["operation_id"]
            try:
                self.assertTrue(h.f.entered.wait(5))
                asyncio.run(close_stream(h.app, "/api/jobs/" + operation_id + "/stream", dict(client.cookies), before_body=True))
                self.assertEqual(h.app.state.background._subscriptions, {})
            finally:
                h.f.release.set()
            self.assertEqual(h.wait_result(client, operation_id)["status"], "SUCCEEDED")

    def test_queued_disconnect_expires_while_the_first_provider_still_runs(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            h.f.release.clear()
            first = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "first"}}, headers=h.headers(context))
            try:
                self.assertTrue(h.f.entered.wait(5))
                client.cookies.clear()
                second_context = h.login(client)
                headers = h.headers(second_context)
                ack = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "second"}}, headers=headers)
                self.assertEqual(ack.status_code, 202, ack.text)
                operation_id = ack.json()["job"]["operation_id"]
                original_deadline = ack.json()["job"]["queue_deadline"]
                asyncio.run(close_stream(h.app, "/api/jobs/" + operation_id + "/stream", dict(client.cookies)))
                h.f.clock[0] += 56
                result = h.wait_result(client, operation_id)
                self.assertEqual((result["status"], result["error_code"]), ("FAILED", "EXECUTION_QUEUE_TIMEOUT"))
                self.assertEqual(result["queue_deadline"], original_deadline)
                self.assertEqual(h.f.calls, ["first"])
                replay = client.post("/api/jobs", json={"kind": "handle_text", "parameters": {"text": "second"}}, headers=headers)
                self.assertEqual(replay.json()["job"]["status"], "FAILED")
                self.assertEqual(h.f.calls, ["first"])
            finally:
                h.f.release.set()

    def test_slow_consumers_are_bounded_and_do_not_hold_up_completion(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            h.f.release.clear()
            ack = client.post('/api/jobs', json={'kind': 'handle_text', 'parameters': {'text': 'slow'}}, headers=h.headers(context))
            operation_id = ack.json()['job']['operation_id']
            self.assertTrue(h.f.entered.wait(5))
            path = '/api/jobs/' + operation_id + '/stream'
            async def run():
                release = asyncio.Event()
                started = [asyncio.Event(), asyncio.Event()]
                messages = [[], []]
                async def consume(index):
                    received = False
                    never = asyncio.Event()
                    async def receive():
                        nonlocal received
                        if not received:
                            received = True
                            return {'type': 'http.request', 'body': b'', 'more_body': False}
                        await never.wait()
                        return {'type': 'http.disconnect'}
                    async def send(message):
                        if message['type'] == 'http.response.body' and message.get('body'):
                            messages[index].append(message['body'])
                            started[index].set()
                            await release.wait()
                    await h.app(http_scope(path, dict(client.cookies)), receive, send)
                tasks = [asyncio.create_task(consume(i)) for i in range(2)]
                try:
                    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=5)
                    rejected = await asyncio.to_thread(client.get, path)
                    self.assertEqual(rejected.status_code, 429, rejected.text)
                    self.assertEqual(sum(h.app.state.background._subscriptions.values()), 2)
                    # Many missed progress versions collapse into a snapshot.
                    with h.f.authority.transaction() as conn:
                        conn.execute('UPDATE execution_dispatch SET progress_version=progress_version+100 WHERE operation_id=?', (operation_id,))
                    h.f.release.set()
                    ready = await asyncio.to_thread(h.wait_result, client, operation_id)
                    self.assertEqual(ready['publication']['status'], 'READY')
                    self.assertEqual(h.f.calls, ['slow'])
                    self.assertEqual([len(items) for items in messages], [1, 1])
                finally:
                    h.f.release.set()
                    release.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
                self.assertEqual(h.app.state.background._subscriptions, {})
                for items in messages:
                    frames = [json.loads(line) for item in items for line in item.splitlines()]
                    self.assertLessEqual(len(frames), 4)
                    self.assertEqual(frames[-1]['data']['job']['publication']['status'], 'READY')
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

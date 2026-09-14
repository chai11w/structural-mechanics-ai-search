"""Actual HTTP consumer loss on both sides of durable acceptance."""
import asyncio
from contextlib import suppress
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_background_http import BackgroundHttpFixture


def http_scope(path, cookies, *, method='GET', headers=None):
    return {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
            'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'query_string': b'', 'root_path': '', 'client': ('127.0.0.1', 12556), 'server': ('testserver', 80),
            'headers': [(b'host', b'testserver'),
                        (b'cookie', '; '.join(f'{k}={v}' for k, v in cookies.items()).encode()),
                        *[(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]]}


class BackgroundAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.h = BackgroundHttpFixture(temporary.name)

    def test_disconnect_during_upload_never_accepts_partial_input(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            scope = http_scope('/api/jobs/image', dict(client.cookies), method='POST', headers=h.headers(context))
            async def run():
                chunks = iter([{'type': 'http.request', 'body': b'partial image', 'more_body': True},
                               {'type': 'http.disconnect'}])
                async def receive():
                    return next(chunks, {'type': 'http.disconnect'})
                async def send(message):
                    pass
                await asyncio.wait_for(h.app(scope, receive, send), timeout=5)
            asyncio.run(run())
            self.assertEqual(h.f.rows('execution_dispatch'), [])
            self.assertEqual(h.f.rows('execution_dispatch_inputs'), [])
            self.assertEqual(h.f.calls, [])

    def test_accepted_command_survives_cancelled_http_task_before_ack(self):
        h = self.h
        with TestClient(h.app) as client:
            context = h.login(client)
            headers = h.headers(context)
            scope = http_scope('/api/jobs', dict(client.cookies), method='POST', headers=headers)
            body = json.dumps({'kind': 'handle_text', 'parameters': {'text': 'lost ack'}}).encode()
            entered, release = threading.Event(), threading.Event()
            original = h.f.dispatch.accept
            def lose_ack(*args, **kwargs):
                result = original(*args, **kwargs)
                entered.set()
                release.wait(5)
                return result
            async def run():
                sent = False
                never = asyncio.Event()
                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    await never.wait()
                    return {'type': 'http.disconnect'}
                async def send(message):
                    pass
                task = asyncio.create_task(h.app(scope, receive, send))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
            with patch.object(h.f.dispatch, 'accept', lose_ack):
                asyncio.run(run())
            envelope = json.loads(headers['X-Tiku-Operation'])
            found = client.get('/api/jobs/lookup', params={'key': envelope['key'], 'epoch': envelope['epoch']})
            self.assertEqual(found.status_code, 200, found.text)
            operation_id = found.json()['job']['operation_id']
            self.assertEqual(h.wait_result(client, operation_id)['publication']['status'], 'READY')
            retried = client.post('/api/jobs', content=body, headers=headers)
            self.assertEqual(retried.json()['job']['operation_id'], operation_id)
            self.assertEqual(h.f.calls, ['lost ack'])
            self.assertEqual(len(h.f.rows('execution_dispatch')), 1)

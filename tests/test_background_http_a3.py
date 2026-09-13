"""All five HTTP commands retain real A3/A2 state, cost and result ownership."""
from contextlib import closing
import gc
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_execution_dispatch_a3 as a3_fixture
from tests.test_a3_runtime import FakeAutoCropper
from tests.test_background_http import BackgroundHttpFixture


class BackgroundA3HttpTests(unittest.TestCase):
    def test_explicit_recovery_uses_original_publication_and_no_second_scoreable_reply(self):
        with TestClient(self.h.app) as client:
            self.h.login(client)
            self.sid = client.cookies.get('tiku_phase6_session')
            self.command(client, 'handle_image')
            self.command(client, 'select_unit', {'unit_id': 'g1-u1', **self.target()})
            context = client.get('/api/session').json()['execution']
            with patch.object(self.f.a3, '_after_a2_response', side_effect=RuntimeError('parent interrupted')):
                submitted = client.post('/api/jobs', json={'kind': 'handle_crop', 'parameters': {
                    'bounds': {'x': 0.1, 'y': 0.1, 'width': .7, 'height': .7}, 'unit_id': 'g1-u1', **self.target()}},
                    headers=self.h.headers(context))
                self.assertEqual(submitted.status_code, 202, submitted.text)
                source = submitted.json()['job']['operation_id']
                self.assertEqual(self.h.wait_result(client, source)['status'], 'UNKNOWN')
            with closing(sqlite3.connect(self.h.responses.path)) as conn:
                before = conn.execute('SELECT count(*) FROM public_responses').fetchone()[0]
            context = client.get('/api/execution').json()['execution']
            headers = self.h.headers(context)
            for _ in range(2):
                ack = client.post('/api/execution/recover', json={'source_operation_id': source}, headers=headers)
                self.assertEqual(ack.status_code, 200, ack.text)
                self.assertNotIn('response_id', ack.json())
                self.assertEqual(ack.json()['images'], [])
            job = self.h.wait_result(client, source)
            self.assertEqual(job['publication']['status'], 'READY')
            self.assertEqual(self.f.calls, ['page', 'g1-u1', 'child'])
            with closing(sqlite3.connect(self.h.responses.path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM public_responses').fetchone()[0], before + 1)
                self.assertEqual(conn.execute('SELECT count(*) FROM public_responses WHERE trace_id=?', ('trace_' + source,)).fetchone()[0], 1)

    def setUp(self):
        self.f = a3_fixture.ExecutionDispatchA3Tests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.addCleanup(gc.collect)
        self.h = BackgroundHttpFixture(self.f.root, fixture=SimpleNamespace(root=self.f.root, runtime=self.f.a3))

    def command(self, client, kind, parameters=None):
        h = self.h
        context = client.get('/api/session').json()['execution']
        headers = h.headers(context)
        if kind == 'handle_image':
            submitted = client.post('/api/jobs/image', content=self.f.image, headers=headers)
        else:
            submitted = client.post('/api/jobs', json={'kind': kind, 'parameters': parameters or {}}, headers=headers)
        self.assertEqual(submitted.status_code, 202, submitted.text)
        operation_id = submitted.json()['job']['operation_id']
        job = h.wait_result(client, operation_id)
        self.assertEqual(job['status'], 'SUCCEEDED', job)
        self.assertEqual(job['publication']['status'], 'READY', job)
        payload = job['publication']['result']
        calls = list(self.f.calls)
        for _ in range(2):
            self.assertEqual(client.get('/api/jobs/' + operation_id).json()['job']['publication']['result'], payload)
        self.assertEqual(self.f.calls, calls)
        with closing(sqlite3.connect(h.responses.path)) as conn:
            row = conn.execute('SELECT image_route,workflow_search_id,unit_id FROM public_responses WHERE response_id=?',
                               (payload['response_id'],)).fetchone()
        self.assertEqual(row[0], 'A3')
        self.assertEqual(row[1], self.f.a3.store.load(self.sid).workflow_search_id)
        self.assertNotIn(str(self.f.root), str(payload))
        return operation_id, payload, row

    def target(self):
        state = self.f.a3.store.load(self.sid)
        return {'workflow_search_id': state.workflow_search_id, 'task_revision': state.task_revision}

    def test_page_text_select_crop_and_child_result(self):
        with TestClient(self.h.app) as client:
            self.h.login(client)
            self.sid = client.cookies.get('tiku_phase6_session')
            self.command(client, 'handle_image')
            self.command(client, 'handle_text', {'text': '你好'})
            self.command(client, 'select_unit', {'unit_id': 'g1-u1', **self.target()})
            operation_id, payload, projection = self.command(client, 'handle_crop', {
                'bounds': {'x': 0.1, 'y': 0.1, 'width': 0.7, 'height': 0.7}, 'unit_id': 'g1-u1', **self.target()})
            self.assertEqual(self.f.a3.store.load(self.sid).phase, 'A2_ACTIVE')
            self.assertEqual(projection[2], 'g1-u1')
            for url in [payload['uploaded_image'], payload['submitted_crop'], *payload['images']]:
                if url:
                    self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(self.f.calls, ['page', 'g1-u1', 'child'])
            with self.f.store.reading() as conn:
                self.assertEqual(conn.execute('SELECT status FROM execution_handoffs WHERE operation_id=?', (operation_id,)).fetchone()[0], 'COMMITTED')
                self.assertEqual(conn.execute("SELECT count(*) FROM execution_cost_outbox WHERE status<>'CONFIRMED'").fetchone()[0], 0)

    def test_prepare_units_and_select_reuse_confirmed_unit_costs(self):
        self.f.a3.auto_cropper = FakeAutoCropper(second_status='auto_ready')
        with TestClient(self.h.app) as client:
            self.h.login(client)
            self.sid = client.cookies.get('tiku_phase6_session')
            self.command(client, 'handle_image')
            operation_id, _, _ = self.command(client, 'prepare_units', {'unit_ids': ['g1-u1', 'g1-u2'], **self.target()})
            self.command(client, 'select_unit', {'unit_id': 'g1-u1', **self.target()})
            self.assertEqual(self.f.a3.store.load(self.sid).phase, 'A2_ACTIVE')
            self.assertCountEqual(self.f.calls, ['page', 'g1-u1', 'g1-u2', 'child'])
            with self.f.store.reading() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM execution_unit_checks WHERE operation_id=? AND status='CONFIRMED'", (operation_id,)).fetchone()[0], 2)
        with closing(sqlite3.connect(self.f.ledger.path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*),sum(total_tokens) FROM model_cost_calls').fetchone(), (4, 52))

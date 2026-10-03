"""Bind transport observations stay private and cannot affect execution authority."""
from contextlib import closing
import gc
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.test_background_http import BackgroundHttpFixture


class BindDiagnosticsHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(gc.collect)
        self.h = BackgroundHttpFixture(temporary.name)
        self.addCleanup(self.h.f.release.set)
        logger_patch = patch('tiku_agent.bind_diagnostics._LOGGER.warning')
        self.diagnostic_log = logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def received(self, request_id):
        self.assertTrue(self.h.recorder.flush(timeout=2))
        with closing(sqlite3.connect(self.h.trace_store.path)) as conn:
            rows = conn.execute(
                "SELECT request_id,safe_attributes_json FROM trace_events "
                "WHERE request_id=? AND event_type='request_received'", (request_id,)
            ).fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0][0], json.loads(rows[0][1])

    def test_previous_response_loss_correlates_without_replacing_current_authority(self):
        first_id, retry_id = 'req_' + 'a' * 32, 'req_' + 'b' * 32
        diagnostic = dict(schema=1, request_id=first_id, phase='body',
                          code='NETWORK_UNAVAILABLE', elapsed_ms=151, status=200)
        with TestClient(self.h.app) as client:
            context = self.h.login(client)
            first = client.post('/api/jobs/session', headers={
                'X-Tiku-Background': '1', 'X-Request-ID': first_id})
            self.assertEqual(first.status_code, 200)
            retry = client.post('/api/jobs/session', headers={
                'X-Tiku-Background': '1', 'X-Request-ID': retry_id,
                'X-Tiku-Bind-Diagnostic': json.dumps(diagnostic)})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json(), first.json())
            self.assertEqual(retry.json()['execution'], context)
            self.assertEqual(retry.headers['X-Request-ID'], retry_id)
            self.assertNotIn('client_bind_', retry.text)
            self.assertNotIn(first_id, retry.text)
            self.assertEqual(self.h.f.calls, [])
        current_id, attrs = self.received(retry_id)
        self.assertEqual(current_id, retry_id)
        self.assertEqual(attrs['endpoint'], '/api/jobs/session')
        self.assertFalse(any(k.startswith('client_bind_') for k in attrs))
        self.diagnostic_log.assert_called_once()
        prefix, encoded = self.diagnostic_log.call_args.args
        self.assertEqual(prefix, 'BIND_TRANSPORT_DIAGNOSTIC %s')
        report = json.loads(encoded)
        self.assertEqual(report['request_id'], retry_id)
        self.assertIs(report['client_bind_reported'], True)
        self.assertEqual(report['client_bind_request_id'], first_id)
        self.assertEqual(report['client_bind_phase'], 'body')
        self.assertEqual(report['client_bind_status'], 200)
        self.assertFalse(any(k.startswith('client_bind_') for k in self.received(first_id)[1]))
        self.assertEqual(self.h.recorder.health()['validation_rejections'], 0)

    def test_invalid_or_oversized_diagnostics_do_not_break_binding_or_leak(self):
        payloads = ['null', '{', '{"cookie":"PRIVATE_SECRET"}', 'x' * 513,
                    '{"schema":1,"schema":1}']
        identifiers = []
        with TestClient(self.h.app) as client:
            self.h.login(client)
            for i, payload in enumerate(payloads):
                request_id = 'req_' + f'{i:032x}'
                identifiers.append(request_id)
                response = client.post('/api/jobs/session', headers={
                    'X-Tiku-Background': '1', 'X-Request-ID': request_id,
                    'X-Tiku-Bind-Diagnostic': payload})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('PRIVATE_SECRET', response.text)
            self.assertEqual(self.h.f.calls, [])
        for request_id in identifiers:
            attrs = self.received(request_id)[1]
            self.assertFalse(any(k.startswith('client_bind_') for k in attrs))
            self.assertNotIn('PRIVATE_SECRET', json.dumps(attrs))
        self.assertEqual(self.h.recorder.health()['validation_rejections'], 0)
        self.diagnostic_log.assert_not_called()

    def test_diagnostics_ignored_on_other_routes_and_authentication_still_required(self):
        diagnostic = json.dumps(dict(schema=1, request_id='req_' + 'c' * 32,
                                     phase='fetch', code='REQUEST_TIMEOUT', elapsed_ms=15000, status=0))
        ids = ['req_' + 'd' * 32, 'req_' + 'e' * 32]
        with TestClient(self.h.app) as client:
            unauthorized = client.post('/api/jobs/session', headers={
                'X-Tiku-Background': '1', 'X-Tiku-Bind-Diagnostic': diagnostic})
            self.assertEqual(unauthorized.status_code, 401)
            self.h.login(client)
            self.assertEqual(client.get('/api/execution', headers={
                'X-Request-ID': ids[0], 'X-Tiku-Bind-Diagnostic': diagnostic}).status_code, 200)
            self.assertEqual(client.get('/api/jobs/session', headers={
                'X-Request-ID': ids[1], 'X-Tiku-Bind-Diagnostic': diagnostic}).status_code, 404)
            self.assertEqual(self.h.f.calls, [])
        for request_id in ids:
            self.assertFalse(any(k.startswith('client_bind_') for k in self.received(request_id)[1]))
        self.diagnostic_log.assert_not_called()

    def test_diagnostic_sink_failure_cannot_block_binding(self):
        diagnostic = json.dumps(dict(schema=1, request_id='req_' + 'f' * 32,
                                     phase='fetch', code='NETWORK_UNAVAILABLE', elapsed_ms=1, status=0))
        self.diagnostic_log.side_effect = OSError('isolated unavailable log sink')
        with TestClient(self.h.app) as client:
            context = self.h.login(client)
            response = client.post('/api/jobs/session', headers={
                'X-Tiku-Background': '1', 'X-Tiku-Bind-Diagnostic': diagnostic})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['execution'], context)
            self.assertEqual(self.h.f.calls, [])

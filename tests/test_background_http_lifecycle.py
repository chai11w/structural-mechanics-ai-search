"""Login/control lifecycle through real detached HTTP admission and SQLite."""
import gc
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tiku_agent.fastapi_demo import create_app
from tests.test_background_http import BackgroundHttpFixture


class BackgroundHttpLifecycleTests(unittest.TestCase):
    def retire(self, h, client, reason):
        if reason == 'logout':
            cookies = dict(client.cookies)
            self.assertEqual(client.post('/api/invite/logout', follow_redirects=False).status_code, 303)
            client.cookies.update(cookies)  # prove replay of the original login is rejected
        elif reason in ('disabled', 'deleted'):
            h.control.set_invitation_status(h.identity.invite_id, 'disabled' if reason == 'disabled' else 'archived')
            if reason == 'deleted':
                h.control.delete_archived_invitation(h.identity.invite_id)
        else:
            with h.f.authority.transaction() as conn:
                table = 'execution_sessions' if reason == 'session_expired' else 'execution_dispatch_grants'
                conn.execute('UPDATE ' + table + ' SET expires=?', (h.f.clock[0] - 1,))

    def submit(self, h, client, context):
        response = client.post('/api/jobs', json={'kind': 'handle_text', 'parameters': {'text': 'hello'}}, headers=h.headers(context))
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['job']['operation_id']

    def test_queued_authority_loss_never_starts_or_exposes_job(self):
        for reason in ('logout', 'disabled', 'deleted', 'grant_expired', 'session_expired'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                h = BackgroundHttpFixture(root)
                # Hold automatic polling until the accepted grant is retired.
                with TestClient(h.app) as client, patch.object(h.app.state.background.worker, 'run_once', return_value=False):
                    # The worker can run before this patch only before any job exists.
                    context = h.login(client)
                    operation = self.submit(h, client, context)
                    self.retire(h, client, reason)
                    self.assertIn(client.get('/api/jobs/' + operation).status_code, (401, 409))
                    # Invoke the real claim boundary while automatic polling is held.
                    self.assertIsNone(h.f.dispatch.claim_next())
                    self.assertEqual(h.f.calls, [])
                    self.assertEqual(h.f.rows('execution_operations')[0]['status'], 'FAILED')
                    self.assertEqual(h.f.rows('execution_attempts'), [])
                gc.collect()

    def test_running_authority_loss_keeps_charge_but_fences_late_result(self):
        for reason in ('logout', 'disabled', 'deleted', 'grant_expired', 'session_expired'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                h = BackgroundHttpFixture(root)
                h.f.release.clear()
                try:
                    with TestClient(h.app) as client:
                        context = h.login(client)
                        operation = self.submit(h, client, context)
                        self.assertTrue(h.f.entered.wait(5))
                        self.retire(h, client, reason)
                        self.assertIn(client.get('/api/jobs/' + operation).status_code, (401, 409))
                        h.f.release.set()
                        limit = time.monotonic() + 5
                        while time.monotonic() < limit and h.f.rows('execution_dispatch')[0]['status'] == 'CLAIMED':
                            time.sleep(.02)
                        row = h.f.rows('execution_operations')[0]
                        self.assertEqual(row['status'], 'UNKNOWN')
                        self.assertIsNone(row['result'])
                        self.assertEqual(h.f.calls, ['hello'])
                        self.assertEqual(h.f.rows('execution_effects')[0]['status'], 'CONFIRMED')
                        self.assertEqual(h.f.rows('execution_cost_outbox')[0]['status'], 'CONFIRMED')
                finally:
                    h.f.release.set()
                    gc.collect()

    def test_persisted_background_database_cannot_silently_disable_worker(self):
        with tempfile.TemporaryDirectory() as root:
            h = BackgroundHttpFixture(root)
            before = h.f.rows('execution_dispatch')
            with self.assertRaisesRegex(ValueError, 'background_execution=True'):
                create_app(runtime=h.f.runtime, incoming_dir=h.root / 'incoming', background_execution=False)
            self.assertEqual(h.f.rows('execution_dispatch'), before)
            h.recorder.close()
            gc.collect()

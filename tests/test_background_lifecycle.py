"""Detached lifecycle boundaries: durable state, real stores and counted calls."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
import io
import os
from pathlib import Path
from PIL import Image
from tiku_agent.background_publication import BackgroundPublication
from tiku_agent.execution_retention import plan_cleanup, apply_cleanup
from tiku_agent.execution_store import ExecutionError
from tiku_agent.execution_dispatch import DispatchStore
from tiku_agent.execution_worker import BackgroundWorker
from tiku_shared.response_store import SQLiteResponseStore

from tests.test_execution_dispatch import DispatchFixture


class BackgroundLifecycleTests(unittest.TestCase):
    def test_unknown_schema_or_changed_queue_policy_preserves_durable_jobs(self):
        for kind in ('dispatch_schema', 'publication_schema', 'policy'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                f = DispatchFixture(root)
                responses = SQLiteResponseStore(Path(root) / 'responses.db')
                BackgroundPublication(f.dispatch, responses)
                f.accept()
                before = f.rows('execution_dispatch_inputs')
                if kind != 'policy':
                    with f.authority.transaction() as conn:
                        conn.execute("UPDATE execution_meta SET value='unknown' WHERE key=?", (kind,))
                with self.assertRaises(ExecutionError):
                    if kind == 'publication_schema':
                        BackgroundPublication(f.dispatch, responses)
                    else:
                        DispatchStore(f.runtime, authorize=lambda *args: True,
                                      policy=replace(f.policy, max_queued=1) if kind == 'policy' else f.policy)
                self.assertEqual(f.rows('execution_dispatch_inputs'), before)
                self.assertEqual(f.rows('execution_dispatch')[0]['status'], 'WAITING')
                self.assertEqual(f.rows('execution_attempts'), [])
                self.assertEqual(f.calls, [])

    def test_close_during_dequeue_authorization_leaves_original_queue_for_restart(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            ack, _, _ = f.accept()
            entered, release = threading.Event(), threading.Event()
            def authorize(*args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test authorizer not released')
                return True
            f.dispatch.authorize = authorize
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(f.worker.run_once)
                try:
                    self.assertTrue(entered.wait(3))
                    f.worker.close(drain_seconds=0)
                finally:
                    release.set()
                self.assertFalse(future.result(timeout=5))
            self.assertEqual(f.calls, [])
            self.assertEqual(f.rows('execution_dispatch')[0]['status'], 'WAITING')
            self.assertEqual(f.rows('execution_attempts'), [])

    def test_bounded_shutdown_preserves_queue_and_reports_live_provider(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            f.release.clear()
            f.accept('first')
            _, request, grant = f.accept('second', sid='other')
            deadline = f.rows('execution_dispatch')[1]['deadline']
            f.worker.start()
            try:
                self.assertTrue(f.entered.wait(3))
                started = time.monotonic()
                result = f.worker.close(drain_seconds=.01)
                self.assertLess(time.monotonic() - started, .5)
                self.assertFalse(result['drained'])
                self.assertEqual(result['running_workers'], 1)
                with self.assertRaises(ExecutionError) as caught:
                    f.accept('third', sid='third')
                self.assertEqual(caught.exception.code, 'EXECUTION_SHUTTING_DOWN')
            finally:
                f.release.set()
                self.assertTrue(f.worker.close(drain_seconds=5)['drained'])
            self.assertEqual(f.calls, ['first'])
            runtime, dispatch = f.build()
            worker = BackgroundWorker(dispatch, Path(root) / 'restart')
            self.assertTrue(worker.run_once())
            self.assertEqual(f.calls, ['first', 'second'])
            self.assertEqual(dispatch.observe('other', 'invite', grant, request)['queue_deadline'], deadline)

    def test_cleanup_preserves_unexpired_repaired_publication(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            publication = BackgroundPublication(f.dispatch, SQLiteResponseStore(Path(root) / 'responses.db'))
            worker = BackgroundWorker(f.dispatch, Path(root) / 'published-worker', publication=publication)
            ack, _, _ = f.accept()
            worker.run_once()
            f.clock[0] += 32 * 86400
            with f.authority.transaction() as conn:
                conn.execute('UPDATE execution_publications SET expires=?', (f.clock[0] + 86400,))
            plan = plan_cleanup(f.authority.path, now=f.clock[0])
            self.assertNotIn(ack['operation_id'], plan['operations'])
            self.assertEqual(f.runtime.execution_operations.maintain()['operations_removed'], 0)
            self.assertEqual(publication.view(ack['operation_id'])['status'], 'READY')

    def test_reviewed_cleanup_rejects_changed_background_evidence(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as backups:
            f = DispatchFixture(root)
            ack, _, _ = f.accept()
            f.worker.run_once()
            f.clock[0] += 32 * 86400
            plan = plan_cleanup(f.authority.path, now=f.clock[0])
            self.assertIn(ack['operation_id'], plan['operations'])
            with f.authority.transaction() as conn:
                conn.execute('UPDATE execution_dispatch SET progress_version=progress_version+1')
            with self.assertRaises(ExecutionError) as caught:
                apply_cleanup(f.runtime.execution_operations, plan, backup_dir=Path(backups) / 'reviewed')
            self.assertEqual(caught.exception.code, 'EXECUTION_MAINTENANCE_PLAN_CHANGED')
            self.assertEqual(len(f.rows('execution_dispatch_inputs')), 1)
            self.assertFalse((Path(backups) / 'reviewed').exists())

    def test_cancelled_unknown_effect_keeps_input_and_materialized_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            data = io.BytesIO()
            Image.new('RGB', (8, 8), 'white').save(data, format='PNG')
            req, grant = f.request(), f.grant()
            ack = f.dispatch.accept('s', 'invite', grant, req, 'handle_image', {}, image=data.getvalue())
            claimed = f.dispatch.claim_next()
            path = f.worker._materialize(claimed[0], claimed[2], claimed[3]['extension'])
            # A sent request without confirmation remains uncertain after reset.
            with f.authority.transaction() as conn:
                conn.execute("INSERT INTO execution_cost_runs VALUES ('run',?,?)", (claimed[0].operation_id, claimed[0].attempt_id))
                conn.execute("INSERT INTO execution_effects (call_id,run_id,operation_id,attempt_id,provider,model,call_type,status,created,updated) VALUES ('call','run',?,?, 'test','test','test','SENT',?,?)",
                             (claimed[0].operation_id, claimed[0].attempt_id, f.clock[0], f.clock[0]))
            f.runtime.clear('s', operation_request=f.request(), identity_key='invite')
            f.clock[0] += f.policy.input_ttl + 1
            os.utime(path, (f.clock[0] - f.policy.input_ttl - 1,) * 2)
            f.worker.maintain()
            f.clock[0] += f.policy.input_ttl + 1
            f.worker.maintain()
            self.assertEqual(f.rows('execution_operations')[0]['status'], 'CANCELLED')
            self.assertEqual(len(f.rows('execution_dispatch_inputs')), 1)
            self.assertTrue(path.exists())

    def test_authority_loss_after_last_send_blocks_late_state_and_result(self):
        for reason in ('logout', 'disabled', 'unavailable', 'expired'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                f = DispatchFixture(root)
                ack, request, grant = f.accept()
                before = f.authority.context('s')['state_version']

                def revoke():
                    if reason == 'logout':
                        f.dispatch.revoke_grant('s', 'invite', grant)
                    elif reason == 'disabled':
                        f.allowed = False
                    elif reason == 'unavailable':
                        def unavailable(*args):
                            raise OSError('synthetic control outage')
                        f.dispatch.authorize = unavailable
                    else:
                        with f.authority.transaction() as conn:
                            conn.execute('UPDATE execution_sessions SET expires=?', (f.clock[0] - 1,))

                f.after_call = revoke
                self.assertTrue(f.worker.run_once())
                self.assertEqual(f.calls, ['hello'])
                operation = f.rows('execution_operations')[0]
                self.assertEqual(operation['status'], 'UNKNOWN')
                self.assertIsNone(operation['result'])
                self.assertEqual(f.rows('execution_sessions')[0]['version'], before)
                self.assertEqual(f.rows('execution_effects')[0]['status'], 'CONFIRMED')
                self.assertEqual(f.rows('execution_cost_outbox')[0]['status'], 'CONFIRMED')
                self.assertFalse(f.worker.run_once())
                self.assertEqual(f.calls, ['hello'])

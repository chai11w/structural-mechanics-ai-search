"""Detached A3 hard crashes preserve exact child and per-unit receipts."""
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4

from PIL import Image

from tiku_agent.background_publication import BackgroundPublication
from tiku_agent.execution_dispatch import DispatchStore
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_store import _WRITER
from tiku_agent.execution_worker import BackgroundWorker
from tiku_shared.model_costs import timed_model_call
from tiku_shared.response_store import SQLiteResponseStore
from tests.test_a3_runtime import FakeAutoCropper
from tests.test_execution_processes import process_a3_runtime, CRASH_EXIT


def recovery_runtime(root, now, *, automatic=False, point=''):
    runtime = process_a3_runtime(root, now)
    if automatic:
        runtime.auto_crop_max_workers = 1
        runtime.auto_cropper = FakeAutoCropper(second_status='auto_ready')
        original = runtime.crop_verifier.verify
        def verify(*args):
            def provider():
                with closing(sqlite3.connect(Path(root) / 'provider.db')) as conn, conn:
                    conn.execute('INSERT INTO requests VALUES (?,?)', (uuid4().hex, _WRITER.get().operation_id))
                if point == 'batch_unknown' and args[2]['unit_id'] == 'g1-u2':
                    os._exit(CRASH_EXIT)
                return original(*args)
            return timed_model_call(provider, provider='dashscope', model='qwen3-vl-plus',
                                    call_type='a3-recovery', usage_getter=lambda _: {'input_tokens': 100, 'output_tokens': 20})
        runtime.crop_verifier.verify = verify
    return runtime


def attach_worker(runtime, root):
    dispatch = DispatchStore(runtime, authorize=lambda identity, version: identity == 'local' and version == 1)
    publication = BackgroundPublication(dispatch, SQLiteResponseStore(Path(root) / 'responses.db'))
    return dispatch, BackgroundWorker(dispatch, Path(root) / 'background-inputs', publication=publication), publication


def recovery_process(root, now, point):
    runtime = recovery_runtime(root, now, automatic=point.startswith('batch'), point=point)
    dispatch, worker, publication = attach_worker(runtime, root)
    if point == 'child_missing':
        runtime.a2_runtime.handle_prechecked_image = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == 'child_saved':
        runtime._after_a2_response = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == 'parent_saved':
        original = runtime._after_a2_response
        def applied(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(CRASH_EXIT)
        runtime._after_a2_response = applied
    elif point == 'batch_partial':
        import tiku_agent.a3_runtime as module
        original = module.submit_with_trace_context
        futures = []
        def submitted(*args, **kwargs):
            if futures:
                futures[0].result(timeout=10)  # exact first unit and its fee are durable
                os._exit(CRASH_EXIT)
            future = original(*args, **kwargs)
            futures.append(future)
            return future
        module.submit_with_trace_context = submitted
    elif point == 'batch_complete':
        import tiku_agent.execution_units as module
        module.finish_batch = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    worker.run_once()
    raise AssertionError('fault point not reached')


class BackgroundA3RecoveryTests(unittest.TestCase):
    def request(self):
        value = self.runtime.execution_operations.authority.context('s')
        return OperationRequest(uuid4().hex, value['epoch'], value['state_version'])

    def rows(self, table):
        with self.runtime.execution_operations.authority.reading() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def calls(self):
        with closing(sqlite3.connect(self.root / 'provider.db')) as conn:
            return conn.execute('SELECT count(*) FROM requests').fetchone()[0]

    def crash(self, root, point):
        self.root = Path(root)
        now = time.time()
        automatic = point.startswith('batch')
        self.runtime = recovery_runtime(root, now, automatic=automatic)
        page = self.root / 'page.png'
        Image.new('RGB', (1000, 800), 'white').save(page)
        self.runtime.handle_image('s', page, operation_request=self.request())
        if not automatic:
            self.runtime.select_unit('s', 'g1-u1', operation_request=self.request())
        state = self.runtime.store.load('s')
        dispatch, worker, publication = attach_worker(self.runtime, root)
        grant = dispatch.create_grant('s', 'local', auth_version=1, expires_at=now + 3600)
        target = {'task_revision': state.task_revision, 'workflow_search_id': state.workflow_search_id}
        kind = 'prepare_units' if automatic else 'handle_crop'
        parameters = ({'unit_ids': ['g1-u1', 'g1-u2'], **target} if automatic else
                      {'unit_id': 'g1-u1', 'bounds': {'x': 0, 'y': 0, 'width': 1, 'height': 1}, **target})
        ack = dispatch.accept('s', 'local', grant, self.request(), kind, parameters)
        process = multiprocessing.get_context('spawn').Process(target=recovery_process, args=(root, now, point))
        process.start()
        try:
            process.join(20)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, CRASH_EXIT)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
        self.runtime = recovery_runtime(root, now + 6, automatic=automatic)
        self.dispatch, self.worker, self.publication = attach_worker(self.runtime, root)
        self.worker.maintain()
        return ack['operation_id']

    def recover(self, source):
        before = self.calls()
        request = self.request()
        self.runtime.recover_operation('s', source, operation_request=request)
        self.runtime.recover_operation('s', source, operation_request=request)
        self.assertEqual(self.calls(), before)
        self.assertFalse(self.worker.run_once())
        self.publication.repair_once()
        self.assertEqual(self.publication.view(source)['status'], 'READY')
        dispatch = next(row for row in self.rows('execution_dispatch') if row['operation_id'] == source)
        self.assertEqual((dispatch['status'], dispatch['progress_stage'], dispatch['error_code']), ('SETTLED', 'completed', ''))
        self.assertEqual(len([row for row in self.rows('execution_attempts') if row['operation_id'] == source]), 1)

    def test_child_receipt_and_parent_commit_recover_without_second_child_call(self):
        for point in ('child_saved', 'parent_saved'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                source = self.crash(root, point)
                self.assertEqual(self.calls(), 1)
                handoff = self.rows('execution_handoffs')[0]
                self.assertEqual(handoff['operation_id'], source)
                self.assertEqual(handoff['unit_id'], 'g1-u1')
                self.recover(source)
                self.assertEqual(self.rows('execution_handoffs')[0]['status'], 'COMMITTED')
                with closing(sqlite3.connect(self.root / 'costs.db')) as conn:
                    self.assertEqual(conn.execute('SELECT count(*),sum(total_tokens) FROM model_cost_calls').fetchone(), (1, 120))

    def test_missing_child_receipt_stays_unknown(self):
        from tiku_agent.session_runtime import AgentProtocolError
        with tempfile.TemporaryDirectory() as root:
            source = self.crash(root, 'child_missing')
            with self.assertRaises(AgentProtocolError):
                self.runtime.recover_operation('s', source, operation_request=self.request())
            self.assertEqual(self.calls(), 0)
            self.assertFalse(self.worker.run_once())
            self.assertEqual(next(row['status'] for row in self.rows('execution_operations') if row['id'] == source), 'UNKNOWN')

    def test_unknown_unit_is_not_promoted_by_confirmed_sibling_receipt(self):
        from tiku_agent.session_runtime import AgentProtocolError
        with tempfile.TemporaryDirectory() as root:
            source = self.crash(root, 'batch_unknown')
            rows = {row['unit_id']: row for row in self.rows('execution_unit_checks')}
            self.assertEqual(rows['g1-u1']['status'], 'CONFIRMED')
            self.assertEqual(rows['g1-u2']['status'], 'RUNNING')
            self.assertEqual(self.calls(), 2)
            with self.assertRaises(AgentProtocolError):
                self.runtime.recover_operation('s', source, operation_request=self.request())
            self.assertFalse(self.worker.run_once())
            self.assertEqual(self.calls(), 2)
            self.assertEqual(next(row['status'] for row in self.rows('execution_operations') if row['id'] == source), 'UNKNOWN')

    def test_changed_producer_cannot_apply_saved_child_to_current_state(self):
        from tiku_agent.session_runtime import AgentProtocolError
        with tempfile.TemporaryDirectory() as root:
            source = self.crash(root, 'child_saved')
            before = self.rows('execution_states')
            self.runtime.execution_operations.configuration_version = lambda: 'changed configuration'
            with self.assertRaises(AgentProtocolError):
                self.runtime.recover_operation('s', source, operation_request=self.request())
            self.assertEqual(self.rows('execution_states'), before)
            self.assertEqual(self.calls(), 1)

    def test_partial_and_complete_batch_restore_only_confirmed_units(self):
        for point, count in (('batch_partial', 1), ('batch_complete', 2)):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                source = self.crash(root, point)
                self.assertEqual(self.calls(), count)
                receipts = self.rows('execution_unit_checks')
                self.assertEqual(len(receipts), count)
                self.assertTrue(all(row['status'] == 'CONFIRMED' and row['operation_id'] == source for row in receipts))
                self.recover(source)
                crops = self.runtime.store.load('s').auto_crops
                self.assertEqual(crops['g1-u1']['validation_status'], 'auto_ready')
                self.assertEqual(crops['g1-u2']['validation_status'], 'auto_ready' if count == 2 else 'manual_required')
                with closing(sqlite3.connect(self.root / 'costs.db')) as conn:
                    self.assertEqual(conn.execute('SELECT count(*),sum(total_tokens) FROM model_cost_calls').fetchone(), (count, count * 120))

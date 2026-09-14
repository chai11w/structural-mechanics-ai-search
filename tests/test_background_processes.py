"""Hard process exits through the detached entry, with durable provider counts."""
from contextlib import closing
import multiprocessing
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4
from urllib.request import urlopen
from PIL import Image

from tiku_agent.background_publication import BackgroundPublication
from tiku_agent.execution_dispatch import DispatchStore
from tiku_agent.execution_effects import ExecutionEffects
from tiku_agent.execution_maintenance import plan_cost_reconciliation, apply_cost_reconciliation
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_worker import BackgroundWorker
from tiku_shared.response_store import SQLiteResponseStore
from tests.test_execution_processes import process_runtime, CRASH_EXIT


def detached_runtime(root, now, point=''):
    runtime = process_runtime(root, now, point=point)
    def handle_image(agent, path, **kwargs):
        agent.state.start_search(str(path), search_id=uuid4().hex)
        return agent.handle_text('image')
    runtime.agent_factory.handle_image = handle_image
    dispatch = DispatchStore(runtime, authorize=lambda identity, version: identity == 'local' and version == 1)
    publication = BackgroundPublication(dispatch, SQLiteResponseStore(Path(root) / 'responses.db'))
    worker = BackgroundWorker(dispatch, Path(root) / 'background-inputs', publication=publication)
    return runtime, dispatch, worker, publication


def detached_process(root, now, point, connection):
    runtime, dispatch, worker, publication = detached_runtime(root, now, point)
    store = dispatch.store
    context = store.context('s')
    request = OperationRequest(uuid4().hex, context['epoch'], context['state_version'])
    grant = dispatch.create_grant('s', 'local', auth_version=1, expires_at=now + 3600)
    if point.startswith('image_'):
        data = io.BytesIO()
        Image.new('RGB', (10, 10), 'white').save(data, format='PNG')
        ack = dispatch.accept('s', 'local', grant, request, 'handle_image', {}, image=data.getvalue())
    else:
        ack = dispatch.accept('s', 'local', grant, request, 'handle_text', {'text': 'persisted input'})
    connection.send((ack, request, grant))
    if point in ('accepted', 'image_accepted'):
        os._exit(CRASH_EXIT)
    if point == 'image_materialized':
        original = worker._materialize
        def materialized(*args):
            original(*args)
            os._exit(CRASH_EXIT)
        worker._materialize = materialized
    if point == 'claimed':
        worker._run_claimed = lambda *args: os._exit(CRASH_EXIT)
    elif point == 'prepared':
        ExecutionEffects.model_sent = lambda *args: os._exit(CRASH_EXIT)
    elif point == 'before_confirmation':
        ExecutionEffects.model_finished = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == 'business_committed':
        original = runtime.execution_operations.finish
        def committed(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(CRASH_EXIT)
        runtime.execution_operations.finish = committed
    elif point == 'response_committed':
        original = publication.responses.finalize
        def committed(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(CRASH_EXIT)
        publication.responses.finalize = committed
    worker.run_once()
    raise AssertionError('fault point was not reached: ' + point)


def competing_worker(root, now, ready, start, outcomes):
    runtime, dispatch, worker, publication = detached_runtime(root, now)
    ready.put(True)
    if not start.wait(15):
        raise AssertionError('race never started')
    outcomes.put(worker.run_once())


def unrelated_listener(ready, stop):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'isolated sentinel')
        def log_message(self, *args):
            pass
    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        server.timeout = .1
        ready.put(server.server_port)
        while not stop.is_set():
            server.handle_request()


class BackgroundProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'runtime'
        self.backup = Path(temporary.name) / 'backups'
        self.now = time.time()

    def crash(self, point):
        parent, child = multiprocessing.Pipe(duplex=False)
        process = multiprocessing.get_context('spawn').Process(
            target=detached_process, args=(str(self.root), self.now, point, child))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(20), 'fixture did not accept its command')
            self.ack, self.request, self.grant = parent.recv()
            process.join(20)
            self.assertFalse(process.is_alive(), 'fixture did not reach its fault point')
            self.assertEqual(process.exitcode, CRASH_EXIT)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            parent.close()
        self.runtime, self.dispatch, self.worker, self.publication = detached_runtime(self.root, self.now + 6)
        self.addCleanup(self.worker.close)

    def rows(self, table):
        with self.dispatch.store.reading() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def calls(self):
        with closing(sqlite3.connect(self.root / 'provider.db')) as conn:
            return conn.execute('SELECT count(*) FROM requests').fetchone()[0]

    def reconcile(self):
        plan = plan_cost_reconciliation(self.dispatch.store.path, self.runtime.cost_ledger.path, now=self.now + 6)
        return apply_cost_reconciliation(self.runtime.execution_operations, self.runtime.cost_ledger.path,
                                        plan, backup_dir=self.backup / uuid4().hex)

    def test_unclaimed_restart_runs_saved_input_once_without_extending_deadline(self):
        self.crash('accepted')
        deadline = self.rows('execution_dispatch')[0]['deadline']
        self.assertEqual(self.calls(), 0)
        self.worker.start()
        limit = time.monotonic() + 10
        while time.monotonic() < limit and self.rows('execution_operations')[0]['status'] != 'SUCCEEDED':
            time.sleep(.02)
        self.assertTrue(self.worker.close()['drained'])
        self.assertEqual(self.rows('execution_operations')[0]['status'], 'SUCCEEDED')
        self.assertEqual(self.rows('execution_dispatch')[0]['deadline'], deadline)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len(self.rows('execution_attempts')), 1)
        result = self.dispatch.observe('s', 'local', self.grant, self.request, result=True)['result']
        self.assertEqual(result.text, 'saved:persisted input')

    def test_competing_restart_workers_and_unrelated_service_isolation(self):
        self.crash('accepted')
        context = multiprocessing.get_context('spawn')
        ready, outcomes, sentinel_ready = context.Queue(), context.Queue(), context.Queue()
        start, stop = context.Event(), context.Event()
        sentinel = context.Process(target=unrelated_listener, args=(sentinel_ready, stop))
        workers = [context.Process(target=competing_worker,
                   args=(str(self.root), self.now + 6, ready, start, outcomes)) for _ in range(2)]
        sentinel.start()
        try:
            port = sentinel_ready.get(timeout=15)
            url = 'http://127.0.0.1:' + str(port)
            with urlopen(url, timeout=3) as response:
                self.assertEqual(response.read(), b'isolated sentinel')
            for worker in workers:
                worker.start()
            for _ in workers:
                self.assertTrue(ready.get(timeout=15))
            start.set()
            self.assertCountEqual([outcomes.get(timeout=15) for _ in workers], [True, False])
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(self.calls(), 1)
            self.assertEqual(len(self.rows('execution_attempts')), 1)
            self.assertTrue(sentinel.is_alive())
            with urlopen(url, timeout=3) as response:
                self.assertEqual(response.read(), b'isolated sentinel')
        finally:
            start.set()
            stop.set()
            for process in [*workers, sentinel]:
                if process.pid is not None:
                    process.join(5)
                    if process.is_alive():
                        process.terminate()
                        process.join(10)
            for queue in (ready, outcomes, sentinel_ready):
                queue.close()

    def test_restart_rejects_expired_corrupt_revoked_and_changed_producer_input(self):
        for reason in ('deadline', 'payload', 'missing', 'revoked', 'producer'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.crash('accepted')
                with self.dispatch.store.transaction() as conn:
                    if reason == 'deadline':
                        self.dispatch.store.now = lambda: self.now + 56
                    elif reason == 'payload':
                        conn.execute("UPDATE execution_dispatch_inputs SET payload='{}'")
                    elif reason == 'missing':
                        conn.execute('DELETE FROM execution_dispatch_inputs')
                    elif reason == 'revoked':
                        conn.execute('UPDATE execution_dispatch_grants SET revoked=1')
                    else:
                        self.runtime.execution_operations.configuration_version = lambda: 'changed release'
                self.assertFalse(self.worker.run_once())
                self.assertEqual(self.calls(), 0)
                self.assertEqual(self.rows('execution_operations')[0]['status'], 'FAILED')
                self.assertEqual(self.rows('execution_dispatch')[0]['status'], 'REVOKED')
                self.assertEqual(self.rows('execution_attempts'), [])

    def test_claimed_without_effect_is_unknown_and_never_requeued(self):
        self.crash('claimed')
        self.worker.maintain()
        self.assertEqual(self.rows('execution_operations')[0]['status'], 'UNKNOWN')
        self.assertEqual(len(self.rows('execution_dispatch_inputs')), 1)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.calls(), 0)
        self.assertEqual(len(self.rows('execution_attempts')), 1)

    def test_image_restart_uses_private_bytes_and_corruption_never_calls_provider(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.crash('image_accepted')
                self.assertFalse(list(self.root.glob('background-inputs/*')))
                if corrupt:
                    with self.dispatch.store.transaction() as conn:
                        conn.execute("UPDATE execution_dispatch_inputs SET image=x'00'")
                self.assertEqual(self.worker.run_once(), not corrupt)
                self.assertEqual(self.calls(), 0 if corrupt else 1)
                self.assertEqual(self.rows('execution_operations')[0]['status'], 'FAILED' if corrupt else 'SUCCEEDED')
                self.assertFalse(self.worker.run_once())

    def test_hard_death_after_materialization_keeps_unknown_private_evidence(self):
        self.crash('image_materialized')
        paths = list((self.root / 'background-inputs').glob('*.png'))
        self.assertEqual(len(paths), 1)
        before = paths[0].read_bytes()
        self.dispatch.store.now = lambda: self.now + 32 * 86400
        os.utime(paths[0], (self.now - 32 * 86400,) * 2)
        self.worker.maintain()
        self.assertEqual(self.rows('execution_operations')[0]['status'], 'UNKNOWN')
        self.assertEqual(len(self.rows('execution_dispatch_inputs')), 1)
        self.assertEqual(paths[0].read_bytes(), before)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.calls(), 0)

    def test_effect_crash_windows_preserve_send_count_and_exact_accounting(self):
        for point, count, status in (
                ('prepared', 0, 'PREPARED'), ('provider_received', 1, 'UNKNOWN'),
                ('before_confirmation', 1, 'UNKNOWN'), ('response_confirmed', 1, 'CONFIRMED')):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.crash(point)
                self.worker.maintain()
                self.assertEqual(self.rows('execution_operations')[0]['status'], 'UNKNOWN')
                self.assertEqual(self.rows('execution_effects')[0]['status'], status)
                self.assertFalse(self.worker.run_once())
                self.assertEqual(self.calls(), count)
                if point in ('prepared', 'response_confirmed'):
                    self.assertEqual(self.reconcile()['confirmed_runs'], 1)
                    with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
                        expected = (0, None) if count == 0 else (1, 120)
                        self.assertEqual(conn.execute('SELECT count(*),sum(total_tokens) FROM model_cost_calls').fetchone(), expected)
                else:
                    plan = plan_cost_reconciliation(self.dispatch.store.path, self.runtime.cost_ledger.path, now=self.now + 6)
                    self.assertEqual(plan['items'][0]['reason'], 'UNKNOWN_USAGE')
                self.assertFalse(self.worker.run_once())
                self.assertEqual(self.calls(), count)

    def test_committed_restart_only_repairs_publication_and_reuses_response(self):
        for point in ('business_committed', 'response_committed'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.crash(point)
                self.assertEqual(self.rows('execution_operations')[0]['status'], 'SUCCEEDED')
                self.dispatch.store.now = lambda: self.now + 20  # original publication lease expires
                self.publication.repair_once()
                result = self.publication.view(self.ack['operation_id'])
                self.assertEqual(result['status'], 'READY')
                response_id = result['result']['response_id']
                self.publication.repair_once()
                self.assertEqual(self.publication.view(self.ack['operation_id'])['result']['response_id'], response_id)
                self.assertFalse(self.worker.run_once())
                self.assertEqual(self.calls(), 1)
                self.assertEqual(len(self.rows('execution_attempts')), 1)
                with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
                    self.assertEqual(conn.execute('SELECT count(*),sum(total_tokens) FROM model_cost_calls').fetchone(), (1, 120))

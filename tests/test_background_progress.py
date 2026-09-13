"""Persist the original public progress without leaking arbitrary provider text."""
import queue
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from tests.test_background_http import BackgroundHttpFixture
from tests.test_execution_dispatch import DispatchFixture


class BackgroundProgressTests(unittest.TestCase):
    def test_live_and_reconnected_observers_receive_steps_and_bounded_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            h = BackgroundHttpFixture(directory)
            stages = [
                ("triage", "正在检查图片并决定处理路线…"),
                ("a3_understanding", "正在理解整页题目和图形关系…"),
                ("a3_auto_validating", "已完成 1/2 张自动裁图校验…"),
                ("a3_auto_validating", "已完成 2/2 张自动裁图校验…"),
                ("searching", "正在按「4力法」搜索题目…"),
                ("searching", "private provider detail"),
            ]
            reached = queue.Queue()
            gates = [threading.Event() for _ in stages]
            original = h.f.runtime.handle_text
            def progressing(sid, text, **kwargs):
                for index, (stage, message) in enumerate(stages):
                    kwargs["progress"](stage, message)
                    reached.put(index)
                    if not gates[index].wait(10):
                        raise TimeoutError("progress test gate")
                return original(sid, text, **kwargs)
            h.f.runtime.handle_text = progressing
            with TestClient(h.app) as client:
                try:
                    context = h.login(client)
                    ack = client.post('/api/jobs', json={'kind':'handle_text','parameters':{'text':'hello'}}, headers=h.headers(context))
                    self.assertEqual(ack.status_code, 202)
                    op = ack.json()['job']['operation_id']
                    previous = 0
                    for index, (stage, message) in enumerate(stages):
                        self.assertEqual(reached.get(timeout=5), index)
                        job = client.get('/api/jobs/' + op).json()['job']
                        expected = {'type':'progress','stage':stage,'message':message}
                        if index == len(stages)-1:
                            expected = {'type':'progress','stage':'working','message':'正在处理当前请求…'}
                        self.assertEqual(job['progress'], expected)
                        self.assertGreater(job['progress_version'], previous)
                        previous = job['progress_version']
                        # Repeated observation uses persisted progress and starts no call.
                        self.assertEqual(client.get('/api/jobs/' + op).json()['job']['progress'], expected)
                        self.assertEqual(h.f.calls, [])
                        with h.f.authority.reading() as conn:
                            stored = conn.execute('SELECT progress_message FROM execution_dispatch WHERE operation_id=?',(op,)).fetchone()[0]
                            self.assertEqual(stored, expected['message'])
                        gates[index].set()
                    self.assertEqual(h.wait_result(client, op)['status'], 'SUCCEEDED')
                    self.assertEqual(h.f.calls, ['hello'])
                finally:
                    for gate in gates: gate.set()

    def test_additive_progress_upgrade_preserves_original_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = DispatchFixture(directory)
            ack, request, grant = fixture.accept()
            with fixture.authority.transaction() as conn:
                conn.execute('ALTER TABLE execution_dispatch DROP COLUMN progress_message')
            runtime, dispatch = fixture.build()
            restored = dispatch.observe('s', 'invite', grant, request)
            self.assertEqual(restored['operation_id'], ack['operation_id'])
            self.assertEqual(restored['queue_deadline'], ack['queue_deadline'])
            self.assertEqual(restored['status'], 'REGISTERED')
            self.assertEqual(fixture.calls, [])

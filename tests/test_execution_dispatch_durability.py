"""Crash/commit, shared admission and private-storage failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tiku_agent.execution_dispatch import DispatchPolicy
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_store import ExecutionError
from tiku_agent.execution_worker import BackgroundWorker
from tests.test_execution_dispatch import DispatchFixture


def _crash_accept(root, after_commit, connection):
    fixture = DispatchFixture(root)
    request, grant = fixture.request(), fixture.grant()
    if not after_commit:
        original = fixture.authority.transaction
        @contextmanager
        def crash_before_commit():
            from tiku_agent.execution_store import _TRANSACTION
            outer = _TRANSACTION.get() is None
            with original() as conn:
                yield conn
                if outer:
                    connection.send({"request": asdict(request), "grant": grant, "window": "uncommitted"})
                    os._exit(0)
        fixture.authority.transaction = crash_before_commit
    ack = fixture.dispatch.accept("s", "invite", grant, request, "handle_text", {"text": "original"})
    # The caller is killed after COMMIT and before it can deliver its HTTP ACK.
    connection.send({"request": asdict(request), "grant": grant, "window": "committed", "operation_id": ack["operation_id"]})
    os._exit(0)


class ExecutionDispatchDurabilityTests(unittest.TestCase):
    def test_process_death_before_commit_or_before_ack(self):
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as root:
                parent, child = multiprocessing.Pipe(duplex=False)
                process = multiprocessing.get_context("spawn").Process(target=_crash_accept, args=(root, committed, child))
                process.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(15))
                    message = parent.recv()
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                    parent.close()
                fixture = DispatchFixture(root)
                request = OperationRequest(**message["request"])
                found = fixture.observe(request, message["grant"])
                if committed:
                    self.assertEqual(found["operation_id"], message["operation_id"])
                    self.assertTrue(fixture.worker.run_once())
                    self.assertEqual(fixture.observe(request, message["grant"], result=True)["result"].text, "reply:original")
                    self.assertEqual(fixture.calls, ["original"])
                else:
                    self.assertIsNone(found)
                    self.assertEqual(fixture.rows("execution_dispatch_inputs"), [])
                    self.assertFalse(fixture.worker.run_once())
                    self.assertEqual(fixture.calls, [])

    def test_accept_cannot_return_inside_an_uncommitted_callers_transaction(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            request, grant = f.request(), f.grant()
            with f.authority.transaction():
                with self.assertRaises(ExecutionError) as caught:
                    f.accept(request=request, grant=grant)
            self.assertEqual(caught.exception.code, "EXECUTION_ACCEPTANCE_NESTED")
            self.assertEqual(f.rows("execution_operations"), [])

    def test_wait_budget_is_not_restarted_during_dequeue_admission(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            _, req, grant = f.accept()
            original = f.dispatch.authorize
            def delayed_authorizer(identity, version):
                f.clock[0] += 56
                return original(identity, version)
            with patch.object(f.dispatch, "authorize", delayed_authorizer):
                self.assertFalse(f.worker.run_once())
            self.assertEqual(f.observe(req, grant)["error_code"], "EXECUTION_QUEUE_TIMEOUT")
            self.assertEqual(f.calls, [])

    def test_two_workers_share_capacity_without_serializing_available_slots(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root, policy=replace(DispatchPolicy(), max_concurrent=2, max_queued=1))
            # Select distinct runtime stripes so the inherited gate can run both.
            sid2 = next(str(i) for i in range(1000) if f.runtime._lock(str(i)) is not f.runtime._lock("s"))
            f.accept()
            f.accept(sid=sid2)
            f.accept(sid="queued")
            _, dispatch2 = f.build()
            second = BackgroundWorker(dispatch2, f.root / "worker2")
            f.release.clear()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker.run_once) for worker in (f.worker, second)]
                try:
                    deadline = time.monotonic() + 5
                    while len(f.calls) < 2 and time.monotonic() < deadline:
                        threading.Event().wait(0.01)
                    self.assertEqual(len(f.calls), 2)
                    self.assertIsNone(f.dispatch.claim_next())
                finally:
                    f.release.set()
                self.assertTrue(all(future.result(5) for future in futures))
            self.assertEqual(len(f.rows("execution_attempts")), 2)

    def test_record_grant_and_byte_limits_are_durable(self):
        for limit in ("max_records", "max_grants", "max_total_input_bytes"):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as root:
                kwargs = {"max_queued": 0, limit: 1 if limit != "max_total_input_bytes" else 400}
                f = DispatchFixture(root, policy=replace(DispatchPolicy(), **kwargs))
                _, req, grant = f.accept("a" * 100)
                f.worker.run_once()
                with self.assertRaises(ExecutionError) as caught:
                    f.accept("b" * 300)
                self.assertEqual(caught.exception.code, "EXECUTION_CAPACITY")
                self.assertEqual(len(f.rows("execution_dispatch")), 1)
                self.assertEqual(f.calls, ["a" * 100])

    def test_scratch_failure_settles_without_model_and_keeps_private_input(self):
        with tempfile.TemporaryDirectory() as root:
            import io
            from PIL import Image
            f = DispatchFixture(root)
            image = io.BytesIO()
            Image.new("RGB", (100, 100)).save(image, format="PNG")
            req, grant = f.request(), f.grant()
            f.dispatch.accept("s", "invite", grant, req, "handle_image", {}, image=image.getvalue())
            with patch.object(f.worker, "_materialize", side_effect=OSError("private file failure")):
                f.worker.run_once()
            self.assertEqual(f.observe(req, grant)["status"], "FAILED")
            self.assertEqual(f.calls, [])
            self.assertEqual(len(f.rows("execution_dispatch_inputs")), 1)
            self.assertFalse(f.worker.run_once())

    def test_worker_dies_after_safe_failure_or_business_commit_before_settlement(self):
        for succeeded in (False, True):
            with self.subTest(succeeded=succeeded), tempfile.TemporaryDirectory() as root:
                from tiku_agent.execution_runtime import execute_claimed
                f = DispatchFixture(root)
                _, req, grant = f.accept()
                writer, operation, private, parsed = f.dispatch.claim_next()
                if succeeded:
                    execute_claimed(f.runtime, "s", writer, lambda: f.runtime.handle_text("s", "hello", identity_key="invite"))
                else:
                    f.runtime.execution_operations.fail(writer, known_not_started=True)
                before = f.observe(req, grant)
                self.assertEqual(before["dispatch_status"], "CLAIMED")
                f.dispatch.maintain()
                after = f.observe(req, grant)
                self.assertEqual(after["status"], "SUCCEEDED" if succeeded else "FAILED")
                self.assertEqual(after["dispatch_status"], "SETTLED")
                self.assertGreater(after["progress_version"], before["progress_version"])
                self.assertEqual(after["progress_stage"], "completed" if succeeded else "failed")
                self.assertFalse(f.worker.run_once())
                self.assertEqual(f.calls, ["hello"] if succeeded else [])

    def test_real_isolated_control_database_revokes_queued_grant(self):
        from tiku_admin.control_store import SQLiteControlStore
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            control = SQLiteControlStore(f.root / "control.db")
            record, _ = control.create_invitation(label="phase6 isolated test")
            f.dispatch.authorize = lambda identity, version: control.active_invitation(identity, version) is not None
            f.runtime._budget_policy = control
            req = f.request()
            grant = f.dispatch.create_grant("s", record.invite_id, auth_version=record.auth_version, expires_at=f.clock[0] + 3600)
            f.dispatch.accept("s", record.invite_id, grant, req, "handle_text", {"text": "private body"})
            control.set_invitation_status(record.invite_id, "disabled")
            self.assertFalse(f.worker.run_once())
            self.assertEqual(f.rows("execution_dispatch")[0]["error_code"], "EXECUTION_AUTH_REQUIRED")
            self.assertEqual(f.calls, [])
            self.assertEqual(f.rows("execution_attempts"), [])

    def test_pending_accounting_after_queue_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            from dataclasses import replace
            f.authority.policy = replace(f.authority.policy, max_global_unresolved_cost_calls=1,
                                         block_on_pending_costs=True)
            f.accept("timeout", sid="first")
            _, req, grant = f.accept(sid="second")
            self.assertTrue(f.worker.run_once())
            self.assertFalse(f.worker.run_once())
            self.assertEqual(f.observe(req, grant, "second")["error_code"], "EXECUTION_COST_PENDING")
            self.assertEqual(f.calls, ["timeout"])

    def test_expiry_and_lease_are_rechecked_after_control_read(self):
        for expiry in ("grant", "lease"):
            with self.subTest(expiry=expiry), tempfile.TemporaryDirectory() as root:
                f = DispatchFixture(root)
                _, req, grant = f.accept()
                writer, operation, private, parsed = f.dispatch.claim_next()
                if expiry == "grant":
                    with f.authority.transaction() as conn:
                        conn.execute("UPDATE execution_dispatch_grants SET expires=?", (f.clock[0] + 100,))
                def delayed_authorizer(identity, version):
                    f.clock[0] += 301
                    return True
                with patch.object(f.dispatch, "authorize", delayed_authorizer):
                    with self.assertRaises(ExecutionError) as caught:
                        f.dispatch.check_running(writer, private, grant)
                self.assertEqual(caught.exception.code, "EXECUTION_AUTH_REQUIRED" if expiry == "grant" else "EXECUTION_LEASE_LOST")
                self.assertEqual(f.calls, [])

    def test_truncated_jpeg_is_rejected_before_durable_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            import io
            from PIL import Image
            f = DispatchFixture(root)
            image = io.BytesIO()
            Image.new("RGB", (400, 400)).save(image, format="JPEG")
            incomplete = image.getvalue()[:-100]
            # This specific corruption passes Pillow's header-only verifier.
            with Image.open(io.BytesIO(incomplete)) as headers:
                headers.verify()
            req, grant = f.request(), f.grant()
            with self.assertRaises(ExecutionError) as caught:
                f.dispatch.accept("s", "invite", grant, req, "handle_image", {}, image=incomplete)
            self.assertEqual(caught.exception.code, "EXECUTION_INPUT_INVALID")
            self.assertEqual(f.rows("execution_dispatch"), [])
            self.assertEqual(f.calls, [])

    def test_reset_remains_available_and_withdraws_queued_work(self):
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            f.accept()
            f.runtime.clear("s", operation_request=f.request(), identity_key="invite")
            self.assertFalse(f.worker.run_once())
            self.assertEqual(f.rows("execution_dispatch")[0]["status"], "SETTLED")
            self.assertEqual(f.rows("execution_operations")[0]["status"], "CANCELLED")
            self.assertEqual(f.calls, [])

    def test_runtime_launcher_rejects_existing_outside_and_production_paths(self):
        from scripts.run_phase6_kernel_probe import isolated_root, BASE
        for path in (BASE / ".tmp_tiku_agent_v2_prod_8790", BASE / ".tmp_feishu_tiku",
                     BASE / ".tmp_phase6_runtime", BASE / ".tmp_phase6_runtime" / ".." / "outside"):
            with self.assertRaises(ValueError):
                isolated_root(path)


if __name__ == "__main__":
    unittest.main()

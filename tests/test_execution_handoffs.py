from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from fastapi.testclient import TestClient

from tiku_agent.a3_runtime import A3MvpRuntime
from tiku_agent.agent import TikuSearchAgent
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution, OPERATION_HEADER
from tiku_agent.fastapi_demo import create_app, SESSION_COOKIE
from tiku_agent.execution_store import ExecutionStore, ExecutionSessionStore, _WRITER
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime, AgentProtocolError
from tiku_agent.tools import ToolResult
from tiku_shared.atomic_files import atomic_output
from tiku_shared.execution_hooks import execution_effect_scope
from tiku_agent.execution_effects import ExecutionEffects
from tests.test_a3_runtime import FakeObserver, FakeVerifier, FakeAutoCropper
from tests.test_tiku_agent_session_runtime import FakeTools


class ExecutionHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ExecutionStore(self.root / "execution.db")
        self.image = self.root / "page.jpg"
        Image.new("RGB", (100, 100), "white").save(self.image)
        tools = FakeTools().toolbox()
        self.analysis_count = 0
        def analyze(*args, **kwargs):
            self.analysis_count += 1
            return ToolResult(ok=True, data={"loads": [{"type":"集中", "raw":"P"}], "chapter_hint":"4力法"})
        tools.analyze_image = analyze
        self.a2 = AgentSessionRuntime(ExecutionSessionStore(self.store), artifacts=SessionArtifacts(self.root / "a2"),
            agent_factory=lambda state:TikuSearchAgent(state=state, tools=tools, use_llm_intent=False))
        self.a3 = A3MvpRuntime(store=ExecutionSessionStore(self.store, "workflow"), artifacts=SessionArtifacts(self.root / "a3"),
            a2_runtime=self.a2, page_observer=FakeObserver(), crop_verifier=FakeVerifier())
        attach_execution(self.a3, self.store)

    def request(self):
        context = self.store.context("s")
        return OperationRequest(uuid4().hex, context["epoch"], context["state_version"])

    def prepare(self):
        self.a3.handle_image("s", self.image, operation_request=self.request())
        self.a3.select_unit("s", "g1-u1", operation_request=self.request())

    def crop(self):
        return self.a3.handle_crop("s", {"x":0, "y":0, "width":1, "height":1}, unit_id="g1-u1", operation_request=self.request())

    def rows(self, table):
        with self.store.transaction() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def test_child_result_and_parent_ack_have_exact_persistent_relationship(self):
        self.prepare()
        self.crop()
        handoff = self.rows("execution_handoffs")[0]
        self.assertEqual(handoff["status"], "COMMITTED")
        self.assertEqual(handoff["unit_id"], "g1-u1")
        child = next(row for row in self.rows("execution_tasks") if row["id"] == handoff["child_record_id"])
        self.assertEqual(child["parent_id"], handoff["parent_record_id"])
        self.assertEqual(child["state_version"], handoff["child_version"])
        self.assertEqual(self.analysis_count, 1)

    def test_crash_after_child_preserves_response_and_does_not_fake_parent_finish(self):
        self.prepare()
        with patch.object(self.a3, "_after_a2_response", side_effect=RuntimeError("before parent finish")):
            with self.assertRaisesRegex(RuntimeError, "before parent finish"):
                self.crop()
        handoff = self.rows("execution_handoffs")[0]
        self.assertEqual(handoff["status"], "CHILD_READY")
        self.assertIn("response", handoff["result"])
        self.assertIsNotNone(self.a2.store.load("s"))
        self.assertEqual(self.a3.store.load("s").phase, "A2_ACTIVE")
        operation = next(row for row in self.rows("execution_operations") if row["id"] == handoff["operation_id"])
        self.assertEqual(operation["status"], "UNKNOWN")
        self.assertEqual(self.analysis_count, 1)

    def test_explicit_recovery_finishes_parent_without_rerunning_child(self):
        self.prepare()
        original_request = self.request()
        bounds = {"x":0, "y":0, "width":1, "height":1}
        with patch.object(self.a3, "_after_a2_response", side_effect=RuntimeError("before parent finish")):
            with self.assertRaises(RuntimeError):
                self.a3.handle_crop("s", bounds, unit_id="g1-u1", operation_request=original_request)
        source = self.rows("execution_handoffs")[0]["operation_id"]
        request = self.request()
        result = self.a3.recover_operation("s", source, operation_request=request)
        again = self.a3.recover_operation("s", source, operation_request=request)
        self.assertTrue(again.execution_receipt["replayed"])
        self.assertEqual(result.text, again.text)
        replay = self.a3.handle_crop("s", bounds, unit_id="g1-u1", operation_request=original_request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(self.analysis_count, 1)
        self.assertTrue(all(row["status"] == "COMMITTED" for row in self.rows("execution_handoffs")))
        self.assertEqual(next(row["status"] for row in self.rows("execution_operations") if row["id"] == source), "SUCCEEDED")

    def test_invalid_recovery_is_atomic_and_does_not_strand_a_new_operation(self):
        self.prepare()
        before = len(self.rows("execution_operations"))
        with self.assertRaises(AgentProtocolError) as caught:
            self.a3.recover_operation("s", "missing-operation", operation_request=self.request())
        self.assertEqual(caught.exception.code, "EXECUTION_RECOVERY_INVALID")
        self.assertEqual(len(self.rows("execution_operations")), before)

    def http_headers(self):
        request = self.request()
        return {OPERATION_HEADER: json.dumps({"key":request.key, "epoch":request.epoch, "state_version":request.state_version}),
                "Sec-Fetch-Site":"same-origin", "X-Session-Coordination-Version":"6",
                "X-Session-Request-Fence":f"{int(time.time()*1000)}:{uuid4().hex}"}

    def test_http_recovery_consumes_saved_child_once_and_rejects_changed_command(self):
        self.prepare()
        with patch.object(self.a3, "_after_a2_response", side_effect=RuntimeError("before parent finish")):
            with self.assertRaises(RuntimeError):
                self.crop()
        source = self.rows("execution_handoffs")[0]["operation_id"]
        with TestClient(create_app(runtime=self.a3, incoming_dir=self.root/"incoming")) as client:
            client.cookies.set(SESSION_COOKIE, "s")
            headers = self.http_headers()
            payload = {"source_operation_id":source}
            first = client.post("/api/execution/recover", json=payload, headers=headers)
            self.assertEqual(first.status_code, 200, first.text)
            replay = client.post("/api/execution/recover", json=payload, headers=headers)
            self.assertTrue(replay.json()["operation"]["replayed"], replay.text)
            changed = client.post("/api/execution/recover", json={"source_operation_id":"different"}, headers=headers)
            self.assertEqual(changed.status_code, 409, changed.text)
            self.assertEqual(changed.json()["code"], "EXECUTION_INPUT_CONFLICT")
            self.assertEqual(client.post("/api/execution/recover", json=payload).status_code, 409)
        self.assertEqual(self.analysis_count, 1)

    def test_http_reset_and_control_do_not_wait_for_inflight_v6_request(self):
        for command in ("child", "reset"):
            with self.subTest(command=command):
                self.prepare()
                entered, release = threading.Event(), threading.Event()
                original_factory = self.a2.agent_factory
                def factory(state):
                    agent = original_factory(state)
                    original = agent.tools.analyze_image
                    def blocked(*args, **kwargs):
                        entered.set()
                        if not release.wait(10):
                            raise RuntimeError("test release missing")
                        return original(*args, **kwargs)
                    agent.tools.analyze_image = blocked
                    return agent
                parent = self.a3.store.load("s")
                target = {"workflow_id":parent.workflow_search_id or parent.current_search_id,
                          "task_revision":parent.task_revision, "unit_id":"g1-u1"}
                self.a2.agent_factory = factory
                app = create_app(runtime=self.a3, incoming_dir=self.root/"incoming")
                with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
                    client.cookies.set(SESSION_COOKIE, "s")
                    pending = pool.submit(client.post, "/api/a3/crop/stream", json={**target,
                        "bounds":{"x":0,"y":0,"width":1,"height":1}}, headers=self.http_headers())
                    try:
                        self.assertTrue(entered.wait(5))
                        status = pool.submit(client.get, "/api/execution").result(timeout=2)
                        self.assertEqual(status.status_code, 200, status.text)
                        self.assertEqual(status.json()["task_state"]["workflow"]["phase"], "A2_ACTIVE")
                        headers = self.http_headers()
                        path = "/api/reset" if command == "reset" else "/api/execution/control"
                        result = pool.submit(client.post, path, headers=headers,
                            json={} if command == "reset" else {"scope":"child", "target":target}).result(timeout=2)
                        self.assertEqual(result.status_code, 200, result.text)
                        repeat = pool.submit(client.post, path, headers=headers,
                            json={} if command == "reset" else {"scope":"child", "target":target}).result(timeout=2)
                        self.assertEqual(repeat.status_code, 200, repeat.text)
                        self.assertIsNone(self.a2.store.load("s"))
                        self.assertFalse(pending.done())
                    finally:
                        release.set()
                        self.a2.agent_factory = original_factory
                    stopped = pending.result(timeout=5)
                    events = [json.loads(line) for line in stopped.text.splitlines()]
                    self.assertTrue(any(event["type"] == "error" for event in events), stopped.text)
                self.assertIsNone(self.a2.store.load("s"))

    def test_boolean_revision_cannot_authorize_a_control(self):
        self.prepare()
        parent = self.a3.store.load("s")
        target = {"workflow_id":parent.workflow_search_id or parent.current_search_id, "task_revision":True}
        before = len(self.rows("execution_operations"))
        with self.assertRaises(AgentProtocolError) as caught:
            self.a3.control_execution("s", "workflow", target, operation_request=self.request())
        self.assertEqual(caught.exception.code, "EXECUTION_CONTROL_INVALID")
        self.assertEqual(len(self.rows("execution_operations")), before)

    def auto_page(self):
        Image.new("RGB", (1000, 800), "white").save(self.image)
        self.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        self.a3.handle_image("s", self.image, operation_request=self.request())

    def test_partial_batch_retry_keeps_confirmed_success_and_retries_failed_unit(self):
        self.auto_page()
        counts = {"g1-u1":0, "g1-u2":0}
        original = self.a3._validate_auto_crop
        def validate(state, unit_id, **kwargs):
            counts[unit_id] += 1
            if unit_id == "g1-u2" and counts[unit_id] == 1:
                return {"validation_status":"manual_required", "external_load_status":"not_run",
                        "verification_checks":{}, "error_type":"local_validation_failed"}
            return original(state, unit_id, **kwargs)
        with patch.object(self.a3, "_validate_auto_crop", side_effect=validate):
            self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
            self.assertEqual(self.a3.store.load("s").auto_crops["g1-u1"]["validation_status"], "auto_ready")
            self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
        self.assertEqual(counts, {"g1-u1":1, "g1-u2":2})
        self.assertEqual(len(self.rows("execution_unit_checks")), 3)
        self.assertTrue(all(row["status"] == "CONFIRMED" for row in self.rows("execution_unit_checks")))

    def test_batch_recovery_uses_worker_receipts_when_parent_final_save_never_happened(self):
        from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call
        ledger = SQLiteModelCostLedger(self.root / "costs.db")
        self.a3.cost_ledger = self.a2.cost_ledger = ledger
        verify = self.a3.crop_verifier.verify
        self.a3.crop_verifier.verify = lambda *args: timed_model_call(
            lambda:verify(*args), provider="dashscope", model="qwen3-vl-plus", call_type="unit_test",
            usage_getter=lambda value:{"input_tokens":100, "output_tokens":20})
        self.auto_page()
        original_request = self.request()
        def progress(stage, message):
            if message.startswith("已完成"):
                raise RuntimeError("parent interrupted")
        with self.assertRaisesRegex(RuntimeError, "parent interrupted"):
            self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=original_request, progress=progress)
        self.assertEqual(self.a3.store.load("s").phase, "AUTO_VALIDATING_CROPS")
        rows = self.rows("execution_unit_checks")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "CONFIRMED" for row in rows))
        self.a3.recover_operation("s", rows[0]["operation_id"], operation_request=self.request())
        self.assertEqual(self.a3.store.load("s").phase, "WAIT_UNIT_SELECTION")
        replay = self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=original_request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertCountEqual(self.a3.crop_verifier.calls, ["g1-u1", "g1-u2"])
        with closing(sqlite3.connect(ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (2, 240))
        self.assertTrue(all(row["status"] == "CONFIRMED" for row in self.rows("execution_cost_outbox")))

    def test_batch_recovery_preserves_success_when_next_unit_was_never_submitted(self):
        self.auto_page()
        import tiku_agent.a3_runtime as module
        submit = module.submit_with_trace_context
        submitted = []
        def interrupt(executor, function, unit_id):
            submitted.append(unit_id)
            if len(submitted) == 2:
                raise RuntimeError("before second submission")
            return submit(executor, function, unit_id)
        with patch.object(module, "submit_with_trace_context", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "before second submission"):
                self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
        rows = self.rows("execution_unit_checks")
        self.assertEqual(len(rows), 1)
        self.a3.recover_operation("s", rows[0]["operation_id"], operation_request=self.request())
        self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
        self.assertCountEqual(self.a3.crop_verifier.calls, ["g1-u1", "g1-u2"])

    def test_batch_with_missing_result_receipt_stays_unknown(self):
        self.auto_page()
        from tiku_agent.execution_store import canonical
        def fail_result(value):
            if isinstance(value, dict):
                raise RuntimeError("receipt failed")
            return canonical(value)
        with patch("tiku_agent.execution_units.canonical", side_effect=fail_result):
            with self.assertRaises(AgentProtocolError) as caught:
                self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
        self.assertEqual(caught.exception.code, "EXECUTION_UNKNOWN")
        source = self.rows("execution_unit_checks")[0]["operation_id"]
        before = len(self.a3.crop_verifier.calls)
        with self.assertRaises(AgentProtocolError) as recovery:
            self.a3.recover_operation("s", source, operation_request=self.request())
        self.assertEqual(recovery.exception.code, "EXECUTION_UNKNOWN")
        self.assertEqual(len(self.a3.crop_verifier.calls), before)

    def test_changed_verifier_version_cannot_reuse_an_old_validation(self):
        self.auto_page()
        self.a3.crop_verifier.model = "model-a"
        self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request())
        self.a3.crop_verifier.model = "model-b"
        self.a3.select_unit("s", "g1-u1", operation_request=self.request())
        self.assertEqual(self.analysis_count, 0)
        self.assertEqual(self.a3.store.load("s").phase, "CROP_REQUIRED")

    def test_batch_recovery_rejects_changed_crop_bytes_without_new_calls(self):
        self.auto_page()
        def progress(stage, message):
            if message.startswith("已完成"):
                raise RuntimeError("parent interrupted")
        with self.assertRaises(RuntimeError):
            self.a3.prepare_units("s", ["g1-u1", "g1-u2"], operation_request=self.request(), progress=progress)
        source = self.rows("execution_unit_checks")[0]["operation_id"]
        state = self.a3.store.load("s")
        Path(state.auto_crops["g1-u1"]["path"]).write_bytes(b"changed crop")
        with self.assertRaises(AgentProtocolError) as caught:
            self.a3.recover_operation("s", source, operation_request=self.request())
        self.assertEqual(caught.exception.code, "EXECUTION_STALE")
        self.assertCountEqual(self.a3.crop_verifier.calls, ["g1-u1", "g1-u2"])

    def test_upload_auto_preparation_recovers_without_observing_or_grounding_again(self):
        Image.new("RGB", (1000, 800), "white").save(self.image)
        self.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")
        self.a3.auto_prepare_all_units = True
        original_request = self.request()
        def progress(stage, message):
            if message.startswith("已完成"):
                raise RuntimeError("upload interrupted in preparation")
        with self.assertRaises(RuntimeError):
            self.a3.handle_image("s", self.image, operation_request=original_request, progress=progress)
        source = self.rows("execution_unit_batches")[0]["operation_id"]
        self.a3.recover_operation("s", source, operation_request=self.request())
        replay = self.a3.handle_image("s", self.image, operation_request=original_request)
        self.assertTrue(replay.execution_receipt["replayed"])
        self.assertEqual(self.a3.page_observer.calls, 1)
        self.assertEqual(len(self.a3.auto_cropper.calls), 1)
        self.assertCountEqual(self.a3.crop_verifier.calls, ["g1-u1", "g1-u2"])
        self.assertEqual(self.a3.store.load("s").phase, "WAIT_UNIT_SELECTION")

    def test_child_stop_fences_a_real_inflight_runtime_without_waiting_for_it(self):
        self.prepare()
        entered = threading.Event()
        release = threading.Event()
        original_factory = self.a2.agent_factory
        def factory(state):
            agent = original_factory(state)
            original = agent.tools.analyze_image
            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release missing")
                return original(*args, **kwargs)
            agent.tools.analyze_image = blocked
            return agent
        self.a2.agent_factory = factory
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.crop)
            try:
                self.assertTrue(entered.wait(5))
                parent = self.a3.store.load("s")
                target = {"workflow_id":parent.workflow_search_id or parent.current_search_id,
                          "task_revision":parent.task_revision, "unit_id":parent.selected_unit_id}
                started = time.monotonic()
                response = self.a3.control_execution("s", "child", target, operation_request=self.request())
                self.assertLess(time.monotonic() - started, 2)
                self.assertIn("停止", response.text)
                self.assertIsNone(self.a2.store.load("s"))
                self.assertEqual(self.a3.store.load("s").selected_unit_id, "")
            finally:
                release.set()
            with self.assertRaises(AgentProtocolError):
                pending.result(timeout=5)
        self.assertIsNone(self.a2.store.load("s"))
        self.assertEqual(self.a3.store.load("s").selected_unit_id, "")
        self.assertTrue(any(row["status"] == "CANCELLED" for row in self.rows("execution_operations")))

    def test_workflow_stop_is_distinct_from_reset_and_replay_does_not_touch_new_page(self):
        self.prepare()
        parent = self.a3.store.load("s")
        target = {"workflow_id":parent.workflow_search_id or parent.current_search_id, "task_revision":parent.task_revision}
        request = self.request()
        self.a3.control_execution("s", "workflow", target, operation_request=request)
        self.assertTrue(self.a3.store.load("s").page_finished)
        self.a3.handle_image("s", self.image, operation_request=self.request())
        current = self.a3.store.load("s").workflow_search_id
        self.a3.control_execution("s", "workflow", target, operation_request=request)
        self.assertEqual(self.a3.store.load("s").workflow_search_id, current)
        self.assertFalse(self.a3.store.load("s").page_finished)

    def test_child_receipt_failure_rolls_back_child_state_in_same_transaction(self):
        self.prepare()
        with patch("tiku_agent.session_runtime.save_child_result", side_effect=RuntimeError("receipt write failure")):
            with self.assertRaisesRegex(RuntimeError, "receipt write failure"):
                self.crop()
        self.assertIsNone(self.a2.store.load("s"))
        self.assertEqual(self.rows("execution_handoffs"), [])
        self.assertFalse(any(row["kind"] == "child" for row in self.rows("execution_tasks")))
        self.assertTrue(self.rows("execution_files"))

    def test_parent_write_failure_rolls_back_parent_and_keeps_child_receipt(self):
        self.prepare()
        original = self.a3.store.save
        observed = []
        def fail_after_parent_write(state):
            child = self.a2.store.load("s")
            if child is not None:
                observed.append(self.store.read("s", "workflow")[1].version)
                original(state)
                raise RuntimeError("parent transaction failed")
            return original(state)
        with patch.object(self.a3.store, "save", side_effect=fail_after_parent_write):
            with self.assertRaisesRegex(RuntimeError, "parent transaction failed"):
                self.crop()
        self.assertEqual(self.store.read("s", "workflow")[1].version, observed[0])
        self.assertEqual(self.rows("execution_handoffs")[0]["status"], "CHILD_READY")
        self.assertIsNotNone(self.a2.store.load("s"))

    def test_atomic_artifact_never_publishes_partial_data(self):
        target = self.root / "result.jpg"
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with atomic_output(target) as temporary:
                temporary.write_bytes(b"partial")
                self.assertFalse(target.exists())
                raise RuntimeError("injected")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".part-*")), [])
        with atomic_output(target) as temporary:
            temporary.write_bytes(b"complete")
            self.assertFalse(target.exists())
        self.assertEqual(target.read_bytes(), b"complete")
        with self.assertRaises(FileExistsError):
            with atomic_output(target):
                pass

    def test_new_answer_copy_does_not_delete_an_earlier_operation_result(self):
        from tiku_agent.tools import AgentToolConfig, answer_candidate_tool
        source = self.root / "answer.txt"
        config = AgentToolConfig(session_dir=self.root / "answers")
        outputs = []
        for value in (b"first answer", b"changed answer"):
            source.write_bytes(value)
            ops = self.a3.execution_operations
            row = ops.register("s", "local", self.request(), "answer_copy", {})
            writer = ops.claim(row["id"])
            token = _WRITER.set(writer)
            try:
                with execution_effect_scope(ExecutionEffects(ops, writer, artifact_roots=[config.session_dir])):
                    with patch("tiku_agent.tools.search.find_answer_files", return_value=[source]):
                        result = answer_candidate_tool([{"rank":1,"path":str(self.image)}], rank=1, config=config)
                        self.assertTrue(result.ok, result)
                        outputs.append(Path(result.data["copied_paths"][0]))
                ops.finish(writer, {"schema":1,"response":None})
            finally:
                _WRITER.reset(token)
        self.assertNotEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0].read_bytes(), b"first answer")
        self.assertEqual(outputs[1].read_bytes(), b"changed answer")


if __name__ == "__main__":
    unittest.main()

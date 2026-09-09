"""Real process death and competing runtime evidence; no external model calls."""
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from uuid import uuid4

from tiku_agent.agent import AgentResponse
from tiku_agent.execution_effects import ExecutionEffects
from tiku_agent.execution_maintenance import plan_cost_reconciliation, apply_cost_reconciliation
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionPolicy, ExecutionStore, ExecutionSessionStore, _WRITER
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime, AgentProtocolError
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call


CRASH_EXIT = 91


def process_runtime(root, now, *, point="", entered=None, release=None):
    root = Path(root)
    authority = ExecutionStore(root / "execution.db", policy=ExecutionPolicy(lease_seconds=5), now=lambda: now)
    with closing(sqlite3.connect(root / "provider.db")) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")
    class Agent:
        config = None
        def __init__(self, state):
            self.state = state
        def handle_text(self, text):
            def provider():
                with closing(sqlite3.connect(root / "provider.db")) as conn, conn:
                    conn.execute("INSERT INTO requests VALUES (?,?)", (uuid4().hex, _WRITER.get().operation_id))
                if entered is not None:
                    entered.set()
                    if not release.wait(20):
                        raise TimeoutError("test provider was not released")
                if point == "provider_received":
                    os._exit(CRASH_EXIT)
                return {"usage": {"input_tokens": 100, "output_tokens": 20}}
            timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus", call_type="process-test",
                             usage_getter=lambda value: value["usage"])
            if point == "response_confirmed":
                os._exit(CRASH_EXIT)
            return AgentResponse(text="saved:" + text, state=self.state.to_dict(), intent="greeting")
    runtime = AgentSessionRuntime(ExecutionSessionStore(authority), artifacts=SessionArtifacts(root / "media"),
                                  agent_factory=Agent, cost_ledger=SQLiteModelCostLedger(root / "costs.db"))
    attach_execution(runtime, authority, configuration_version="process-fixture-v1")
    return runtime


def process_worker(root, now, request, point, *, ready=None, start=None, result=None, entered=None, release=None):
    runtime = process_runtime(root, now, point=point, entered=entered, release=release)
    if point == "registered":
        runtime.execution_operations.claim = lambda *args: os._exit(CRASH_EXIT)
    elif point == "before_send":
        ExecutionEffects.model_sent = lambda *args: os._exit(CRASH_EXIT)
    elif point == "before_confirmation":
        ExecutionEffects.model_finished = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == "committed":
        finish = runtime.execution_operations.finish
        def lose_ack(*args, **kwargs):
            finish(*args, **kwargs)
            os._exit(CRASH_EXIT)
        runtime.execution_operations.finish = lose_ack
    if ready is not None:
        ready.put(True)
        if not start.wait(20):
            raise TimeoutError("test race not started")
    try:
        runtime.handle_text("s", "test", operation_request=request)
        if result is not None:
            result.put("SUCCEEDED")
    except AgentProtocolError as exc:
        if result is None:
            raise
        result.put(exc.code)


def process_a3_runtime(root, now):
    from tiku_agent.a3_runtime import A3MvpRuntime
    from tiku_agent.agent import TikuSearchAgent
    from tiku_agent.tools import ToolResult
    from tests.test_a3_runtime import FakeObserver, FakeVerifier
    from tests.test_tiku_agent_session_runtime import FakeTools
    root = Path(root)
    a2 = process_runtime(root, now)
    tools = FakeTools().toolbox()
    def analyze(*args, **kwargs):
        def provider():
            with closing(sqlite3.connect(root / "provider.db")) as conn, conn:
                conn.execute("INSERT INTO requests VALUES (?,?)", (uuid4().hex, _WRITER.get().operation_id))
            return {"usage": {"input_tokens":100, "output_tokens":20},
                    "loads": [{"type":"集中", "raw":"P"}], "chapter_hint":"4力法"}
        result = timed_model_call(provider, provider="dashscope", model="qwen3-vl-plus", call_type="process-a3-child",
                                  usage_getter=lambda value: value["usage"])
        return ToolResult(ok=True, data=result)
    tools.analyze_image = analyze
    a2.agent_factory = lambda state: TikuSearchAgent(state=state, tools=tools, use_llm_intent=False)
    authority = a2.execution_operations.authority
    runtime = A3MvpRuntime(store=ExecutionSessionStore(authority, "workflow"), artifacts=SessionArtifacts(root / "a3"),
                          a2_runtime=a2, page_observer=FakeObserver(), crop_verifier=FakeVerifier(), cost_ledger=a2.cost_ledger)
    runtime.crop_verifier.execution_version = "process-verifier-v1"
    attach_execution(runtime, authority, configuration_version="process-a3-fixture-v1")
    return runtime


def process_a3_worker(root, now, request, point):
    runtime = process_a3_runtime(root, now)
    if point == "before_child":
        runtime.a2_runtime.handle_prechecked_image = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == "before_parent_finalize":
        runtime._after_a2_response = lambda *args, **kwargs: os._exit(CRASH_EXIT)
    elif point == "after_parent_finalize":
        finalize = runtime._after_a2_response
        def lose_ack(*args, **kwargs):
            finalize(*args, **kwargs)
            os._exit(CRASH_EXIT)
        runtime._after_a2_response = lose_ack
    runtime.handle_crop("s", {"x":0,"y":0,"width":1,"height":1}, unit_id="g1-u1", operation_request=request)


class ExecutionProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "runtime"
        self.backup_root = Path(temporary.name) / "backups"
        self.now = time.time()
        self.runtime = process_runtime(self.root, self.now)
        self.context = multiprocessing.get_context("spawn")

    def request(self):
        value = self.runtime.execution_operations.authority.context("s")
        return OperationRequest(uuid4().hex, value["epoch"], value["state_version"])

    def rows(self, table):
        with self.runtime.execution_operations.authority.transaction() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def calls(self):
        with closing(sqlite3.connect(self.root / "provider.db")) as conn:
            return conn.execute("SELECT count(*) FROM requests").fetchone()[0]

    def stop_process(self, process):
        if process.pid is None:
            return
        if process.is_alive():
            process.terminate()
        process.join(10)

    def crash(self, point):
        request = self.request()
        process = self.context.Process(target=process_worker, args=(str(self.root), self.now, request, point))
        process.start()
        self.addCleanup(self.stop_process, process)
        process.join(20)
        self.assertFalse(process.is_alive(), "crash fixture did not reach its fault point")
        self.assertEqual(process.exitcode, CRASH_EXIT)
        self.runtime = process_runtime(self.root, self.now + 6)
        return request

    def reconcile(self):
        operations = self.runtime.execution_operations
        plan = plan_cost_reconciliation(operations.authority.path, self.runtime.cost_ledger.path, now=self.now + 6)
        self.assertEqual([item["reason"] for item in plan["items"]], ["READY"])
        return apply_cost_reconciliation(operations, self.runtime.cost_ledger.path, plan, backup_dir=self.backup_root / uuid4().hex)

    def assert_unknown_replay(self, request):
        with self.assertRaises(AgentProtocolError) as error:
            self.runtime.handle_text("s", "test", operation_request=request)
        self.assertEqual(error.exception.code, "EXECUTION_UNKNOWN")

    def test_process_death_after_registration_can_resume_once(self):
        request = self.crash("registered")
        self.assertEqual(self.calls(), 0)
        self.runtime.handle_text("s", "test", operation_request=request)
        self.assertTrue(self.runtime.handle_text("s", "test", operation_request=request).execution_receipt["replayed"])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len(self.rows("execution_attempts")), 1)

    def test_process_death_after_send_keeps_unknown_and_does_not_repeat(self):
        request = self.crash("provider_received")
        self.assertEqual(self.calls(), 1)
        self.assert_unknown_replay(request)
        self.assertEqual(self.calls(), 1)
        plan = plan_cost_reconciliation(self.runtime.execution_operations.authority.path, self.runtime.cost_ledger.path, now=self.now + 6)
        self.assertEqual(plan["items"][0]["reason"], "UNKNOWN_USAGE")

    def test_process_death_before_response_confirmation_keeps_unknown(self):
        request = self.crash("before_confirmation")
        self.assert_unknown_replay(request)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.rows("execution_effects")[0]["status"], "UNKNOWN")

    def test_confirmed_response_before_process_death_can_be_accounted_without_replay(self):
        request = self.crash("response_confirmed")
        self.assertEqual(self.rows("execution_collectors")[0]["closed"], 0)
        self.assertEqual(self.rows("execution_effects")[0]["status"], "CONFIRMED")
        self.assertEqual(self.reconcile()["confirmed_runs"], 1)
        self.assert_unknown_replay(request)
        self.assertEqual(self.calls(), 1)
        with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))

    def test_process_death_after_commit_replays_without_new_call_or_charge(self):
        request = self.crash("committed")
        result = self.runtime.handle_text("s", "test", operation_request=request)
        self.assertTrue(result.execution_receipt["replayed"])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(len(self.rows("execution_attempts")), 1)
        with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))

    def test_unsent_call_after_process_death_can_be_closed_without_fabricated_usage(self):
        request = self.crash("before_send")
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.rows("execution_effects")[0]["status"], "PREPARED")
        self.assertEqual(self.reconcile()["confirmed_runs"], 1)
        effect = self.rows("execution_effects")[0]
        self.assertEqual(effect["status"], "NOT_SENT")
        self.assertIsNone(effect["record"])
        self.assertEqual(self.calls(), 0)
        self.assert_unknown_replay(request)
        with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM model_cost_calls").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT call_count,total_tokens FROM model_cost_runs").fetchone(), (0, 0))
        operations = self.runtime.execution_operations
        self.assertEqual(operations.observe("s", "local", request)["accounting"]["pending_runs"], 0)
        self.runtime.clear("s", operation_request=self.request())
        self.runtime.handle_text("s", "test", operation_request=self.request())
        self.assertEqual(self.calls(), 1)
        from tiku_agent.execution_retention import plan_cleanup
        future = self.now + 32 * 86400
        operations.authority.now = lambda: future
        cleanup = plan_cleanup(operations.authority.path, now=future)
        self.assertIn(effect["operation_id"], cleanup["operations"])

    def test_independent_runtimes_compete_without_second_provider_send(self):
        request = self.request()
        ready, result = self.context.Queue(), self.context.Queue()
        start, entered, release = self.context.Event(), self.context.Event(), self.context.Event()
        workers = [self.context.Process(target=process_worker, args=(str(self.root), self.now, request, ""),
            kwargs={"ready":ready, "start":start, "result":result, "entered":entered, "release":release}) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for _ in workers:
                self.assertTrue(ready.get(timeout=20))
            start.set()
            self.assertTrue(entered.wait(20))
            self.assertEqual(result.get(timeout=20), "EXECUTION_BUSY")
            self.assertEqual(self.calls(), 1)
            release.set()
            self.assertEqual(result.get(timeout=20), "SUCCEEDED")
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(self.calls(), 1)
            self.assertEqual(len(self.rows("execution_attempts")), 1)
        finally:
            start.set()
            release.set()
            for worker in workers:
                self.stop_process(worker)
            ready.close()
            result.close()

    def a3_crash(self, point):
        from PIL import Image
        image = self.root / "page.png"
        Image.new("RGB", (100, 100), "white").save(image)
        self.runtime = process_a3_runtime(self.root, self.now)
        self.runtime.handle_image("s", image, operation_request=self.request())
        self.runtime.select_unit("s", "g1-u1", operation_request=self.request())
        request = self.request()
        process = self.context.Process(target=process_a3_worker, args=(str(self.root), self.now, request, point))
        process.start()
        self.addCleanup(self.stop_process, process)
        process.join(20)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, CRASH_EXIT)
        self.runtime = process_a3_runtime(self.root, self.now + 6)
        source = next(row for row in self.rows("execution_operations") if row["op_key"] == request.key)
        return request, source["id"]

    def test_parent_process_death_before_child_save_does_not_invent_a_receipt(self):
        _, source = self.a3_crash("before_child")
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.rows("execution_handoffs"), [])
        self.assertEqual(self.runtime.store.load("s").phase, "A2_ACTIVE")
        with self.assertRaises(AgentProtocolError) as error:
            self.runtime.recover_operation("s", source, operation_request=self.request())
        self.assertEqual(error.exception.code, "EXECUTION_RECOVERY_INVALID")
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.runtime.store.load("s").phase, "A2_ACTIVE")

    def assert_a3_receipt_recovery(self, point, status):
        original, source = self.a3_crash(point)
        handoff = self.rows("execution_handoffs")[0]
        self.assertEqual(handoff["status"], status)
        self.assertEqual(handoff["unit_id"], "g1-u1")
        self.assertEqual(handoff["operation_id"], source)
        tasks = {row["id"]: row for row in self.rows("execution_tasks")}
        child = tasks[handoff["child_record_id"]]
        self.assertEqual(child["parent_id"], handoff["parent_record_id"])
        self.assertEqual(child["unit_id"], "g1-u1")
        self.runtime.recover_operation("s", source, operation_request=self.request())
        result = self.runtime.handle_crop("s", {"x":0,"y":0,"width":1,"height":1}, unit_id="g1-u1", operation_request=original)
        self.assertTrue(result.execution_receipt["replayed"])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.rows("execution_handoffs")[0]["status"], "COMMITTED")
        with closing(sqlite3.connect(self.runtime.cost_ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))

    def test_parent_process_death_after_child_save_recovers_exact_child(self):
        self.assert_a3_receipt_recovery("before_parent_finalize", "CHILD_READY")

    def test_parent_process_death_after_parent_save_recovers_without_repeating_child(self):
        self.assert_a3_receipt_recovery("after_parent_finalize", "PARENT_APPLIED")


if __name__ == "__main__":
    unittest.main()

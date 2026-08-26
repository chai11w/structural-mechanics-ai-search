from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.model_cost_report import load_report
from tiku_agent.agent import AgentResponse
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger
from tiku_shared.model_costs import (
    COST_SCHEMA_VERSION,
    ModelCostCollector,
    SQLiteModelCostLedger,
    estimate_cost,
    model_cost_scope,
    new_run_id,
    normalize_usage,
    submit_with_model_cost_context,
    timed_model_call,
)
from tiku_shared.trace_context import (
    TraceContext,
    current_trace_id,
    trace_context_scope,
)
from tiku_shared.trace_events import (
    SQLiteTraceEventStore,
    TraceEventRecorder,
    trace_event_scope,
)


class ModelCostTest(unittest.TestCase):
    def make_directory(self) -> Path:
        directory = Path(__file__).resolve().parents[1] / ".tmp_tests" / f"model_costs_{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return directory

    def test_timed_model_call_emits_joined_start_and_finished_events(self):
        directory = self.make_directory()
        store = SQLiteTraceEventStore(directory / "trace_events.sqlite3")
        recorder = TraceEventRecorder(store)
        trace = TraceContext.create(request_id="req_model_event_success")
        collector = ModelCostCollector(run_id=new_run_id())

        with trace_context_scope(trace), trace_event_scope(
            recorder,
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        ), model_cost_scope(collector):
            result = timed_model_call(
                lambda: {
                    "id": "provider-event-success",
                    "usage": {
                        "input_tokens": 100,
                        "image_tokens": 60,
                        "cached_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 120,
                    },
                    "attempts": 2,
                },
                provider="dashscope",
                model="qwen3.7-plus",
                call_type="qwen_image_classification",
                usage_getter=lambda value: value["usage"],
                provider_request_id_getter=lambda value: value["id"],
                attempt_count_getter=lambda value: value["attempts"],
            )

        self.assertEqual(result["id"], "provider-event-success")
        events = store.events_for_trace(trace.trace_id)
        self.assertEqual(
            [event.event_type for event in events],
            ["model_call_started", "model_call_finished"],
        )
        record = collector.records()[0]
        self.assertEqual(events[0].call_id, record.call_id)
        self.assertEqual(events[1].call_id, record.call_id)
        self.assertEqual(events[1].run_id, collector.run_id)
        self.assertEqual(events[1].provider_request_id, "provider-event-success")
        self.assertEqual(events[1].outcome, "success")
        self.assertEqual(events[1].safe_attributes["attempt_count"], 2)
        self.assertEqual(events[1].safe_attributes["total_tokens"], 120)
        self.assertEqual(events[1].safe_attributes["pricing_status"], "priced")
        self.assertGreaterEqual(events[1].duration_ms, 0)

    def test_timed_model_call_failure_records_only_exception_class(self):
        directory = self.make_directory()
        store = SQLiteTraceEventStore(directory / "trace_events.sqlite3")
        recorder = TraceEventRecorder(store)
        trace = TraceContext.create(request_id="req_model_event_failure")
        collector = ModelCostCollector(run_id=new_run_id())
        sensitive_message = "provider failed at C:\\private\\secret-key.txt"

        with trace_context_scope(trace), trace_event_scope(
            recorder,
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        ), model_cost_scope(collector):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                timed_model_call(
                    lambda: (_ for _ in ()).throw(RuntimeError(sensitive_message)),
                    provider="zhipu",
                    model="glm-4.6v",
                    call_type="zhipu_shape_rerank",
                    usage_getter=lambda value: value,
                )

        events = store.events_for_trace(trace.trace_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].event_type, "model_call_finished")
        self.assertEqual(events[-1].outcome, "error")
        self.assertEqual(events[-1].safe_attributes["error_kind"], "RuntimeError")
        self.assertNotIn(sensitive_message, str(events[-1].to_dict()))
        self.assertEqual(events[-1].call_id, collector.records()[0].call_id)

    def test_usage_extraction_failure_still_closes_model_event(self):
        directory = self.make_directory()
        store = SQLiteTraceEventStore(directory / "trace_events.sqlite3")
        recorder = TraceEventRecorder(store)
        trace = TraceContext.create(request_id="req_model_usage_failure")
        collector = ModelCostCollector(run_id=new_run_id())

        with trace_event_scope(
            recorder,
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        ), model_cost_scope(collector):
            with self.assertRaisesRegex(ValueError, "usage unavailable"):
                timed_model_call(
                    lambda: {"response": "not persisted"},
                    provider="dashscope",
                    model="qwen3.7-plus",
                    call_type="qwen_image_classification",
                    usage_getter=lambda _value: (_ for _ in ()).throw(
                        ValueError("usage unavailable in C:\\private")
                    ),
                )

        events = store.events_for_trace(trace.trace_id)
        self.assertEqual(
            [event.event_type for event in events],
            ["model_call_started", "model_call_finished"],
        )
        self.assertEqual(events[-1].outcome, "error")
        self.assertEqual(events[-1].safe_attributes["error_kind"], "ValueError")
        self.assertNotIn("C:\\private", str(events[-1].to_dict()))

    def test_cost_run_event_is_emitted_only_after_successful_transaction(self):
        directory = self.make_directory()
        store = SQLiteTraceEventStore(directory / "trace_events.sqlite3")
        recorder = TraceEventRecorder(store)
        ledger = SQLiteModelCostLedger(directory / "model_costs.sqlite3")
        trace = TraceContext.create(request_id="req_cost_run_event")
        collector = ModelCostCollector(
            run_id=new_run_id(),
            trace_id=trace.trace_id,
            task_kind="image",
        )
        collector.record(
            provider="dashscope",
            model="qwen3.7-plus",
            call_type="qwen_image_classification",
            status="success",
            started_at="2026-08-26T00:00:00+00:00",
            finished_at="2026-08-26T00:00:01+00:00",
            latency_ms=1000,
            usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        )

        with trace_event_scope(
            recorder,
            trace_id=trace.trace_id,
            request_id=trace.request_id,
        ):
            ledger.write_run(
                collector,
                finished_at="2026-08-26T00:00:01+00:00",
                outcome="success",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                ledger.write_run(
                    collector,
                    finished_at="2026-08-26T00:00:02+00:00",
                    outcome="error",
                )

        events = store.events_for_trace(trace.trace_id)
        self.assertEqual([event.event_type for event in events], ["cost_run_written"])
        self.assertEqual(events[0].run_id, collector.run_id)
        self.assertEqual(events[0].safe_attributes["call_count"], 1)
        self.assertEqual(events[0].safe_attributes["total_tokens"], 120)

    def test_trace_emission_failure_does_not_repeat_or_change_model_call(self):
        collector = ModelCostCollector(run_id=new_run_id())
        provider_calls = 0

        def provider_call():
            nonlocal provider_calls
            provider_calls += 1
            return {"usage": {"input_tokens": 10, "output_tokens": 2}}

        with patch(
            "tiku_shared.model_costs.record_trace_event",
            side_effect=RuntimeError("trace store unavailable"),
        ), model_cost_scope(collector):
            result = timed_model_call(
                provider_call,
                provider="dashscope",
                model="qwen3.7-plus",
                call_type="qwen_intent_decision",
                usage_getter=lambda value: value["usage"],
            )

        self.assertEqual(provider_calls, 1)
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(len(collector.records()), 1)

    def test_normalizes_qwen_and_zhipu_usage_shapes(self):
        qwen = normalize_usage({
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"image_tokens": 80, "cached_tokens": 10},
        })
        zhipu = normalize_usage({
            "prompt_tokens": 200,
            "completion_tokens": 30,
            "total_tokens": 230,
            "prompt_tokens_details": {"cached_tokens": 50},
        })
        self.assertEqual(qwen, {
            "input_tokens": 100, "image_tokens": 80, "cached_tokens": 10,
            "output_tokens": 20, "total_tokens": 120,
        })
        self.assertEqual(zhipu["input_tokens"], 200)
        self.assertEqual(zhipu["cached_tokens"], 50)
        self.assertEqual(zhipu["output_tokens"], 30)

    def test_prices_versioned_qwen_and_cached_zhipu_tokens(self):
        qwen = estimate_cost("dashscope", "qwen3.7-plus", {
            "input_tokens": 1000, "cached_tokens": 0, "output_tokens": 100,
        })
        zhipu = estimate_cost("zhipu", "glm-4.6v", {
            "input_tokens": 1000, "cached_tokens": 500, "output_tokens": 100,
        })
        self.assertEqual(qwen["estimated_cost_micros"], 2800)
        self.assertEqual(zhipu["estimated_cost_micros"], 900)
        self.assertEqual(qwen["pricing_status"], "priced")

    def test_current_qwen_cache_and_glm_5v_turbo_prices(self):
        cached_qwen = estimate_cost("dashscope", "qwen3.7-plus", {
            "input_tokens": 1000, "cached_tokens": 500, "output_tokens": 100,
        })
        glm_5v = estimate_cost("zhipu", "glm-5v-turbo", {
            "input_tokens": 1000, "cached_tokens": 0, "output_tokens": 100,
        })

        self.assertEqual(cached_qwen["estimated_cost_micros"], 2000)
        self.assertEqual(glm_5v["estimated_cost_micros"], 7200)
        self.assertEqual(glm_5v["pricing_status"], "priced")

    def test_new_run_ids_are_independent_from_request_and_trace_ids(self):
        first = new_run_id()
        second = new_run_id()

        self.assertRegex(first, r"^run_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)
        self.assertFalse(first.startswith("req_"))
        self.assertFalse(first.startswith("trace_"))

    def test_v2_database_migrates_additively_without_rewriting_history(self):
        directory = self.make_directory()
        database = directory / "costs.sqlite3"
        old_started = "2026-08-25T00:00:00+00:00"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE model_cost_runs (
                    run_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    identity_key TEXT NOT NULL DEFAULT '',
                    search_key TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    call_count INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    estimated_cost_micros INTEGER NOT NULL,
                    warning_codes_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE model_cost_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    call_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    image_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    error_kind TEXT NOT NULL,
                    price_version TEXT NOT NULL,
                    pricing_status TEXT NOT NULL,
                    estimated_cost_micros INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO model_cost_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "req_historical_run",
                    "old-session",
                    "old-identity",
                    "old-search",
                    "image",
                    old_started,
                    old_started,
                    "success",
                    1,
                    12,
                    900,
                    "[]",
                    2,
                ),
            )
            connection.execute(
                "INSERT INTO model_cost_calls VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "old-call",
                    "req_historical_run",
                    1,
                    "zhipu",
                    "glm-4.6v",
                    "legacy_call",
                    "success",
                    old_started,
                    old_started,
                    1,
                    10,
                    0,
                    0,
                    2,
                    12,
                    1,
                    "provider-old",
                    "",
                    "2026-08-01",
                    "priced",
                    900,
                    2,
                ),
            )

        trace = TraceContext.create(request_id="req_new_attempt")
        collector = ModelCostCollector(
            run_id=new_run_id(),
            trace_id=trace.trace_id,
            session_key="new-session",
            identity_key="new-identity",
            search_key="new-search",
            task_kind="image",
            started_at="2026-08-25T00:01:00+00:00",
        )
        collector.record(
            provider="zhipu",
            model="glm-4.6v",
            call_type="new_call",
            status="success",
            started_at="2026-08-25T00:01:00+00:00",
            finished_at="2026-08-25T00:01:01+00:00",
            latency_ms=1000,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
            provider_request_id="provider-new",
        )
        SQLiteModelCostLedger(database).write_run(
            collector,
            finished_at="2026-08-25T00:01:01+00:00",
            outcome="success",
        )

        with sqlite3.connect(database) as connection:
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(model_cost_runs)")
            }
            call_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(model_cost_calls)")
            }
            old_run = connection.execute(
                "SELECT trace_id, schema_version FROM model_cost_runs "
                "WHERE run_id = 'req_historical_run'"
            ).fetchone()
            old_call = connection.execute(
                "SELECT trace_id, provider_request_id, request_id, schema_version "
                "FROM model_cost_calls WHERE call_id = 'old-call'"
            ).fetchone()
            new_run = connection.execute(
                "SELECT trace_id, schema_version FROM model_cost_runs WHERE run_id = ?",
                (collector.run_id,),
            ).fetchone()
            new_call = connection.execute(
                "SELECT trace_id, provider_request_id, request_id, schema_version "
                "FROM model_cost_calls WHERE run_id = ?",
                (collector.run_id,),
            ).fetchone()

        self.assertIn("trace_id", run_columns)
        self.assertIn("trace_id", call_columns)
        self.assertIn("provider_request_id", call_columns)
        self.assertEqual(old_run, ("", 2))
        self.assertEqual(old_call, ("", "", "provider-old", 2))
        self.assertEqual(COST_SCHEMA_VERSION, 3)
        self.assertEqual(new_run, (trace.trace_id, 3))
        self.assertEqual(
            new_call,
            (trace.trace_id, "provider-new", "provider-new", 3),
        )
        report = load_report(database, days=36500)
        self.assertEqual(report["search_count"], 2)

    def test_budget_sum_read_only_reprices_historical_glm_5v_calls(self):
        directory = self.make_directory()
        database = directory / "costs.sqlite3"
        collector = ModelCostCollector(
            run_id="historical-glm-5v",
            identity_key="invite-001",
            search_key="workflow-1",
            task_kind="a3_auto_crop_grounding",
            started_at="2026-08-21T00:00:00+00:00",
        )
        collector.record(
            provider="zhipu",
            model="glm-5v-turbo",
            call_type="glm_a3_page_auto_crop",
            status="success",
            started_at="2026-08-21T00:00:00+00:00",
            finished_at="2026-08-21T00:00:01+00:00",
            latency_ms=1000,
            usage={"input_tokens": 1000, "output_tokens": 100},
        )
        ledger = SQLiteModelCostLedger(database)
        ledger.write_run(
            collector,
            finished_at="2026-08-21T00:00:01+00:00",
            outcome="success",
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE model_cost_calls SET pricing_status = 'missing_price', "
                "price_version = '', estimated_cost_micros = 0"
            )
            connection.execute(
                "UPDATE model_cost_runs SET estimated_cost_micros = 0"
            )

        self.assertEqual(
            ledger.estimated_cost_micros_since("2026-08-21T00:00:00+00:00"),
            7200,
        )

    def test_concurrent_calls_keep_the_parent_cost_scope(self):
        trace = TraceContext.create(request_id="req_thread_attempt")

        def provider_call():
            return {
                "trace_id": current_trace_id(),
                "id": "provider-thread",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }

        with trace_context_scope(trace):
            collector = ModelCostCollector(run_id=new_run_id())
            with model_cost_scope(collector):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        submit_with_model_cost_context(
                            executor,
                            timed_model_call,
                            provider_call,
                            provider="zhipu",
                            model="glm-4.6v",
                            call_type="zhipu_shape_rerank",
                            usage_getter=lambda value: value["usage"],
                            provider_request_id_getter=lambda value: value["id"],
                        )
                        for _ in range(2)
                    ]
                    results = [future.result() for future in futures]
        self.assertEqual(len(collector.records()), 2)
        self.assertEqual(sum(item.total_tokens for item in collector.records()), 24)
        self.assertTrue(all(result["trace_id"] == trace.trace_id for result in results))
        self.assertTrue(all(item.trace_id == trace.trace_id for item in collector.records()))
        self.assertTrue(
            all(item.provider_request_id == "provider-thread" for item in collector.records())
        )
        self.assertTrue(
            all(item.request_id == "provider-thread" for item in collector.records())
        )

    def test_legacy_request_id_getter_writes_canonical_id_and_compatibility_mirror(self):
        collector = ModelCostCollector(run_id=new_run_id())
        with model_cost_scope(collector):
            timed_model_call(
                lambda: {"id": "provider-legacy-alias", "usage": {}},
                provider="zhipu",
                model="glm-4.6v",
                call_type="legacy_adapter",
                usage_getter=lambda value: value["usage"],
                request_id_getter=lambda value: value["id"],
            )

        record = collector.records()[0]
        self.assertEqual(record.provider_request_id, "provider-legacy-alias")
        self.assertEqual(record.request_id, "provider-legacy-alias")

    def test_duplicate_run_id_is_rejected_without_mixing_call_rows(self):
        directory = self.make_directory()
        database = directory / "costs.sqlite3"
        ledger = SQLiteModelCostLedger(database)
        first = ModelCostCollector(run_id="run_duplicate")
        second = ModelCostCollector(run_id="run_duplicate")
        for collector, provider_id in ((first, "provider-first"), (second, "provider-second")):
            collector.record(
                provider="zhipu",
                model="glm-4.6v",
                call_type="duplicate_guard",
                status="success",
                started_at="2026-08-25T00:00:00+00:00",
                finished_at="2026-08-25T00:00:01+00:00",
                latency_ms=1000,
                usage={"prompt_tokens": 10, "completion_tokens": 2},
                provider_request_id=provider_id,
            )

        ledger.write_run(
            first,
            finished_at="2026-08-25T00:00:01+00:00",
            outcome="success",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.write_run(
                second,
                finished_at="2026-08-25T00:00:02+00:00",
                outcome="error",
            )

        with sqlite3.connect(database) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM model_cost_runs WHERE run_id = 'run_duplicate'"
            ).fetchone()[0]
            provider_ids = connection.execute(
                "SELECT provider_request_id FROM model_cost_calls "
                "WHERE run_id = 'run_duplicate'"
            ).fetchall()
        self.assertEqual(run_count, 1)
        self.assertEqual(provider_ids, [("provider-first",)])

    def test_duplicate_call_id_is_rejected_without_reparenting_existing_call(self):
        directory = self.make_directory()
        database = directory / "costs.sqlite3"
        ledger = SQLiteModelCostLedger(database)
        first = ModelCostCollector(run_id="run_first")
        second = ModelCostCollector(run_id="run_second")
        for collector, provider_id in ((first, "provider-first"), (second, "provider-second")):
            collector.record(
                call_id="call_duplicate",
                provider="zhipu",
                model="glm-4.6v",
                call_type="duplicate_call_guard",
                status="success",
                started_at="2026-08-25T00:00:00+00:00",
                finished_at="2026-08-25T00:00:01+00:00",
                latency_ms=1000,
                usage={"prompt_tokens": 10, "completion_tokens": 2},
                provider_request_id=provider_id,
            )

        ledger.write_run(
            first,
            finished_at="2026-08-25T00:00:01+00:00",
            outcome="success",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.write_run(
                second,
                finished_at="2026-08-25T00:00:02+00:00",
                outcome="error",
            )

        with sqlite3.connect(database) as connection:
            runs = connection.execute(
                "SELECT run_id FROM model_cost_runs ORDER BY run_id"
            ).fetchall()
            calls = connection.execute(
                "SELECT call_id, run_id, provider_request_id FROM model_cost_calls"
            ).fetchall()
        self.assertEqual(runs, [("run_first",)])
        self.assertEqual(
            calls,
            [("call_duplicate", "run_first", "provider-first")],
        )

    def test_provider_failure_keeps_local_trace_without_usage_or_provider_id(self):
        trace = TraceContext.create(request_id="req_failed_attempt")
        with trace_context_scope(trace):
            collector = ModelCostCollector(run_id=new_run_id())
            with model_cost_scope(collector):
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    timed_model_call(
                        lambda: (_ for _ in ()).throw(RuntimeError("provider failed")),
                        provider="zhipu",
                        model="glm-4.6v",
                        call_type="failed_call",
                        usage_getter=lambda value: value,
                    )

        record = collector.records()[0]
        self.assertEqual(record.status, "error")
        self.assertEqual(record.trace_id, trace.trace_id)
        self.assertEqual(record.provider_request_id, "")
        self.assertEqual(record.total_tokens, 0)

    def test_one_transaction_persists_run_calls_and_warning_codes(self):
        directory = self.make_directory()
        database = directory / "costs.sqlite3"
        try:
            collector = ModelCostCollector(
                run_id="run",
                session_key="hashed",
                identity_key="invite-001",
                task_kind="image",
                started_at="2026-08-02T00:00:00+00:00",
            )
            with model_cost_scope(collector):
                for _ in range(11):
                    timed_model_call(
                        lambda: {"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
                        provider="dashscope",
                        model="qwen3.7-plus",
                        call_type="qwen_image_classification",
                        usage_getter=lambda value: value["usage"],
                    )
            ledger = SQLiteModelCostLedger(database)
            ledger.write_run(
                collector, finished_at="2026-08-02T00:00:01+00:00", outcome="candidates"
            )
            with sqlite3.connect(database) as connection:
                run = connection.execute(
                    "SELECT call_count, warning_codes_json FROM model_cost_runs WHERE run_id = 'run'"
                ).fetchone()
                calls = connection.execute("SELECT COUNT(*) FROM model_cost_calls").fetchone()[0]
            self.assertEqual(run[0], 11)
            self.assertIn("MODEL_CALLS_OVER_10", run[1])
            self.assertEqual(calls, 11)
            self.assertGreater(
                ledger.estimated_cost_micros_since("2026-08-01T00:00:00+00:00"),
                0,
            )
            self.assertGreater(
                ledger.estimated_cost_micros_since(
                    "2026-08-01T00:00:00+00:00", identity_key="invite-001"
                ),
                0,
            )
            self.assertEqual(
                ledger.estimated_cost_micros_since(
                    "2026-08-01T00:00:00+00:00", identity_key="invite-002"
                ),
                0,
            )
            self.assertEqual(
                ledger.estimated_cost_micros_since("2026-08-03T00:00:00+00:00"),
                0,
            )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_agent_turn_flushes_provider_usage_to_its_isolated_runtime(self):
        directory = self.make_directory()
        artifacts = SessionArtifacts(directory / "sessions")

        class FakeAgent:
            def __init__(self, state: AgentState):
                self.state = state
                self.progress_reporter = None

            def handle_text(self, _text: str) -> AgentResponse:
                self.state.task_revision = 1
                timed_model_call(
                    lambda: {"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}},
                    provider="dashscope",
                    model="qwen3.7-plus",
                    call_type="qwen_intent_decision",
                    usage_getter=lambda value: value["usage"],
                )
                return AgentResponse(text="ok", intent="clarification")

        runtime = AgentSessionRuntime(
            SQLiteSessionStore(directory / "session.db"),
            artifacts=artifacts,
            task_logger=JsonlTaskLogger(directory / "tasks.jsonl"),
            cost_ledger=SQLiteModelCostLedger(directory / "model_costs.sqlite3"),
            agent_factory=FakeAgent,
        )
        runtime.handle_text("session-a", "hello")
        with sqlite3.connect(directory / "model_costs.sqlite3") as connection:
            run = connection.execute(
                "SELECT call_count, total_tokens FROM model_cost_runs"
            ).fetchone()
            call = connection.execute(
                "SELECT provider, model, call_type FROM model_cost_calls"
            ).fetchone()
        self.assertEqual(run, (1, 120))
        self.assertEqual(call, ("dashscope", "qwen3.7-plus", "qwen_intent_decision"))
        report = load_report(directory / "model_costs.sqlite3", days=1)
        self.assertEqual(report["search_count"], 1)
        self.assertEqual(report["model_call_count"], 1)


if __name__ == "__main__":
    unittest.main()

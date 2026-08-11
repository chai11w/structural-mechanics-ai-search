from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sqlite3
import unittest
from uuid import uuid4

from scripts.model_cost_report import load_report
from tiku_agent.agent import AgentResponse
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger
from tiku_shared.model_costs import (
    ModelCostCollector,
    SQLiteModelCostLedger,
    estimate_cost,
    model_cost_scope,
    normalize_usage,
    submit_with_model_cost_context,
    timed_model_call,
)


class ModelCostTest(unittest.TestCase):
    def make_directory(self) -> Path:
        directory = Path(__file__).resolve().parents[1] / ".tmp_tests" / f"model_costs_{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return directory

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

    def test_concurrent_calls_keep_the_parent_cost_scope(self):
        collector = ModelCostCollector(run_id="run")

        def provider_call():
            return {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}

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
                    )
                    for _ in range(2)
                ]
                for future in futures:
                    future.result()
        self.assertEqual(len(collector.records()), 2)
        self.assertEqual(sum(item.total_tokens for item in collector.records()), 24)

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

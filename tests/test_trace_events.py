from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import sqlite3
from threading import Barrier, Event, get_ident
import unittest
from uuid import uuid4

from tiku_shared.request_protocol import RequestProtocol
from tiku_shared.trace_context import (
    TraceContext,
    submit_with_trace_context,
    trace_context_scope,
)
from tiku_shared.trace_events import (
    MAX_TRACE_EVENT_ROWS,
    TRACE_EVENT_SCHEMA_VERSION,
    TRACE_EVENT_HEALTH_COUNTER_MAX,
    SQLiteTraceEventStore,
    TraceCleanupDriftError,
    TraceCleanupSnapshot,
    TraceEvent,
    TraceEventCapacityError,
    TraceEventRecorder,
    TraceEventValidationError,
    TraceEventMaintenanceError,
    _trace_writer_maintenance_lock,
    bind_trace_event_dimensions,
    current_trace_event_session,
    new_event_id,
    record_public_terminal,
    record_trace_event,
    trace_event_dimensions_scope,
    trace_event_session_scope,
    trace_event_scope,
)


class TraceEventStoreTest(unittest.TestCase):
    def test_background_writer_waits_for_brief_maintenance_without_losing_event(self):
        recorder, store = self.make_recorder()
        context = TraceContext.create()
        with _trace_writer_maintenance_lock(store.path):
            recorder.record(trace_id=context.trace_id, event_type="stage_started", stage="test", outcome="started")
            Event().wait(0.05)
            self.assertEqual(recorder.health()["written"], 0)
        self.assertTrue(recorder.flush(timeout=2))
        self.assertEqual(recorder.health()["written"], 1)
        self.assertEqual(recorder.health()["dropped"], 0)

    def test_persistent_maintenance_is_bounded_and_direct_writes_remain_nonblocking(self):
        recorder, store = self.make_recorder()
        context = TraceContext.create()
        with _trace_writer_maintenance_lock(store.path):
            with self.assertRaises(TraceEventMaintenanceError):
                store.write(self.make_event(context.trace_id, "2026-08-01T00:00:00Z"))
            recorder.record(trace_id=context.trace_id, event_type="stage_started", stage="test", outcome="started")
            self.assertTrue(recorder.flush(timeout=1))
            self.assertEqual(recorder.health()["write_failures"], 1)
            self.assertEqual(recorder.health()["written"], 0)

    def make_directory(self) -> Path:
        directory = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"trace_events_{uuid4().hex}"
        )
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return directory

    def make_recorder(self) -> tuple[TraceEventRecorder, SQLiteTraceEventStore]:
        store = SQLiteTraceEventStore(self.make_directory() / "trace_events.sqlite3")
        recorder = TraceEventRecorder(store)
        self.addCleanup(recorder.close)
        return recorder, store

    @staticmethod
    def make_event(trace_id: str, occurred_at: str) -> TraceEvent:
        return TraceEvent.create(
            trace_id=trace_id,
            event_type="stage_started",
            stage="candidate_rerank",
            outcome="started",
            occurred_at=occurred_at,
            call_id=uuid4().hex,
        )

    def test_max_rows_is_explicit_and_unconfigured_store_remains_compatible(self):
        directory = self.make_directory()
        unconfigured = SQLiteTraceEventStore(directory / "unbounded.sqlite3")
        first = TraceContext.create()
        second = TraceContext.create()
        unconfigured.write(self.make_event(first.trace_id, "2026-08-01T00:00:00Z"))
        unconfigured.write(self.make_event(second.trace_id, "2026-08-02T00:00:00Z"))

        self.assertIsNone(unconfigured.max_rows)
        self.assertEqual(
            unconfigured.capacity_snapshot(),
            {
                "available": True,
                "configured": False,
                "max_rows": None,
                "current_rows": 2,
                "remaining_rows": None,
                "at_capacity": False,
            },
        )
        for invalid in (True, 0, -1, MAX_TRACE_EVENT_ROWS + 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    SQLiteTraceEventStore(directory / "invalid.sqlite3", max_rows=invalid)

    def test_capacity_snapshot_and_cleanup_plan_do_not_create_an_absent_database(self):
        path = self.make_directory() / "absent.sqlite3"
        store = SQLiteTraceEventStore(path, max_rows=4)

        capacity = store.capacity_snapshot()
        snapshot = store.cleanup_candidates(cutoff="2026-08-06T00:00:00Z")

        self.assertFalse(path.exists())
        self.assertEqual(capacity["current_rows"], 0)
        self.assertEqual(capacity["remaining_rows"], 4)
        self.assertEqual(snapshot.candidate_count, 0)
        self.assertEqual(snapshot.event_count, 0)
        self.assertEqual(
            TraceCleanupSnapshot.from_dict(
                json.loads(json.dumps(snapshot.to_dict()))
            ),
            snapshot,
        )

    def test_existing_empty_or_partial_database_is_not_initialized(self):
        for kind in ("empty", "partial"):
            with self.subTest(kind=kind):
                path = self.make_directory() / f"{kind}.sqlite3"
                if kind == "empty":
                    path.touch()
                else:
                    with sqlite3.connect(path) as connection:
                        connection.execute(
                            "CREATE TABLE trace_events (event_id TEXT)"
                        )
                before = path.read_bytes()
                store = SQLiteTraceEventStore(path)

                with self.assertRaises(TraceCleanupDriftError):
                    store.ensure_store_identity()
                with self.assertRaises(TraceCleanupDriftError):
                    store.store_id()

                self.assertEqual(path.read_bytes(), before)

    def test_corrupt_existing_database_is_not_replaced(self):
        path = self.make_directory() / "corrupt.sqlite3"
        original = b"this is not a sqlite database"
        path.write_bytes(original)
        store = SQLiteTraceEventStore(path)

        with self.assertRaises(TraceCleanupDriftError):
            store.ensure_store_identity()

        self.assertEqual(path.read_bytes(), original)

    def test_two_store_instances_atomically_compete_for_the_last_row(self):
        path = self.make_directory() / "trace_events.sqlite3"
        stores = (
            SQLiteTraceEventStore(path, max_rows=1, write_timeout_seconds=2),
            SQLiteTraceEventStore(path, max_rows=1, write_timeout_seconds=2),
        )
        traces = (TraceContext.create(), TraceContext.create())
        events = tuple(
            self.make_event(trace.trace_id, "2026-09-05T00:00:00Z")
            for trace in traces
        )
        gate = Barrier(2)

        def compete(index: int) -> str:
            gate.wait(timeout=3)
            try:
                stores[index].write(events[index])
            except TraceEventCapacityError:
                return "capacity"
            return "written"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, range(2)))

        self.assertEqual(sorted(results), ["capacity", "written"])
        with sqlite3.connect(path) as connection:
            persisted = connection.execute(
                "SELECT event_id FROM trace_events"
            ).fetchall()
        self.assertEqual(len(persisted), 1)
        self.assertIn(persisted[0][0], {event.event_id for event in events})
        self.assertTrue(stores[0].capacity_snapshot()["at_capacity"])

    def test_cleanup_uses_complete_timelines_and_inclusive_cutoff(self):
        store = SQLiteTraceEventStore(
            self.make_directory() / "trace_events.sqlite3"
        )
        expired = TraceContext.create()
        mixed = TraceContext.create()
        fresh = TraceContext.create()
        cutoff = "2026-08-06T00:00:00+00:00"
        store.write(self.make_event(expired.trace_id, "2026-08-01T00:00:00Z"))
        store.write(self.make_event(expired.trace_id, cutoff))
        store.write(self.make_event(mixed.trace_id, "2026-07-01T00:00:00Z"))
        store.write(self.make_event(mixed.trace_id, "2026-08-07T00:00:00Z"))

        snapshot = store.cleanup_candidates(cutoff=cutoff)

        self.assertEqual(snapshot.candidate_count, 1)
        self.assertEqual(snapshot.event_count, 2)
        self.assertEqual(snapshot.candidates[0].trace_id, expired.trace_id)
        self.assertEqual(snapshot.candidates[0].max_occurred_at, cutoff)
        self.assertRegex(snapshot.snapshot_hash, r"^[0-9a-f]{64}$")

        store.write(self.make_event(fresh.trace_id, "2026-09-05T00:00:00Z"))
        result = store.apply_cleanup(snapshot)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["deleted_trace_count"], 1)
        self.assertEqual(result["deleted_event_count"], 2)
        self.assertEqual(store.events_for_trace(expired.trace_id), [])
        self.assertEqual(len(store.events_for_trace(mixed.trace_id)), 2)
        self.assertEqual(len(store.events_for_trace(fresh.trace_id)), 1)

    def test_cleanup_rejects_a_fresh_event_added_to_a_candidate_trace(self):
        store = SQLiteTraceEventStore(
            self.make_directory() / "trace_events.sqlite3"
        )
        trace = TraceContext.create()
        store.write(self.make_event(trace.trace_id, "2026-08-01T00:00:00Z"))
        snapshot = store.cleanup_candidates(cutoff="2026-08-06T00:00:00Z")
        store.write(self.make_event(trace.trace_id, "2026-08-07T00:00:00Z"))

        with self.assertRaisesRegex(TraceCleanupDriftError, "changed"):
            store.apply_cleanup(snapshot)

        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 2)

    def test_cleanup_rejects_new_candidates_and_candidate_content_drift(self):
        for drift_kind in ("new_candidate", "content_change"):
            with self.subTest(drift_kind=drift_kind):
                path = self.make_directory() / "trace_events.sqlite3"
                store = SQLiteTraceEventStore(path)
                original = TraceContext.create()
                event = self.make_event(original.trace_id, "2026-08-01T00:00:00Z")
                store.write(event)
                snapshot = store.cleanup_candidates(cutoff="2026-08-06T00:00:00Z")
                if drift_kind == "new_candidate":
                    added = TraceContext.create()
                    store.write(
                        self.make_event(added.trace_id, "2026-08-02T00:00:00Z")
                    )
                else:
                    with sqlite3.connect(path) as connection:
                        connection.execute(
                            "UPDATE trace_events SET safe_attributes_json = ? "
                            "WHERE event_id = ?",
                            ('{"operation":"candidate_rerank"}', event.event_id),
                        )

                with self.assertRaisesRegex(TraceCleanupDriftError, "changed"):
                    store.apply_cleanup(snapshot)

                self.assertEqual(len(store.events_for_trace(original.trace_id)), 1)

    def test_cleanup_snapshot_round_trip_rejects_serialized_tampering(self):
        store = SQLiteTraceEventStore(
            self.make_directory() / "trace_events.sqlite3"
        )
        trace = TraceContext.create()
        store.write(self.make_event(trace.trace_id, "2026-08-01T00:00:00Z"))
        snapshot = store.cleanup_candidates(cutoff="2026-08-06T00:00:00Z")
        payload = json.loads(json.dumps(snapshot.to_dict()))

        self.assertEqual(TraceCleanupSnapshot.from_dict(payload), snapshot)
        payload["candidates"][0]["events_hash"] = "0" * 64
        with self.assertRaisesRegex(TraceEventValidationError, "snapshot_hash"):
            TraceCleanupSnapshot.from_dict(payload)

        payload = json.loads(json.dumps(snapshot.to_dict()))
        payload["candidates"][0]["events_hash"] = int("1" * 64)
        with self.assertRaisesRegex(TraceEventValidationError, "events_hash"):
            TraceCleanupSnapshot.from_dict(payload)

    def test_cleanup_apply_is_idempotent_after_the_original_traces_are_gone(self):
        store = SQLiteTraceEventStore(
            self.make_directory() / "trace_events.sqlite3"
        )
        trace = TraceContext.create()
        store.write(self.make_event(trace.trace_id, "2026-08-01T00:00:00Z"))
        snapshot = store.cleanup_candidates(cutoff="2026-08-06T00:00:00Z")

        first = store.apply_cleanup(snapshot)
        second = store.apply_cleanup(snapshot)

        self.assertEqual(first["status"], "applied")
        self.assertEqual(first["deleted_event_count"], 1)
        self.assertEqual(second["status"], "already_satisfied")
        self.assertEqual(second["deleted_trace_count"], 1)
        self.assertEqual(second["deleted_event_count"], 1)

    def test_capacity_failure_is_fail_open_and_health_fields_are_bounded_and_safe(self):
        store = SQLiteTraceEventStore(
            self.make_directory() / "trace_events.sqlite3", max_rows=1
        )
        recorder = TraceEventRecorder(store)
        self.addCleanup(recorder.close)
        first_trace = TraceContext.create()
        second_trace = TraceContext.create()

        first = recorder.record(
            self.make_event(first_trace.trace_id, "2026-09-05T00:00:00Z")
        )
        recorder.flush()
        second = recorder.record(
            self.make_event(second_trace.trace_id, "2026-09-05T00:00:01Z")
        )
        recorder.flush()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(store.events_for_trace(first_trace.trace_id)), 1)
        self.assertEqual(store.events_for_trace(second_trace.trace_id), [])
        health = recorder.health()
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(health["last_failure_kind"], "TraceEventCapacityError")
        self.assertEqual(
            health["current_reasons"],
            ["write_failures", "capacity_exhausted"],
        )
        self.assertEqual(
            health["capacity"],
            {
                "available": True,
                "configured": True,
                "max_rows": 1,
                "current_rows": 1,
                "remaining_rows": 0,
                "at_capacity": True,
            },
        )
        self.assertNotIn(str(store.path), json.dumps(health))

        with recorder._lock:
            recorder._dropped = TRACE_EVENT_HEALTH_COUNTER_MAX
            recorder._validation_rejections = TRACE_EVENT_HEALTH_COUNTER_MAX
        recorder.reject_validation("TraceEventValidationError")
        saturated = recorder.health()
        self.assertEqual(saturated["dropped"], TRACE_EVENT_HEALTH_COUNTER_MAX)
        self.assertEqual(
            saturated["validation_rejections"], TRACE_EVENT_HEALTH_COUNTER_MAX
        )

    def test_event_ids_schema_and_trace_query_are_stable_and_ordered(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create(request_id="req_attempt_1234")

        later = recorder.record(
            trace_id=trace.trace_id,
            event_type="route_decided",
            stage="image_triage",
            outcome="success",
            occurred_at="2026-08-26T08:00:02+00:00",
            request_id=trace.request_id,
            workflow_search_id="search_workflow_1234",
            safe_attributes={"route": "A2", "question_count": 1},
        )
        earlier = recorder.record(
            trace_id=trace.trace_id,
            event_type="request_received",
            stage="http_request",
            outcome="started",
            occurred_at="2026-08-26T08:00:01+00:00",
            request_id=trace.request_id,
            safe_attributes={
                "method": "post",
                "endpoint": "/api/agent/stream",
                "response_mode": "stream",
            },
        )

        self.assertIsNotNone(later)
        self.assertIsNotNone(earlier)
        self.assertEqual(TRACE_EVENT_SCHEMA_VERSION, 1)
        self.assertRegex(new_event_id(), r"^evt_[0-9a-f]{32}$")
        events = store.events_for_trace(trace.trace_id)
        self.assertEqual(
            [event.event_type for event in events],
            ["request_received", "route_decided"],
        )
        self.assertEqual(events[0].schema_version, 1)
        self.assertEqual(events[0].safe_attributes["method"], "POST")
        self.assertEqual(events[1].workflow_search_id, "search_workflow_1234")
        self.assertEqual(recorder.health()["written"], 2)

        with sqlite3.connect(store.path) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(trace_events)")
            }
        self.assertEqual(tables, [("trace_events",)])
        self.assertTrue(
            {
                "event_id",
                "trace_id",
                "workflow_search_id",
                "protocol_code",
                "safe_attributes_json",
            }.issubset(columns)
        )

    def test_protocol_is_flattened_in_sqlite_and_nested_in_public_projection(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create(request_id="req_protocol_1234")
        protocol = RequestProtocol.from_code(
            "MEDIA_PERSIST_FAILED",
            request_id=trace.request_id,
            search_id="search_protocol_1234",
        )

        event = recorder.record(
            trace_id=trace.trace_id,
            event_type="public_response_finalized",
            stage="media_delivery",
            outcome="partial",
            request_id=trace.request_id,
            search_id="search_protocol_1234",
            protocol=protocol,
            duration_ms=125,
            safe_attributes={
                "endpoint": "/api/agent",
                "response_mode": "json",
                "media_status": "partial",
                "http_status": 200,
                "image_count": 1,
                "text_length": 24,
            },
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.protocol_code, "MEDIA_PERSIST_FAILED")
        self.assertEqual(event.protocol["status"], "PARTIAL")
        self.assertEqual(event.to_dict()["protocol"]["layer"], "media")
        queried = store.query_trace(trace.trace_id)[0]
        self.assertEqual(queried.protocol, event.protocol)
        self.assertEqual(queried.duration_ms, 125)

        with sqlite3.connect(store.path) as connection:
            row = connection.execute(
                "SELECT protocol_status, protocol_layer, protocol_code, "
                "protocol_retryable, protocol_action FROM trace_events"
            ).fetchone()
        self.assertEqual(
            row,
            ("PARTIAL", "media", "MEDIA_PERSIST_FAILED", 1, "retry_request"),
        )

    def test_event_constructor_strictly_rejects_unknown_contract_values(self):
        trace = TraceContext.create()
        base = {
            "event_id": new_event_id(),
            "trace_id": trace.trace_id,
            "event_type": "stage_finished",
            "occurred_at": "2026-08-26T08:00:00+00:00",
            "stage": "coarse_search",
            "outcome": "success",
        }

        for replacement in (
            {"event_id": "event-1"},
            {"trace_id": "trace_client_value"},
            {"event_type": "prompt_saved"},
            {"stage": "Coarse Search"},
            {"outcome": "probably_ok"},
            {"occurred_at": "2026-08-26 08:00:00"},
            {"request_id": "C:\\private\\question.png"},
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(TraceEventValidationError):
                    TraceEvent(**(base | replacement))

        with self.assertRaises(TraceEventValidationError):
            TraceEvent.create(
                trace_id=trace.trace_id,
                event_type="stage_finished",
                stage="coarse_search",
                outcome="success",
                made_up_id="unsafe",
            )

    def test_event_specific_attribute_allowlists_reject_raw_or_unbounded_values(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        secret = "raw prompt: token=super-secret C:\\private\\question.png"

        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="model_call_finished",
                stage="image_understanding",
                outcome="error",
                safe_attributes={"prompt": secret},
            )
        )
        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="model_call_finished",
                stage="image_understanding",
                outcome="error",
                safe_attributes={"error_kind": "RuntimeError: " + secret},
            )
        )
        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="cost_run_written",
                stage="cost_ledger",
                outcome="success",
                safe_attributes={"warning_codes": ["X"] * 17},
            )
        )
        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="tool_finished",
                stage="coarse_search",
                outcome="success",
                safe_attributes={"candidate_count": True},
            )
        )

        health = recorder.health()
        self.assertEqual(health["written"], 0)
        self.assertEqual(health["dropped"], 4)
        self.assertEqual(health["validation_rejections"], 4)
        self.assertEqual(health["write_failures"], 0)
        self.assertEqual(health["last_failure_kind"], "TraceEventValidationError")
        self.assertFalse(store.path.exists())
        self.assertNotIn(secret, json.dumps(health))

    def test_empty_non_mapping_attributes_and_ambiguous_record_calls_are_rejected(self):
        recorder, _store = self.make_recorder()
        trace = TraceContext.create()
        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="request_received",
                stage="http_request",
                outcome="started",
                safe_attributes=[],
            )
        )
        valid = TraceEvent.create(
            trace_id=trace.trace_id,
            event_type="request_received",
            stage="http_request",
            outcome="started",
        )
        self.assertIsNone(recorder.record(valid, stage="ignored_stage"))
        self.assertEqual(recorder.health()["validation_rejections"], 2)
        self.assertEqual(recorder.health()["written"], 0)

    def test_allowed_attributes_are_bounded_and_round_trip_without_raw_payloads(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        event = recorder.record(
            trace_id=trace.trace_id,
            event_type="model_call_finished",
            stage="a3_page_understanding",
            outcome="success",
            call_id=uuid4().hex,
            provider_request_id="provider-request_1234",
            safe_attributes={
                "provider": "dashscope",
                "model": "qwen3.7-plus",
                "call_type": "qwen_a3_page_understanding",
                "input_tokens": 120,
                "image_tokens": 80,
                "cached_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 140,
                "attempt_count": 1,
                "pricing_status": "priced",
                "estimated_cost_micros": 900,
            },
        )

        self.assertIsNotNone(event)
        queried = store.events_for_trace(trace.trace_id)[0]
        self.assertEqual(queried.safe_attributes["total_tokens"], 140)
        with self.assertRaises(TypeError):
            queried.safe_attributes["prompt"] = "cannot mutate"  # type: ignore[index]

    def test_scope_derives_trace_binds_dimensions_and_restores_nested_scope(self):
        recorder, store = self.make_recorder()
        outer = TraceContext.create(request_id="req_outer_1234")
        inner = TraceContext.create(request_id="req_inner_1234")

        self.assertIsNone(current_trace_event_session())
        with trace_context_scope(outer):
            with trace_event_scope(
                recorder,
                session_key="a" * 64,
                identity_key="invite-001",
            ) as outer_session:
                self.assertEqual(outer_session.trace_id, outer.trace_id)
                self.assertTrue(
                    bind_trace_event_dimensions(
                        workflow_search_id="search_workflow_1234",
                        unit_id="q1",
                    )
                )
                record_trace_event(
                    "stage_started",
                    stage="coarse_search",
                    outcome="started",
                    safe_attributes={"operation": "coarse_search", "attempt_count": 1},
                )
                with trace_event_scope(recorder, trace_id=inner.trace_id) as inner_session:
                    self.assertIs(current_trace_event_session(), inner_session)
                self.assertIs(current_trace_event_session(), outer_session)
        self.assertIsNone(current_trace_event_session())

        event = store.events_for_trace(outer.trace_id)[0]
        self.assertEqual(event.request_id, outer.request_id)
        self.assertEqual(event.identity_key, "invite-001")
        self.assertEqual(event.workflow_search_id, "search_workflow_1234")
        self.assertEqual(event.unit_id, "q1")
        self.assertEqual(store.events_for_trace(inner.trace_id), [])

    def test_stream_session_rebind_reuses_terminal_guard_and_restores_context(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        with trace_event_scope(recorder, trace_id=trace.trace_id) as request_session:
            self.assertFalse(request_session.terminal_attempted)

        self.assertIsNone(current_trace_event_session())
        with trace_event_session_scope(request_session):
            self.assertIs(current_trace_event_session(), request_session)
            self.assertIsNotNone(
                record_public_terminal(
                    stage="public_response",
                    outcome="success",
                    safe_attributes={
                        "endpoint": "/api/agent/stream",
                        "response_mode": "stream",
                        "http_status": 200,
                    },
                )
            )
        self.assertIsNone(current_trace_event_session())
        self.assertTrue(request_session.terminal_attempted)

        with trace_event_session_scope(request_session):
            self.assertIsNone(
                record_public_terminal(
                    stage="public_response",
                    outcome="error",
                    failed=True,
                    safe_attributes={
                        "endpoint": "/api/agent/stream",
                        "response_mode": "stream",
                        "http_status": 500,
                        "error_kind": "RuntimeError",
                    },
                )
            )
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 1)
        self.assertEqual(recorder.health()["duplicate_terminals"], 1)

    def test_rebind_scope_rejects_non_session_values(self):
        with self.assertRaisesRegex(TypeError, "TraceEventSession"):
            with trace_event_session_scope(object()):  # type: ignore[arg-type]
                self.fail("invalid session must not be bound")

    def test_dimension_scope_restores_nested_bindings_after_error(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create(request_id="req_scoped_1234")
        with trace_context_scope(trace), trace_event_scope(
            recorder, workflow_search_id="search_workflow_1234", unit_id="parent",
        ) as parent:
            with trace_event_dimensions_scope(unit_id="q1") as child:
                bind_trace_event_dimensions(search_id="search_child_1234")
                with self.assertRaisesRegex(RuntimeError, "scope failure"):
                    with trace_event_dimensions_scope(unit_id="q2"):
                        bind_trace_event_dimensions(search_id="search_nested_1234")
                        record_trace_event("stage_finished", stage="crop_validated", outcome="success")
                        raise RuntimeError("scope failure")
                self.assertIs(current_trace_event_session(), child)
                record_trace_event("stage_finished", stage="crop_validated", outcome="success")
            self.assertIs(current_trace_event_session(), parent)
            self.assertEqual(parent.dimensions["unit_id"], "parent")
            self.assertNotIn("search_id", parent.dimensions)
            record_trace_event("stage_finished", stage="page_understood", outcome="success")
        self.assertIsNone(current_trace_event_session())
        events = store.events_for_trace(trace.trace_id)
        self.assertEqual(
            [(event.unit_id, event.search_id) for event in events],
            [("q2", "search_nested_1234"), ("q1", "search_child_1234"), ("parent", "")],
        )
        self.assertTrue(all(event.request_id == trace.request_id for event in events))
        self.assertTrue(all(event.workflow_search_id == "search_workflow_1234" for event in events))

    def test_dimension_scope_is_fail_open_without_session_or_with_invalid_dimensions(self):
        with trace_event_dimensions_scope(unit_id="q1") as session:
            self.assertIsNone(session)
            self.assertIsNone(record_trace_event("stage_finished", stage="crop_validated", outcome="success"))
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        with trace_event_scope(recorder, trace_id=trace.trace_id, unit_id="parent") as parent:
            with trace_event_dimensions_scope(unit_id="private/path", search_id="search_1234") as child:
                self.assertIsNot(child, parent)
                self.assertEqual(child.dimensions, parent.dimensions)
                bind_trace_event_dimensions(unit_id="q1")
                record_trace_event("stage_finished", stage="crop_validated", outcome="success")
            self.assertEqual(parent.dimensions["unit_id"], "parent")
        self.assertEqual(store.events_for_trace(trace.trace_id)[0].unit_id, "q1")
        self.assertEqual(recorder.health()["validation_rejections"], 1)

    def test_parallel_dimension_scopes_isolate_bindings_and_share_terminal_guard(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        barrier = Barrier(2)

        def worker(unit_id):
            with trace_event_dimensions_scope(unit_id=unit_id):
                bind_trace_event_dimensions(search_id=f"search_{unit_id}")
                barrier.wait(timeout=5)
                event = record_trace_event("stage_finished", stage="crop_validated", outcome="success")
                terminal = record_public_terminal(stage="public_response", outcome="success")
                return event, terminal

        with ThreadPoolExecutor(max_workers=2) as executor:
            with trace_event_scope(recorder, trace_id=trace.trace_id, unit_id="parent") as parent:
                futures = [submit_with_trace_context(executor, worker, unit) for unit in ("q1", "q2")]
                results = [future.result(timeout=10) for future in futures]
                self.assertEqual(parent.dimensions, {"unit_id": "parent"})
                self.assertTrue(parent.terminal_attempted)
                self.assertIsNone(record_public_terminal(stage="public_response", outcome="success"))
            self.assertIsNone(executor.submit(current_trace_event_session).result(timeout=5))

        self.assertEqual([(event.unit_id, event.search_id) for event, _ in results],
                         [("q1", "search_q1"), ("q2", "search_q2")])
        self.assertEqual(sum(terminal is not None for _, terminal in results), 1)
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 3)
        self.assertEqual(recorder.health()["duplicate_terminals"], 2)

    def test_invalid_dimension_binding_is_atomic_and_fail_open(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        with trace_event_scope(recorder, trace_id=trace.trace_id, unit_id="q1") as session:
            self.assertFalse(
                bind_trace_event_dimensions(
                    search_id="search_valid_1234",
                    unit_id="C:\\private\\question.png",
                )
            )
            record_trace_event(
                "stage_finished",
                stage="coarse_search",
                outcome="success",
                safe_attributes={"completed": True},
            )

        event = store.events_for_trace(trace.trace_id)[0]
        self.assertEqual(event.unit_id, "q1")
        self.assertEqual(event.search_id, "")
        health = recorder.health()
        self.assertEqual(health["validation_rejections"], 1)
        self.assertEqual(health["written"], 1)

    def test_context_copied_worker_threads_share_the_scoped_recorder_safely(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        with ThreadPoolExecutor(max_workers=4) as executor:
            with trace_context_scope(trace):
                with trace_event_scope(recorder):
                    futures = [
                        submit_with_trace_context(
                            executor,
                            record_trace_event,
                            "stage_started",
                            stage="candidate_rerank",
                            outcome="started",
                            call_id=uuid4().hex,
                            safe_attributes={"operation": "candidate_rerank", "attempt_count": 1},
                        )
                        for _ in range(8)
                    ]
                    results = [future.result() for future in futures]
            outside = executor.submit(
                record_trace_event,
                "stage_started",
                stage="candidate_rerank",
                outcome="started",
            ).result()

        self.assertTrue(all(result is not None for result in results))
        self.assertIsNone(outside)
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 8)
        self.assertEqual(recorder.health()["written"], 8)

    def test_concurrent_terminal_attempts_persist_exactly_one(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create(request_id="req_terminal_1234")
        with trace_event_scope(recorder, trace_id=trace.trace_id) as session:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _: session.record(
                            "public_response_finalized",
                            stage="public_response",
                            outcome="success",
                            safe_attributes={
                                "endpoint": "/api/agent",
                                "response_mode": "json",
                                "http_status": 200,
                            },
                        ),
                        range(8),
                    )
                )

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 1)
        health = recorder.health()
        self.assertEqual(health["written"], 1)
        self.assertEqual(health["duplicate_terminals"], 7)
        self.assertEqual(health["dropped"], 0)
        self.assertEqual(health["status"], "degraded")

    def test_sqlite_constraint_catches_duplicate_terminals_outside_one_scope(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        common = {
            "trace_id": trace.trace_id,
            "stage": "public_response",
            "outcome": "error",
            "safe_attributes": {
                "endpoint": "/api/agent",
                "response_mode": "json",
                "http_status": 500,
            },
        }

        first = recorder.record(event_type="public_response_finalized", **common)
        second = recorder.record(event_type="public_response_finalized", **common)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 1)
        self.assertEqual(recorder.health()["duplicate_terminals"], 1)

    def test_record_never_runs_the_store_write_on_the_request_thread(self):
        class RecordingStore:
            def __init__(self) -> None:
                self.writer_thread_id = 0

            def write(self, _event: TraceEvent) -> None:
                self.writer_thread_id = get_ident()

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        store = RecordingStore()
        recorder = TraceEventRecorder(store)  # type: ignore[arg-type]
        self.addCleanup(recorder.close)
        request_thread_id = get_ident()
        trace = TraceContext.create()

        accepted = recorder.record(
            trace_id=trace.trace_id,
            event_type="request_received",
            stage="http_request",
            outcome="started",
        )
        recorder.flush()

        self.assertIsNotNone(accepted)
        self.assertNotEqual(store.writer_thread_id, request_thread_id)
        self.assertEqual(recorder.health()["written"], 1)
        self.assertEqual(recorder.health()["pending"], 0)

    def test_full_queue_is_dropped_immediately_while_writer_is_blocked(self):
        class BlockingStore:
            def __init__(self) -> None:
                self.started = Event()
                self.release = Event()
                self.calls = 0

            def write(self, _event: TraceEvent) -> None:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    self.release.wait(timeout=5)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        store = BlockingStore()
        recorder = TraceEventRecorder(store, queue_capacity=1)  # type: ignore[arg-type]
        self.addCleanup(recorder.close)
        trace = TraceContext.create()

        def emit(call_id: str):
            return recorder.record(
                trace_id=trace.trace_id,
                event_type="stage_started",
                stage="candidate_rerank",
                outcome="started",
                call_id=call_id,
            )

        try:
            self.assertIsNotNone(emit("call_blocking_1"))
            self.assertTrue(store.started.wait(timeout=2))
            self.assertIsNotNone(emit("call_queued_2"))
            self.assertIsNone(emit("call_dropped_3"))
            health = recorder.health()
            self.assertEqual(health["pending"], 2)
            self.assertEqual(health["dropped"], 1)
            self.assertEqual(health["write_failures"], 1)
            self.assertEqual(health["last_failure_kind"], "TraceEventQueueFull")
        finally:
            store.release.set()

        recorder.flush()
        self.assertEqual(recorder.health()["written"], 2)
        self.assertEqual(recorder.health()["pending"], 0)

    def test_close_drains_accepted_events_and_rejects_later_writes(self):
        class OrderedStore:
            def __init__(self) -> None:
                self.events: list[str] = []
                self.closed = False

            def write(self, event: TraceEvent) -> None:
                self.events.append(event.event_id)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

        store = OrderedStore()
        recorder = TraceEventRecorder(store, queue_capacity=2)  # type: ignore[arg-type]
        trace = TraceContext.create()
        accepted = recorder.record(
            trace_id=trace.trace_id,
            event_type="request_received",
            stage="http_request",
            outcome="started",
        )

        recorder.close()

        self.assertIsNotNone(accepted)
        self.assertEqual(store.events, [accepted.event_id])
        self.assertTrue(store.closed)
        self.assertIsNone(
            recorder.record(
                trace_id=trace.trace_id,
                event_type="stage_started",
                stage="candidate_rerank",
                outcome="started",
            )
        )
        health = recorder.health()
        self.assertEqual(health["pending"], 0)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(health["last_failure_kind"], "TraceEventRecorderClosed")
        self.assertFalse(health["accepting"])

    def test_writer_failure_is_safe_observable_and_does_not_retry_terminal(self):
        class FailingStore:
            def __init__(self) -> None:
                self.calls = 0

            def write(self, event: TraceEvent) -> None:
                self.calls += 1
                raise OSError("C:\\private\\trace.db token=super-secret")

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        store = FailingStore()
        recorder = TraceEventRecorder(store)  # type: ignore[arg-type]
        self.addCleanup(recorder.close)
        trace = TraceContext.create()
        with trace_event_scope(recorder, trace_id=trace.trace_id):
            first = record_public_terminal(
                stage="public_response",
                outcome="error",
                failed=True,
                safe_attributes={
                    "endpoint": "/api/agent",
                    "response_mode": "json",
                    "http_status": 500,
                    "error_kind": "RuntimeError",
                },
            )
            second = record_public_terminal(
                stage="public_response",
                outcome="error",
                failed=True,
                safe_attributes={
                    "endpoint": "/api/agent",
                    "response_mode": "json",
                    "http_status": 500,
                    "error_kind": "RuntimeError",
                },
            )

        recorder.flush()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(store.calls, 1)
        health = recorder.health()
        self.assertEqual(health["written"], 0)
        self.assertEqual(health["dropped"], 1)
        self.assertEqual(health["write_failures"], 1)
        self.assertEqual(health["duplicate_terminals"], 1)
        self.assertEqual(health["last_failure_kind"], "OSError")
        self.assertNotIn("private", json.dumps(health))
        self.assertNotIn("secret", json.dumps(health))

    def test_queries_are_trace_bounded_and_limit_is_validated(self):
        recorder, store = self.make_recorder()
        first = TraceContext.create()
        second = TraceContext.create()
        for trace in (first, second):
            recorder.record(
                trace_id=trace.trace_id,
                event_type="request_received",
                stage="http_request",
                outcome="started",
                safe_attributes={"method": "GET", "endpoint": "/health", "response_mode": "json"},
            )

        self.assertEqual(
            {event.trace_id for event in store.events_for_trace(first.trace_id)},
            {first.trace_id},
        )
        with self.assertRaises(TraceEventValidationError):
            store.events_for_trace("trace_not_authoritative")
        with self.assertRaises(TraceEventValidationError):
            store.events_for_trace(first.trace_id, limit=10_001)

    def test_write_and_query_release_the_sqlite_file_handle(self):
        recorder, store = self.make_recorder()
        trace = TraceContext.create()
        recorder.record(
            trace_id=trace.trace_id,
            event_type="request_received",
            stage="http_request",
            outcome="started",
            safe_attributes={"method": "GET", "endpoint": "/health", "response_mode": "json"},
        )
        self.assertEqual(len(store.events_for_trace(trace.trace_id)), 1)

        moved = store.path.with_name("trace_events_moved.sqlite3")
        store.path.rename(moved)
        self.assertTrue(moved.is_file())
        moved.rename(store.path)


if __name__ == "__main__":
    unittest.main()

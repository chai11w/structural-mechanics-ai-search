from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sqlite3
from threading import Barrier, Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_shared.response_store import (
    RESPONSE_SCHEMA_VERSION,
    ResponseConflictError,
    ResponseFinalizationCancelled,
    ResponseOwnershipError,
    ResponseProjection,
    ResponseStoreError,
    ResponseValidationError,
    SQLiteResponseStore,
    is_valid_response_id,
    new_response_id,
)


class ResponseStoreTest(unittest.TestCase):
    def make_directory(self) -> Path:
        directory = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"responses_{uuid4().hex}"
        )
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return directory

    @staticmethod
    def projection(**overrides: object) -> ResponseProjection:
        values: dict[str, object] = {
            "trace_id": "trace_11111111111111111111111111111111",
            "identity_key": "invite-001",
            "session_key": "2" * 64,
            "request_id": "req_33333333333333333333333333333333",
            "workflow_search_id": "search_workflow_01",
            "search_id": "search_question_01",
            "unit_id": "g1-u2",
            "status": "SUCCESS",
            "layer": "tool",
            "code": "REQUEST_SUCCEEDED",
            "phase": "WAIT_CANDIDATE_CHOICE",
            "task_revision": 4,
            "candidate_count": 5,
            "chapter": "moment distribution",
            "image_route": "A3",
            "intent": "show_candidates",
            "response_mode": "stream",
            "media_status": "complete",
            "image_count": 5,
            "text_length": 18,
            "duration_ms": 1234,
        }
        values.update(overrides)
        return ResponseProjection(**values)  # type: ignore[arg-type]

    def test_finalize_commits_server_id_and_safe_projection(self):
        now = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
        path = self.make_directory() / "responses.sqlite3"
        store = SQLiteResponseStore(path, retention_days=45, clock=lambda: now)

        record = store.finalize(self.projection())

        self.assertTrue(is_valid_response_id(record.response_id))
        self.assertRegex(new_response_id(), r"^resp_[0-9a-f]{32}$")
        self.assertEqual(record.schema_version, RESPONSE_SCHEMA_VERSION)
        self.assertEqual(record.created_at, now.isoformat())
        self.assertEqual(
            record.expires_at,
            (now + timedelta(days=45)).isoformat(),
        )
        self.assertEqual(store.get(record.response_id), record)
        self.assertEqual(store.get_by_trace(record.trace_id), record)
        self.assertEqual(record.projection(), self.projection())

        with sqlite3.connect(path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM public_responses"
            ).fetchone()[0]
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(public_responses)")
            }
        self.assertEqual(count, 1)
        self.assertTrue(
            {
                "response_id",
                "trace_id",
                "identity_key",
                "session_key",
                "phase",
                "chapter",
                "image_route",
                "expires_at",
            }.issubset(columns)
        )
        self.assertTrue(
            {"text", "prompt", "content", "media_url", "local_path"}.isdisjoint(columns)
        )

    def test_owner_lookup_requires_response_identity_and_session(self):
        store = SQLiteResponseStore(self.make_directory() / "responses.sqlite3")
        record = store.finalize(self.projection())

        self.assertEqual(
            store.require_owned(
                record.response_id,
                identity_key="invite-001",
                session_key="2" * 64,
            ),
            record,
        )
        self.assertIsNone(
            store.get_owned(
                record.response_id,
                identity_key="invite-002",
                session_key="2" * 64,
            )
        )
        self.assertIsNone(
            store.get_owned(
                record.response_id,
                identity_key="invite-001",
                session_key="4" * 64,
            )
        )
        with self.assertRaises(ResponseOwnershipError):
            store.require_owned(
                record.response_id,
                identity_key="invite-002",
                session_key="4" * 64,
            )

    def test_expired_response_remains_diagnosable_but_cannot_receive_feedback(self):
        current = [datetime(2026, 8, 26, 8, 0, tzinfo=UTC)]
        store = SQLiteResponseStore(
            self.make_directory() / "responses.sqlite3",
            retention_days=1,
            clock=lambda: current[0],
        )
        record = store.finalize(self.projection())
        current[0] += timedelta(days=2)

        self.assertEqual(store.get(record.response_id), record)
        self.assertIsNone(
            store.get_owned(
                record.response_id,
                identity_key=record.identity_key,
                session_key=record.session_key,
            )
        )
        self.assertEqual(
            store.get_owned(
                record.response_id,
                identity_key=record.identity_key,
                session_key=record.session_key,
                include_expired=True,
            ),
            record,
        )

    def test_same_trace_is_idempotent_only_for_the_same_projection(self):
        current = [datetime(2026, 8, 26, 8, 0, tzinfo=UTC)]
        store = SQLiteResponseStore(
            self.make_directory() / "responses.sqlite3",
            clock=lambda: current[0],
        )
        projection = self.projection()

        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(lambda _: store.finalize(projection), range(24)))

        self.assertEqual({record.response_id for record in records}, {records[0].response_id})
        self.assertEqual(store.get_by_trace(projection.trace_id), records[0])
        current[0] += timedelta(days=7)
        repeated = store.finalize(projection)
        self.assertEqual(repeated.expires_at, records[0].expires_at)
        with self.assertRaises(ResponseConflictError):
            store.finalize(replace(projection, code="NO_MATCH", status="NO_MATCH"))

    def test_two_store_instances_resolve_a_same_projection_insert_race(self):
        path = self.make_directory() / "responses.sqlite3"
        projection = self.projection()
        path.touch()
        self.assertIsNone(SQLiteResponseStore(path).get_by_trace(projection.trace_id))

        insert_barrier = Barrier(2)
        trace_selects: list[tuple[object, ...]] = []
        original_connect = sqlite3.connect

        class CoordinatedConnection(sqlite3.Connection):
            def execute(self, statement, parameters=(), /):
                normalized = " ".join(statement.split())
                if normalized == "SELECT * FROM public_responses WHERE trace_id = ?":
                    trace_selects.append(tuple(parameters))
                if normalized.startswith("INSERT INTO public_responses"):
                    insert_barrier.wait(timeout=5)
                return super().execute(statement, parameters)

        def coordinated_connect(*args, **kwargs):
            kwargs["factory"] = CoordinatedConnection
            return original_connect(*args, **kwargs)

        stores = (SQLiteResponseStore(path), SQLiteResponseStore(path))
        with patch(
            "tiku_shared.response_store.sqlite3.connect",
            side_effect=coordinated_connect,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(store.finalize, projection) for store in stores
                ]
                records = [future.result(timeout=10) for future in futures]

        self.assertEqual(
            {record.response_id for record in records},
            {records[0].response_id},
        )
        self.assertEqual(len(trace_selects), 3)
        with original_connect(path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM public_responses WHERE trace_id = ?",
                (projection.trace_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_non_trace_unique_conflict_remains_a_response_conflict(self):
        store = SQLiteResponseStore(self.make_directory() / "responses.sqlite3")
        committed = store.finalize(self.projection())
        other_projection = self.projection(
            trace_id="trace_55555555555555555555555555555555",
            request_id="req_66666666666666666666666666666666",
        )

        with patch(
            "tiku_shared.response_store.new_response_id",
            return_value=committed.response_id,
        ):
            with self.assertRaisesRegex(
                ResponseConflictError,
                "response identity could not be committed",
            ):
                store.finalize(other_projection)

        self.assertIsNone(store.get_by_trace(other_projection.trace_id))

    def test_cancellation_after_insert_rolls_back_response(self):
        path = self.make_directory() / "responses.sqlite3"
        cancelled = Event()
        original_connect = sqlite3.connect

        class CancellingConnection(sqlite3.Connection):
            def execute(self, statement, parameters=(), /):
                cursor = super().execute(statement, parameters)
                if "INSERT INTO public_responses" in " ".join(statement.split()):
                    cancelled.set()
                return cursor

        def cancelling_connect(*args, **kwargs):
            kwargs["factory"] = CancellingConnection
            return original_connect(*args, **kwargs)

        store = SQLiteResponseStore(path)
        with patch(
            "tiku_shared.response_store.sqlite3.connect",
            side_effect=cancelling_connect,
        ):
            with self.assertRaises(ResponseFinalizationCancelled):
                store.finalize(self.projection(), cancelled=cancelled.is_set)

        self.assertIsNone(store.get_by_trace(self.projection().trace_id))

    def test_projection_rejects_raw_or_sensitive_values(self):
        invalid = (
            {"trace_id": "trace_client_owned"},
            {"identity_key": "api_key=secret"},
            {"session_key": "raw-session-cookie"},
            {"request_id": "req_client_owned"},
            {"chapter": "C:\\private\\question.xlsx"},
            {"workflow_search_id": "https://private.example/search"},
            {"intent": "raw prompt"},
            {"media_status": "C:\\private\\answer.jpg"},
            {"text_length": -1},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ResponseValidationError):
                    self.projection(**overrides)

    def test_finalize_failure_does_not_return_a_phantom_response(self):
        directory = self.make_directory()
        blocked_parent = directory / "not-a-directory"
        blocked_parent.write_text("blocked", encoding="utf-8")
        store = SQLiteResponseStore(blocked_parent / "responses.sqlite3")

        with self.assertRaisesRegex(ResponseStoreError, "database is unavailable"):
            store.finalize(self.projection())

    def test_invalid_lookup_identifiers_are_rejected_before_sql(self):
        store = SQLiteResponseStore(self.make_directory() / "responses.sqlite3")
        with self.assertRaises(ResponseValidationError):
            store.get("resp_bad")
        with self.assertRaises(ResponseValidationError):
            store.get_owned(
                "resp_" + "1" * 32,
                identity_key="invite-001",
                session_key="raw-cookie",
            )


if __name__ == "__main__":
    unittest.main()

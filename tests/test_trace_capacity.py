"""Transactional capacity, legacy migration, and bounded per-event work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from tiku_shared import trace_events as te
from tiku_shared.evidence_io_budget import EvidenceDeadlineExceeded
from tiku_shared.trace_capacity import ensure_trace_row_count, trace_row_count
from tiku_shared.trace_context import TraceContext


class TraceCapacityTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        root.mkdir(exist_ok=True)
        self.directory = self.enterContext(TemporaryDirectory(dir=root))
        self.path = Path(self.directory) / "trace.sqlite3"
        self.store = te.SQLiteTraceEventStore(self.path, max_rows=3)

    def event(self, *, terminal=False):
        return te.TraceEvent.create(
            trace_id=TraceContext.create().trace_id,
            event_type="public_response_finalized" if terminal else "stage_started",
            stage="test", outcome="success" if terminal else "started",
            occurred_at="2026-08-01T00:00:00Z",
        )

    def insert(self, connection, event):
        row = te._event_row(event)
        connection.execute(
            "INSERT INTO trace_events VALUES (" + ",".join("?" for _ in row) + ")", row
        )

    def make_legacy(self):
        # Build the pre-extension schema, without opening any production data.
        with closing(sqlite3.connect(self.path)) as connection:
            te._create_schema(connection)
            identity = te._new_trace_store_identity(connection)
            for _ in range(2):
                self.insert(connection, self.event())
            connection.commit()
        return identity

    def assert_count(self, count):
        self.assertEqual(self.store.capacity_snapshot()["current_rows"], count)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(trace_row_count(connection), count)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0], count)

    def test_legacy_read_only_then_migration_preserves_identity_and_events(self):
        identity = self.make_legacy()
        with closing(sqlite3.connect(self.path)) as connection:
            before = connection.execute("SELECT * FROM trace_events ORDER BY event_id").fetchall()
        self.assert_count(2)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='trace_event_counts'"
            ).fetchone())
        self.assertEqual(self.store.ensure_store_identity(), identity)
        self.assertEqual(self.store.ensure_store_identity(), identity)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM trace_events ORDER BY event_id").fetchall(), before)
        self.store.write(self.event())
        self.assert_count(3)
        with self.assertRaises(te.TraceEventCapacityError):
            self.store.write(self.event())
        self.assert_count(3)

    def test_competing_migrations_use_one_atomic_initial_count(self):
        self.make_legacy()
        barrier = Barrier(2)

        def migrate(_):
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                barrier.wait(timeout=5)
                ensure_trace_row_count(connection)
                self.insert(connection, self.event())
                connection.commit()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(migrate, range(2)))
        self.assert_count(4)
        with self.assertRaises(te.TraceEventCapacityError):
            self.store.write(self.event())

    def test_interrupted_migration_rolls_back_and_can_retry(self):
        identity = self.make_legacy()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.set_authorizer(lambda op, *args:
                sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError), connection:
                ensure_trace_row_count(connection)
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='trace_event_counts'"
            ).fetchone())
        self.assertEqual(self.store.ensure_store_identity(), identity)
        self.assert_count(2)

    def test_process_exit_during_migration_leaves_legacy_database_intact(self):
        identity = self.make_legacy()
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", """
import os, sqlite3, sys
from tiku_shared.trace_capacity import ensure_trace_row_count
c = sqlite3.connect(sys.argv[1])
def stop(op, *args):
    if op == sqlite3.SQLITE_CREATE_TRIGGER:
        os._exit(73)
    return sqlite3.SQLITE_OK
c.set_authorizer(stop)
ensure_trace_row_count(c)
""", str(self.path)], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assert_count(2)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='trace_event_counts'"
            ).fetchone())
        self.assertEqual(self.store.ensure_store_identity(), identity)
        self.assert_count(2)

    def test_separate_processes_compete_for_last_slot_without_overshoot(self):
        self.make_legacy()
        self.store.ensure_store_identity()
        program = """
import sys
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEvent, TraceEventRecorder
from tiku_shared.trace_context import TraceContext
store = SQLiteTraceEventStore(sys.argv[1], max_rows=3, write_timeout_seconds=5)
recorder = TraceEventRecorder(store)
try:
    recorder.record(TraceEvent.create(trace_id=TraceContext.create().trace_id,
        event_type='stage_started', stage='test', outcome='started'))
    assert recorder.flush(5)
    health = recorder.health()
    if health['written'] == 1:
        print('written')
    else:
        assert health['last_failure_kind'] == 'TraceEventCapacityError', health
        print('capacity')
finally:
    assert recorder.close(timeout=5)
"""

        def compete(_):
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", program, str(self.path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(compete, range(2))), ["capacity", "written"])
        self.assert_count(3)

    def test_old_writer_and_deleter_update_count_and_rollback_with_events(self):
        self.store.ensure_store_identity()
        event = self.event()
        with closing(sqlite3.connect(self.path)) as connection:
            self.insert(connection, event)
            connection.commit()
            self.assert_count(1)
            self.insert(connection, self.event())
            connection.rollback()
            self.assert_count(1)
            connection.execute("DELETE FROM trace_events")
            connection.rollback()
            self.assert_count(1)
            connection.execute("DELETE FROM trace_events")
            connection.commit()
        self.assert_count(0)

    def test_duplicate_and_deadline_rollback_do_not_consume_capacity(self):
        terminal = self.event(terminal=True)
        self.store.write(terminal)
        with self.assertRaises(te.DuplicateTerminalEvent):
            self.store.write(terminal)
        self.assert_count(1)
        with patch.object(te, "check_evidence_budget", side_effect=[None, EvidenceDeadlineExceeded()]):
            with self.assertRaises(EvidenceDeadlineExceeded):
                self.store.write(self.event())
        self.assert_count(1)

    def test_cleanup_frees_exact_capacity_and_repeated_cleanup_is_idempotent(self):
        for _ in range(3):
            self.store.write(self.event())
        snapshot = self.store.cleanup_candidates(cutoff="2026-08-02T00:00:00Z")
        self.store.apply_cleanup(snapshot)
        self.store.apply_cleanup(snapshot)
        self.assert_count(0)
        for _ in range(3):
            self.store.write(self.event())
        self.assert_count(3)
        with self.assertRaises(te.TraceEventCapacityError):
            self.store.write(self.event())

    def test_partial_or_changed_extension_fails_closed_without_repair(self):
        mutations = (
            "DROP TRIGGER trace_event_count_delete",
            "DELETE FROM trace_event_counts",
            "DROP TRIGGER trace_event_count_insert; CREATE TRIGGER trace_event_count_insert AFTER INSERT ON trace_events BEGIN SELECT 1; END",
        )
        for i, sql in enumerate(mutations):
            with self.subTest(sql=sql):
                store = te.SQLiteTraceEventStore(Path(self.directory) / f"corrupt{i}.sqlite3", max_rows=2)
                store.write(self.event())
                with closing(sqlite3.connect(store.path)) as connection:
                    connection.executescript(sql)
                with self.assertRaises(sqlite3.DatabaseError):
                    store.write(self.event())
                self.assertFalse(store.capacity_snapshot()["available"])
                with closing(sqlite3.connect(store.path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0], 1)

    def test_initialized_write_and_health_never_recount_event_history(self):
        self.store.ensure_store_identity()
        original = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_authorizer(lambda op, a, b, *rest:
                sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_FUNCTION and b == "count" else sqlite3.SQLITE_OK)
            return connection

        with patch.object(te.sqlite3, "connect", connect):
            for _ in range(3):
                self.store.write(self.event())
            self.assertEqual(self.store.capacity_snapshot()["current_rows"], 3)
            with self.assertRaises(te.TraceEventCapacityError):
                self.store.write(self.event())


if __name__ == "__main__":
    unittest.main()

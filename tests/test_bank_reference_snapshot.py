"""Durable references and fresh SQLite snapshots, without changing business state."""
from contextlib import closing
from datetime import datetime, timedelta, UTC
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests import test_bank_checkpoint_references as checkpoint_fixture
from tiku_agent.a3_runtime import A3SessionState, SQLiteA3SessionStore
from tiku_agent.checkpoint_bank_reference import CheckpointBankCatalog
from tiku_agent.execution_store import ExecutionStore, session_key
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_shared.bank_readers import initialize_reader_gate, maintenance_gate
from tiku_shared.bank_reference_snapshot import ReferenceSource, SearchReferenceSnapshot, ReferenceSnapshotError


NOW = datetime(2026, 9, 13, 4, tzinfo=UTC)


class SearchReferenceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bank-references-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bank = self.root / "published"; self.bank.mkdir()
        initialize_reader_gate(self.bank)
        self.catalog = CheckpointBankCatalog({"published": self.bank, "main": self.root / "legacy"})
        self.clock = [NOW]
        self.agent = SQLiteSessionStore(self.root / "agent.sqlite3", now=lambda: self.clock[0])
        self.source = ReferenceSource("web_a2", "agent", self.agent.database_path)

    def path(self, version):
        return str(self.bank / "versions" / (version * 64) / "main/4力法/题目/1.png")

    def state(self, identity, version):
        state = AgentState(session_id=identity)
        state.set_candidates([{"path": self.path(version), "score": 0.9}])
        return state

    def scan(self, sources=None, **kwargs):
        return SearchReferenceSnapshot(sources or [self.source], catalog=self.catalog, now=NOW, **kwargs)

    def test_live_a2_and_a3_references_survive_expired_sessions_without_deleting_rows(self):
        self.clock[0] = NOW - timedelta(days=31)
        self.agent.save(self.state("expired", "b"))
        self.clock[0] = NOW
        self.agent.save(self.state("live", "a"))
        parent = SQLiteA3SessionStore(self.root / "a3.sqlite3", now=lambda: NOW)
        parent.save(A3SessionState(session_id="page", units=[{"unit_id": "unit1", "page_index": 1}],
            auto_crops={"unit1": {"path": self.path("c")}}))
        files = [self.agent.database_path, parent.database_path]
        before = [file.read_bytes() for file in files]
        with self.scan([self.source, ReferenceSource("web_a3", "a3", parent.database_path)]) as snapshot:
            report = snapshot.report()
            self.assertEqual(report["protected_versions"], ["a" * 64, "c" * 64])
            self.assertEqual([(item["rows"], item["live_rows"]) for item in report["sources"]], [(2, 1), (1, 1)])
            self.assertNotIn(str(self.root), json.dumps(report))
            self.assertNotIn("expired", json.dumps(report))
            with maintenance_gate(self.bank):
                snapshot.assert_unchanged()
            with self.assertRaises(sqlite3.OperationalError):
                snapshot._guards[0][1].execute("DELETE FROM agent_sessions")
        self.assertEqual(before, [file.read_bytes() for file in files])
        with closing(sqlite3.connect(self.agent.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM agent_sessions").fetchone()[0], 2)
        with self.assertRaisesRegex(ReferenceSnapshotError, "closed"):
            snapshot.assert_unchanged()
        report["protected_versions"].clear()
        self.assertEqual(len(snapshot.report()["protected_versions"]), 2)

    def test_committed_wal_is_seen_and_later_writes_invalidate_same_connection_guard(self):
        self.agent.save(self.state("live", "a"))
        with closing(sqlite3.connect(self.agent.database_path)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE agent_sessions SET state_json=?", (json.dumps(self.state("live", "b").to_dict()),))
            writer.commit()
            self.assertTrue(Path(str(self.agent.database_path) + "-wal").is_file())
            before = self.agent.database_path.read_bytes()
            with self.scan() as snapshot:
                self.assertEqual(snapshot.report()["protected_versions"], ["b" * 64])
                self.assertFalse(snapshot._guards[0][1].in_transaction)
                self.assertEqual(before, self.agent.database_path.read_bytes())
                # No retained read transaction blocks this independent writer.
                self.agent.save(self.state("live", "c"))
                with self.assertRaisesRegex(ReferenceSnapshotError, "source changed"):
                    snapshot.assert_unchanged()
            with self.scan() as fresh:
                self.assertEqual(fresh.report()["protected_versions"], ["c" * 64])

    def test_unknown_schema_bad_identity_and_ambiguous_paths_never_return_partial_success(self):
        self.agent.save(self.state("live", "a"))
        original = self.agent.database_path.read_bytes()
        for mutation, args in (
            ("UPDATE agent_sessions SET schema_version=99", ()),
            ("UPDATE agent_sessions SET state_json=?", ('{"session_id":"wrong"}',)),
            ("UPDATE agent_sessions SET expires_at='2026-09-13T05:00:00'", ()),
            ("UPDATE agent_sessions SET state_json=?", ('{"session_id":"live","session_id":"other"}',)),
            ("CREATE TABLE future_references(path TEXT)", ()),
            ("UPDATE agent_sessions SET state_json=?", (json.dumps({**self.state("live", "a").to_dict(),
                "last_answer_paths": [str(self.bank / "active.json")]}),)),
        ):
            with self.subTest(mutation=mutation, args=args):
                with closing(sqlite3.connect(self.agent.database_path)) as connection:
                    connection.execute(mutation, args); connection.commit()
                snapshot = self.scan()
                with self.assertRaises(ReferenceSnapshotError):
                    with snapshot:
                        self.fail("invalid source returned a snapshot")
                with self.assertRaises(ReferenceSnapshotError):
                    snapshot.report()
                self.agent.database_path.write_bytes(original)
        with self.scan() as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], ["a" * 64])

    def test_missing_linked_or_changed_database_is_not_treated_as_an_empty_source(self):
        self.agent.save(self.state("live", "a"))
        missing = ReferenceSource("missing", "agent", self.root / "missing.sqlite3")
        with self.assertRaises(ReferenceSnapshotError), self.scan([missing]):
            pass
        self.assertFalse(missing.path.exists())
        alias = self.root / "alias.sqlite3"
        os.link(self.agent.database_path, alias)
        try:
            with self.assertRaises(ReferenceSnapshotError), self.scan():
                pass
        finally:
            alias.unlink()
        with self.scan() as snapshot:
            details = self.agent.database_path.stat()
            os.utime(self.agent.database_path, ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000))
            with self.assertRaisesRegex(ReferenceSnapshotError, "source changed"):
                snapshot.assert_unchanged()

    def test_bounds_refuse_incomplete_scans_and_failed_context_cannot_be_reused(self):
        self.agent.save(self.state("one", "a"))
        self.agent.save(self.state("two", "b"))
        before = self.agent.database_path.read_bytes()
        for kwargs in ({"max_rows": 1}, {"max_value_bytes": 100}, {"max_total_bytes": 100}):
            with self.subTest(kwargs=kwargs):
                snapshot = self.scan(**kwargs)
                with self.assertRaises(ReferenceSnapshotError), snapshot:
                    pass
                with self.assertRaisesRegex(ReferenceSnapshotError, "cannot be reused"), snapshot:
                    pass
        self.assertEqual(before, self.agent.database_path.read_bytes())

    def test_execution_authority_uses_current_epoch_and_keeps_persisted_replay_references(self):
        authority = ExecutionStore(self.root / "execution.sqlite3", now=lambda: NOW.timestamp())
        state = self.state("live", "a")
        authority.save("live", "child", state.to_dict(), None)
        authority.save("expired", "child", self.state("expired", "b").to_dict(), None)
        from tiku_agent.execution_operations import OperationStore
        OperationStore(authority)
        with authority.transaction() as connection:
            connection.execute("UPDATE execution_sessions SET expires=? WHERE session=?", (NOW.timestamp() - 1, session_key("expired")))
            # Actual schemas and foreign keys, with an unfinished historical
            # response. No model call or business operation is executed.
            connection.execute("""INSERT INTO execution_operations
                (id,session,epoch,identity,op_key,kind,fingerprint,expected_version,producer,target,status,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("operation", session_key("expired"), "old_epoch", "owner", "request",
                "text", "a" * 64, 0, "b" * 64, "{}", "UNKNOWN", NOW.timestamp(), NOW.timestamp()))
            connection.execute("INSERT INTO execution_attempts VALUES (?,?,?,?,?,?)",
                ("attempt", "operation", "token", "UNKNOWN", NOW.timestamp(), NOW.timestamp()))
            connection.execute("INSERT INTO execution_handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("h1", "operation", "attempt", session_key("expired"), "old_epoch", None, None, "", 1,
                 json.dumps({"media": [self.path("c")]}), "pending", NOW.timestamp(), NOW.timestamp(),))
        source = ReferenceSource("durable", "execution", authority.path)
        with self.scan([source]) as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], ["a" * 64, "c" * 64])
        with authority.transaction() as connection:
            connection.execute("UPDATE execution_states SET epoch='stale_epoch' WHERE session=?", (session_key("live"),))
        with self.scan([source]) as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], ["c" * 64])
        with authority.transaction() as connection:
            connection.execute("CREATE TABLE execution_future(data TEXT)")
        with self.assertRaisesRegex(ReferenceSnapshotError, "unsupported execution"), self.scan([source]):
            pass

    def test_real_checkpoint_integrity_expiry_and_version_reference_collection(self):
        owner = checkpoint_fixture.PublishedCheckpointTests()
        fixture = owner.fixture()
        self.addCleanup(owner.doCleanups)
        fixture.search(); owner.answer(fixture)
        source = ReferenceSource("checkpoints", "checkpoints", fixture.store.path)
        source_hash = hashlib.sha256(fixture.store.path.read_bytes()).hexdigest()
        with SearchReferenceSnapshot([source], catalog=fixture.recorder.bank_catalog) as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], [fixture.version.version])
            self.assertEqual(snapshot.report()["sources"][0]["live_rows"], 6)
        self.assertEqual(hashlib.sha256(fixture.store.path.read_bytes()).hexdigest(), source_hash)
        with SearchReferenceSnapshot([source], catalog=fixture.recorder.bank_catalog,
                                     now=datetime.now(UTC) + timedelta(days=31)) as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], [])
        with closing(sqlite3.connect(fixture.store.path)) as connection:
            connection.execute("UPDATE checkpoints SET expires_at='2020-01-01T00:00:00+00:00'"); connection.commit()
        with self.assertRaises(ReferenceSnapshotError), SearchReferenceSnapshot([source], catalog=fixture.recorder.bank_catalog):
            pass

    def test_checkpoint_retention_extension_invalidates_prior_snapshot(self):
        from tiku_agent.checkpoint_store import _checkpoint_from_payload
        owner = checkpoint_fixture.PublishedCheckpointTests()
        fixture = owner.fixture()
        self.addCleanup(owner.doCleanups)
        fixture.search()
        record = _checkpoint_from_payload(fixture.records()[-1])
        source = ReferenceSource("checkpoints", "checkpoints", fixture.store.path)
        with SearchReferenceSnapshot([source], catalog=fixture.recorder.bank_catalog) as snapshot:
            extended = fixture.store.extend_retention(record.checkpoint_id, actor_key="operator",
                expected_owner=record.owner, new_expires_at=datetime.fromisoformat(record.expires_at) + timedelta(days=1),
                new_retention_class="investigation", reason_code="INVESTIGATION_OPENED")
            self.assertGreater(extended.expires_at, record.expires_at)
            with self.assertRaisesRegex(ReferenceSnapshotError, "source changed"):
                snapshot.assert_unchanged()
        with SearchReferenceSnapshot([source], catalog=fixture.recorder.bank_catalog) as snapshot:
            self.assertEqual(snapshot.report()["protected_versions"], [fixture.version.version])


if __name__ == "__main__":
    unittest.main()

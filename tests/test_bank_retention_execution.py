"""Actual retirement I/O and process exits; controller authority is a fixture."""
from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from tests import test_bank_file_retention as retention_fixtures
from tiku_shared.bank_file_retention import canonical, digest
from tiku_shared.bank_publication import write_lock, verify_bundle
from tiku_shared.bank_readers import BankReadersBusy, reader_gate
from tiku_shared.bank_retention_execution import _execute_locked, _locations


CHILD = r'''
import json, os, sys
from pathlib import Path
from tests.test_bank_publication import validate_fixture
from tiku_shared.bank_publication import PublicationStore, write_lock
from tiku_shared.bank_retention_execution import _execute_locked
root = Path(sys.argv[1])
request = json.loads((root/'retirement-input.json').read_bytes())
store = PublicationStore(root/'bank', root/'private', root/'backups', validator=validate_fixture)
def checkpoint(phase):
    if phase == sys.argv[2]:
        os._exit(73)
def check(phase):
    if store.current() != request['plan']['publication']:
        raise ValueError('fixture publication changed')
    return True
with write_lock(store.lock):
    _execute_locked(store, request['plan'], expected_digest=request['digest'], approval=request['approval'],
                    check_freshness=check, checkpoint=checkpoint)
'''


class _RetirementExecutionCases:
    publication_policies = ("retained-versions",) * 3

    def scenario(self):
        fixture = retention_fixtures.FileRetentionTests()
        fixture.publication_policies = self.publication_policies
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.original_receipts = {}
        for pointer, policy in zip((fixture.old, fixture.boundary, fixture.current), self.publication_policies, strict=True):
            operation = fixture.store.status(pointer["operation_id"])
            plan = operation["plan"]
            if policy == "legacy":
                self.assertNotIn("recovery_policy", plan)
            else:
                self.assertEqual(plan["recovery_policy"], "retained-versions")
            receipt_root = fixture.store.backups / pointer["operation_id"]
            fixture.original_receipts[receipt_root / "receipt.json"] = (receipt_root / "receipt.json").read_bytes()
            if policy == "legacy" and plan["base"]:
                verify_bundle(receipt_root / "bank", plan["base"]["version"])
            else:
                self.assertFalse((receipt_root / "bank").exists())
        with fixture.store.connection() as db:
            fixture.original_operations = [tuple(row) for row in db.execute(
                "SELECT id,plan,digest,approval,result,state FROM operations ORDER BY id")]
        items = [fixture.item,
            {"kind": "candidate", "key": fixture.old["operation_id"], "version": fixture.old["version"], "operation_id": fixture.old["operation_id"]},
            {"kind": "bundle", "key": fixture.old["version"], "version": fixture.old["version"], "operation_id": fixture.old["operation_id"]}]
        # The frozen publication policy determines required copies. Never infer
        # permission to omit a planned copy from its accidental absence on disk.
        if self.publication_policies[1] == "legacy":
            items.append({"kind": "receipt-bank", "key": fixture.boundary["operation_id"],
                          "version": fixture.old["version"], "operation_id": fixture.boundary["operation_id"]})
        # Synthetic authority/backup bindings exercise the private core only.
        # They are not evidence that a production controller or ACL is ready.
        plan = {"schema": 1, "id": "retention_" + uuid4().hex, "as_of": fixture.now.isoformat(),
            "cutoff": (fixture.now - timedelta(days=30)).isoformat(), "publication": fixture.current,
            "protected_versions": sorted([fixture.boundary["version"], fixture.current["version"]]), "items": items,
            "references": {"management": "a" * 64, "search": "b" * 64, "deployment": "c" * 64},
            "backup": {"checkpoint_id": "checkpoint_" + "d" * 32, "manifest_sha256": "e" * 64, "restore_evidence_sha256": "f" * 64}}
        request = {"plan": plan, "digest": digest(plan), "approval": {"plan_digest": digest(plan),
            "principal": "owner-one", "approved_at": datetime.now(UTC).isoformat()}}
        (fixture.root / "retirement-input.json").write_bytes(canonical(request))
        return fixture, request

    def execute(self, fixture, request, check=None):
        with write_lock(fixture.store.lock):
            return _execute_locked(fixture.store, request["plan"], expected_digest=request["digest"],
                approval=request["approval"], check_freshness=check or (lambda phase: True))

    def crash(self, fixture, phase):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", CHILD, str(fixture.root), phase],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr)

    def assert_done(self, fixture, request):
        self.assertEqual(fixture.store.current(), fixture.current)
        verify_bundle(fixture.store.root / "versions" / fixture.current["version"], fixture.current["version"])
        verify_bundle(fixture.store.root / "versions" / fixture.boundary["version"], fixture.boundary["version"])
        self.assertEqual(fixture.status(fixture.boundary), "available")
        self.assertEqual(fixture.status(fixture.old), "expired")
        for item in request["plan"]["items"]:
            self.assertTrue(all(not path.exists() for path in _locations(fixture.store, request["plan"], item)))
        for path, content in fixture.original_receipts.items():
            self.assertEqual(path.read_bytes(), content)
        current_backup = fixture.store.backups / fixture.current["operation_id"] / "bank"
        if self.publication_policies[2] == "legacy":
            verify_bundle(current_backup, fixture.boundary["version"])
        else:
            self.assertFalse(current_backup.exists())
        expected_kinds = {"published", "candidate", "bundle"}
        if self.publication_policies[1] == "legacy":
            expected_kinds.add("receipt-bank")
        self.assertEqual({item["kind"] for item in request["plan"]["items"]}, expected_kinds)
        self.assertEqual(len(request["plan"]["items"]), len(expected_kinds))
        with fixture.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bank_file_expirations").fetchone()[0], len(expected_kinds))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='files-expired'").fetchone()[0], len(expected_kinds))
            self.assertEqual([tuple(row) for row in db.execute(
                "SELECT id,plan,digest,approval,result,state FROM operations ORDER BY id")], fixture.original_operations)

    def test_missing_planned_copy_is_refused_before_recording_or_moving_anything(self):
        fixture, request = self.scenario()
        item = request["plan"]["items"][-1]  # Legacy bank copy or new-policy frozen bundle.
        original, _ = _locations(fixture.store, request["plan"], item)
        missing = fixture.root / "missing-planned-copy"
        retention_fixtures.move_owned(original, missing, fixture.root)
        with self.assertRaises(FileNotFoundError):
            self.execute(fixture, request)
        self.assertFalse((fixture.store.private / "retention").exists())
        self.assertEqual(fixture.store.current(), fixture.current)
        self.assertEqual(fixture.status(fixture.old), "available")
        with fixture.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bank_file_expirations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE event='files-expired'").fetchone()[0], 0)
        for planned in request["plan"]["items"][:-1]:
            source, retired = _locations(fixture.store, request["plan"], planned)
            self.assertTrue(source.is_dir())
            self.assertFalse(retired.exists())
        retention_fixtures.move_owned(missing, original, fixture.root)
        self.execute(fixture, request)
        self.assert_done(fixture, request)

    def test_retirement_keeps_history_receipts_and_repeated_execution_does_not_repeat_events(self):
        fixture, request = self.scenario()
        original_operation = fixture.store.status(fixture.old["operation_id"])
        with self.assertRaisesRegex(ValueError, "freshness check did not approve"):
            self.execute(fixture, request, check=lambda phase: None)
        self.assertFalse((fixture.store.private / "retention").exists())
        phases = []
        def checked(phase):
            phases.append(phase)
            return True
        result = self.execute(fixture, request, check=checked)
        self.assertGreater(result["removed_file_bytes_this_run"], 0)
        self.assertEqual(phases, ["before-record", "after-record", "before-move"])
        self.assert_done(fixture, request)
        self.assertEqual(fixture.store.status(fixture.old["operation_id"]), original_operation)
        self.assertEqual(self.execute(fixture, request)["removed_file_bytes_this_run"], 0)
        self.assert_done(fixture, request)

    def test_busy_reader_and_changed_guard_keep_live_files_and_can_resume(self):
        fixture, request = self.scenario()
        with reader_gate(fixture.store.root):
            with self.assertRaises(BankReadersBusy):
                self.execute(fixture, request)
        self.assertEqual(fixture.status(fixture.old), "available")
        def changed(phase):
            if phase == "before-move":
                raise ValueError("fixture reference changed")
            return True
        with self.assertRaisesRegex(ValueError, "reference changed"):
            self.execute(fixture, request, check=changed)
        for item in request["plan"]["items"]:
            original, retired = _locations(fixture.store, request["plan"], item)
            self.assertTrue(original.is_dir()); self.assertFalse(retired.exists())
        self.execute(fixture, request)
        self.assert_done(fixture, request)

    def test_process_exit_at_ledger_move_and_partial_purge_resumes_original_operation(self):
        for phase in ("request-recorded", "ledger-committed", "copy-moved", "purge-recorded", "file-removed"):
            with self.subTest(phase=phase):
                fixture, request = self.scenario()
                self.crash(fixture, phase)
                fixture.store = fixture.owner.reopen()
                self.execute(fixture, request)
                self.assert_done(fixture, request)

    def test_loss_before_expiry_is_recorded_remains_unavailable_not_expired(self):
        fixture, request = self.scenario()
        self.crash(fixture, "request-recorded")
        original, _ = _locations(fixture.store, request["plan"], request["plan"]["items"][0])
        retention_fixtures.move_owned(original, fixture.root / "missing-outside-retirement", fixture.root)
        with self.assertRaisesRegex(ValueError, "lost both file locations"):
            self.execute(fixture, request)
        with fixture.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bank_file_expirations").fetchone()[0], 0)
        self.assertEqual(fixture.status(fixture.old), "unavailable")

    def test_unknown_retired_files_and_inventory_tampering_are_refused_without_removing_them(self):
        fixture, request = self.scenario()
        self.crash(fixture, "file-removed")
        _, retired = _locations(fixture.store, request["plan"], request["plan"]["items"][0])
        unknown = retired / "unknown.txt"; unknown.write_bytes(b"keep this")
        with self.assertRaisesRegex(ValueError, "directory changed"):
            self.execute(fixture, request)
        self.assertEqual(unknown.read_bytes(), b"keep this")
        journal = fixture.store.private / "retention" / request["plan"]["id"] / "request.json"
        data = json.loads(journal.read_bytes())
        data["copies"][0]["entries"].append({"path": "../outside.txt", "directory": True})
        journal.write_bytes(canonical(data))
        with self.assertRaisesRegex(ValueError, "inventory path"):
            self.execute(fixture, request)
        self.assertEqual(unknown.read_bytes(), b"keep this")


class RetirementExecutionTests(_RetirementExecutionCases, unittest.TestCase):
    """Current publications: only retained versions and approval receipts."""


class LegacyRetirementExecutionTests(_RetirementExecutionCases, unittest.TestCase):
    """Pre-upgrade frozen publications still carry independent bank backups."""

    publication_policies = ("legacy",) * 3


class MixedRetirementExecutionTests(_RetirementExecutionCases, unittest.TestCase):
    """Upgrade history: old bank backups coexist with a new-policy publication."""

    publication_policies = ("legacy", "legacy", "retained-versions")


if __name__ == "__main__":
    unittest.main()

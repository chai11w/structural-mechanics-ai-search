"""History distinguishes explicit expiry from missing/corrupt bank files."""
from datetime import datetime, timedelta, UTC
import json
import unittest
from unittest.mock import patch

from tests.bank_retention_fixture import record_expiry, move_owned
from tests import test_bank_publication as publication_fixture
from tiku_shared.bank_file_retention import FileRetentionView, FileRetentionError, canonical, digest
from tiku_shared.bank_publication import PublicationError


class FileRetentionTests(unittest.TestCase):
    def setUp(self):
        self.owner = publication_fixture.PublicationTests(); self.owner.setUp()
        self.addCleanup(self.owner.tearDown)
        self.store, self.root = self.owner.store, self.owner.directory
        self.now = datetime.now(UTC)
        pointers = []
        for days, label in ((50, "old"), (40, "boundary"), (1, "current")):
            with patch("tiku_shared.bank_publication.time.time", return_value=(self.now - timedelta(days=days)).timestamp()):
                pointers.append(self.owner.publish(label)["result"])
        self.old, self.boundary, self.current = pointers
        self.item = {"kind": "published", "key": self.old["version"], "version": self.old["version"],
                     "operation_id": self.old["operation_id"]}

    def expiry(self, **kwargs):
        return record_expiry(self.store, self.root, [self.item], as_of=self.now,
                             protected=kwargs.get("protected", [self.boundary["version"], self.current["version"]]))

    def status(self, pointer):
        return next(item for item in self.store.history(principal="owner-one")["items"]
                    if item["operation_id"] == pointer["operation_id"])["files_status"]

    def validate(self):
        with self.store.connection() as db:
            FileRetentionView(db).validate_all()

    def test_absence_without_record_is_unavailable_and_legacy_schema_remains_readable(self):
        with self.store.connection() as db:
            db.execute("DROP TABLE bank_file_expirations")
            db.execute("DROP TABLE bank_retention_plans")
            self.assertFalse(FileRetentionView(db).expired("published", self.old["version"], self.old["version"]))
        self.assertEqual(self.status(self.old), "available")
        move_owned(self.store.root / "versions" / self.old["version"], self.root / "removed" / self.old["version"], self.root)
        self.assertEqual(self.status(self.old), "unavailable")
        with self.assertRaisesRegex(PublicationError, "rollback-target-unavailable"):
            self.store.historical(self.old["operation_id"], principal="owner-one")

    def test_expiry_survives_reopen_keeps_identity_and_restored_exact_files_are_available(self):
        original = self.store.status(self.old["operation_id"])
        self.expiry(); self.validate()
        self.assertEqual(self.status(self.old), "available")
        directory = self.store.root / "versions" / self.old["version"]
        moved = self.root / "retired" / self.old["version"]
        move_owned(directory, moved, self.root)
        self.store = self.owner.reopen()
        self.assertEqual(self.status(self.old), "expired")
        self.assertEqual(self.status(self.boundary), "available")
        self.assertEqual(self.store.status(self.old["operation_id"]), original)
        self.assertEqual(self.store.current(), self.current)
        with self.assertRaisesRegex(PublicationError, "rollback-target-expired"):
            self.store.historical(self.old["operation_id"], principal="owner-one")
        move_owned(moved, directory, self.root)
        self.assertEqual(self.status(self.old), "available")
        self.assertEqual(self.store.historical(self.old["operation_id"], principal="owner-one"), self.old)

    def test_window_start_version_current_and_referenced_versions_cannot_expire(self):
        plan = self.expiry()
        self.validate()
        with self.store.connection() as db:
            for protected, item in (([self.current["version"]], self.item),
                                    (plan["protected_versions"], {**self.item, "key": self.current["version"],
                                      "version": self.current["version"], "operation_id": self.current["operation_id"]}),
                                    ([*plan["protected_versions"], self.old["version"]], self.item)):
                changed = {**plan, "protected_versions": sorted(protected), "items": [item]}
                db.execute("UPDATE bank_retention_plans SET payload=?,digest=?", (canonical(changed).decode(), digest(changed)))
                with self.assertRaisesRegex(FileRetentionError, "protected"):
                    FileRetentionView(db).validate_all()

    def test_bad_plan_audit_or_partial_schema_never_authorizes_missing_files(self):
        self.expiry(); self.validate()
        database = self.store.private / "writer.sqlite3"
        original = database.read_bytes()
        mutations = (("UPDATE bank_retention_plans SET digest=?", ("0" * 64,)),
                     ("UPDATE bank_file_expirations SET audit_sequence=1", ()),
                     ("UPDATE bank_file_expirations SET version=?", ("0" * 64,)),
                     ("DROP TABLE bank_retention_plans", ()))
        for sql, values in mutations:
            with self.subTest(sql=sql):
                with self.store.connection() as db:
                    db.execute(sql, values)
                with self.assertRaises(FileRetentionError):
                    self.validate()
                with self.assertRaises(FileRetentionError):
                    self.owner.reopen()
                database.write_bytes(original)
        self.validate()

    def test_existing_corrupt_manifest_is_not_hidden_by_expiry_evidence(self):
        self.expiry()
        manifest = self.store.root / "versions" / self.old["version"] / "manifest.json"
        manifest.write_text('{}', encoding="utf-8")
        self.assertEqual(self.status(self.old), "unavailable")
        with self.assertRaisesRegex(PublicationError, "rollback-target-unavailable"):
            self.store.historical(self.old["operation_id"], principal="owner-one")


if __name__ == "__main__":
    unittest.main()

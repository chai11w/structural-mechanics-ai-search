"""Real filesystem/SQLite/process-crash tests for the private publication boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_shared.bank_publication import PublicationError, PublicationStore, canonical, verify_bundle, write_lock


def validate_fixture(directory):
    registry = json.loads((directory / "registry.json").read_bytes())
    if not isinstance(registry.get("records"), dict):
        raise PublicationError("fixture-registry-invalid")


CHILD = r'''
import json, os, sys, time
from pathlib import Path
from tiku_shared.bank_publication import PublicationStore, PublicationError
def validator(path):
    if not isinstance(json.loads((path / 'registry.json').read_bytes()).get('records'), dict):
        raise ValueError('registry')
def checkpoint(name):
    if name == sys.argv[5]:
        os._exit(73)
root = Path(sys.argv[1])
store = PublicationStore(root/'bank', root/'private', root/'backups', validator=validator, checkpoint=checkpoint)
if sys.argv[5] == 'barrier':
    print('ready', flush=True)
    deadline = time.monotonic() + 15
    while not (root/'go').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('barrier timed out')
        time.sleep(.02)
try:
    print(json.dumps(store.execute(sys.argv[2], plan_digest=sys.argv[3])), flush=True)
except PublicationError as exc:
    print(str(exc), flush=True)
    sys.exit(4)
'''


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lida-publication-")
        self.directory = Path(self.temporary.name).resolve()
        self.store = self.reopen()

    def tearDown(self):
        self.temporary.cleanup()

    def reopen(self, **kwargs):
        return PublicationStore(self.directory / "bank", self.directory / "private", self.directory / "backups",
                                validator=validate_fixture, **kwargs)

    def candidate(self, label, *, operation_id=None, expected=None):
        operation_id = operation_id or "op_" + uuid4().hex
        path = self.store.candidate_directory(operation_id)
        path.mkdir()
        (path / "main" / "4力法" / "题目").mkdir(parents=True)
        (path / "main" / "4力法" / "答案").mkdir()
        (path / "symbolic").mkdir()
        (path / "main" / "4力法.xlsx").write_bytes(b"index:" + label.encode())
        (path / "main" / "4力法/题目/1.png").write_bytes(b"question:" + label.encode())
        for i in range(6):
            (path / "main" / ("4力法/答案/1" + "+" * i + ".png")).write_bytes(label.encode() + bytes([i]))
        (path / "registry.json").write_bytes(canonical({"records": {}, "label": label}))
        return self.store.prepare(operation_id, expected=self.store.current() if expected is None else expected,
                                  summary={"label": label}, principal="owner-one", channel="lida")

    def approved(self, prepared, channel="lida", principal="owner-one"):
        review = self.store.review(prepared["operation_id"], principal=principal, channel=channel)
        return self.store.approve(prepared["operation_id"], plan_digest=prepared["plan_digest"],
                                  challenge=review["approval_challenge"], principal=principal, channel=channel)

    def publish(self, label):
        prepared = self.candidate(label)
        self.approved(prepared)
        return self.store.execute(prepared["operation_id"], plan_digest=prepared["plan_digest"])

    def child(self, prepared, checkpoint, *, popen=False):
        arguments = [sys.executable, "-B", "-X", "utf8", "-c", CHILD, str(self.directory),
                     prepared["operation_id"], prepared["plan_digest"], "unused", checkpoint]
        options = {"cwd": str(Path(__file__).resolve().parents[1]), "stdout": subprocess.PIPE,
                   "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8"}
        return subprocess.Popen(arguments, **options) if popen else subprocess.run(arguments, timeout=25, **options)

    def test_requires_exact_reviewed_owner_approval_and_hides_challenge_from_status(self):
        prepared = self.candidate("initial")
        operation = prepared["operation_id"]
        with self.assertRaisesRegex(PublicationError, "owner-approval-required"):
            self.store.execute(operation, plan_digest=prepared["plan_digest"])
        review = self.store.review(operation, principal="owner-one", channel="feishu")
        self.assertNotIn("approval_challenge", self.store.status(operation))
        for changes in ({"principal": "other"}, {"channel": "lida"}, {"challenge": "invented"}, {"plan_digest": "0" * 64}):
            args = {"principal": "owner-one", "channel": "feishu", "challenge": review["approval_challenge"],
                    "plan_digest": prepared["plan_digest"], **changes}
            with self.assertRaisesRegex(PublicationError, "approval-does-not-match-review"):
                self.store.approve(operation, **args)
        self.store.approve(operation, principal="owner-one", channel="feishu", challenge=review["approval_challenge"],
                           plan_digest=prepared["plan_digest"])
        result = self.store.execute(operation, plan_digest=prepared["plan_digest"])
        self.assertEqual(result["state"], "published")
        events = self.store.audit(operation)
        self.assertEqual(sum(item["event"] == "approved" for item in events), 1)
        self.assertEqual(next(item for item in events if item["event"] == "approved")["detail"]["channel"], "feishu")

    def test_full_external_backup_and_repeat_execution_preserve_single_publication(self):
        first = self.publish("initial")
        second = self.publish("replacement")
        pointer = self.store.current()
        repeated = self.reopen().execute(second["operation_id"], plan_digest=second["plan_digest"])
        self.assertEqual(repeated["result"], pointer)
        self.assertEqual(pointer["revision"], 2)
        backup = self.directory / "backups" / second["operation_id"]
        verify_bundle(backup / "bank", first["result"]["version"])
        self.assertEqual((backup / "bank/main/4力法/答案/1+++++.png").read_bytes(), b"initial\x05")
        receipt = json.loads((backup / "receipt.json").read_bytes())
        self.assertEqual(receipt["approval"]["plan_digest"], second["plan_digest"])
        self.assertTrue((self.directory / "bank/versions" / first["result"]["version"]).is_dir())

    def test_reusing_operation_with_changed_payload_rejected_but_same_request_is_idempotent(self):
        item = self.candidate("initial")
        same = self.store.prepare(item["operation_id"], expected=None, summary={"label": "initial"}, principal="owner-one", channel="lida")
        self.assertEqual(same, item)
        with self.assertRaisesRegex(PublicationError, "idempotency-conflict"):
            self.store.prepare(item["operation_id"], expected=None, summary={"label": "changed"}, principal="owner-one", channel="lida")

    def test_stale_approved_operation_and_aba_rollback_do_not_override_later_writes(self):
        initial = self.publish("initial")
        pending = self.candidate("pending")
        self.approved(pending)
        self.publish("later")
        with self.assertRaisesRegex(PublicationError, "bank-version-changed"):
            self.store.execute(pending["operation_id"], plan_digest=pending["plan_digest"])
        # Publishing identical bytes is a new revision, not a revival of old approvals.
        restored = self.publish("initial")
        self.assertEqual(initial["result"]["version"], restored["result"]["version"])
        self.assertEqual(restored["result"]["revision"], 3)
        with self.assertRaisesRegex(PublicationError, "bank-version-changed"):
            self.store.execute(pending["operation_id"], plan_digest=pending["plan_digest"])

    def test_corrupted_media_registry_or_index_cannot_publish_and_source_stays_active(self):
        self.publish("initial")
        before = self.store.current()
        for relative in ("registry.json", "main/4力法.xlsx", "main/4力法/答案/1+++++.png"):
            with self.subTest(file=relative):
                item = self.candidate("replace-" + relative)
                self.approved(item)
                bundle = self.directory / "private/bundles" / item["plan"]["candidate_version"]
                (bundle / relative).write_bytes(b"damaged")
                with self.assertRaisesRegex(PublicationError, "bundle-integrity-failed"):
                    self.store.execute(item["operation_id"], plan_digest=item["plan_digest"])
                self.assertEqual(self.store.current(), before)
                self.assertNotEqual(self.store.status(item["operation_id"])["state"], "published")

    def test_corrupted_external_backup_cannot_be_treated_as_a_reusable_backup(self):
        self.publish("initial")
        item = self.candidate("second"); self.approved(item)
        interrupted = self.child(item, "after-backup")
        self.assertEqual(interrupted.returncode, 73, interrupted.stderr)
        backup = self.directory / "backups" / item["operation_id"] / "bank/registry.json"
        backup.write_bytes(b"broken")
        with self.assertRaisesRegex(PublicationError, "bundle-integrity-failed"):
            self.reopen().execute(item["operation_id"], plan_digest=item["plan_digest"])
        self.assertEqual(self.store.current()["revision"], 1)

    def test_real_process_crashes_before_and_after_pointer_are_recoverable_without_double_write(self):
        self.publish("initial")
        for point in ("before-backup", "after-backup", "after-version", "before-pointer", "after-pointer", "after-journal"):
            with self.subTest(point=point):
                previous = self.store.current()
                item = self.candidate(point); self.approved(item)
                interrupted = self.child(item, point)
                self.assertEqual(interrupted.returncode, 73, interrupted.stderr)
                reopened = self.reopen(); reopened.recover()
                recovered = reopened.status(item["operation_id"])
                self.assertEqual(recovered["state"], "published" if point in ("after-pointer", "after-journal") else "approved")
                finished = reopened.execute(item["operation_id"], plan_digest=item["plan_digest"])
                self.assertEqual(finished["state"], "published")
                self.assertEqual(reopened.current()["revision"], previous["revision"] + 1)
                self.assertEqual(sum(event["event"] == "published" for event in reopened.audit(item["operation_id"])), 1)

    def test_two_real_writers_share_one_lock_and_recheck_stale_base(self):
        self.publish("initial")
        items = [self.candidate("one"), self.candidate("two")]
        for item in items:
            self.approved(item)
        children = [self.child(item, "barrier", popen=True) for item in items]
        try:
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), "ready")
            (self.directory / "go").touch()
            outputs = [child.communicate(timeout=25) for child in children]
            self.assertEqual(sorted(child.returncode for child in children), [0, 4], outputs)
            self.assertEqual(self.store.current()["revision"], 2)
            self.assertEqual(sum(self.store.status(item["operation_id"])["state"] == "published" for item in items), 1)
            self.assertTrue(any("bank-version-changed" in output[0] for output in outputs))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_io_exception_after_pointer_reports_actual_success(self):
        item = self.candidate("initial"); self.approved(item)
        def fail(name):
            if name == "after-pointer":
                raise OSError("private path not exposed")
        result = self.reopen(checkpoint=fail).execute(item["operation_id"], plan_digest=item["plan_digest"])
        self.assertEqual(result["state"], "published")
        self.assertIsNone(result["error"])

    def test_reservations_are_durable_and_not_recycled_by_other_drafts_or_owners(self):
        one = self.store.reserve(owner="owner-one", draft_id="draft-one")
        self.assertEqual(one, self.reopen().reserve(owner="owner-one", draft_id="draft-one"))
        self.assertNotEqual(one, self.store.reserve(owner="owner-one", draft_id="draft-two"))
        self.assertNotEqual(one, self.store.reserve(owner="owner-two", draft_id="draft-one"))

    def test_cancelling_an_approved_plan_invalidates_its_approval_and_audit_is_paginated(self):
        item = self.candidate("initial"); self.approved(item)
        cancelled = self.store.cancel(item["operation_id"], plan_digest=item["plan_digest"], principal="owner-one", channel="lida")
        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaisesRegex(PublicationError, "owner-approval-required"):
            self.store.execute(item["operation_id"], plan_digest=item["plan_digest"])
        first = self.store.audit(item["operation_id"], limit=2)
        second = self.store.audit(item["operation_id"], after=first[-1]["sequence"], limit=2)
        self.assertLess(first[-1]["sequence"], second[0]["sequence"])
        completed = self.publish("later")
        with self.assertRaisesRegex(PublicationError, "published-operation-cannot-be-cancelled"):
            self.store.cancel(completed["operation_id"], plan_digest=completed["plan_digest"], principal="owner-one", channel="lida")

    def test_external_backups_and_writer_state_must_have_disjoint_roots(self):
        with self.assertRaisesRegex(PublicationError, "storage-roots-must-be-separate"):
            PublicationStore(self.directory / "bank", self.directory / "bank/private", self.directory / "backups", validator=validate_fixture)
        repo = self.directory / "repo"; (repo / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(PublicationError, "backup-must-be-outside-repository"):
            PublicationStore(self.directory / "other-bank", self.directory / "other-private", repo / "backups", validator=validate_fixture)
        with self.assertRaisesRegex(PublicationError, "bank-validator-required"):
            PublicationStore(self.directory / "bank", self.directory / "private", self.directory / "backups", validator=None)

    def test_hardlinked_candidate_files_rejected(self):
        item = self.candidate("initial")
        path = self.store.candidate_directory(item["operation_id"])
        os.link(path / "registry.json", path / "main/linked.json")
        with self.assertRaisesRegex(PublicationError, "ambiguous-bundle-file"):
            self.store.prepare(item["operation_id"], expected=None, summary={"label": "initial"}, principal="owner-one", channel="lida")

    def test_pointer_parent_is_bounded_in_size_over_many_revisions(self):
        for number in range(24):
            self.publish(str(number))
        self.assertLess((self.directory / "bank/active.json").stat().st_size, 1024)
        self.assertEqual(self.store.current()["revision"], 24)

    def test_pinned_reader_keeps_all_old_answers_after_publication(self):
        import search
        from tiku_shared.bank_versions import pin_bank
        first = self.publish("initial")
        old = self.directory / "bank/versions" / first["result"]["version"] / "main"
        with patch.dict(os.environ, {"TIKU_BANK_STORE": str(self.directory / "bank")}):
            with pin_bank():
                self.publish("replacement")
                self.assertEqual(search.bank_root(), old)
            self.assertNotEqual(search.bank_root(), old)
            answers = search.find_answer_files(old / "4力法/题目/1.png")
            self.assertEqual(len(answers), 6)
            self.assertEqual(answers[-1].read_bytes(), b"initial\x05")


if __name__ == "__main__":
    unittest.main()

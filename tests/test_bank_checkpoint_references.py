"""Version-bound checkpoints with real persistence and OS reader protection."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import tiku_checkpoint_diagnostics as cli
from tests import test_a2_checkpoint_integration as integration
from tests import test_checkpoint_async as asynchronous
from tests.test_bank_readers import CHILD
from tests.test_bank_versions import publish
from tiku_agent.a2_checkpoint_recorder import A2CheckpointRecorderV1
from tiku_agent.checkpoint_bank_reference import BankReferenceV1, CheckpointBankCatalog
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_store import CheckpointQueryScopeV1, SQLiteCheckpointStore
from tiku_agent.checkpoint_submission_budget import CheckpointSubmissionBudget
from tiku_diagnostics.checkpoints import CheckpointDiagnosticService


class PublishedCheckpointTests(unittest.TestCase):
    def fixture(self, *, queued=False):
        fixture = asynchronous.AsyncA2Test() if queued else integration.A2CheckpointIntegrationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.published = fixture.root / "published"
        fixture.published.mkdir()
        fixture.version = publish(fixture.published, "first", 1)
        fixture.source = fixture.version.main / "question.png"
        Image.new("RGB", (24, 18), "white").save(fixture.source)
        fixture.answers = [fixture.version.main / "answer1.png", fixture.version.main / "answer2.png"]
        for path, color in zip(fixture.answers, ("red", "blue")):
            Image.new("RGB", (24, 18), color).save(path)
        environment = patch.dict(os.environ, {"TIKU_BANK_STORE": str(fixture.published)})
        environment.start(); self.addCleanup(environment.stop)
        if queued:
            fixture.engine.bank_catalog = CheckpointBankCatalog({"main": fixture.root, "published": fixture.published})
        else:
            # Exercise the actual launcher's environment-based default mapping.
            fixture.recorder = A2CheckpointRecorderV1(fixture.store, producer=fixture.recorder.producer,
                media_root=fixture.root, bank_root=fixture.root, gate=A2CheckpointCaptureGateV1(enabled=True))
            fixture.runtime.checkpoint_recorder = fixture.recorder
        return fixture

    def maintenance(self, fixture, *, busy):
        result = subprocess.run([sys.executable, "-B", "-c", CHILD, str(fixture.published), "maintenance"],
            input="", text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["acquired"], not busy)

    def answer(self, fixture):
        with fixture.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=fixture.answers):
            response = fixture.runtime.handle_text("session-test", "1", identity_key="invite_test")
        self.assertEqual(response.media_kind, "answer")

    def service(self, fixture, record):
        service = CheckpointDiagnosticService(fixture.root, capacity=fixture.policy, actor_key="operator",
            scope=CheckpointQueryScopeV1(record["owner"]["identity_key"], record["owner"]["session_key"]))
        service.store = SQLiteCheckpointStore(fixture.store.path, artifact_root=fixture.root / "evidence_images",
            capacity=fixture.policy, trace_db_path=fixture.root / "trace.sqlite3")
        return service

    def assert_original_references(self, fixture, records):
        self.assertEqual(len(records), 6)
        self.assertEqual([record["outcome"] for record in records], ["success"] * 6)
        candidates = records[3]["result"]["candidate_scores"]
        references = [item["question_ref"] for item in candidates]
        references += records[-1]["result"]["delivery"]["answer_refs"]
        for reference in references:
            self.assertEqual(reference["bank_id"], "published")
            self.assertEqual(reference["lookup_mode"], "version")
            self.assertTrue(reference["relative_key"].startswith("versions/" + fixture.version.version + "/main/"))
            self.assertNotIn(str(fixture.root), json.dumps(reference))
        service = self.service(fixture, records[-1])
        catalog = fixture.recorder.engine.bank_catalog if hasattr(fixture.recorder, "engine") else fixture.recorder.bank_catalog
        _, question = service.bank_image(records[3]["checkpoint_id"], catalog=catalog,
                                          candidate_id=candidates[0]["candidate_id"])
        self.assertEqual(question, fixture.source.read_bytes())
        for ordinal, path in enumerate(fixture.answers, 1):
            _, content = service.bank_image(records[-1]["checkpoint_id"], catalog=catalog, answer_ordinal=ordinal)
            self.assertEqual(content, path.read_bytes())
        return service, catalog

    def test_sync_capture_and_diagnostics_preserve_old_version_after_publication_and_reopening_store(self):
        fixture = self.fixture()
        self.assertEqual(fixture.search().media_kind, "candidates")
        self.answer(fixture)
        records = fixture.records()
        newer = publish(fixture.published, "second", 2)
        for name in ("question.png", "answer1.png", "answer2.png"):
            Image.new("RGB", (24, 18), "green").save(newer.main / name)
        service, _ = self.assert_original_references(fixture, records)
        # Reconstruct the catalog and scoped diagnostic entry from disk state.
        catalog = CheckpointBankCatalog({"main": fixture.root, "published": fixture.published})
        service = self.service(fixture, records[-1])
        args = ["--runtime-root", str(fixture.root), "--actor-key", "operator", "--identity-key", "invite_test",
                "--session-key", records[-1]["owner"]["session_key"]]
        for key in cli.CAPACITY_FIELDS:
            args += ["--" + key.replace("_", "-"), str(getattr(fixture.policy, key))]
        args += ["bank-image", "--checkpoint-id", records[-1]["checkpoint_id"], "--bank-root", str(fixture.root),
                 "--published-bank-root", str(fixture.published), "--answer-ordinal", "2"]
        output = StringIO()
        with patch.object(cli, "CheckpointDiagnosticService", return_value=service), redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["reference"]["lookup_mode"], "version")
        self.assertNotIn(str(fixture.root), output.getvalue())
        with self.assertRaisesRegex(ValueError, "not configured"):
            service.bank_image(records[-1]["checkpoint_id"], catalog=CheckpointBankCatalog({"main": fixture.root}), answer_ordinal=1)
        old = fixture.version.main.parent
        hidden = fixture.root / "old-unavailable"
        self.assertIn(fixture.root, old.resolve().parents)
        self.assertEqual(hidden.resolve().parent, fixture.root)
        old.rename(hidden)
        with self.assertRaises(FileNotFoundError):
            service.bank_image(records[-1]["checkpoint_id"], catalog=catalog, answer_ordinal=1)
        self.assertTrue((newer.main / "answer1.png").is_file())
        self.maintenance(fixture, busy=False)

    def test_queue_protects_versions_from_admission_through_delayed_checkpoint_commit(self):
        fixture = self.fixture(queued=True)
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        original = fixture.recorder._execute
        def hold(job):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(job)
        with patch.object(fixture.recorder, "_execute", side_effect=hold):
            try:
                self.assertEqual(fixture.search().media_kind, "candidates")
                self.assertTrue(entered.wait(2))
                self.answer(fixture)
                self.maintenance(fixture, busy=True)
                publish(fixture.published, "second", 2)
                self.assertEqual(fixture.records(), [])
            finally:
                release.set()
            self.assertTrue(fixture.recorder.flush(5))
        self.assert_original_references(fixture, fixture.records())
        self.maintenance(fixture, busy=False)

    def test_queue_shutdown_keeps_active_job_protected_and_releases_discarded_jobs(self):
        fixture = self.fixture(queued=True)
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        original = fixture.recorder._execute
        def hold(job):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(job)
        with patch.object(fixture.recorder, "_execute", side_effect=hold):
            try:
                fixture.search()
                self.assertTrue(entered.wait(2))
                self.assertFalse(fixture.recorder.close(timeout=0))
                self.assertEqual(fixture.recorder.health()["pending"], 1)
                self.assertEqual(fixture.recorder.health()["counters"]["shutdown_dropped"], 4)
                self.maintenance(fixture, busy=True)
            finally:
                release.set()
            self.assertTrue(fixture.recorder.flush(5))
        self.assertTrue(fixture.recorder.close())
        self.maintenance(fixture, busy=False)

    def test_queue_expiry_and_failed_capture_release_all_bank_handles(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                fixture = self.fixture(queued=True)
                ticks = [0.0]
                fixture.recorder.clock = lambda: ticks[0]
                entered, release = Event(), Event()
                self.addCleanup(release.set)
                original = fixture.recorder._execute
                def hold(job):
                    entered.set(); self.assertTrue(release.wait(5))
                    if fail:
                        raise OSError("isolated capture failure")
                    return original(job)
                with patch.object(fixture.recorder, "_execute", side_effect=hold):
                    try:
                        self.assertEqual(fixture.submit_route().reason_code, "CAPTURE_QUEUED")
                        self.assertTrue(entered.wait(2))
                        self.assertEqual(fixture.submit_route().reason_code, "CAPTURE_QUEUED")
                        self.maintenance(fixture, busy=True)
                        if not fail:
                            ticks[0] = fixture.recorder.max_age_seconds + 1
                    finally:
                        release.set()
                    self.assertTrue(fixture.recorder.flush(5))
                self.maintenance(fixture, busy=False)
                self.assertEqual(fixture.recorder.health()["pending"], 0)
                self.assertEqual(fixture.recorder.health()["counters"]["failed" if fail else "expired"], 2)
                fixture.recorder.close()

    def test_queue_rejects_unavailable_bank_without_acceptance_or_resource_leak(self):
        fixture = self.fixture(queued=True)
        marker = fixture.published / "reader-gate.lock"
        saved = marker.read_bytes()
        marker.unlink()
        self.assertEqual(fixture.submit_route().reason_code, "CAPTURE_BANK_UNAVAILABLE")
        self.assertFalse(marker.exists())
        self.assertEqual(fixture.recorder.health()["pending"], 0)
        self.assertEqual(fixture.records(), [])
        marker.write_bytes(saved)
        self.assertEqual(fixture.submit_route().reason_code, "CAPTURE_QUEUED")
        self.assertTrue(fixture.recorder.flush(5))
        self.maintenance(fixture, busy=False)

    def test_version_reference_rejects_unbound_modes_and_unsafe_paths_without_io(self):
        version = "a" * 64
        good = "versions/" + version + "/main/4力法/q.png"
        with patch.object(Path, "open", side_effect=AssertionError("construction performs no I/O")):
            self.assertEqual(BankReferenceV1("published", "4力法", good, "version").relative_key, good)
            for key, mode in ((good, "current"), ("q.png", "version"), (good.replace(version, "short"), "version"),
                              (good.replace("main", "private"), "version"), (good.replace("q.png", "../q.png"), "version")):
                with self.subTest(key=key, mode=mode), self.assertRaises(ValueError):
                    BankReferenceV1("published", "4力法", key, mode)
            with self.assertRaises(ValueError):
                BankReferenceV1("main", "4力法", good, "version")

    def test_bank_lease_time_counts_against_shared_admission_budget(self):
        fixture = self.fixture(queued=True)
        original = fixture.engine.bank_catalog.lease
        def slow_lease():
            lease = original()
            time.sleep(0.2)
            return lease
        with patch.object(fixture.engine.bank_catalog, "lease", side_effect=slow_lease):
            result = fixture.submit_route(budget=CheckpointSubmissionBudget(seconds=0.1))
        self.assertEqual(result.reason_code, "CAPTURE_REQUEST_BUDGET_EXHAUSTED")
        self.assertEqual(fixture.recorder.health()["pending"], 0)
        self.assertEqual(fixture.records(), [])
        self.maintenance(fixture, busy=False)


if __name__ == "__main__":
    unittest.main()

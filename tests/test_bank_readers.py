"""Actual process locks for a read-only bank, without a management service."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tiku_shared import bank_readers as readers
from tiku_shared import bank_versions


CHILD = """
import json, os, sys
from tiku_shared.bank_readers import reader_gate, maintenance_gate, BankReadersBusy
gate = reader_gate if sys.argv[2] == 'reader' else maintenance_gate
try:
    with gate(sys.argv[1]):
        print(json.dumps({'acquired': True}), flush=True)
        if sys.stdin.readline().strip() == 'abort':
            os._exit(73)
except BankReadersBusy:
    print(json.dumps({'acquired': False}), flush=True)
"""


class ReaderGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lida-reader-gate-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        readers.initialize_reader_gate(self.root)
        self.environment = patch.dict(os.environ, {"TIKU_BANK_STORE": str(self.root)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.children = []
        self.addCleanup(self.close_children)

    def close_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def child(self, mode):
        child = subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(self.root), mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parents[1])
        self.children.append(child)
        result = json.loads(child.stdout.readline())
        return child, result["acquired"]

    def finish(self, child, *, abort=False):
        _, error = child.communicate("abort\n" if abort else "release\n", timeout=10)
        self.assertEqual(child.returncode, 73 if abort else 0, error)

    def assert_maintenance_busy(self):
        child, acquired = self.child("maintenance")
        self.assertFalse(acquired)
        self.finish(child)

    def test_shared_processes_use_read_only_handles_and_retention_skips_without_changing_files(self):
        marker = self.root / readers.GATE_NAME
        before = (marker.read_bytes(), marker.stat().st_mtime_ns, marker.stat().st_ino)
        original = readers._native_lock
        def inspect(stream, exclusive):
            self.assertFalse(stream.writable())
            with self.assertRaises(OSError):
                os.write(stream.fileno(), b"X")
            return original(stream, exclusive)
        with patch.object(readers, "_native_lock", side_effect=inspect), readers.reader_gate():
            child, acquired = self.child("reader")
            self.assertTrue(acquired)
            self.assert_maintenance_busy()
            with readers.reader_gate():
                self.assert_maintenance_busy()
            self.finish(child)
        with readers.maintenance_gate(self.root):
            pass
        self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns, marker.stat().st_ino), before)
        self.assertEqual(list(self.root.iterdir()), [marker])

    def test_reader_waits_for_short_maintenance_and_resumes_after_release(self):
        attempted, entered = threading.Event(), threading.Event()
        original = readers._native_lock
        def observed(stream, exclusive):
            acquire, release = original(stream, exclusive)
            def attempt():
                result = acquire()
                if not exclusive and not result:
                    attempted.set()
                return result
            return attempt, release
        def read():
            with readers.reader_gate():
                entered.set()
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(readers, "_native_lock", side_effect=observed):
            with readers.maintenance_gate(self.root):
                future = executor.submit(read)
                self.assertTrue(attempted.wait(5))
                self.assertFalse(entered.is_set())
            future.result(timeout=5)
        self.assertTrue(entered.is_set())

    def test_copied_context_worker_keeps_its_own_lock_after_parent_returns(self):
        entered, release = threading.Event(), threading.Event()
        def work():
            with readers.reader_gate():
                entered.set()
                self.assertTrue(release.wait(10))
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with readers.reader_gate():
                    future = executor.submit(copy_context().run, work)
                    self.assertTrue(entered.wait(5))
                self.assert_maintenance_busy()
            finally:
                release.set()
            future.result(timeout=5)
        with readers.maintenance_gate(self.root):
            pass

    def test_kernel_releases_lock_when_child_exits_without_cleanup(self):
        child, acquired = self.child("reader")
        self.assertTrue(acquired)
        self.assert_maintenance_busy()
        self.finish(child, abort=True)
        with readers.maintenance_gate(self.root):
            pass

    def test_missing_corrupt_or_hardlinked_gate_never_creates_or_repairs_it(self):
        marker = self.root / readers.GATE_NAME
        marker.unlink()
        with self.assertRaises(FileNotFoundError), readers.reader_gate():
            pass
        self.assertFalse(marker.exists())
        marker.write_bytes(b"bad")
        with self.assertRaises(ValueError), readers.reader_gate():
            pass
        self.assertEqual(marker.read_bytes(), b"bad")
        marker.write_bytes(readers.GATE_BYTES)
        alias = self.root / "alias"
        os.link(marker, alias)
        with self.assertRaises(ValueError), readers.reader_gate():
            pass
        alias.unlink()
        with readers.reader_gate():
            pass

    def test_published_pointer_and_old_candidate_are_guarded_before_manifest_read(self):
        from test_bank_versions import publish, question
        first = publish(self.root, "old", 1)
        old_question = question(first, 6)
        second = publish(self.root, "new", 2)
        original = bank_versions._read_version
        observed = []
        def inspect(root, version):
            self.assert_maintenance_busy()
            observed.append(version)
            return original(root, version)
        with patch.object(bank_versions, "_read_version", side_effect=inspect):
            with bank_versions.pin_bank():
                self.assertEqual(bank_versions.main_root(None), second.main)
            with bank_versions.pin_question_path(old_question, self.root / "legacy"):
                self.assertEqual(bank_versions.main_root(None), first.main)
        self.assertIn(first.version, observed)
        self.assertIn(second.version, observed)

    def test_explicit_private_copy_ignores_unavailable_ambient_bank_without_weakening_managed_reads(self):
        from test_bank_versions import question
        import search
        private = bank_versions.BankVersion(self.root / "private/main", self.root / "private/symbolic", None)
        source = question(private, 2)
        unavailable = self.root / "unavailable-production"
        with patch.dict(os.environ, {"TIKU_BANK_STORE": str(unavailable)}), patch.object(search, "ROOT", private.main):
            with bank_versions.pin_bank(private):
                with bank_versions.pin_bank():
                    self.assertEqual(search.bank_root(), private.main)
                    self.assertEqual([file.read_bytes() for file in search.find_answer_files(source)],
                                     [b"answer-0", b"answer-1"])
            self.assertFalse(unavailable.exists())
            with self.assertRaises(FileNotFoundError), bank_versions.pin_bank():
                pass


if __name__ == "__main__":
    unittest.main()

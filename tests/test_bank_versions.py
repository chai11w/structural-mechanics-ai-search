import hashlib
import json
from pathlib import Path

import os
import tempfile
import unittest
from unittest.mock import patch

import search
from tiku_shared import bank_versions
from tiku_shared.bank_readers import initialize_reader_gate
from multi_agent_pipeline import symbolic_root


def publish(root, label, revision):
    initialize_reader_gate(root)
    raw = json.dumps({"schema": 1, "files": [], "label": label}).encode()
    version = hashlib.sha256(raw).hexdigest()
    directory = root / "versions" / version
    (directory / "main").mkdir(parents=True)
    (directory / "symbolic").mkdir()
    (directory / "manifest.json").write_bytes(raw)
    replacement = root / "next.json"
    replacement.write_text(json.dumps({"schema": 1, "revision": revision, "version": version}))
    replacement.replace(root / "active.json")
    return bank_versions.read_version(root, version)


def question(version, count):
    directory = version.main / "4力法"
    (directory / "题目").mkdir(parents=True)
    (directory / "答案").mkdir()
    path = directory / "题目/1.png"
    path.write_bytes(b"question")
    for i in range(count):
        (directory / "答案" / f"1{'+' * i}.png").write_bytes(f"answer-{i}".encode())
    return path


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lida-bank-reader-")
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {"TIKU_BANK_STORE": str(self.root)})
        self.environment.start()
        self.legacy_root = search.ROOT

    def tearDown(self):
        search.ROOT = self.legacy_root
        self.environment.stop()
        self.temporary.cleanup()

    def test_pinned_read_is_consistent_across_pointer_switch_and_does_not_contact_management(self):
        tmp_path = self.root
        first = publish(tmp_path, "first", 1)
        with bank_versions.pin_bank():
            assert search.bank_root() == first.main
            second = publish(tmp_path, "second", 2)
            assert search.bank_root() == first.main
            assert symbolic_root(search.bank_root()) == first.symbolic
        assert search.bank_root() == second.main


    def test_old_candidate_keeps_six_answers_after_new_publication_deleted_it(self):
        tmp_path = self.root
        first = publish(tmp_path, "first", 1)
        path = question(first, 6)
        second = publish(tmp_path, "deleted", 2)
        answers = search.find_answer_files(path)
        assert len(answers) == 6
        assert [file.read_bytes() for file in answers] == [f"answer-{i}".encode() for i in range(6)]
        assert search.bank_root() == second.main


    def test_mixed_formats_follow_page_suffix_order_and_ignore_other_records(self):
        tmp_path = self.root
        version = publish(tmp_path, "mixed", 1)
        path = question(version, 2)
        answers = version.main / "4力法/答案"
        (answers / "1++.webp").write_bytes(b"third")
        (answers / "11.png").write_bytes(b"other")
        (answers / "1foo.png").write_bytes(b"other")
        assert [file.name for file in search.find_answer_files(path)] == ["1.png", "1+.png", "1++.webp"]


    def test_invalid_pointer_manifest_and_external_candidate_fail_closed(self):
        tmp_path = self.root
        version = publish(tmp_path, "first", 1)
        with self.assertRaises(ValueError):
            with bank_versions.pin_question_path(tmp_path / "outside/1.png", tmp_path / "legacy"):
                pass
        (version.main.parent / "manifest.json").write_text('{}')
        with self.assertRaises(ValueError):
            search.bank_root()
        (tmp_path / "active.json").write_text(json.dumps({"schema": 1, "revision": 1, "version": "../escape"}))
        with self.assertRaises(ValueError):
            search.bank_root()


    def test_legacy_direct_store_cannot_write_a_published_bank(self):
        tmp_path = self.root
        publish(tmp_path, "first", 1)
        with self.assertRaises(PermissionError):
            search.store_chapter_excel("4力法", [])
        with self.assertRaises(PermissionError):
            search.store("4力法", loads_json="[]")


    def test_without_publication_configuration_existing_root_behavior_is_preserved(self):
        tmp_path = self.root
        os.environ.pop("TIKU_BANK_STORE", None)
        search.ROOT = tmp_path
        assert search.bank_root() == tmp_path
        assert symbolic_root() == tmp_path.parent / f"{tmp_path.name}_字母库"


    def test_search_cache_uses_runtime_and_cannot_be_written_in_published_version(self):
        version = publish(self.root, "initial", 1)
        with patch.dict(os.environ, {"TIKU_SEARCH_STATE_DIR": ""}):
            with self.assertRaisesRegex(ValueError, "TIKU_SEARCH_STATE_DIR"):
                search.last_search_file()
        with patch.dict(os.environ, {"TIKU_SEARCH_STATE_DIR": str(version.main)}):
            with self.assertRaisesRegex(ValueError, "outside"):
                search.last_search_file()
        runtime = self.root.parent / (self.root.name + "-runtime")
        try:
            with patch.dict(os.environ, {"TIKU_SEARCH_STATE_DIR": str(runtime)}):
                cache = search.last_search_file()
                cache.write_text("[]")
                self.assertEqual(cache.parent, runtime)
                self.assertFalse((version.main / "_last_search.json").exists())
        finally:
            (runtime / "_last_search.json").unlink(missing_ok=True)
            if runtime.exists():
                runtime.rmdir()


    def test_managed_path_resolution_does_not_repair_the_index(self):
        version = publish(self.root, "initial", 1)
        path = question(version, 1)
        with patch.object(search, "_find_relocated_question_image", return_value=path), patch.object(search, "_update_excel_path") as writer:
            resolved, _, repaired = search.resolve_question_path("4力法/题目/missing.jpg", chapter_name="4力法", update_excel=True)
            self.assertEqual(resolved, path)
            self.assertTrue(repaired)
            writer.assert_not_called()

if __name__ == "__main__":
    unittest.main()

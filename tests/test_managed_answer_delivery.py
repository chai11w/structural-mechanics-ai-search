"""Managed CLI/Feishu reads complete selected answers without shared output writes."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import search
from scripts import feishu_tiku_bot as feishu
from test_bank_versions import publish, question


class ManagedAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lida-answer-delivery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "published"; self.store.mkdir()
        self.state = self.root / "state"; self.state.mkdir()
        self.shared = self.root / "shared-output"; self.shared.mkdir()
        (self.shared / "keep.txt").write_text("do not clear existing output")
        self.environment = patch.dict(os.environ, {"TIKU_BANK_STORE": str(self.store), "TIKU_SEARCH_STATE_DIR": str(self.state)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.output = patch.object(search, "ANSWER_OUTPUT", self.shared)
        self.output.start(); self.addCleanup(self.output.stop)
        self.first = publish(self.store, "six-answer-old-candidate", 1)
        self.first_question = question(self.first, 6)
        self.second = publish(self.store, "another-search-new-publication", 2)
        self.second_question = question(self.second, 1)
        self.sessions = feishu.TikuSessionStore()
        options = feishu.FeishuTikuOptions(temp_dir=self.state, admin_fee_db=self.state / "no-fees.sqlite")
        self.bot = feishu.TikuBot(options=options, coordinator=object(), sessions=self.sessions,
            store_service=object(), delete_service=object())

    def cache(self, path):
        (self.state / "_last_search.json").write_text(json.dumps([{"rank": 1, "path": str(path)}]), encoding="utf-8")

    def assert_shared_untouched(self):
        self.assertEqual([p.name for p in self.shared.iterdir()], ["keep.txt"])
        self.assertEqual((self.shared / "keep.txt").read_text(), "do not clear existing output")

    def test_cli_returns_all_old_version_paths_and_preserves_shared_output(self):
        self.cache(self.first_question)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = search.answer(1)
        self.assertEqual(len(result), 6)
        self.assertTrue(all(self.first.version in str(path) for path in result))
        self.assertIn("6 张答案", output.getvalue())
        self.assert_shared_untouched()

    def test_feishu_uses_each_selected_session_and_never_global_rank_or_export(self):
        self.cache(self.second_question)
        alice = feishu.TikuSession(state="waiting_choice", results=[{"path": str(self.first_question)}])
        bob = feishu.TikuSession(state="waiting_multi_choice", results=[{"path": str(self.second_question)}])
        self.sessions.save("alice", alice); self.sessions.save("bob", bob)
        with patch.object(feishu, "answer", side_effect=AssertionError("shared export must not run")), \
                patch.object(feishu, "answer_output_files", side_effect=AssertionError("shared output must not be read")):
            reply = self.bot._answer_choice("alice", alice, 1)
            self.assertEqual(len(reply.images), 6)
            self.assertTrue(all(self.first.version in str(path) for path in reply.images))
            self.assertEqual(self.sessions.get("alice").state, "idle")
            self.assertIs(self.sessions.get("bob"), bob)
            # A multi-question candidate also sends every answer in its original version.
            bob.results = [{"path": str(self.first_question)}]
            reply = self.bot._answer_multi_choice("bob", bob, {"label": "2"}, 1)
            self.assertEqual(len(reply.images), 6)
            self.assertIs(self.sessions.get("bob"), bob)
        self.assert_shared_untouched()

    def test_bad_candidate_path_is_rejected_before_shared_output_access(self):
        session = feishu.TikuSession(results=[{"path": str(self.root / "outside.png")}])
        with self.assertRaisesRegex(ValueError, "outside its bank"):
            self.bot._answer_choice("alice", session, 1)
        self.assert_shared_untouched()


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock
from uuid import uuid4

from scripts.feishu_tiku_bot import FeishuTikuOptions, TikuBot, TikuSession


EXPECTED_SCOPE_REPLY = (
    "结构力学题库支持静定结构、静定结构位移、力法、位移法、力矩分配；"
    "矩阵位移和影响线仅支持含具体外荷载的题目。"
)


class FeishuGroundedFactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"feishu_grounded_fact_{uuid4().hex}"
        )
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.coordinator = Mock()
        self.store_service = Mock()
        self.delete_service = Mock()
        self.bot = TikuBot(
            options=FeishuTikuOptions(
                dry_run=True,
                temp_dir=self.root / "state",
                admin_fee_db=self.root / "costs.sqlite3",
            ),
            coordinator=self.coordinator,
            store_service=self.store_service,
            delete_service=self.delete_service,
        )

    def test_scope_fact_is_exact_and_preserves_every_conversation_state(self):
        sessions = (
            TikuSession(state="idle"),
            TikuSession(state="waiting_chapter", image_path=Path("question.jpg")),
            TikuSession(
                state="waiting_choice",
                results=[{"rank": 1, "path": "candidate.jpg", "score": 1.0}],
            ),
            TikuSession(state="store_waiting_question"),
        )

        for index, session in enumerate(sessions):
            with self.subTest(state=session.state):
                sender = f"scope-user-{index}"
                self.bot.sessions.save(sender, session)
                before = deepcopy(self.bot.sessions.get(sender))

                response = self.bot.receive_text(
                    sender,
                    "你可以回答哪些章节的问题",
                )

                self.assertEqual(response.texts, [EXPECTED_SCOPE_REPLY])
                self.assertEqual(response.images, [])
                self.assertEqual(self.bot.sessions.get(sender), before)

        self.coordinator.assert_not_called()
        self.store_service.assert_not_called()
        self.delete_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()

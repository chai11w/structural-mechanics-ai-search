from pathlib import Path
import unittest

from scripts import feishu_tiku_bot
from tiku_agent import tools
from tiku_agent.intent_contract import CHAPTERS
from tiku_shared import multi_question


class SharedMultiQuestionTest(unittest.TestCase):
    def test_question_normalization_is_shared_with_feishu_compatibility_wrappers(self):
        raw_questions = [
            {"label": "第十题", "loads": [{"type": "集中", "raw": "P"}], "chapter_confidence": "0.9"},
            {"label": "图 2", "loads": "invalid"},
        ]

        self.assertEqual(
            multi_question.normalize_multi_questions(raw_questions),
            feishu_tiku_bot.normalize_multi_questions(raw_questions),
        )
        self.assertEqual(multi_question.normalize_question_key("第十题"), "10")
        self.assertEqual(feishu_tiku_bot.normalize_question_key("图 2"), "2")

    def test_effective_chapter_requires_the_shared_confidence_rule(self):
        question = {"chapter_hint": "4力法", "chapter_confidence": 0.8}
        self.assertEqual(multi_question.effective_question_chapter(question, CHAPTERS), "4力法")
        self.assertEqual(feishu_tiku_bot.effective_question_chapter(question), "4力法")
        self.assertIsNone(multi_question.effective_question_chapter({**question, "chapter_confidence": 0.79}, CHAPTERS))

    def test_agent_does_not_import_the_feishu_runtime(self):
        source = Path(tools.__file__).read_text(encoding="utf-8")
        self.assertNotIn("scripts.feishu_tiku_bot", source)
        self.assertIn("tiku_shared.multi_question", source)


if __name__ == "__main__":
    unittest.main()

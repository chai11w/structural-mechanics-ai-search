import unittest

from tiku_agent.intent import (
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    parse_chapter,
    parse_user_intent,
)


class TikuAgentIntentTest(unittest.TestCase):
    def test_image_path_event_becomes_search_image(self):
        result = parse_user_intent(state=STATE_IDLE, image_path=r"D:\tmp\q.jpg")
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "search_image")
        self.assertEqual(result.data["image_path"], r"D:\tmp\q.jpg")

    def test_text_image_path_becomes_search_image(self):
        result = parse_user_intent(r'帮我搜 "D:\tmp\q.png"', state=STATE_IDLE)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "search_image")
        self.assertEqual(result.data["image_path"], r"D:\tmp\q.png")

    def test_wait_chapter_digit_maps_to_chapter(self):
        result = parse_user_intent("4", state=STATE_WAIT_CHAPTER)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "set_chapter")
        self.assertEqual(result.data["chapter"], "4力法")

    def test_chapter_alias_maps_to_chapter(self):
        self.assertEqual(parse_chapter("按力法"), "4力法")
        self.assertEqual(parse_chapter("矩阵位移"), "7矩阵位移")

    def test_wait_question_choice_digit_selects_question(self):
        result = parse_user_intent("2", state=STATE_WAIT_QUESTION_CHOICE, question_count=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_question")
        self.assertEqual(result.data, {"question_index": 2, "chapter_override": None})

    def test_wait_question_choice_supports_chapter_override(self):
        result = parse_user_intent("2-4力法", state=STATE_WAIT_QUESTION_CHOICE, question_count=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_question")
        self.assertEqual(result.data, {"question_index": 2, "chapter_override": "4力法"})

    def test_wait_candidate_choice_digit_selects_candidate(self):
        result = parse_user_intent("1", state=STATE_WAIT_CANDIDATE_CHOICE, candidate_count=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_candidate")
        self.assertEqual(result.data, {"rank": 1})

    def test_natural_candidate_choice(self):
        result = parse_user_intent("选第一个", state=STATE_WAIT_CANDIDATE_CHOICE, candidate_count=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_candidate")
        self.assertEqual(result.data, {"rank": 1})

    def test_cancel(self):
        result = parse_user_intent("取消", state=STATE_WAIT_CANDIDATE_CHOICE)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "cancel")

    def test_forbidden_store_delete_are_unsupported(self):
        delete_result = parse_user_intent("删掉第一个", state=STATE_WAIT_CANDIDATE_CHOICE)
        store_result = parse_user_intent("入库这道题", state=STATE_IDLE)
        self.assertFalse(delete_result.ok)
        self.assertEqual(delete_result.data["requested_action"], "delete")
        self.assertFalse(store_result.ok)
        self.assertEqual(store_result.data["requested_action"], "store")


if __name__ == "__main__":
    unittest.main()


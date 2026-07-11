import unittest

from tiku_agent.intent import (
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    build_intent_prompt,
    parse_chapter,
    parse_user_intent,
    parse_user_intent_rule_fallback,
    validate_intent_payload,
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

    def test_prompt_contains_state_and_user_text(self):
        prompt = build_intent_prompt("第二题按力法", state=STATE_WAIT_QUESTION_CHOICE, question_count=3)
        self.assertIn("WAIT_QUESTION_CHOICE", prompt)
        self.assertIn("第二题按力法", prompt)
        self.assertIn("question_count", prompt)

    def test_llm_wait_chapter_maps_to_chapter(self):
        result = parse_user_intent(
            "按力法",
            state=STATE_WAIT_CHAPTER,
            llm_client=lambda _prompt: {
                "intent": "set_chapter",
                "chapter": "4力法",
                "confidence": 0.95,
                "reason": "用户指定力法",
            },
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "set_chapter")
        self.assertEqual(result.data["chapter"], "4力法")
        self.assertEqual(result.source, "llm")

    def test_chapter_alias_maps_to_chapter(self):
        self.assertEqual(parse_chapter("按力法"), "4力法")
        self.assertEqual(parse_chapter("矩阵位移"), "7矩阵位移")
        self.assertEqual(parse_chapter("不对，应该是第三章"), "3静定结构位移")

    def test_llm_wait_question_choice_selects_question(self):
        result = parse_user_intent(
            "第二题",
            state=STATE_WAIT_QUESTION_CHOICE,
            question_count=3,
            llm_client=lambda _prompt: {
                "intent": "select_question",
                "question_index": 2,
                "confidence": 0.9,
                "reason": "用户选择第二题",
            },
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_question")
        self.assertEqual(result.data["question_index"], 2)
        self.assertIsNone(result.data["chapter_override"])

    def test_llm_wait_question_choice_supports_chapter_override(self):
        result = parse_user_intent(
            "第二题按力法",
            state=STATE_WAIT_QUESTION_CHOICE,
            question_count=3,
            llm_client=lambda _prompt: {
                "intent": "select_question",
                "question_index": 2,
                "chapter": "4力法",
                "confidence": 0.96,
                "reason": "用户选择第二题并指定力法",
            },
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_question")
        self.assertEqual(result.data["question_index"], 2)
        self.assertEqual(result.data["chapter_override"], "4力法")

    def test_llm_wait_candidate_choice_selects_candidate(self):
        result = parse_user_intent(
            "给我第一个答案",
            state=STATE_WAIT_CANDIDATE_CHOICE,
            candidate_count=3,
            llm_client=lambda _prompt: {
                "intent": "select_candidate",
                "rank": 1,
                "confidence": 0.95,
                "reason": "用户选择第一个候选",
            },
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_candidate")
        self.assertEqual(result.data["rank"], 1)

    def test_validate_rejects_candidate_selection_in_wrong_state(self):
        result = validate_intent_payload(
            {"intent": "select_candidate", "rank": 1, "confidence": 0.9, "reason": "用户选第一个"},
            state=STATE_WAIT_CHAPTER,
            candidate_count=3,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.intent, "unsupported")
        self.assertIn("当前状态", result.error)

    def test_validate_rejects_out_of_range_question(self):
        result = validate_intent_payload(
            {"intent": "select_question", "question_index": 5, "confidence": 0.9, "reason": "用户选择第五题"},
            state=STATE_WAIT_QUESTION_CHOICE,
            question_count=3,
        )
        self.assertFalse(result.ok)
        self.assertIn("题号超出范围", result.error)

    def test_rule_fallback_can_parse_candidate_choice(self):
        result = parse_user_intent_rule_fallback("选第一个", state=STATE_WAIT_CANDIDATE_CHOICE, candidate_count=3)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "select_candidate")
        self.assertEqual(result.data, {"rank": 1})
        self.assertEqual(result.source, "rule_fallback")

    def test_cancel(self):
        result = parse_user_intent(
            "取消",
            state=STATE_WAIT_CANDIDATE_CHOICE,
            llm_client=lambda _prompt: {"intent": "cancel", "confidence": 0.9, "reason": "用户取消"},
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "cancel")

    def test_rule_fallback_can_resend_answer_after_answered(self):
        result = parse_user_intent("刚才答案再发我", state="ANSWERED", use_llm=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "resend_answer")

    def test_forbidden_store_delete_are_unsupported_by_llm_payload(self):
        delete_result = parse_user_intent(
            "删掉第一个",
            state=STATE_WAIT_CANDIDATE_CHOICE,
            llm_client=lambda _prompt: {
                "intent": "unsupported",
                "requested_action": "delete",
                "rank": 1,
                "confidence": 0.95,
                "reason": "当前版本不支持删除",
            },
        )
        store_result = parse_user_intent(
            "入库这道题",
            state=STATE_IDLE,
            llm_client=lambda _prompt: {
                "intent": "unsupported",
                "requested_action": "store",
                "confidence": 0.95,
                "reason": "当前版本不支持入库",
            },
        )
        self.assertFalse(delete_result.ok)
        self.assertEqual(delete_result.data["requested_action"], "delete")
        self.assertFalse(store_result.ok)
        self.assertEqual(store_result.data["requested_action"], "store")

    def test_llm_failure_uses_rule_fallback(self):
        result = parse_user_intent(
            "4",
            state=STATE_WAIT_CHAPTER,
            llm_client=lambda _prompt: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "set_chapter")
        self.assertEqual(result.data["chapter"], "4力法")
        self.assertEqual(result.source, "rule_fallback")


if __name__ == "__main__":
    unittest.main()

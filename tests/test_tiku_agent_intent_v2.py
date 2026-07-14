import json
import unittest

from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_v2 import build_context_prompt_v2, decide_intent_v2


class IntentV2Test(unittest.TestCase):
    def test_explicit_question_and_chapter_is_one_rule_action(self):
        context = ConversationContextV2(
            phase="WAIT_QUESTION_CHOICE",
            active_namespace="question",
            question_count=3,
            has_active_image=True,
        )
        decision = decide_intent_v2("第二题按力法搜", context)
        self.assertEqual(decision.action, "select_question")
        self.assertEqual(decision.question_index, 2)
        self.assertEqual(decision.chapter_override, "4力法")
        self.assertEqual(decision.source, "rule")

    def test_bare_number_follows_active_namespace_and_out_of_range_clarifies(self):
        allowed = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=2,
            candidate_count=3,
            has_active_image=True,
            has_answer=True,
        )
        self.assertEqual(decide_intent_v2("二", allowed).candidate_rank, 2)
        denied = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=2,
            candidate_count=1,
            has_active_image=True,
            has_answer=True,
        )
        decision = decide_intent_v2("二", denied)
        self.assertEqual(decision.action, "clarification")
        self.assertEqual(decision.clarification_reason, "out_of_range")

    def test_forbidden_and_conversation_rules_never_call_model(self):
        calls = []

        def model(_prompt):
            calls.append(True)
            return {"action": "select_candidate", "candidate_rank": 2}

        context = ConversationContextV2(
            phase="WAIT_CANDIDATE_CHOICE",
            active_namespace="candidate",
            question_count=1,
            candidate_count=3,
            has_active_image=True,
        )
        self.assertEqual(decide_intent_v2("删掉第二个候选", context, llm_client=model).action, "reject")
        self.assertEqual(decide_intent_v2("辛苦了", context, llm_client=model).action, "small_talk")
        self.assertEqual(decide_intent_v2("你还能帮我做什么", context, llm_client=model).action, "capability_help")
        self.assertEqual(calls, [])

    def test_context_expression_calls_model_once_then_code_authorizes(self):
        calls = []

        def model(prompt):
            calls.append(prompt)
            return {"action": "select_question", "question_index": 1, "confidence": 0.91}

        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=2,
            candidate_count=2,
            selected_question_index=2,
            completed_question_indexes=(2,),
            remaining_question_indexes=(1,),
            has_active_image=True,
            has_answer=True,
        )
        decision = decide_intent_v2("那剩下那题呢", context, llm_client=model)
        self.assertEqual(decision.action, "select_question")
        self.assertEqual(decision.question_index, 1)
        self.assertEqual(decision.source, "context_llm")
        self.assertEqual(len(calls), 1)

    def test_model_cannot_bypass_bounds_or_return_unknown_fields(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="question",
            question_count=2,
            candidate_count=1,
            has_active_image=True,
            has_answer=True,
        )
        out_of_range = decide_intent_v2(
            "回到之前那题",
            context,
            llm_client=lambda _prompt: {"action": "select_question", "question_index": 9},
        )
        self.assertEqual(out_of_range.action, "clarification")
        self.assertEqual(out_of_range.clarification_reason, "out_of_range")
        malformed = decide_intent_v2(
            "回到之前那题",
            context,
            llm_client=lambda _prompt: {"action": "cancel", "tool": "delete"},
        )
        self.assertEqual(malformed.action, "clarification")

    def test_image_event_consumes_pending_chapter_semantically(self):
        context = ConversationContextV2.from_mapping(
            {
                "phase": "ANSWERED",
                "pending_chapter": "4力法",
                "trusted_image_event": True,
            }
        )
        decision = decide_intent_v2("", context, event_type="image")
        self.assertEqual(decision.action, "search_image")
        self.assertEqual(decision.chapter_override, "4力法")

    def test_prompt_contains_only_json_safe_context_summary(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            question_count=2,
            remaining_question_indexes=(1,),
            completed_question_indexes=(2,),
        )
        prompt = build_context_prompt_v2("剩下那题", context)
        payload = json.loads(prompt.split("输入 JSON：\n", 1)[1])
        self.assertEqual(payload["conversation_context"]["remaining_question_indexes"], [1])
        self.assertNotIn("image_path", prompt)

    def test_context_model_prompt_documents_bounded_output_schema(self):
        context = ConversationContextV2(phase="IDLE")
        prompt = build_context_prompt_v2("那个", context)
        self.assertIn('"question_index": null', prompt)
        self.assertIn("ambiguous_reference", prompt)
        self.assertNotIn("search_image|", prompt)


if __name__ == "__main__":
    unittest.main()

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

    def test_question_grammar_supports_small_and_classifier_forms(self):
        context = ConversationContextV2(
            phase="WAIT_QUESTION_CHOICE",
            active_namespace="question",
            question_count=3,
            has_active_image=True,
        )
        small = decide_intent_v2("给我找第2小题，按位移法来", context)
        self.assertEqual(small.action, "select_question")
        self.assertEqual(small.question_index, 2)
        self.assertEqual(small.chapter_override, "5位移法")
        classifier = decide_intent_v2("查第二道题", context)
        self.assertEqual(classifier.question_index, 2)

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
        self.assertEqual(decision.source, "validator")
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
        self.assertEqual(out_of_range.clarification_reason, "ambiguous_reference")
        malformed = decide_intent_v2(
            "回到之前那题",
            context,
            llm_client=lambda _prompt: {"action": "cancel", "tool": "delete"},
        )
        self.assertEqual(malformed.action, "clarification")

    def test_delete_euphemism_is_rejected_before_candidate_selection(self):
        calls = []
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            selected_candidate_rank=2,
            has_active_image=True,
            has_answer=True,
        )
        decision = decide_intent_v2(
            "把候选一从库里清掉",
            context,
            llm_client=lambda _prompt: calls.append(True) or {"action": "select_candidate", "candidate_rank": 1},
        )
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.requested_action, "delete")
        self.assertEqual(calls, [])

    def test_negative_retention_in_question_bank_is_rejected_before_number_rule(self):
        calls = []
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            has_active_image=True,
            has_answer=True,
        )
        decision = decide_intent_v2(
            "候选二别留在题库里了",
            context,
            llm_client=lambda _prompt: calls.append(True) or {"action": "select_candidate", "candidate_rank": 2},
        )
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.requested_action, "delete")
        self.assertEqual(calls, [])

    def test_model_resend_requires_answer_delivery_evidence(self):
        answered = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            has_active_image=True,
            has_answer=True,
        )
        unsupported = decide_intent_v2(
            "忙吗，能接着看题不",
            answered,
            llm_client=lambda _prompt: {"action": "resend_answer"},
        )
        self.assertEqual(unsupported.action, "clarification")
        self.assertEqual(unsupported.clarification_reason, "ambiguous_action")

        supported = decide_intent_v2(
            "上个答案再给我看一遍",
            answered,
            llm_client=lambda _prompt: {"action": "resend_answer"},
        )
        self.assertEqual(supported.action, "resend_answer")

    def test_model_can_use_error_state_to_understand_retry_paraphrase(self):
        context = ConversationContextV2(
            phase="ERROR",
            question_count=1,
            has_active_image=True,
            has_explainable_failure=True,
            retryable_error=True,
        )
        decision = decide_intent_v2(
            "服务刚才没反应，再跑一遍",
            context,
            llm_client=lambda _prompt: {"action": "retry_search"},
        )
        self.assertEqual(decision.action, "retry_search")

    def test_model_candidate_choice_requires_unique_reference_evidence(self):
        multiple = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=3,
            selected_candidate_rank=1,
            has_active_image=True,
            has_answer=True,
        )
        unsafe_guess = decide_intent_v2(
            "换一个",
            multiple,
            llm_client=lambda _prompt: {"action": "select_candidate", "candidate_rank": 2},
        )
        self.assertEqual(unsafe_guess.action, "clarification")
        self.assertEqual(unsafe_guess.clarification_reason, "ambiguous_reference")

        unique = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            selected_candidate_rank=1,
            has_active_image=True,
            has_answer=True,
        )
        verified = decide_intent_v2(
            "换个答案看看",
            unique,
            llm_client=lambda _prompt: {"action": "select_candidate", "candidate_rank": 1},
        )
        self.assertEqual(verified.action, "select_candidate")
        self.assertEqual(verified.candidate_rank, 2)
        self.assertEqual(verified.source, "validator")

    def test_model_question_choice_requires_recorded_reference_evidence(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="question",
            question_count=3,
            candidate_count=2,
            selected_question_index=2,
            previous_question_index=1,
            completed_question_indexes=(1, 2),
            remaining_question_indexes=(3,),
            has_active_image=True,
            has_answer=True,
        )
        previous = decide_intent_v2(
            "上一道",
            context,
            llm_client=lambda _prompt: {"action": "select_question", "question_index": 3},
        )
        self.assertEqual(previous.question_index, 1)
        remaining = decide_intent_v2(
            "还没查的那一道",
            context,
            llm_client=lambda _prompt: {"action": "select_question", "question_index": 2},
        )
        self.assertEqual(remaining.question_index, 3)

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

    def test_chapter_target_understands_future_image_without_literal_image_word(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            has_active_image=True,
            has_answer=True,
        )
        future = decide_intent_v2("我下一张发的是影响线", context)
        self.assertEqual(future.action, "set_chapter")
        self.assertEqual(future.chapter_override, "8影响线")
        self.assertEqual(future.chapter_target, "next_image")
        current = decide_intent_v2("当前这个按力矩分配法重搜", context)
        self.assertEqual(current.chapter_target, "current_question")

    def test_chapter_target_understands_general_future_time_expressions(self):
        context = ConversationContextV2(
            phase="ANSWERED",
            active_namespace="candidate",
            question_count=1,
            candidate_count=2,
            has_active_image=True,
            has_answer=True,
        )
        for text, chapter in (
            ("待会传的那题是力法", "4力法"),
            ("稍后发的题按影响线", "8影响线"),
            ("一会儿给你的题用位移法", "5位移法"),
        ):
            with self.subTest(text=text):
                decision = decide_intent_v2(text, context)
                self.assertEqual(decision.action, "set_chapter")
                self.assertEqual(decision.chapter_override, chapter)
                self.assertEqual(decision.chapter_target, "next_image")

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

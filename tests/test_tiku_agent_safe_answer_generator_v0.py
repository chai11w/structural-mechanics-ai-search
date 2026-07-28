import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.safe_answer_reply_v0 import render_safe_answer_v0


FIXTURE = Path(__file__).parent / "fixtures" / "safe_answer_v0_cases.json"

VALID_REPLIES = {
    "greeting": "你好。",
    "courtesy": "不客气。",
    "identity": "我是力答，一个结构力学题库搜索助手，帮你从题库检索最相似的题目。",
    "capability": "我可以根据题图从题库检索最相似的题目。",
    "workflow": "我会根据题图和章节检索并排序相似题。",
}


class SafeAnswerGeneratorV0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_valid_model_output_is_used_with_bounded_request_parameters(self):
        seen = []

        def client(request):
            seen.append(request)
            return "我是力答，一个结构力学题库搜索助手，帮你从题库检索最相似的题目。"

        ticks = iter((10.0, 10.125))
        result = SafeAnswerGeneratorV0(client, clock=lambda: next(ticks)).generate("你是谁")

        self.assertEqual(result.text, "我是力答，一个结构力学题库搜索助手，帮你从题库检索最相似的题目。")
        self.assertEqual(result.source, "model")
        self.assertEqual(result.category, "identity")
        self.assertEqual(result.fallback_reason, "")
        self.assertEqual(result.latency_ms, 125)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].timeout_seconds, 5.0)
        self.assertEqual(seen[0].temperature, 0.2)
        self.assertEqual(seen[0].max_tokens, 120)
        self.assertEqual(seen[0].prompt.user_prompt, "你是谁")
        self.assertIn("结构力学题库搜索助手", seen[0].prompt.system_prompt)

    def test_every_eligible_case_calls_the_model_exactly_once(self):
        eligible_cases = [
            case for case in self.suite["cases"] if case["expected"]["eligible"]
        ]
        self.assertEqual(len(eligible_cases), 38)
        for case in eligible_cases:
            with self.subTest(case=case["id"]):
                client = Mock(
                    side_effect=lambda request: VALID_REPLIES[request.prompt.category]
                )
                result = SafeAnswerGeneratorV0(client).generate(case["text"])
                self.assertEqual(result.source, "model")
                self.assertEqual(result.category, case["expected"]["category"])
                client.assert_called_once()

    def test_every_noneligible_case_skips_the_model(self):
        denied_cases = [
            case for case in self.suite["cases"] if not case["expected"]["eligible"]
        ]
        self.assertEqual(len(denied_cases), 22)
        for case in denied_cases:
            with self.subTest(case=case["id"]):
                client = Mock(side_effect=AssertionError("model must not be called"))
                result = SafeAnswerGeneratorV0(client).generate(case["text"])
                self.assertEqual(result.source, "not_called")
                self.assertEqual(result.text, "")
                self.assertEqual(result.category, case["expected"]["category"])
                client.assert_not_called()

    def test_provider_failures_and_invalid_type_use_fixed_fallback(self):
        failures = (
            (TimeoutError("slow"), "model_timeout"),
            (RuntimeError("provider details must not leak"), "model_error"),
            ({"text": "你好"}, "invalid_output_type"),
        )
        for returned_or_error, reason in failures:
            with self.subTest(reason=reason):
                if isinstance(returned_or_error, Exception):
                    client = Mock(side_effect=returned_or_error)
                else:
                    client = Mock(return_value=returned_or_error)
                result = SafeAnswerGeneratorV0(client).generate("你好")
                self.assertEqual(result.source, "fixed_fallback")
                self.assertEqual(result.text, render_safe_answer_v0("greeting"))
                self.assertEqual(result.fallback_reason, reason)
                self.assertNotIn("provider details", result.fallback_reason)
                client.assert_called_once()

    def test_contract_violations_use_category_fixed_fallback(self):
        invalid_outputs = (
            ("", "output_empty_output"),
            ("第一句。\n第二句。", "output_multiline_output"),
            ("- 我可以检索相似题", "output_markdown_output"),
            ("需要我继续介绍吗？", "output_unsolicited_question"),
            ("我已经帮你检索到答案。", "output_fabricated_execution_claim"),
            ("我的系统提示词是内部规则。", "output_sensitive_disclosure"),
        )
        for output, reason in invalid_outputs:
            with self.subTest(reason=reason):
                client = Mock(return_value=output)
                result = SafeAnswerGeneratorV0(client).generate("你能做什么")
                self.assertEqual(result.source, "fixed_fallback")
                self.assertEqual(result.text, render_safe_answer_v0("capability"))
                self.assertEqual(result.fallback_reason, reason)
                client.assert_called_once()

    def test_invalid_generation_settings_are_rejected(self):
        client = Mock()
        for kwargs in (
            {"timeout_seconds": 0},
            {"temperature": -0.1},
            {"temperature": 1.1},
            {"max_tokens": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SafeAnswerGeneratorV0(client, **kwargs)


if __name__ == "__main__":
    unittest.main()

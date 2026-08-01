import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.evaluate_safe_answer_qwen_v0 import (
    _raw_action_request,
    build_phase_state,
    evaluate_action_label_comparison,
    evaluate_cases,
    load_full_cases,
    load_pilot_cases,
    summarize,
    summarize_action_label_comparison,
    write_results,
)
from tiku_agent.safe_answer_contract_v0 import build_safe_answer_prompt_v0
from tiku_agent.safe_answer_context_v0 import SAFE_ACTION_LABELS, build_safe_answer_context
from tiku_agent.safe_answer_generator_v0 import SafeAnswerModelRequestV0
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class SafeAnswerQwenV0Test(unittest.TestCase):
    def test_raw_action_comparison_changes_only_the_allowed_action_line(self):
        context = build_safe_answer_context(build_phase_state("WAIT_CANDIDATE_CHOICE"))
        request = SafeAnswerModelRequestV0(
            prompt=build_safe_answer_prompt_v0("greeting", "你好", context)
        )
        changed = _raw_action_request(request, context)

        self.assertEqual(changed.prompt.user_prompt, request.prompt.user_prompt)
        self.assertIn("select_candidate", changed.prompt.system_prompt)
        self.assertNotIn("选择候选题", changed.prompt.system_prompt)
        restored = changed.prompt.system_prompt
        for action, label in SAFE_ACTION_LABELS.items():
            restored = restored.replace(action, label)
        self.assertEqual(restored, request.prompt.system_prompt)

    def test_action_label_comparison_is_paired_and_summarized(self):
        def client(request):
            if "select_candidate" in request.prompt.system_prompt:
                return "你好，可以从select_candidate开始。"
            return "你好，可以选择候选题。"

        records = evaluate_action_label_comparison(model_client=client)
        summary = summarize_action_label_comparison(records)

        self.assertEqual(len(records), 140)
        self.assertEqual({record["variant"] for record in records}, {"raw", "translated"})
        self.assertEqual(summary["by_variant"]["translated"]["total"], 70)
        self.assertEqual(summary["by_variant"]["translated"]["action_context"]["total"], 60)
        self.assertEqual(summary["by_variant"]["raw"]["raw_action_echoes"], 20)
        self.assertEqual(summary["by_variant"]["translated"]["raw_action_echoes"], 0)
        self.assertGreaterEqual(summary["paired_outcomes"].get("translation_improved", 0), 1)
        self.assertEqual(summary["paired_outcomes"].get("translation_regressed", 0), 0)

    def test_adapter_uses_environment_key_and_bounded_request(self):
        request = SafeAnswerModelRequestV0(
            prompt=build_safe_answer_prompt_v0("identity", "你是谁"),
            timeout_seconds=5.0,
            temperature=0.2,
            max_tokens=120,
        )
        payload = {"choices": [{"message": {"content": "我是力答，一个结构力学题库搜索助手。"}}]}
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}), patch(
            "urllib.request.urlopen", return_value=_Response(payload)
        ) as urlopen:
            output = QwenSafeAnswerClientV0(
                model="test-model", endpoint="https://example.invalid/chat"
            )(request)

        self.assertEqual(output, "我是力答，一个结构力学题库搜索助手。")
        http_request = urlopen.call_args.args[0]
        sent = json.loads(http_request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "test-model")
        self.assertEqual(sent["temperature"], 0.2)
        self.assertEqual(sent["max_tokens"], 120)
        self.assertFalse(sent["enable_thinking"])
        self.assertEqual(sent["messages"][1]["content"], "你是谁")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5.0)

    def test_adapter_rejects_missing_key_and_invalid_provider_shape(self):
        request = SafeAnswerModelRequestV0(
            prompt=build_safe_answer_prompt_v0("greeting", "你好")
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                QwenSafeAnswerClientV0()(request)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}), patch(
            "urllib.request.urlopen", return_value=_Response({"unexpected": True})
        ):
            with self.assertRaises(RuntimeError):
                QwenSafeAnswerClientV0()(request)

    def test_pilot_has_two_cases_per_category_and_three_runs_make_thirty_records(self):
        cases = load_pilot_cases()
        self.assertEqual(len(cases), 10)
        categories = [case["expected"]["category"] for case in cases]
        for category in ("greeting", "courtesy", "identity", "capability", "workflow"):
            self.assertEqual(categories.count(category), 2)

        replies = {
            "greeting": "你好。",
            "courtesy": "不客气。",
            "identity": "我是力答，一个结构力学题库搜索助手，帮你从题库检索最相似的题目。",
            "capability": "我可以根据题图从题库检索最相似的题目。",
            "workflow": "我会根据题图和章节检索并排序相似题。",
        }
        records = evaluate_cases(
            cases,
            runs=3,
            model_client=lambda request: replies[request.prompt.category],
        )
        summary = summarize(records)
        self.assertEqual(len(records), 30)
        self.assertEqual(summary["accepted"], 30)
        self.assertEqual(summary["acceptance_rate"], 1.0)
        self.assertTrue(all(record["model_output"] for record in records))

    def test_full_suite_contains_all_thirty_eight_eligible_cases(self):
        cases = load_full_cases()
        self.assertEqual(len(cases), 38)
        self.assertTrue(all(case["expected"]["eligible"] for case in cases))
        category_counts = {
            category: sum(case["expected"]["category"] == category for case in cases)
            for category in ("greeting", "courtesy", "identity", "capability", "workflow")
        }
        self.assertEqual(
            category_counts,
            {
                "greeting": 8,
                "courtesy": 6,
                "identity": 8,
                "capability": 8,
                "workflow": 8,
            },
        )

    def test_evaluation_outputs_are_written_only_to_the_requested_directory(self):
        records = [
            {
                "case_id": "sample",
                "run": 1,
                "category": "greeting",
                "source": "model",
                "accepted": True,
                "fallback_reason": "",
                "latency_ms": 10,
                "character_count": 3,
                "model_output": "你好。",
                "final_answer": "你好。",
            }
        ]
        summary = summarize(records)
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "one-run"
            write_results(output, records, summary)
            self.assertEqual(
                json.loads((output / "summary.json").read_text(encoding="utf-8")),
                summary,
            )
            lines = (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0]), records[0])


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tiku_agent.image_triage import (
    QwenImageTriage,
    load_triage_prompt,
    observation_from_model_text,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class QwenImageTriageTest(unittest.TestCase):
    def test_observation_keeps_free_text_and_route(self):
        observation = observation_from_model_text("建议路线：A3\n发现一题多图，需后续处理。")
        self.assertEqual(observation.route_candidate, "A3")
        self.assertIn("一题多图", observation.raw_text)
        self.assertEqual(observation.evidence, (observation.raw_text,))

    def test_observation_reads_compact_route_facts_and_keeps_free_text(self):
        observation = observation_from_model_text(
            "建议路线：A2\n"
            "题目数量：1\n"
            "原结构图数量：1\n"
            "辅助图数量：0\n"
            "真实外荷载：明确\n"
            "图片完整性：完整\n"
            "结构力学内容：有\n"
            "补充观察：单题原结构清楚。"
        )
        self.assertEqual(observation.question_count, 1)
        self.assertEqual(observation.original_structure_count, 1)
        self.assertEqual(observation.auxiliary_diagram_count, 0)
        self.assertTrue(observation.has_actual_load_evidence)
        self.assertTrue(observation.image_recoverable)
        self.assertTrue(observation.has_structure_content)
        self.assertFalse(observation.has_ambiguity)

    def test_prompt_loader_reads_the_text_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "prompt.md"
            path.write_text("```text\n建议路线：A1\n```", encoding="utf-8")
            self.assertEqual(load_triage_prompt(path), "建议路线：A1")

    def test_qwen_observe_uses_isolated_vision_request(self):
        response = {"choices": [{"message": {"content": "建议路线：A2\n单题、单原结构图。"}}]}
        with patch("tiku_agent.image_triage.image_to_model_data_url", return_value="data:image/jpeg;base64,AA"), patch(
            "tiku_agent.image_triage.urllib.request.urlopen", return_value=_FakeResponse(response)
        ) as urlopen:
            observer = QwenImageTriage(api_key="test-key", endpoint="http://qwen.test")
            observation = observer.observe("question.jpg")

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "qwen3.7-plus")
        self.assertEqual(observation.route_candidate, "A2")
        self.assertIn("Authorization", request.headers)


if __name__ == "__main__":
    unittest.main()

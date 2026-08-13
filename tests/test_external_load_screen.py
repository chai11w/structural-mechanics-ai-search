from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import urllib.request
from uuid import uuid4

from tiku_agent.external_load_screen import ZhipuExternalLoadScreen
from tiku_shared.model_costs import ModelCostCollector, model_cost_scope


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ExternalLoadScreenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / f".tmp_external_load_screen_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.image = self.root / "question.jpg"
        self.image.parent.mkdir(parents=True, exist_ok=True)
        self.image.write_bytes(b"image bytes")

    def test_normalizes_yes_and_records_model_cost(self):
        collector = ModelCostCollector(run_id="screen-test")
        response = FakeResponse(
            {
                "id": "request-1",
                "choices": [{"message": {"content": "yes"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            }
        )
        screen = ZhipuExternalLoadScreen(timeout_seconds=15)

        with patch.dict("os.environ", {"ZHIPUAI_API_KEY": "test-key"}), patch.object(
            urllib.request, "urlopen", return_value=response
        ), model_cost_scope(collector):
            verdict = screen(self.image)

        self.assertEqual(verdict, "yes")
        record = collector.records()[0]
        self.assertEqual(record.provider, "zhipu")
        self.assertEqual(record.call_type, "external_load_screen")
        self.assertEqual(record.total_tokens, 11)

    def test_rejects_non_binary_output(self):
        response = FakeResponse(
            {"choices": [{"message": {"content": "无法判断"}}], "usage": {}}
        )
        screen = ZhipuExternalLoadScreen()

        with patch.dict("os.environ", {"ZHIPUAI_API_KEY": "test-key"}), patch.object(
            urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                screen(self.image)


if __name__ == "__main__":
    unittest.main()

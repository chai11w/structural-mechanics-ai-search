from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.map_a3_regions import DEFAULT_RUNTIME_DIR, build_argument_parser
from tiku_agent.image_region_mapper import (
    A3_REGION_MAP_SCHEMA_VERSION,
    A3RegionMapRuntime,
    A3RegionModelResponse,
    QwenA3RegionObserver,
    assess_a3_region_map,
    build_a3_region_map_prompt,
    parse_a3_region_map,
)


def region_payload(*, labels=("(a)",), relationship="shared_subquestions"):
    return {
        "schema_version": A3_REGION_MAP_SCHEMA_VERSION,
        "groups": [
            {
                "group_id": "g1",
                "parent_question_label": "5-2",
                "relationship": relationship,
                "visible_stem_text": "计算下列结构",
            }
        ],
        "regions": [
            {
                "region_id": "r1",
                "group_id": "g1",
                "visible_labels": list(labels),
                "bbox": [5, 10, 45, 40],
                "content_type": "diagram",
                "notes": "左上结构图",
            }
        ],
        "unknowns": [],
    }


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class StaticObserver:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.calls = 0

    def observe(self, _image_path: Path) -> A3RegionModelResponse:
        self.calls += 1
        return A3RegionModelResponse(
            raw_text=self.raw_text,
            model="fake-region-model",
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
        )


class A3RegionMapperTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="a3_region_mapper_"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))
        self.image_path = self.temp_dir / "page.jpg"
        Image.new("RGB", (1000, 800), "white").save(self.image_path)

    def test_prompt_is_limited_to_first_step_and_separates_adjacent_subfigures(self):
        prompt = build_a3_region_map_prompt()

        self.assertIn("(c) 和 (d) 必须输出两个 region", prompt)
        self.assertIn("同页不等于同题", prompt)
        self.assertIn("后续 OpenCV", prompt)
        self.assertIn("禁止输出章节、荷载、结构类型、图角色", prompt)
        self.assertNotIn('"chapter_hint"', prompt)

    def test_independent_big_questions_do_not_require_subquestion_labels(self):
        payload = region_payload(labels=(), relationship="independent_question")
        payload["groups"][0]["parent_question_label"] = "四"

        observation = parse_a3_region_map(payload)

        self.assertEqual(observation.groups[0].parent_question_label, "四")
        self.assertEqual(observation.regions[0].visible_labels, ())
        self.assertEqual(assess_a3_region_map(observation), ())

    def test_one_box_with_c_and_d_is_blocked(self):
        observation = parse_a3_region_map(region_payload(labels=("(c)", "(d)")))

        reasons = assess_a3_region_map(observation)

        self.assertIn("region_contains_multiple_labels", reasons)

    def test_overlapping_diagram_regions_are_blocked(self):
        payload = region_payload(labels=("(c)",))
        payload["regions"].append(
            {
                "region_id": "r2",
                "group_id": "g1",
                "visible_labels": ["(d)"],
                "bbox": [10, 12, 46, 42],
                "content_type": "diagram",
            }
        )
        observation = parse_a3_region_map(payload)

        self.assertIn(
            "overlapping_diagram_regions", assess_a3_region_map(observation)
        )

    def test_runtime_saves_raw_normalized_overlay_and_redacted_metrics(self):
        raw_text = json.dumps(region_payload(), ensure_ascii=False)
        observer = StaticObserver(raw_text)
        runtime = A3RegionMapRuntime(self.temp_dir / "runtime", observer=observer)

        result = runtime.map_page(self.image_path)

        self.assertEqual(result.status, "ready")
        self.assertEqual(observer.calls, 1)
        self.assertTrue(Path(result.raw_response_path).is_file())
        self.assertTrue(Path(result.normalized_json_path).is_file())
        self.assertTrue(Path(result.overlay_path).is_file())
        self.assertEqual(Path(result.raw_response_path).read_text(encoding="utf-8"), raw_text)
        log_text = runtime.logger.path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.image_path), log_text)
        self.assertNotIn("计算下列结构", log_text)
        log = json.loads(log_text)
        self.assertEqual(log["status"], "ready")
        self.assertEqual(log["region_count"], 1)
        self.assertEqual(log["total_tokens"], 100)

    def test_invalid_schema_keeps_raw_response_for_diagnosis(self):
        observer = StaticObserver('{"groups": [], "regions": []}')
        runtime = A3RegionMapRuntime(self.temp_dir / "runtime", observer=observer)

        result = runtime.map_page(self.image_path)

        self.assertEqual(result.status, "uncertain")
        self.assertEqual(result.reason_codes, ("invalid_region_map_schema",))
        self.assertTrue(Path(result.raw_response_path).is_file())
        self.assertEqual(result.model, "fake-region-model")
        self.assertEqual(result.total_tokens, 100)

    def test_saved_observation_skips_model_and_still_renders_overlay(self):
        observer = StaticObserver("must not be called")
        runtime = A3RegionMapRuntime(self.temp_dir / "runtime", observer=observer)
        observation = parse_a3_region_map(region_payload())

        result = runtime.map_page(self.image_path, observation=observation)

        self.assertEqual(result.status, "ready")
        self.assertEqual(observer.calls, 0)
        self.assertEqual(result.raw_response_path, "")
        self.assertTrue(Path(result.overlay_path).is_file())

    def test_qwen_observer_sends_only_the_full_page(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(region_payload(), ensure_ascii=False)
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 90,
                "completion_tokens": 30,
                "total_tokens": 120,
            },
        }
        with patch(
            "tiku_agent.image_region_mapper.image_to_model_data_url",
            return_value="data:image/jpeg;base64,AA",
        ) as encode, patch(
            "tiku_agent.image_region_mapper.urllib.request.urlopen",
            return_value=FakeResponse(response),
        ) as urlopen:
            observer = QwenA3RegionObserver(
                api_key="test-key", endpoint="http://qwen.test"
            )
            result = observer.observe(self.image_path)

        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        images = [
            item
            for item in body["messages"][1]["content"]
            if item.get("type") == "image_url"
        ]
        self.assertEqual(len(images), 1)
        encode.assert_called_once_with(self.image_path, normalize_orientation=True)
        self.assertEqual(result.total_tokens, 120)

    def test_cli_defaults_are_isolated_from_existing_a3_runtime(self):
        parser = build_argument_parser()
        args = parser.parse_args(["--image", "question.jpg"])

        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_region_map_8892")
        self.assertIsNone(args.observation_json)


if __name__ == "__main__":
    unittest.main()

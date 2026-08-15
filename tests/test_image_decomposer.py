import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.run_tiku_agent_8892 import (
    DEFAULT_RUNTIME_DIR,
    SERVICE_ID,
    build_argument_parser,
)
from scripts.classify_question_bank import (
    CHAPTER_RECOGNITION_RULES,
    SYSTEM_PROMPT as CLASSIFIER_SYSTEM_PROMPT,
    build_chapter_recognition_prompt,
)
from tiku_agent.image_contracts import (
    A3_DECOMPOSITION_SCHEMA_VERSION,
    A3DecompositionResult,
    ChapterHint,
)
from tiku_agent.image_decomposer import (
    A3ChapterObserverResult,
    A3DecompositionRuntime,
    A3ObserverResult,
    CandidateBlock,
    CandidateSet,
    QwenA3ChapterObserver,
    QwenA3Observer,
    build_a3_observation_prompt,
    build_decomposition_result,
    detect_candidate_blocks,
    parse_a3_chapter_hint,
    parse_a3_observation,
    recognize_group_chapters,
)
from tiku_shared.model_costs import (
    ModelCostCollector,
    model_cost_scope,
    record_model_call,
)
import tiku_agent.image_decomposer as image_decomposer


def observation_payload(*, diagrams, labels=("(a)",), stem="试用力法计算下列结构"):
    return {
        "schema_version": A3_DECOMPOSITION_SCHEMA_VERSION,
        "groups": [
            {
                "group_id": "g1",
                "parent_question_label": "5-2",
                "member_labels": list(labels),
                "shared_stem_text": stem,
            }
        ],
        "diagrams": diagrams,
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


class RecordingChapterObserver:
    def __init__(self, *, barrier_parties: int = 1) -> None:
        self.calls: list[str] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(barrier_parties)

    def recognize(self, _image_path: Path, group) -> A3ChapterObserverResult:
        with self._lock:
            self.calls.append(group.group_id)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self._barrier.wait(timeout=2)
        value = "5位移法" if "位移法" in group.shared_stem_text else "4力法"
        result = A3ChapterObserverResult(
            hint=ChapterHint(
                value=value,
                scope="question_group",
                source_text=group.shared_stem_text,
                evidence=f"“{group.shared_stem_text}”",
                confidence=0.95,
            ),
            model="fake-chapter",
            total_tokens=12,
        )
        with self._lock:
            self._active -= 1
        return result


class ImageDecomposerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="a3_decomposer_"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def make_candidate(self, block_id: int, *, width: int = 240, height: int = 120):
        path = self.temp_dir / f"candidate_{block_id}.jpg"
        Image.new("RGB", (width, height), "white").save(path)
        return CandidateBlock(
            block_id=block_id,
            bbox=(10, block_id * 150, 10 + width, block_id * 150 + height),
            crop_path=path,
        )

    def test_chapter_guard_is_reused_by_separate_chapter_parser(self):
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ],
                stem="",
            )
        )

        hint = parse_a3_chapter_hint(
            {
                "chapter_hint": "4力法",
                "chapter_confidence": 0.95,
                "chapter_evidence": "",
            },
            observation.groups[0],
        )

        self.assertEqual(hint.value, "unknown")
        self.assertLessEqual(hint.confidence, 0.5)
        self.assertFalse(hint.available)

    def test_decomposition_prompt_does_not_classify_chapters(self):
        prompt = build_a3_observation_prompt(2)

        self.assertNotIn('"chapter_hint"', prompt)
        self.assertIn("不输出 chapter_hint", prompt)
        self.assertIn(CHAPTER_RECOGNITION_RULES, build_chapter_recognition_prompt())
        self.assertIn(CHAPTER_RECOGNITION_RULES, CLASSIFIER_SYSTEM_PROMPT)

    def test_model_chapter_fields_are_not_trusted_by_decomposition_parser(self):
        payload = observation_payload(
            diagrams=[
                {
                    "block_id": 1,
                    "role": "original_structure",
                    "group_id": "g1",
                    "question_label": "(a)",
                }
            ],
        )
        payload["groups"][0].update(
            {
                "chapter_hint": "8影响线",
                "chapter_confidence": 1.0,
                "chapter_evidence": "伪造证据",
            }
        )

        parsed = parse_a3_observation(payload)

        hint = parsed.groups[0].shared_chapter_hint
        self.assertEqual(hint.value, "unknown")

    def test_single_original_with_auxiliary_becomes_one_unit(self):
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    },
                    {
                        "block_id": 2,
                        "role": "auxiliary_unit_load",
                        "group_id": "g1",
                        "question_label": "(a)",
                    },
                ]
            )
        )

        result = build_decomposition_result(
            observation,
            (self.make_candidate(1), self.make_candidate(2)),
            self.temp_dir / "units",
        )

        self.assertEqual(result.status, "single_ready")
        self.assertEqual(len(result.search_units), 1)
        self.assertEqual(result.search_units[0].source_block_id, 1)
        self.assertEqual(result.search_units[0].chapter_hint.value, "unknown")
        self.assertTrue(Path(result.search_units[0].primary_diagram_path).is_file())

    def test_multiple_originals_wait_for_user_choice(self):
        observation = parse_a3_observation(
            observation_payload(
                labels=("(a)", "(b)"),
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    },
                    {
                        "block_id": 2,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(b)",
                    },
                ],
            )
        )

        result = build_decomposition_result(
            observation,
            (self.make_candidate(1), self.make_candidate(2)),
            self.temp_dir / "units",
        )

        self.assertEqual(result.status, "multiple_wait_choice")
        self.assertEqual([unit.question_label for unit in result.search_units], ["(a)", "(b)"])
        self.assertEqual(result.selected_unit_id, "")

    def test_shared_stem_group_runs_chapter_recognition_once(self):
        observation = parse_a3_observation(
            observation_payload(
                labels=("(a)", "(b)"),
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    },
                    {
                        "block_id": 2,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(b)",
                    },
                ],
            )
        )
        observer = RecordingChapterObserver()

        enriched, batch = recognize_group_chapters(
            self.temp_dir / "page.jpg", observation, observer
        )

        self.assertEqual(observer.calls, ["g1"])
        self.assertEqual(batch.attempted_group_count, 1)
        self.assertEqual(enriched.groups[0].shared_chapter_hint.value, "4力法")

    def test_independent_groups_recognize_chapters_with_bounded_concurrency(self):
        payload = observation_payload(
            diagrams=[
                {
                    "block_id": 1,
                    "role": "original_structure",
                    "group_id": "g1",
                    "question_label": "(a)",
                },
                {
                    "block_id": 2,
                    "role": "original_structure",
                    "group_id": "g2",
                    "question_label": "(b)",
                },
            ]
        )
        payload["groups"].append(
            {
                "group_id": "g2",
                "parent_question_label": "6-1",
                "member_labels": ["(b)"],
                "shared_stem_text": "试用位移法计算下列结构",
            }
        )
        observation = parse_a3_observation(payload)
        observer = RecordingChapterObserver(barrier_parties=2)

        enriched, batch = recognize_group_chapters(
            self.temp_dir / "page.jpg", observation, observer, max_workers=2
        )

        self.assertEqual(observer.max_active, 2)
        self.assertEqual(batch.attempted_group_count, 2)
        self.assertEqual(batch.succeeded_group_count, 2)
        self.assertEqual(
            [group.shared_chapter_hint.value for group in enriched.groups],
            ["4力法", "5位移法"],
        )

    def test_missing_visible_stem_skips_chapter_model(self):
        observation = parse_a3_observation(
            observation_payload(
                stem="",
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ],
            )
        )
        observer = RecordingChapterObserver()

        enriched, batch = recognize_group_chapters(
            self.temp_dir / "page.jpg", observation, observer
        )

        self.assertEqual(observer.calls, [])
        self.assertEqual(batch.attempted_group_count, 0)
        self.assertEqual(enriched.groups[0].shared_chapter_hint.value, "unknown")

    def test_parallel_chapter_calls_keep_model_cost_context(self):
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ]
            )
        )
        observer = RecordingChapterObserver()
        original_recognize = observer.recognize

        def recognize_with_cost(image_path, group):
            record_model_call(
                provider="dashscope",
                model="fake-chapter",
                call_type="qwen_a3_chapter_recognition",
                status="success",
                started_at="2026-08-15T00:00:00+00:00",
                finished_at="2026-08-15T00:00:01+00:00",
                latency_ms=1000,
                usage={"total_tokens": 12},
            )
            return original_recognize(image_path, group)

        observer.recognize = recognize_with_cost
        collector = ModelCostCollector(run_id="a3-test")

        with model_cost_scope(collector):
            recognize_group_chapters(
                self.temp_dir / "page.jpg", observation, observer
            )

        self.assertEqual(len(collector.records()), 1)
        self.assertEqual(
            collector.records()[0].call_type, "qwen_a3_chapter_recognition"
        )

    def test_unknown_role_is_uncertain_and_cannot_auto_continue(self):
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    },
                    {"block_id": 2, "role": "unknown"},
                ]
            )
        )

        result = build_decomposition_result(
            observation,
            (self.make_candidate(1), self.make_candidate(2)),
            self.temp_dir / "units",
        )

        self.assertEqual(result.status, "uncertain")
        self.assertIn("unknown_diagram_role", result.reason_codes)
        self.assertTrue(all(unit.requires_user_confirmation for unit in result.search_units))

    def test_no_original_structure_returns_no_unit(self):
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "internal_force_diagram",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ]
            )
        )

        result = build_decomposition_result(
            observation,
            (self.make_candidate(1),),
            self.temp_dir / "units",
        )

        self.assertEqual(result.status, "no_unit")
        self.assertEqual(result.search_units, ())

    def test_result_states_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            A3DecompositionResult(status="single_ready")
        with self.assertRaises(ValueError):
            A3DecompositionResult(status="multiple_wait_choice")

    def test_runtime_uses_isolated_paths_and_redacted_log(self):
        source = self.temp_dir / "private_question.jpg"
        Image.new("RGB", (800, 600), "white").save(source)
        candidate = self.make_candidate(1)
        contact = self.temp_dir / "contact.jpg"
        Image.new("RGB", (400, 300), "white").save(contact)

        def detector(_source: Path, _output: Path) -> CandidateSet:
            return CandidateSet((candidate,), contact)

        runtime_root = self.temp_dir / "runtime_8892"
        chapter_observer = RecordingChapterObserver()
        runtime = A3DecompositionRuntime(
            runtime_root,
            detector=detector,
            chapter_observer=chapter_observer,
        )
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ]
            )
        )

        result = runtime.decompose(source, observation=observation)

        self.assertEqual(result.status, "single_ready")
        self.assertEqual(result.search_units[0].chapter_hint.value, "4力法")
        self.assertTrue(runtime.incoming_dir.is_dir())
        self.assertTrue(runtime.sessions_dir.is_dir())
        self.assertTrue(Path(result.search_units[0].primary_diagram_path).is_relative_to(runtime_root))
        log_text = runtime.logger.path.read_text(encoding="utf-8")
        self.assertNotIn(str(source), log_text)
        self.assertNotIn("试用力法", log_text)
        self.assertNotIn("primary_diagram_path", log_text)
        log_record = json.loads(log_text)
        self.assertEqual(log_record["chapter_group_count"], 1)
        self.assertEqual(log_record["chapter_success_count"], 1)

    def test_qwen_observer_sends_full_image_and_contact_sheet(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            observation_payload(
                                diagrams=[
                                    {
                                        "block_id": 1,
                                        "role": "original_structure",
                                        "group_id": "g1",
                                        "question_label": "(a)",
                                    }
                                ]
                            ),
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        candidate = self.make_candidate(1)
        with patch(
            "tiku_agent.image_decomposer.image_to_model_data_url",
            return_value="data:image/jpeg;base64,AA",
        ), patch(
            "tiku_agent.image_decomposer.urllib.request.urlopen",
            return_value=FakeResponse(response),
        ) as urlopen:
            observer = QwenA3Observer(api_key="test-key", endpoint="http://qwen.test")
            result = observer.observe(candidate.crop_path, (candidate,), candidate.crop_path)

        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        images = [
            item
            for item in body["messages"][1]["content"]
            if item.get("type") == "image_url"
        ]
        self.assertEqual(len(images), 2)
        self.assertEqual(result.observation.diagrams[0].role, "original_structure")
        self.assertEqual(result.total_tokens, 120)

    def test_qwen_chapter_observer_uses_shared_prompt_and_group_scope(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "chapter_hint": "4力法",
                                "chapter_confidence": 0.96,
                                "chapter_evidence": "“试用力法计算下列结构”",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92},
        }
        observation = parse_a3_observation(
            observation_payload(
                diagrams=[
                    {
                        "block_id": 1,
                        "role": "original_structure",
                        "group_id": "g1",
                        "question_label": "(a)",
                    }
                ]
            )
        )
        image_path = self.temp_dir / "page.jpg"
        Image.new("RGB", (400, 300), "white").save(image_path)
        with patch(
            "tiku_agent.image_decomposer.image_to_model_data_url",
            return_value="data:image/jpeg;base64,AA",
        ), patch(
            "tiku_agent.image_decomposer.urllib.request.urlopen",
            return_value=FakeResponse(response),
        ) as urlopen:
            observer = QwenA3ChapterObserver(
                api_key="test-key", endpoint="http://qwen.test"
            )
            result = observer.recognize(image_path, observation.groups[0])

        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(
            body["messages"][0]["content"], build_chapter_recognition_prompt()
        )
        user_text = body["messages"][1]["content"][1]["text"]
        self.assertIn('"parent_question_label": "5-2"', user_text)
        self.assertIn("试用力法计算下列结构", user_text)
        self.assertEqual(result.hint.value, "4力法")
        self.assertEqual(result.total_tokens, 92)

    @unittest.skipIf(
        image_decomposer.cv2 is None or image_decomposer.np is None,
        "OpenCV is unavailable in this Python runtime",
    )
    def test_tracked_shared_stem_fixture_produces_cv_candidates(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "experiments"
            / "complex_image_eval"
            / "images"
            / "cebc2d0707f2a7dd9cb6f01afbfd4bfd.jpg"
        )

        candidates = detect_candidate_blocks(source, self.temp_dir / "detected")

        self.assertGreaterEqual(len(candidates.blocks), 1)
        self.assertTrue(candidates.contact_sheet_path.is_file())
        self.assertTrue(
            all(block.crop_path.is_file() for block in candidates.blocks)
        )

    def test_8892_launcher_defaults_are_isolated(self):
        parser = build_argument_parser()
        args = parser.parse_args(["--image", "question.jpg"])

        self.assertEqual(SERVICE_ID, 8892)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_8892")
        self.assertEqual(args.runtime_dir, DEFAULT_RUNTIME_DIR)
        self.assertIsNone(args.observation_json)
        self.assertEqual(args.max_chapter_workers, 2)


if __name__ == "__main__":
    unittest.main()

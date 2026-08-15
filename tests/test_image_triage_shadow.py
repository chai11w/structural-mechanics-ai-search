import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from uuid import uuid4

from tiku_agent.image_contracts import ImageTriageObservation
from tiku_agent.image_triage_shadow import (
    ImageTriageShadowRunner,
    ImageTriageShadowRuntime,
)


class _A2Observer:
    def observe(self, _image_path):
        return ImageTriageObservation(
            route_candidate="A2",
            question_count=1,
            original_structure_count=1,
            auxiliary_diagram_count=0,
            has_actual_load_evidence=True,
            has_structure_content=True,
            image_recoverable=True,
            has_ambiguity=False,
            evidence=("单题、单原结构图、真实外荷载清楚",),
            raw_text="建议路线：A2\n单题、单原结构图、真实外荷载清楚",
        )


class _ParseFailureObserver:
    def observe(self, _image_path):
        raise ValueError("raw model output should not be logged")


class _DelegateRuntime:
    def __init__(self):
        self.calls = []

    def handle_image(self, session_id, image_path, **kwargs):
        self.calls.append((session_id, Path(image_path).name, kwargs))
        return SimpleNamespace(text="原检索结果", intent="search")


class _BrokenShadow:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("shadow failed")


class ImageTriageShadowTest(unittest.TestCase):
    def make_root(self, label):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_triage_shadow_{label}_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        root.mkdir(parents=True)
        return root

    def test_success_writes_route_summary_observation_and_no_path(self):
        root = self.make_root("success")
        image = root / "source.jpg"
        image.write_bytes(b"test-image")
        runner = ImageTriageShadowRunner(_A2Observer(), runtime_dir=root)
        self.addCleanup(runner.close)

        self.assertTrue(runner.submit(image, request_id="req-shadow-ok"))
        self.assertTrue(runner.wait_for_idle(2.0))

        record = json.loads((root / "triage_shadow.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["request_id"], "req-shadow-ok")
        self.assertEqual(record["route_candidate"], "A2")
        self.assertEqual(record["final_route"], "A2")
        self.assertEqual(record["question_count"], 1)
        self.assertIn("单原结构图", record["observation"])
        self.assertNotIn(str(root), json.dumps(record, ensure_ascii=False))
        self.assertEqual(list((root / "triage_shadow_inputs").iterdir()), [])

    def test_parse_failure_is_recorded_without_raw_exception(self):
        root = self.make_root("parse")
        image = root / "source.jpg"
        image.write_bytes(b"test-image")
        runner = ImageTriageShadowRunner(_ParseFailureObserver(), runtime_dir=root)
        self.addCleanup(runner.close)

        runner.submit(image, request_id="req-shadow-parse")
        self.assertTrue(runner.wait_for_idle(2.0))

        record = json.loads((root / "triage_shadow.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error_kind"], "parse_error")
        self.assertNotIn("raw model output", json.dumps(record, ensure_ascii=False))

    def test_shadow_failure_never_changes_existing_runtime_result(self):
        delegate = _DelegateRuntime()
        runtime = ImageTriageShadowRuntime(delegate, _BrokenShadow())

        response = runtime.handle_image(
            "session-1",
            "question.jpg",
            identity_key="identity-1",
            request_id="req-1",
        )

        self.assertEqual(response.text, "原检索结果")
        self.assertEqual(len(delegate.calls), 1)
        self.assertEqual(delegate.calls[0][2]["request_id"], "req-1")


if __name__ == "__main__":
    unittest.main()

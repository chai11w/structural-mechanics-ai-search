"""Isolated A3 acceptance with real local images and deterministic model substitutes.

Run: python -B -m tests.checkpoint_acceptance_harness --image ... --image ...
     --answer ... --answer ... --answer ... --rounds 25 --output <outside checkout>
No live asset writes, ports, network models or service configuration changes.
"""

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import shutil
from threading import Lock
from time import perf_counter
import unittest
from unittest.mock import patch

from PIL import Image
import search
from tests import test_checkpoint_async as fixtures
from tests import test_a3_runtime as models
from tiku_agent.a3_auto_crop import A3AutoCropPage, A3AutoCropTarget
from tiku_agent.a3_page_parser import parse_a3_page_understanding
from tiku_agent.task_state_runtime import TaskStateEntryCapabilities


def percentiles(values):
    values = sorted(values)
    return {key: round(values[max(0, math.ceil(len(values) * fraction) - 1)], 3) if values else 0.0
            for key, fraction in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99), ("max_ms", 1))}


class Observer:
    def __init__(self, units):
        self.units = units

    def observe(self, _image):
        page = models._single_page_payload()
        seed, diagram = deepcopy(page["groups"][0]["units"][0]), deepcopy(page["diagrams"][0])
        page["groups"][0]["units"], page["diagrams"] = [], []
        for index in range(1, self.units + 1):
            unit_id, diagram_id = f"g1-u{index}", f"d{index}"
            page["groups"][0]["units"].append({**deepcopy(seed), "unit_id": unit_id,
                "question_label": str(index), "diagram_ids": [diagram_id]})
            page["diagrams"].append({**deepcopy(diagram), "diagram_id": diagram_id, "unit_ids": [unit_id]})
        return parse_a3_page_understanding(page)


class Cropper:
    def ground(self, _image, units, _understanding):
        return A3AutoCropPage(page_status="ready", targets=tuple(A3AutoCropTarget(
            target_id=f"c{index:03d}", unit_id=unit["unit_id"], question_label=str(index),
            bbox=(80 + index, 100, 920 - index, 700), status="auto_ready", reason_codes=(),
            binding_evidence="isolated performance fixture") for index, unit in enumerate(units, 1)), unknowns=())


class Harness(unittest.TestCase):
    setUp = fixtures.AsyncA3Test.setUp
    trace = fixtures.AsyncA3Test.trace
    records = fixtures.AsyncA3Test.records

    @contextmanager
    def business(self):
        scan = search.ChapterCandidateScan(scored=[(1.0, "q1.png"), (1.0, "q2.png"), (1.0, "q3.png")],
            structure_filter_applied=False, dimensions_by_name={}, chapter_scanned=7)
        def rerank(_image, candidates, **kwargs):
            return [{**item, "rerank_status": "completed", "rerank_score": .98 - index * .02,
                     "final_score": .97 - index * .02} for index, item in enumerate(candidates)]
        with fixtures.AsyncA3Test.business(self), patch("tiku_agent.tools.search.scan_chapter_candidates", return_value=scan), patch(
            "tiku_agent.tools.search.resolve_question_path",
            side_effect=lambda name, **kwargs: (self.questions[Path(name).name], Path(name).name, False)
        ), patch("tiku_agent.tools.search.rerank_candidates", side_effect=rerank):
            yield

    def configure(self, *, units=1, image=None, answers=(), enabled=True):
        self.units = units
        self.recorder.gate.enabled = enabled
        self.runtime.page_observer, self.runtime.auto_cropper = Observer(units), Cropper()
        self.policy = replace(self.policy, max_checkpoint_rows=10_000, max_artifact_rows=10_000,
            max_audit_rows=100_000, max_trace_rows=100_000, max_artifact_bytes=256 * 1024 * 1024)
        self.store.capacity = self.policy
        if image is not None:
            self.source = self.root / ("source" + Path(image).suffix)
            shutil.copyfile(image, self.source)
        self.questions = {}
        for index in range(1, 4):
            question = self.root / f"question-{index}{self.source.suffix}"
            shutil.copyfile(self.source, question)
            self.questions[f"q{index}.png"] = question
        self.answers = []
        for index, source in enumerate(answers or (self.source,) * 3):
            target = self.root / f"answer-{index}{Path(source).suffix}"
            shutil.copyfile(source, target)
            self.answers.append(target)
        self.stage_ms, self.request_capture_ms, self.request_wall_ms = [], [], []
        self.peak_pending, self.peak_bytes = 0, 0
        self.worker_ids = set()
        self._profile_lock = Lock()
        enqueue = self.recorder._enqueue
        def measured(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            elapsed = (perf_counter() - kwargs["started"]) * 1000
            health = self.recorder.health()
            with self._profile_lock:
                self.stage_ms.append(elapsed)
                self.peak_pending = max(self.peak_pending, health["pending"])
                self.peak_bytes = max(self.peak_bytes, health["pending_bytes"])
                self.worker_ids.add(self.recorder._worker.ident)
            return result
        self.recorder._enqueue = measured

    def call(self, fn):
        start, offset = perf_counter(), len(self.stage_ms)
        response = fn()
        self.request_wall_ms.append((perf_counter() - start) * 1000)
        self.request_capture_ms.append(sum(self.stage_ms[offset:]))
        return response

    def operations(self):
        caps = TaskStateEntryCapabilities(trusted_image_event=True, reset_session_available=True)
        kwargs = {"identity_key": "invite_test", "task_state_capabilities": caps}
        with self.business():
            response = self.call(lambda: self.runtime.handle_image("session-test", self.source, **kwargs))
        if self.units > 1:
            with self.business():
                response = self.call(lambda: self.runtime.select_unit("session-test", "g1-u1", **kwargs))
        self.assertEqual(response.media_kind, "candidates")
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=self.answers):
            response = self.call(lambda: self.runtime.handle_text("session-test", "1", **kwargs))
        self.assertEqual(response.media_kind, "answer")
        self.assertEqual(len(response.images), len(self.answers))
        if self.units > 1:
            with self.business():
                response = self.call(lambda: self.runtime.select_unit("session-test", f"g1-u{self.units}", **kwargs))
            self.assertEqual(response.media_kind, "candidates")
            with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=self.answers):
                self.call(lambda: self.runtime.handle_text("session-test", "1", **kwargs))


def run_scenario(image, answers, units, rounds, *, enabled, paced=True):
    fixture = Harness()
    fixture.setUp()
    try:
        fixture.configure(units=units, image=image, answers=answers, enabled=enabled)
        for _ in range(rounds):
            fixture.operations()
            if paced:
                fixture.assertTrue(fixture.recorder.flush(30))
        fixture.assertTrue(fixture.recorder.flush(60))
        fixture.assertTrue(fixture.traces.flush(10))
        counts = fixture.recorder.health()["counters"]
        attempted = counts["queued"] + counts["rejected"]
        lost = attempted - counts["stored"]
        return {"capture": enabled, "paced": paced, "units": units, "rounds": rounds,
            "requests": len(fixture.request_wall_ms), "submission_samples": len(fixture.stage_ms),
            "submission": percentiles(fixture.stage_ms), "request_capture": percentiles(fixture.request_capture_ms),
            "request_wall": percentiles(fixture.request_wall_ms), "peak_pending": fixture.peak_pending,
            "peak_pending_bytes": fixture.peak_bytes, "consumer_threads": len(fixture.worker_ids),
            "counts": counts, "loss_rate": round(lost / attempted, 6) if attempted else 0,
            "trace": fixture.traces.health(),
            "submission_target_met": percentiles(fixture.stage_ms)["p99_ms"] <= 10,
            "request_target_met": percentiles(fixture.request_capture_ms)["p99_ms"] <= 100}
    finally:
        fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", action="append", required=True, type=Path)
    parser.add_argument("--answer", action="append", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 1 <= len(args.answer) <= 10:
        parser.error("rounds must be 1..100 and answers 1..10")
    report = {"python": platform.python_version(), "platform": platform.system(),
              "method": "real local images; deterministic models; temporary bank; paced drain after each workflow",
              "images": [], "scenarios": []}
    for path in args.image:
        with Image.open(path) as image:
            report["images"].append({"sha256": sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size, "width": image.width, "height": image.height})
    for image_index, path in enumerate(args.image):
        for units in (1, 3, 10):
            for enabled in (False, True):
                result = run_scenario(path, args.answer, units, args.rounds, enabled=enabled)
                report["scenarios"].append({"image_index": image_index, **result})
                print(json.dumps({"image_index": image_index, **{key: result[key] for key in
                    ("units", "capture", "request_capture", "submission", "loss_rate", "peak_pending")}}), flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    burst = run_scenario(args.image[-1], args.answer, 10, args.rounds, enabled=True, paced=False)
    report["scenarios"].append({"image_index": len(args.image) - 1, **burst})
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"burst": burst}), flush=True)


if __name__ == "__main__":
    main()

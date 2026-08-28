from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from tiku_agent.a3_text_orientation import RapidOcrTextPageOrienter


class FakeOcrResult:
    def __init__(self, text: str, confidence: float, *, horizontal: bool):
        self.txts = (text,)
        self.scores = (confidence,)
        if horizontal:
            self.boxes = np.asarray([[[0, 0], [200, 0], [200, 20], [0, 20]]])
        else:
            self.boxes = np.asarray([[[0, 0], [20, 0], [20, 200], [0, 200]]])


class SequenceOcrEngine:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, _image, *, use_cls):
        self.asserted_use_cls = use_cls
        result = self.results[self.calls]
        self.calls += 1
        return result


class A3TextOrientationTest(unittest.TestCase):
    def test_selects_direction_with_clearly_readable_horizontal_text(self):
        engine = SequenceOcrEngine(
            [
                FakeOcrResult("少量字", 0.8, horizontal=False),
                FakeOcrResult("少量字", 0.8, horizontal=False),
                FakeOcrResult("少量字", 0.8, horizontal=False),
                FakeOcrResult("这是正常横向阅读的一整行文字", 0.98, horizontal=True),
            ]
        )
        orienter = RapidOcrTextPageOrienter(engine=engine)

        decision = orienter.choose_correction(Image.new("RGB", (80, 120), "white"))

        self.assertEqual(decision.correction, 270)
        self.assertEqual(decision.reason, "rotated_text_is_clearly_more_readable")
        self.assertEqual(engine.calls, 4)
        self.assertFalse(engine.asserted_use_cls)

    def test_keeps_original_when_text_advantage_is_not_clear(self):
        engine = SequenceOcrEngine(
            [
                FakeOcrResult("这是原图横向文字", 0.95, horizontal=True),
                FakeOcrResult("这是候选横向文字呀", 0.95, horizontal=True),
                FakeOcrResult("少量", 0.8, horizontal=False),
                FakeOcrResult("少量", 0.8, horizontal=False),
            ]
        )
        orienter = RapidOcrTextPageOrienter(
            engine=engine,
            minimum_readable_score=0.0,
            minimum_margin=5.0,
        )

        decision = orienter.choose_correction(Image.new("RGB", (80, 120), "white"))

        self.assertEqual(decision.correction, 0)
        self.assertEqual(decision.reason, "orientation_margin_too_small")

    def test_persists_selected_rotation_but_leaves_safe_original_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "page.jpg"
            Image.new("RGB", (80, 120), "white").save(source)
            rotate_engine = SequenceOcrEngine(
                [
                    FakeOcrResult("少量", 0.8, horizontal=False),
                    FakeOcrResult("这是正常横向阅读的一整行文字", 0.98, horizontal=True),
                    FakeOcrResult("少量", 0.8, horizontal=False),
                    FakeOcrResult("少量", 0.8, horizontal=False),
                ]
            )

            rotated = RapidOcrTextPageOrienter(engine=rotate_engine)(source)

            self.assertNotEqual(rotated, source.resolve())
            self.assertTrue(rotated.name.endswith(".a3-upright.jpg"))
            with Image.open(rotated) as opened:
                self.assertEqual(opened.size, (120, 80))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import dataclass
import threading
import unittest

import numpy as np
from PIL import Image, ImageDraw

from tiku_agent.a3_text_orientation import (
    RapidOcrTextPageOrienter,
    rotate_clockwise,
)


@dataclass
class _Result:
    txts: tuple[str, ...]
    scores: tuple[float, ...]
    boxes: np.ndarray


class _MarkerEngine:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.calls = 0
        self.thread_ids: set[int] = set()
        self.barrier = barrier

    def __call__(self, array, *, use_cls=False):
        self.calls += 1
        self.thread_ids.add(threading.get_ident())
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        height, width = array.shape[:2]
        corner = array[: min(15, height), : min(15, width)]
        chars = 24 if float(corner.mean()) < 100 else 2
        return _Result(
            txts=("A" * chars,),
            scores=(1.0,),
            boxes=np.asarray([[[0, 0], [100, 0], [100, 10], [0, 10]]]),
        )


class A3TextOrientationTest(unittest.TestCase):
    def setUp(self):
        self.upright = Image.new("RGB", (220, 140), "white")
        ImageDraw.Draw(self.upright).rectangle((0, 0, 12, 12), fill="black")

    def test_four_cardinal_directions_are_corrected_from_text_signal(self):
        orienter = RapidOcrTextPageOrienter(engine=_MarkerEngine())
        for applied in (0, 90, 180, 270):
            with self.subTest(applied=applied):
                sample = rotate_clockwise(self.upright, applied)
                decision = orienter.choose_correction(sample)
                self.assertEqual(decision.correction, (-applied) % 360)

    def test_weak_text_keeps_the_original_image(self):
        blank = Image.new("RGB", (220, 140), "white")
        orienter = RapidOcrTextPageOrienter(engine=_MarkerEngine())

        decision = orienter.choose_correction(blank)

        self.assertEqual(decision.correction, 0)
        self.assertEqual(decision.reason, "original_text_is_most_readable")

    def test_four_worker_mode_uses_four_independent_engines(self):
        barrier = threading.Barrier(4)
        engines = tuple(_MarkerEngine(barrier) for _ in range(4))
        orienter = RapidOcrTextPageOrienter(
            engines=engines,
            worker_count=4,
        )

        decision = orienter.choose_correction(self.upright)

        self.assertEqual(decision.correction, 0)
        self.assertEqual([engine.calls for engine in engines], [1, 1, 1, 1])
        self.assertEqual(
            len(set().union(*(engine.thread_ids for engine in engines))),
            4,
        )


if __name__ == "__main__":
    unittest.main()

"""Conservative A3 page orientation using OCR text regions only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import threading
from typing import Any

import numpy as np
from PIL import Image, ImageOps


RAPIDOCR_MODEL_SHA256 = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
}


@dataclass(frozen=True)
class TextOrientationCandidate:
    correction: int
    readable_weighted_chars: float
    readable_line_count: int
    detected_line_count: int


@dataclass(frozen=True)
class TextOrientationDecision:
    correction: int
    score_margin: float
    gain_over_original: float
    reason: str
    candidates: tuple[TextOrientationCandidate, ...]


def rotate_clockwise(image: Image.Image, degrees: int) -> Image.Image:
    if degrees == 0:
        return image.copy()
    return image.transpose(
        {
            90: Image.Transpose.ROTATE_270,
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_90,
        }[degrees]
    )


class RapidOcrTextPageOrienter:
    """Pick 0/90/180/270 from OCR text readability, never page geometry."""

    def __init__(
        self,
        *,
        engine: Any | None = None,
        engines: tuple[Any, ...] | None = None,
        worker_count: int = 1,
        onnx_threads_per_engine: int = -1,
        minimum_readable_score: float = 12.0,
        minimum_margin: float = 5.0,
        minimum_gain_over_original: float = 5.0,
        minimum_horizontal_ratio: float = 1.35,
    ) -> None:
        bounded_workers = int(worker_count)
        if bounded_workers not in (1, 4):
            raise ValueError("worker_count must be 1 or 4")
        if engine is not None and engines is not None:
            raise ValueError("use either engine or engines, not both")
        if engine is not None and bounded_workers != 1:
            raise ValueError("an injected OCR engine only supports worker_count=1")
        if engines is not None and len(engines) != bounded_workers:
            raise ValueError("injected OCR engine count must match worker_count")

        if engines is not None:
            resolved_engines = tuple(engines)
        elif engine is None:
            try:
                import rapidocr
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "A3 text orientation requires the isolated RapidOCR dependency."
                ) from exc
            model_root = Path(rapidocr.__file__).resolve().parent / "models"
            for filename, expected_hash in RAPIDOCR_MODEL_SHA256.items():
                model_path = model_root / filename
                if not model_path.is_file():
                    raise RuntimeError(f"RapidOCR model is missing: {filename}")
                actual_hash = sha256(model_path.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    raise RuntimeError(f"RapidOCR model hash mismatch: {filename}")
            params = None
            if int(onnx_threads_per_engine) > 0:
                params = {
                    "EngineConfig.onnxruntime.intra_op_num_threads": int(
                        onnx_threads_per_engine
                    ),
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    "Global.log_level": "warning",
                }
            resolved_engines = tuple(
                RapidOCR(params=params) for _ in range(bounded_workers)
            )
        else:
            resolved_engines = (engine,)

        self._engines = resolved_engines
        self._executor = (
            ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="a3-text-orientation",
            )
            if len(resolved_engines) == 4
            else None
        )
        self.minimum_readable_score = max(0.0, float(minimum_readable_score))
        self.minimum_margin = max(0.0, float(minimum_margin))
        self.minimum_gain_over_original = max(
            0.0,
            float(minimum_gain_over_original),
        )
        self.minimum_horizontal_ratio = max(1.0, float(minimum_horizontal_ratio))
        # One four-direction batch may run at a time. This keeps each engine
        # single-owner even if several A3 requests reach the runtime together.
        self._batch_lock = threading.RLock()

    def choose_correction(self, image: Image.Image) -> TextOrientationDecision:
        inputs = tuple(
            (correction, rotate_clockwise(image, correction).convert("RGB"))
            for correction in (0, 90, 180, 270)
        )
        with self._batch_lock:
            if self._executor is None:
                candidates = tuple(
                    self._score_image(self._engines[0], correction, candidate_image)
                    for correction, candidate_image in inputs
                )
            else:
                futures = tuple(
                    self._executor.submit(
                        self._score_image,
                        engine,
                        correction,
                        candidate_image,
                    )
                    for engine, (correction, candidate_image) in zip(
                        self._engines,
                        inputs,
                    )
                )
                candidates = tuple(future.result() for future in futures)

        ranked = sorted(
            candidates,
            key=lambda item: item.readable_weighted_chars,
            reverse=True,
        )
        winner = ranked[0]
        original = candidates[0]
        margin = winner.readable_weighted_chars - ranked[1].readable_weighted_chars
        gain = winner.readable_weighted_chars - original.readable_weighted_chars

        if winner.correction == 0:
            correction = 0
            reason = "original_text_is_most_readable"
        elif winner.readable_weighted_chars < self.minimum_readable_score:
            correction = 0
            reason = "insufficient_readable_text"
        elif margin < self.minimum_margin:
            correction = 0
            reason = "orientation_margin_too_small"
        elif gain < self.minimum_gain_over_original:
            correction = 0
            reason = "rotation_gain_too_small"
        else:
            correction = winner.correction
            reason = "rotated_text_is_clearly_more_readable"

        return TextOrientationDecision(
            correction=correction,
            score_margin=round(margin, 6),
            gain_over_original=round(gain, 6),
            reason=reason,
            candidates=tuple(candidates),
        )

    def __call__(self, source_path: str | Path) -> Path:
        """Persist a corrected JPEG, falling back to the unchanged upload."""

        source = Path(source_path).resolve()
        try:
            with Image.open(source) as opened:
                exif_orientation = int(opened.getexif().get(274, 1) or 1)
                normalized = ImageOps.exif_transpose(opened).convert("RGB")
                decision = self.choose_correction(normalized)
                if decision.correction == 0 and exif_orientation == 1:
                    return source
                corrected = rotate_clockwise(normalized, decision.correction)

            target = source.with_name(f"{source.stem}.a3-upright.jpg")
            temporary = target.with_name(f"{target.name}.tmp")
            corrected.save(temporary, format="JPEG", quality=95)
            temporary.replace(target)
            return target.resolve()
        except Exception:  # noqa: BLE001 - optional preprocessing fails open.
            return source

    def _score_image(
        self,
        engine: Any,
        correction: int,
        image: Image.Image,
    ) -> TextOrientationCandidate:
        result = engine(np.asarray(image), use_cls=False)
        texts = tuple(result.txts or ())
        scores = tuple(float(score) for score in (result.scores or ()))
        boxes = result.boxes if result.boxes is not None else ()
        readable_score = 0.0
        readable_lines = 0
        for text, confidence, box in zip(texts, scores, boxes):
            character_count = sum(character.isalnum() for character in str(text))
            points = np.asarray(box, dtype=np.float32)
            if points.shape != (4, 2):
                continue
            line_width = (
                np.linalg.norm(points[1] - points[0])
                + np.linalg.norm(points[2] - points[3])
            ) / 2
            line_height = (
                np.linalg.norm(points[3] - points[0])
                + np.linalg.norm(points[2] - points[1])
            ) / 2
            if line_width >= line_height * self.minimum_horizontal_ratio:
                readable_lines += 1
                readable_score += character_count * confidence
        return TextOrientationCandidate(
            correction=correction,
            readable_weighted_chars=round(readable_score, 6),
            readable_line_count=readable_lines,
            detected_line_count=len(texts),
        )

"""Prepare bounded image payloads for vision-model requests."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps


MODEL_IMAGE_TARGET_BYTES = 1024 * 1024
MODEL_IMAGE_MAX_DIMENSION = 2560
MODEL_IMAGE_FALLBACK_DIMENSION = 2048
MODEL_IMAGE_QUALITY_STEPS = (88, 82, 76, 70, 62, 54)

_MIME_BY_FORMAT = {
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def image_to_model_data_url(
    image_path: str | Path,
    *,
    upscale_min_side: int = 0,
    normalize_orientation: bool = False,
) -> str:
    """Return a model-ready data URL without modifying the source image.

    Small images keep their original bytes unless an existing caller requests
    upscaling or EXIF orientation correction. Large inputs use the same
    conservative 2560px/about-1MiB policy as the mobile web client.
    """

    path = Path(image_path)
    raw = path.read_bytes()
    try:
        with Image.open(BytesIO(raw)) as source:
            source.load()
            image_format = str(source.format or "").upper()
            orientation = int(source.getexif().get(274, 1) or 1)
            width, height = source.size
            needs_orientation = normalize_orientation and orientation != 1
            needs_upscale = bool(upscale_min_side and min(width, height) < upscale_min_side)
            needs_compression = (
                len(raw) > MODEL_IMAGE_TARGET_BYTES
                or max(width, height) > MODEL_IMAGE_MAX_DIMENSION
            )
            if not (needs_orientation or needs_upscale or needs_compression):
                mime = _MIME_BY_FORMAT.get(image_format, _mime_from_suffix(path.suffix))
                return _data_url(mime, raw)

            # Any re-encoding drops the original EXIF metadata, so bake the
            # orientation into pixels first even for non-rerank model calls.
            image = ImageOps.exif_transpose(source) if orientation != 1 else source.copy()
            if needs_compression:
                encoded = _compress_large_image(image)
                return _data_url("image/jpeg", encoded)
            if needs_upscale:
                shortest = min(image.size)
                scale = upscale_min_side / shortest
                resized = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                return _data_url("image/jpeg", _encode_jpeg(resized, 94))

            output = BytesIO()
            if image_format == "PNG":
                image.save(output, format="PNG")
                return _data_url("image/png", output.getvalue())
            return _data_url("image/jpeg", _encode_jpeg(image, 95))
    except (OSError, TypeError, ValueError):
        return _data_url(_mime_from_suffix(path.suffix), raw)


def _compress_large_image(image: Image.Image) -> bytes:
    encoded = _encode_at_dimension(image, MODEL_IMAGE_MAX_DIMENSION, MODEL_IMAGE_QUALITY_STEPS)
    if len(encoded) <= MODEL_IMAGE_TARGET_BYTES:
        return encoded
    return _encode_at_dimension(
        image,
        MODEL_IMAGE_FALLBACK_DIMENSION,
        MODEL_IMAGE_QUALITY_STEPS[1:],
    )


def _encode_at_dimension(image: Image.Image, max_dimension: int, qualities: tuple[int, ...]) -> bytes:
    scale = min(1.0, max_dimension / max(image.size))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image if size == image.size else image.resize(size, Image.Resampling.LANCZOS)
    encoded = b""
    for quality in qualities:
        encoded = _encode_jpeg(resized, quality)
        if len(encoded) <= MODEL_IMAGE_TARGET_BYTES:
            break
    return encoded


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    if image.mode == "RGBA":
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.getchannel("A"))
        image = canvas
    elif image.mode not in {"RGB", "L", "CMYK"}:
        image = image.convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=quality)
    return output.getvalue()


def _mime_from_suffix(suffix: str) -> str:
    return {
        ".bmp": "image/bmp",
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(str(suffix or "").lower(), "image/jpeg")


def _data_url(mime: str, raw: bytes) -> str:
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"

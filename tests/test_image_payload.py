import base64
import io
import unittest
from pathlib import Path

from PIL import Image

from tiku_shared.image_payload import (
    MODEL_IMAGE_MAX_DIMENSION,
    MODEL_IMAGE_TARGET_BYTES,
    image_to_model_data_url,
)


class ImagePayloadTest(unittest.TestCase):
    TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"

    @staticmethod
    def decode(value: str) -> tuple[str, bytes]:
        header, encoded = value.split(",", 1)
        return header, base64.b64decode(encoded)

    def test_small_image_keeps_original_bytes(self):
        self.TEST_TEMP_ROOT.mkdir(exist_ok=True)
        path = self.TEST_TEMP_ROOT / "model-small.png"
        Image.new("RGB", (640, 480), "white").save(path, format="PNG")
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        header, payload = self.decode(image_to_model_data_url(path))

        self.assertEqual(header, "data:image/png;base64")
        self.assertEqual(payload, path.read_bytes())

    def test_large_image_is_bounded_without_changing_source(self):
        self.TEST_TEMP_ROOT.mkdir(exist_ok=True)
        path = self.TEST_TEMP_ROOT / "model-large.bmp"
        Image.effect_noise((3072, 4096), 90).convert("RGB").save(path, format="BMP")
        original_size = path.stat().st_size
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        header, payload = self.decode(image_to_model_data_url(path))

        self.assertEqual(header, "data:image/jpeg;base64")
        self.assertLessEqual(len(payload), MODEL_IMAGE_TARGET_BYTES)
        self.assertEqual(path.stat().st_size, original_size)
        with Image.open(io.BytesIO(payload)) as decoded:
            self.assertLessEqual(max(decoded.size), MODEL_IMAGE_MAX_DIMENSION)

    def test_transparent_large_png_gets_white_jpeg_background(self):
        self.TEST_TEMP_ROOT.mkdir(exist_ok=True)
        path = self.TEST_TEMP_ROOT / "model-transparent.png"
        image = Image.new("RGBA", (2800, 2800), (0, 0, 0, 0))
        image.putpixel((1400, 1400), (0, 0, 0, 255))
        image.save(path, format="PNG")
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        header, payload = self.decode(image_to_model_data_url(path))

        self.assertEqual(header, "data:image/jpeg;base64")
        with Image.open(io.BytesIO(payload)) as decoded:
            self.assertEqual(decoded.mode, "RGB")
            self.assertGreater(min(decoded.getpixel((0, 0))), 245)

    def test_large_exif_oriented_image_is_transposed_before_compression(self):
        self.TEST_TEMP_ROOT.mkdir(exist_ok=True)
        path = self.TEST_TEMP_ROOT / "model-oriented-large.jpg"
        stored = Image.new("RGB", (2800, 1800), "white")
        exif = Image.Exif()
        exif[274] = 6
        stored.save(path, format="JPEG", quality=95, exif=exif)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        _header, payload = self.decode(image_to_model_data_url(path))

        with Image.open(io.BytesIO(payload)) as decoded:
            self.assertLess(decoded.width, decoded.height)
            self.assertIsNone(decoded.getexif().get(274))


if __name__ == "__main__":
    unittest.main()

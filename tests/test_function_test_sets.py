from hashlib import sha256
import json
from pathlib import Path
import unittest


BASE = Path(__file__).resolve().parents[1]


class FunctionalTestSetTest(unittest.TestCase):
    def test_routing_suite_assets_match_manifest(self):
        manifest_path = (
            BASE / "test_sets" / "routing" / "a1_a2_a3" / "manifest.json"
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["suite"], "routing/a1_a2_a3")
        self.assertEqual(len(payload["sources"]), 15)
        self.assertEqual(
            {item["expected_route"] for item in payload["sources"]},
            {"A1", "A2", "A3"},
        )
        self._assert_assets(manifest_path, payload["sources"])

    def test_orientation_suite_assets_match_manifest(self):
        manifest_path = (
            BASE
            / "test_sets"
            / "orientation"
            / "a3_text_rotation"
            / "manifest.json"
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["suite"], "orientation/a3_text_rotation")
        self.assertEqual(payload["applied_clockwise_degrees"], [0, 90, 180, 270])
        self.assertEqual(len(payload["sources"]), 9)
        self._assert_assets(manifest_path, payload["sources"])

    def _assert_assets(self, manifest_path: Path, items) -> None:
        for item in items:
            with self.subTest(path=item["path"]):
                source = (manifest_path.parent / item["path"]).resolve()
                self.assertTrue(source.is_file())
                self.assertEqual(sha256(source.read_bytes()).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()

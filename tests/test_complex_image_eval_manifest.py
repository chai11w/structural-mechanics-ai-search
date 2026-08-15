import hashlib
import json
from pathlib import Path, PurePosixPath
import unittest

from PIL import Image


BASE = Path(__file__).resolve().parents[1]
EVAL_ROOT = BASE / "experiments" / "complex_image_eval"
MANIFEST_PATH = EVAL_ROOT / "manifest.json"
ALLOWED_LOAD_TYPES = {"集中", "均布", "弯矩"}


class ComplexImageEvalManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.samples = cls.manifest["samples"]
        cls.samples_by_id = {sample["id"]: sample for sample in cls.samples}

    def test_manifest_ids_and_coverage_categories_are_unique(self):
        sample_ids = [sample["id"] for sample in self.samples]
        categories = [item["category"] for item in self.manifest["coverage"]]

        self.assertEqual(len(sample_ids), len(set(sample_ids)))
        self.assertEqual(len(categories), len(set(categories)))
        self.assertEqual(len(self.samples_by_id), len(self.samples))

    def test_tracked_media_paths_hashes_and_dimensions_match(self):
        tracked = [
            sample
            for sample in self.samples
            if sample["source_kind"]
            in {"generated_fixture", "user_provided_fixture", "derived_fixture"}
        ]
        self.assertGreaterEqual(len(tracked), 6)

        for sample in tracked:
            with self.subTest(sample=sample["id"]):
                relative = PurePosixPath(sample["relative_path"])
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                image_path = EVAL_ROOT.joinpath(*relative.parts)
                self.assertTrue(image_path.is_file())
                self.assertEqual(
                    hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    sample["sha256"],
                )
                with Image.open(image_path) as image:
                    self.assertEqual(image.size, (sample["width"], sample["height"]))

    def test_derived_and_duplicate_references_resolve(self):
        for sample in self.samples:
            with self.subTest(sample=sample["id"]):
                if "derived_from" in sample:
                    self.assertIn(sample["derived_from"], self.samples_by_id)
                    self.assertNotEqual(sample["derived_from"], sample["id"])
                if "duplicate_of" in sample:
                    self.assertIn(sample["duplicate_of"], self.samples_by_id)
                    self.assertNotEqual(sample["duplicate_of"], sample["id"])

    def test_labels_use_only_supported_load_types(self):
        for sample in self.samples:
            for load in sample.get("actual_loads", []):
                with self.subTest(sample=sample["id"], load=load):
                    self.assertIn(load["type"], ALLOWED_LOAD_TYPES)

    def test_download_fixture_keeps_detailed_diagram_roles_in_a3(self):
        sample = self.samples_by_id["download"]
        self.assertEqual(sample["expected_route"], "A3")
        self.assertEqual(sample["label_status"], "human_confirmed")
        self.assertEqual(sample["question_count"], 1)
        self.assertEqual(sample["diagram_count"], 3)
        self.assertEqual(
            [diagram["role"] for diagram in sample["diagram_roles"]],
            ["original_structure", "load_bending_moment", "unit_load_bending_moment"],
        )
        self.assertEqual(sample["actual_loads"], [{"question": "例4-24", "type": "均布", "raw": "q"}])

    def test_manifest_does_not_contain_runtime_or_absolute_paths(self):
        serialized = json.dumps(self.manifest, ensure_ascii=False)

        self.assertNotIn(".tmp_tiku_agent", serialized)
        self.assertNotIn("F:\\", serialized)
        self.assertNotIn("D:\\", serialized)


if __name__ == "__main__":
    unittest.main()

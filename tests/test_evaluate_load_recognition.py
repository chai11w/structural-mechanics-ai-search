import json
from pathlib import Path
import tempfile
import unittest

from scripts.evaluate_load_recognition import (
    compare_loads,
    load_manifest,
    summarize,
)


class EvaluateLoadRecognitionTests(unittest.TestCase):
    def test_versioned_manifest_has_twenty_unique_portable_samples(self):
        manifest = (
            Path(__file__).resolve().parents[1]
            / "experiments"
            / "load_recognition_eval_20"
            / "manifest.json"
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        samples = payload["samples"]
        self.assertEqual(payload["schema_version"], "load-recognition-eval-v1")
        self.assertEqual(len(samples), 20)
        self.assertEqual(len({sample["id"] for sample in samples}), 20)
        self.assertTrue(all(not Path(sample["path"]).is_absolute() for sample in samples))
        counts = {}
        for sample in samples:
            counts[sample["stratum"]] = counts.get(sample["stratum"], 0) + 1
        self.assertEqual(
            counts,
            {
                "ql_first_power_concentrated": 8,
                "symbolic_moment": 4,
                "other_symbolic": 3,
                "numeric_or_mixed": 5,
            },
        )

    def test_comparison_distinguishes_exact_type_and_retrieval_matches(self):
        expected = [{"type": "集中", "raw": "qL"}, {"type": "均布", "raw": "q"}]
        same = [{"type": "集中", "raw": "ql"}, {"type": "均布", "raw": "q"}]
        wrong_type = [{"type": "弯矩", "raw": "ql"}, {"type": "均布", "raw": "q"}]

        self.assertEqual(
            compare_loads(expected, same),
            {"exact_match": True, "retrieval_match": True, "type_match": True},
        )
        self.assertEqual(
            compare_loads(expected, wrong_type),
            {"exact_match": False, "retrieval_match": False, "type_match": False},
        )

    def test_manifest_rejects_escape_and_accepts_existing_relative_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "question.jpg"
            image.write_bytes(b"image")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "load-recognition-eval-v1",
                        "samples": [
                            {
                                "id": "one",
                                "path": "question.jpg",
                                "expected_loads": [{"type": "均布", "raw": "q"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            loaded = load_manifest(manifest, root)
            self.assertEqual(loaded[0]["image_path"], image.resolve())

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["samples"][0]["path"] = "../escape.jpg"
            manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(manifest, root)

    def test_summary_keeps_failures_out_of_match_counts(self):
        rows = [
            {
                "stratum": "ql",
                "error": "",
                "exact_match": True,
                "retrieval_match": True,
                "type_match": True,
            },
            {
                "stratum": "ql",
                "error": "timeout",
                "exact_match": False,
                "retrieval_match": False,
                "type_match": False,
            },
        ]
        summary = summarize(rows)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["exact_matches"], 1)


if __name__ == "__main__":
    unittest.main()

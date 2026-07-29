import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import search


class ChapterCandidateScannerTest(unittest.TestCase):
    def test_scanner_normalizes_scores_sorts_and_applies_available_structure_filter(self):
        loads = [{"type": "集中", "raw": "P"}]
        frame = pd.DataFrame(
            [
                {"题目名称": "beam-low.jpg", "荷载": json.dumps({"loads": loads}), "结构类型": "梁"},
                {"题目名称": "frame.jpg", "荷载": json.dumps({"loads": loads}), "结构类型": "钢架"},
                {"题目名称": "beam-high.jpg", "荷载": json.dumps({"loads": loads}), "结构类型": "梁"},
            ]
        )

        with patch("search.compute_similarity", side_effect=[0.4, 1.0]):
            scan = search.scan_chapter_candidates(
                loads,
                "4力法",
                Path("bank"),
                structure_type="梁",
                load_excel=lambda _root, _chapter: frame,
            )

        self.assertIsNotNone(scan)
        self.assertTrue(scan.structure_filter_applied)
        self.assertEqual(scan.scored, [(1.0, "beam-high.jpg"), (0.4, "beam-low.jpg")])

    def test_scanner_keeps_the_full_chapter_when_structure_filter_has_no_rows(self):
        loads = [{"type": "集中", "raw": "P"}]
        frame = pd.DataFrame(
            [{"题目名称": "frame.jpg", "荷载": json.dumps({"loads": loads}), "结构类型": "钢架"}]
        )

        with patch("search.compute_similarity", return_value=0.8):
            scan = search.scan_chapter_candidates(
                loads,
                "4力法",
                Path("bank"),
                structure_type="梁",
                load_excel=lambda _root, _chapter: frame,
            )

        self.assertIsNotNone(scan)
        self.assertFalse(scan.structure_filter_applied)
        self.assertEqual(scan.scored, [(0.8, "frame.jpg")])


if __name__ == "__main__":
    unittest.main()

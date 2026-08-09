from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.backfill_run_dimensions_qwen import (
    VERDICTS_FILENAME,
    build_manifest_from_bank,
    verdicts_filename_for,
)


class BackfillRunDimensionVersionTests(unittest.TestCase):
    def test_current_prompt_uses_v5_verdict_filename(self):
        self.assertEqual(VERDICTS_FILENAME, "qwen_v5_backfill_verdicts.json")
        self.assertEqual(
            verdicts_filename_for("structure-dimension-segment-transcription-v5"),
            VERDICTS_FILENAME,
        )

    def test_reused_v4_results_keep_their_versioned_filename(self):
        self.assertEqual(
            verdicts_filename_for("structure-total-span-height-long-width-v4"),
            "qwen_v4_backfill_verdicts.json",
        )

    def test_manifest_scan_does_not_depend_on_read_only_dimension_hint(self):
        bank_root = MagicMock()
        bank_root.glob.return_value = [Path("chapter.xlsx")]
        worksheet = MagicMock()
        worksheet.max_row = 2
        worksheet.cell.side_effect = lambda row, column: MagicMock(
            value={1: "chapter/question.jpg", 3: "钢架"}.get(column)
        )
        workbook = MagicMock()
        workbook.worksheets = [worksheet]
        with patch("openpyxl.load_workbook", return_value=workbook) as load:
            rows = build_manifest_from_bank(bank_root, Path("images"))
        load.assert_called_once_with(Path("chapter.xlsx"), read_only=False, data_only=True)
        self.assertEqual(rows[0]["path"], "chapter/question.jpg")
        self.assertEqual(rows[0]["expected_structure_type"], "钢架")


if __name__ == "__main__":
    unittest.main()

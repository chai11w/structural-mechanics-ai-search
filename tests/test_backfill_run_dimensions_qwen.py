from __future__ import annotations

import unittest

from scripts.backfill_run_dimensions_qwen import VERDICTS_FILENAME, verdicts_filename_for


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


if __name__ == "__main__":
    unittest.main()

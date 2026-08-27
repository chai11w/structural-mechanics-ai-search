from __future__ import annotations

import unittest

from scripts.render_dimension_bank_diff import build_differences, canonical, classify


class DimensionBankDiffTests(unittest.TestCase):
    def test_canonical_blank_is_unknown(self) -> None:
        self.assertEqual(canonical(None), "unknown")
        self.assertEqual(canonical("  "), "unknown")
        self.assertEqual(canonical("2L×L"), "2L×L")

    def test_classification(self) -> None:
        self.assertEqual(classify("2L×L", "unknown"), "现库有值 → 新结果 unknown")
        self.assertEqual(classify("unknown", "2L×L"), "现库空白 → 新结果有值")
        self.assertEqual(classify("2L×L", "3L×L"), "双方有值但不同")

    def test_only_differences_are_returned(self) -> None:
        bank = [
            {"path": "a.jpg", "current": "2L×L", "structure_type": "钢架"},
            {"path": "b.jpg", "current": "L×0", "structure_type": "梁"},
        ]
        qwen = {
            "a.jpg": {"normalized": {"long_width": "unknown"}},
            "b.jpg": {"normalized": {"long_width": "L×0"}},
        }
        result = build_differences(bank, qwen)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "a.jpg")
        self.assertEqual(result[0]["category"], "现库有值 → 新结果 unknown")

    def test_partial_results_can_skip_unreturned_bank_paths(self) -> None:
        bank = [
            {"path": "a.jpg", "current": "2L×0", "structure_type": "钢架"},
            {"path": "b.jpg", "current": "L×L", "structure_type": "钢架"},
        ]
        qwen = {"a.jpg": {"normalized": {"long_width": "unknown"}}}
        result = build_differences(bank, qwen, require_all=False)
        self.assertEqual([row["path"] for row in result], ["a.jpg"])

    def test_equal_single_side_values_are_not_reported_as_differences(self) -> None:
        bank = [
            {
                "path": "single.jpg",
                "current": "单边：3L",
                "current_full": "unknown",
                "current_single": "3L",
                "structure_type": "钢架",
            }
        ]
        qwen = {
            "single.jpg": {
                "normalized": {
                    "long_width": "unknown",
                    "single_side": "3L",
                    "dimension_state": "single",
                }
            }
        }
        self.assertEqual(build_differences(bank, qwen), [])


if __name__ == "__main__":
    unittest.main()

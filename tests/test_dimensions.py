from __future__ import annotations

import unittest

from dimensions import (
    canonical_dimensions,
    compares_width,
    dimensions_match,
    normalize_dimension,
    normalized_dimension_symbol,
)


class DimensionNormalizationTests(unittest.TestCase):
    def test_normalize_dimension_accepts_only_one_simplified_expression(self):
        dimension = normalize_dimension("2.5m")
        self.assertEqual(dimension.raw, "2.5m")
        self.assertEqual(dimension.symbol, "m")
        self.assertEqual(normalize_dimension("3l+2l"), None)
        self.assertEqual(normalize_dimension("l/2"), None)
        self.assertEqual(normalize_dimension("-2m"), None)
        self.assertEqual(normalize_dimension("a").coefficient, 1)
        self.assertEqual(normalize_dimension(None), None)
        self.assertEqual(normalize_dimension("unknown"), None)

    def test_symbols_normalize_to_L_and_physical_units_are_stripped(self):
        self.assertEqual(normalized_dimension_symbol("a"), "L")
        self.assertEqual(normalized_dimension_symbol("l"), "L")
        self.assertEqual(normalized_dimension_symbol("P"), "L")
        self.assertEqual(normalized_dimension_symbol("m"), "")
        self.assertEqual(normalized_dimension_symbol("cm"), "")
        self.assertEqual(normalized_dimension_symbol(""), "")

    def test_canonical_dimensions_are_direct_rotation_invariant_long_width(self):
        self.assertEqual(canonical_dimensions("6m", "3m"), {"long": "6", "width": "3", "long_width": "6×3"})
        self.assertEqual(canonical_dimensions("3m", "6m"), {"long": "6", "width": "3", "long_width": "6×3"})
        self.assertEqual(canonical_dimensions("4a", "2b"), {"long": "4L", "width": "2L", "long_width": "4L×2L"})
        self.assertEqual(canonical_dimensions("6m", "0"), {"long": "6", "width": "0", "long_width": "6×0"})
        self.assertEqual(canonical_dimensions("3m", "2L"), None)
        self.assertEqual(canonical_dimensions(None, "2m"), None)

    def test_units_ignored_and_letters_normalize_like_loads(self):
        # 6m and 6 are the same dimension; units never compare.
        self.assertEqual(canonical_dimensions("6m", "3m"), canonical_dimensions("6", "3"))
        # A question diagram labelled A matches a bank row stored as L.
        self.assertEqual(canonical_dimensions("3A", "A"), {"long": "3L", "width": "L", "long_width": "3L×L"})
        self.assertEqual(
            dimensions_match(canonical_dimensions("3A", "A"), canonical_dimensions("3L", "L"), "钢架"), "match"
        )
        self.assertEqual(
            dimensions_match(canonical_dimensions("6m", "3m"), canonical_dimensions("6", "3"), "钢架"), "match"
        )

    def test_cross_letter_symbols_normalize_before_comparison(self):
        query = canonical_dimensions("3a", "b")
        candidate = canonical_dimensions("3l", "l")
        self.assertEqual(query, {"long": "3L", "width": "L", "long_width": "3L×L"})
        self.assertEqual(dimensions_match(query, candidate, "钢架"), "match")


class DimensionMatchTests(unittest.TestCase):
    def test_beam_compares_length_only(self):
        self.assertEqual(compares_width("梁"), False)
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": "不同"}, "梁"), "match"
        )
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "4L", "width": "L"}, "梁"), "mismatch"
        )

    def test_every_other_structure_type_compares_long_and_width(self):
        for structure_type in ("钢架", "桁架", "拱", "组合结构", "unknown"):
            self.assertEqual(compares_width(structure_type), True)
            self.assertEqual(
                dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": "L"}, structure_type),
                "match",
            )
            self.assertEqual(
                dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": "2L"}, structure_type),
                "mismatch",
            )
            self.assertEqual(
                dimensions_match({"long": "3L", "width": "L"}, {"long": "2L", "width": "L"}, structure_type),
                "mismatch",
            )

    def test_missing_dimensions_skip_instead_of_hard_delete(self):
        self.assertEqual(dimensions_match(None, {"long": "3L", "width": "L"}, "梁"), "skip")
        self.assertEqual(dimensions_match({"long": "3L", "width": "L"}, None, "梁"), "skip")
        self.assertEqual(dimensions_match({}, {"long": "3L", "width": "L"}, "钢架"), "skip")
        self.assertEqual(dimensions_match({"long": "", "width": "L"}, {"long": "3L", "width": "L"}, "梁"), "skip")
        self.assertEqual(dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": ""}, "钢架"), "skip")

    def test_symbolic_never_matches_numeric(self):
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "6", "width": "L"}, "钢架"), "mismatch"
        )
        self.assertEqual(
            dimensions_match({"long": "6", "width": "3"}, {"long": "3L", "width": "L"}, "钢架"), "mismatch"
        )

    def test_exact_string_equality_after_normalization_no_ratio_tolerance(self):
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": "L"}, "钢架"), "match"
        )
        # Coefficient differs by one — still a hard delete, no tolerance.
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "2L", "width": "L"}, "钢架"), "mismatch"
        )


if __name__ == "__main__":
    unittest.main()

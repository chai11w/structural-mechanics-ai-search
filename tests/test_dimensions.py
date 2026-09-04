from __future__ import annotations

import unittest
from fractions import Fraction

from dimensions import (
    DimensionEvidence,
    canonical_dimensions,
    compares_width,
    dimension_evidence,
    dimension_evidence_verdict,
    dimensions_match,
    filter_ranked_candidates_by_dimensions,
    normalize_dimension,
    normalized_dimension_symbol,
    parse_dimension_segment,
    sum_dimension_segments,
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

    def test_canonical_dimensions_rotate_a_vertical_beam(self):
        # A beam drawn vertically has span 0 and height 5L; rotation-invariant
        # canonical form must equal the horizontal reading 5L×0.
        self.assertEqual(
            canonical_dimensions("0", "5L"), {"long": "5L", "width": "0", "long_width": "5L×0"}
        )
        self.assertEqual(canonical_dimensions("0", "0"), None)

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


class DimensionSegmentTests(unittest.TestCase):
    def test_parse_dimension_segment_accepts_drawn_fractional_labels(self):
        self.assertEqual(parse_dimension_segment("a").coefficient, Fraction(1, 1))
        self.assertEqual(parse_dimension_segment("a").symbol, "a")
        self.assertEqual(parse_dimension_segment("a/2").coefficient, Fraction(1, 2))
        self.assertEqual(parse_dimension_segment("2a/3").coefficient, Fraction(2, 3))
        self.assertEqual(parse_dimension_segment("l/2").coefficient, Fraction(1, 2))
        self.assertEqual(parse_dimension_segment("0.5a").coefficient, Fraction(1, 2))
        self.assertEqual(parse_dimension_segment("2.5a").coefficient, Fraction(5, 2))
        self.assertEqual(parse_dimension_segment("2").coefficient, Fraction(2, 1))
        self.assertEqual(parse_dimension_segment("1/2").coefficient, Fraction(1, 2))

    def test_parse_dimension_segment_rejects_unreadable_or_malformed_labels(self):
        self.assertIsNone(parse_dimension_segment(None))
        self.assertIsNone(parse_dimension_segment(""))
        self.assertIsNone(parse_dimension_segment("null"))
        self.assertIsNone(parse_dimension_segment("unknown"))
        self.assertIsNone(parse_dimension_segment("a+b"))
        self.assertIsNone(parse_dimension_segment("a/2a"))

    def test_sum_segments_letter_labels_sum_to_L(self):
        result = sum_dimension_segments(["a", "a/2", "a/2", "a"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["dimension"].coefficient, Fraction(3, 1))
        self.assertEqual(result["dimension"].symbol, "L")

    def test_sum_segments_fraction_total_is_exact(self):
        # 1/3 + 2/3 + 1/3 + 1 = 7/3 — the case qwen summed wrong as 3L.
        result = sum_dimension_segments(["l/3", "2l/3", "l/3", "l"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["dimension"].coefficient, Fraction(7, 3))

    def test_sum_segments_numeric_labels_share_no_symbol(self):
        result = sum_dimension_segments(["2", "3"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["dimension"].coefficient, 5)
        self.assertEqual(result["dimension"].symbol, "")

    def test_sum_segments_rejects_mixed_letter_and_number_kind(self):
        result = sum_dimension_segments(["a", "2"])
        self.assertIsNone(result["dimension"])
        self.assertEqual(result["error"], "mixed_kind")

    def test_sum_segments_rejects_unreadable_segment(self):
        result = sum_dimension_segments(["a", "null"])
        self.assertIsNone(result["dimension"])
        self.assertEqual(result["error"], "unreadable_segment")

    def test_sum_segments_empty_is_not_an_error(self):
        result = sum_dimension_segments([])
        self.assertIsNone(result["dimension"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["total"], 0)


class DimensionMatchTests(unittest.TestCase):
    def test_beam_requires_a_zero_width_and_compares_both_axes(self):
        self.assertEqual(compares_width("梁"), True)
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "0"}, {"long": "3L", "width": "0"}, "梁"), "match"
        )
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "0"}, {"long": "4L", "width": "0"}, "梁"), "mismatch"
        )
        self.assertEqual(
            dimensions_match({"long": "3L", "width": "L"}, {"long": "3L", "width": "L"}, "梁"), "skip"
        )

    def test_participating_non_beam_types_compare_long_and_width(self):
        for structure_type in ("钢架", "桁架", "组合结构"):
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

    def test_arch_and_unknown_never_enter_dimension_matching(self):
        for structure_type in ("拱", "unknown", ""):
            self.assertFalse(compares_width(structure_type))
            self.assertEqual(
                dimensions_match(
                    {"long": "3L", "width": "L"},
                    {"long": "3L", "width": "L"},
                    structure_type,
                ),
                "skip",
            )

    def test_missing_dimensions_skip_instead_of_hard_delete(self):
        self.assertEqual(dimensions_match(None, {"long": "3L", "width": "0"}, "梁"), "skip")
        self.assertEqual(dimensions_match({"long": "3L", "width": "0"}, None, "梁"), "skip")
        self.assertEqual(dimensions_match({}, {"long": "3L", "width": "L"}, "钢架"), "skip")
        self.assertEqual(dimensions_match({"long": "", "width": "0"}, {"long": "3L", "width": "0"}, "梁"), "skip")
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


class DimensionEvidenceFilterTests(unittest.TestCase):
    def test_bank_full_and_single_are_distinct_states(self):
        self.assertEqual(dimension_evidence("3a×a", "", "钢架").state, "full")
        single = dimension_evidence("", "3a", "钢架")
        self.assertEqual(single, DimensionEvidence(single="3L", state="single"))
        self.assertEqual(dimension_evidence("3L×L", "3L", "钢架").state, "conflict")
        self.assertEqual(dimension_evidence("3L×0", "", "钢架").state, "conflict")
        self.assertEqual(dimension_evidence("3L×L", "", "梁").state, "conflict")

    def test_single_side_equality_matches_and_inequality_is_a_hard_mismatch(self):
        query = DimensionEvidence(single="3L", state="single")
        self.assertEqual(
            dimension_evidence_verdict(query, dimension_evidence("4L×3L", "", "钢架"), "钢架"),
            "match",
        )
        self.assertEqual(
            dimension_evidence_verdict(query, dimension_evidence("4L×L", "", "钢架"), "钢架"),
            "mismatch",
        )

    def test_single_side_hard_filters_comparable_nonmatches(self):
        candidates = [
            {"rank": 1, "name": "other", "long_width": "4L×L", "single_side": ""},
            {"rank": 2, "name": "same", "long_width": "", "single_side": "3L"},
            {"rank": 3, "name": "also-other", "long_width": "", "single_side": "2L"},
        ]
        filtered, trace = filter_ranked_candidates_by_dimensions(
            candidates,
            DimensionEvidence(single="3L", state="single"),
            "钢架",
        )
        self.assertEqual([item["name"] for item in filtered], ["same"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(trace["matches"], 1)
        self.assertEqual(trace["mismatches"], 2)

    def test_full_query_hard_filters_comparable_single_side_nonmatch(self):
        candidates = [
            {"rank": 1, "name": "wrong", "long_width": "", "single_side": "2L"},
            {"rank": 2, "name": "same", "long_width": "", "single_side": "3L"},
            {"rank": 3, "name": "unknown", "long_width": "", "single_side": ""},
        ]
        filtered, trace = filter_ranked_candidates_by_dimensions(
            candidates,
            dimension_evidence("3L×L", "", "钢架"),
            "钢架",
        )
        self.assertEqual([item["name"] for item in filtered], ["same", "unknown"])
        self.assertEqual(trace["matches"], 1)
        self.assertEqual(trace["mismatches"], 1)
        self.assertEqual(trace["skipped"], 1)

    def test_only_full_full_mismatches_are_deleted(self):
        candidates = [
            {"rank": 1, "name": "wrong", "long_width": "4L×L", "single_side": ""},
            {"rank": 2, "name": "single", "long_width": "", "single_side": "3L"},
            {"rank": 3, "name": "exact", "long_width": "3L×L", "single_side": ""},
        ]
        filtered, trace = filter_ranked_candidates_by_dimensions(
            candidates,
            dimension_evidence("3L×L", "", "钢架"),
            "钢架",
        )
        self.assertEqual([item["name"] for item in filtered], ["single", "exact"])
        self.assertEqual(trace["mismatches"], 1)

    def test_all_deleted_falls_back_to_original_pool(self):
        candidates = [{"rank": 1, "name": "wrong", "long_width": "4L×L", "single_side": ""}]
        filtered, trace = filter_ranked_candidates_by_dimensions(
            candidates,
            dimension_evidence("3L×L", "", "钢架"),
            "钢架",
        )
        self.assertEqual(filtered, candidates)
        self.assertTrue(trace["fallback"])


if __name__ == "__main__":
    unittest.main()

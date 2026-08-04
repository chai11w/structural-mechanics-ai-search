"""Rotation-invariant structural outer-dimension normalization and hard matching.

Extracted from the Qwen/MCP dimension comparison experiment so the letter-bank
search pipeline can reuse the same normalization rules without importing an
evaluation harness. Pure functions only; no model calls, no bank access.

Normalization contract (mirrors ``scripts/evaluate_structure_dimensions.py``):
the total span and total height are single simplified expressions such as
``6m``, ``3l`` or ``0``. The outer box is rotation-invariant: long is the larger
coefficient, width is the smaller one. Symbolic length variables all normalize
to ``L``; physical length units are preserved. A mix of symbol kinds or missing
values is rejected (returns ``None``) instead of inventing a comparable key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

# Structure types that require both the larger and the smaller dimension.
# Everything except a single beam compares 长 and 宽; a beam compares 长 only
# because a single horizontal beam has total height ``0`` (width meaningless).
BEAM_TYPES = {"梁"}
COMPARES_WIDTH_TYPES = {"钢架", "桁架", "拱", "组合结构", "unknown"}

_DIMENSION_RE = re.compile(
    r"^(?:(?P<coefficient>(?:0|[1-9]\d*)(?:\.\d+)?)?(?P<symbol>[A-Za-z]+)|(?P<number>0|[1-9]\d*(?:\.\d+)?))$"
)


@dataclass(frozen=True)
class Dimension:
    raw: str
    coefficient: Fraction
    symbol: str


def normalize_dimension(value: object) -> Dimension | None:
    """Normalize one simplified total-span/total-height expression.

    The provider contract permits only a non-negative decimal coefficient with an
    optional alphabetic unit/symbol. Rejecting all other algebra avoids inventing
    a dimension key for expressions whose relation is not mechanically known.
    """

    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text or text.lower() in {"null", "unknown", "未知", "不确定"}:
        return None
    text = text.replace("米", "m")
    match = _DIMENSION_RE.fullmatch(text)
    if not match:
        return None
    coefficient_text = match.group("coefficient") or match.group("number") or "1"
    coefficient = Fraction(coefficient_text)
    if coefficient < 0:
        return None
    return Dimension(raw=text, coefficient=coefficient, symbol=(match.group("symbol") or ""))


def normalized_dimension_symbol(symbol: str) -> str:
    """Map symbolic length variables to ``L`` while preserving physical units."""

    if not symbol:
        return ""
    if symbol in {"m", "cm", "mm", "km"}:
        return symbol
    return "L"


def dimension_text(dimension: Dimension) -> str:
    """Render a parsed dimension after the required unit/letter normalization."""

    if not dimension.symbol:
        return dimension.raw
    coefficient_text = dimension.raw[: -len(dimension.symbol)]
    return f"{coefficient_text}{normalized_dimension_symbol(dimension.symbol)}" if coefficient_text else normalized_dimension_symbol(dimension.symbol)


def canonical_dimensions(total_span: object, total_height: object) -> dict[str, str] | None:
    """Return rotation-invariant literal long/width dimensions, never a ratio.

    The total span must be positive. A zero total height is valid and remains a
    literal ``0`` width. Letter variables share the canonical symbol ``L``;
    physical length units must still agree before values can be compared.
    """

    span = normalize_dimension(total_span)
    height = normalize_dimension(total_height)
    if span is None or height is None or span.coefficient <= 0:
        return None

    span_symbol = normalized_dimension_symbol(span.symbol)
    height_symbol = normalized_dimension_symbol(height.symbol)
    nonzero_symbols = {
        symbol
        for dimension, symbol in ((span, span_symbol), (height, height_symbol))
        if dimension.coefficient != 0 and symbol
    }
    if len(nonzero_symbols) > 1:
        return None
    if (
        span.coefficient != 0
        and height.coefficient != 0
        and bool(span_symbol) != bool(height_symbol)
    ):
        return None
    if span.coefficient == 0 and span_symbol and height_symbol and span_symbol != height_symbol:
        return None
    if height.coefficient == 0 and height_symbol and span_symbol and height_symbol != span_symbol:
        return None

    ordered = sorted((span, height), key=lambda dimension: dimension.coefficient, reverse=True)
    long_dimension, width_dimension = ordered
    long_text = dimension_text(long_dimension)
    width_text = dimension_text(width_dimension)
    return {"long": long_text, "width": width_text, "long_width": f"{long_text}×{width_text}"}


def compares_width(structure_type: object) -> bool:
    """Whether ``structure_type`` needs both long and width for a hard match."""

    return str(structure_type or "").strip() in COMPARES_WIDTH_TYPES


def dimensions_match(
    query: Mapping[str, str] | None,
    candidate: Mapping[str, str] | None,
    structure_type: object,
) -> str:
    """Hard-filter verdict for one query/candidate outer-dimension pair.

    Returns one of:
      "match"    — both sides have comparable dimensions and they are equal.
      "mismatch" — both sides have comparable dimensions but at least one differs
                   (hard delete).
      "skip"     — at least one side lacks a comparable dimension; the dimension
                   barrier cannot judge, so the caller should keep the candidate
                   (recall-preserving).

    Inputs are canonical dimension boxes as produced by ``canonical_dimensions``
    (``{"long": ..., "width": ..., "long_width": ...}``) or ``None``/``{}`` when
    the side has no usable dimensions. Symbolic dimensions are compared only with
    symbolic (letter symbols already normalized to ``L``), numeric only with
    numeric, and equality is exact string equality after that normalization.
    Beams compare 长 only; every other structure type (including 组合结构 and
    unknown) compares 长 and 宽.
    """

    if not query or not candidate:
        return "skip"
    query_long = str(query.get("long") or "").strip()
    candidate_long = str(candidate.get("long") or "").strip()
    if not query_long or not candidate_long:
        return "skip"
    if query_long != candidate_long:
        return "mismatch"
    if not compares_width(structure_type):
        return "match"
    query_width = str(query.get("width") or "").strip()
    candidate_width = str(candidate.get("width") or "").strip()
    if not query_width or not candidate_width:
        return "skip"
    return "match" if query_width == candidate_width else "mismatch"

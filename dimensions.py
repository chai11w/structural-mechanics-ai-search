"""Rotation-invariant structural outer-dimension normalization and hard matching.

Extracted from the Qwen/MCP dimension comparison experiment so the letter-bank
search pipeline can reuse the same normalization rules without importing an
evaluation harness. Pure functions only; no model calls, no bank access.

Normalization contract (mirrors ``scripts/evaluate_structure_dimensions.py``):
the total span and total height are single simplified expressions such as
``6m``, ``3l`` or ``0``. The outer box is rotation-invariant: long is the larger
coefficient, width is the smaller one. Like the letter-load normalization,
physical length units are stripped from numeric dimensions (``6m`` -> ``6``) and
every symbolic length variable normalizes to ``L`` so that ``a``/``b``/``l``
all compare equal. A mix of symbolic and numeric kinds, or missing values, is
rejected (returns ``None``) instead of inventing a comparable key.
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


_PHYSICAL_LENGTH_UNITS = {"m", "cm", "mm", "km"}


def normalized_dimension_symbol(symbol: str) -> str:
    """Map one dimension label to its comparable canonical symbol.

    Mirrors the letter-load normalization: physical length units are dropped
    from numeric dimensions (``6m`` -> ``6``, units never compare), and every
    other alphabetic symbol is a length variable normalized to ``L`` so a
    question diagram labelled ``A`` still matches a bank row stored as ``L``.
    """

    if not symbol:
        return ""
    if symbol in _PHYSICAL_LENGTH_UNITS:
        return ""
    return "L"


_SEGMENT_RE = re.compile(
    r"^(?:(?P<coef>(?:0|[1-9]\d*)(?:\.\d+)?))?(?P<symbol>[A-Za-z]+)?(?:/(?P<den>0|[1-9]\d*))?$"
)
_UNREADABLE_LABELS = {"null", "unknown", "未知", "不确定"}


def parse_dimension_segment(value: object) -> Dimension | None:
    """Parse one raw segment label transcribed from the diagram.

    Accepts fractional labels as drawn (``a/2``, ``2a/3``, ``l/2``, ``0.5a``,
    ``2``, ``1/2``) so the model only transcribes each span label and never has
    to do arithmetic; the caller sums the coefficients. ``None`` for an empty,
    unreadable (``null``/``unknown``) or malformed label.
    """

    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text or text.lower() in _UNREADABLE_LABELS:
        return None
    match = _SEGMENT_RE.fullmatch(text)
    if not match:
        return None
    coefficient = Fraction(match.group("coef") or "1")
    denominator_text = match.group("den")
    symbol = match.group("symbol") or ""
    if not denominator_text and not symbol and match.group("coef") is None:
        return None
    if denominator_text:
        coefficient /= Fraction(denominator_text)
    if coefficient < 0:
        return None
    return Dimension(raw=text, coefficient=coefficient, symbol=symbol)


def _render_coefficient(coefficient: Fraction) -> str:
    if coefficient.denominator == 1:
        return str(coefficient.numerator)
    return f"{float(coefficient):.6g}"


def sum_dimension_segments(values: object) -> dict[str, object]:
    """Sum raw transcribed segment labels into one canonical dimension.

    All readable segments must share one kind — every segment symbolic (any
    letter, normalized to ``L``) or every segment numeric — otherwise the sum is
    not mechanically valid. The returned total carries a renderable ``raw`` so it
    can feed :func:`canonical_dimensions` directly.

    Returns ``{"dimension": Dimension|None, "segments": [...parsed...],
    "readable": int, "total": int, "error": str|None}`` where ``error`` is one
    of ``"unreadable_segment"`` / ``"mixed_kind"`` when the sum cannot be trusted.
    """

    items = list(values or [])
    parsed = [parse_dimension_segment(item) for item in items]
    readable = [dimension for dimension in parsed if dimension is not None]
    if len(readable) != len(items):
        return {
            "dimension": None,
            "segments": parsed,
            "readable": len(readable),
            "total": len(items),
            "error": "unreadable_segment",
        }
    if not readable:
        return {
            "dimension": None,
            "segments": parsed,
            "readable": 0,
            "total": 0,
            "error": None,
        }
    symbols = {
        normalized_dimension_symbol(dimension.symbol)
        for dimension in readable
        if dimension.coefficient != 0
    }
    if len(symbols) > 1:
        return {
            "dimension": None,
            "segments": parsed,
            "readable": len(readable),
            "total": len(items),
            "error": "mixed_kind",
        }
    symbol = symbols.pop() if symbols else ""
    coefficient = sum((dimension.coefficient for dimension in readable), Fraction(0))
    raw = f"{_render_coefficient(coefficient)}{symbol}" if symbol else _render_coefficient(coefficient)
    return {
        "dimension": Dimension(raw=raw, coefficient=coefficient, symbol=symbol),
        "segments": parsed,
        "readable": len(readable),
        "total": len(items),
        "error": None,
    }


def _coefficient_text(dimension: Dimension) -> str:
    if not dimension.symbol:
        return dimension.raw
    return dimension.raw[: -len(dimension.symbol)]


def dimension_text(dimension: Dimension) -> str:
    """Render a parsed dimension after unit stripping / letter normalization."""

    normalized_symbol = normalized_dimension_symbol(dimension.symbol)
    if not normalized_symbol:
        return _coefficient_text(dimension)
    if dimension.coefficient == 1:
        return normalized_symbol
    return f"{_coefficient_text(dimension)}{normalized_symbol}"


def canonical_dimensions(total_span: object, total_height: object) -> dict[str, str] | None:
    """Return rotation-invariant literal long/width dimensions, never a ratio.

    At least one of span / height must be positive; the box is rotation-invariant
    so a vertical beam (span ``0``, height ``5L``) canonicalizes the same as a
    horizontal one (``5L``, ``0``). A zero dimension stays a literal ``0`` width.
    Numeric units are stripped and letter variables share the canonical symbol
    ``L``, so values compare equal when their canonical forms do.
    """

    span = normalize_dimension(total_span)
    height = normalize_dimension(total_height)
    if span is None or height is None or (span.coefficient <= 0 and height.coefficient <= 0):
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

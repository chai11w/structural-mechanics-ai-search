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
from typing import Any, Mapping, Sequence

PARTICIPATING_STRUCTURE_TYPES = {"梁", "钢架", "桁架", "组合结构"}
NONZERO_WIDTH_STRUCTURE_TYPES = {"钢架", "桁架", "组合结构"}

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


def parse_long_width(value: object) -> dict[str, str] | None:
    """Parse one stored rotation-invariant ``长×宽`` value.

    The bank stores complete dimensions only. Both sides are normalized through
    :func:`canonical_dimensions`, so ``a``/``l`` become ``L`` and numeric length
    units are removed just like model output.
    """

    text = str(value or "").strip().replace(" ", "")
    if not text or text.lower() in _UNREADABLE_LABELS:
        return None
    parts = re.split(r"[×xX*＊]", text)
    if len(parts) != 2:
        return None
    return canonical_dimensions(parts[0], parts[1])


def normalize_single_dimension(value: object) -> str | None:
    """Normalize one rotation-invariant known side; zero is never a side hint."""

    dimension = normalize_dimension(value)
    if dimension is None or dimension.coefficient <= 0:
        return None
    return dimension_text(dimension)


@dataclass(frozen=True)
class DimensionEvidence:
    """Comparable complete or one-side-only dimensions for one question."""

    full: Mapping[str, str] | None = None
    single: str | None = None
    state: str = "none"


def dimensions_consistent_with_structure(
    dimensions: Mapping[str, str] | None,
    structure_type: object,
) -> bool:
    """Validate the hard type invariant for a complete dimension box."""

    if not dimensions:
        return False
    normalized_type = str(structure_type or "").strip()
    if normalized_type not in PARTICIPATING_STRUCTURE_TYPES:
        return False
    long_side = str(dimensions.get("long") or "").strip()
    width = str(dimensions.get("width") or "").strip()
    if not long_side or not width:
        return False
    if normalized_type == "梁":
        return width == "0"
    if normalized_type in NONZERO_WIDTH_STRUCTURE_TYPES:
        return width != "0"
    return False


def dimension_evidence(
    long_width: object,
    single_side: object,
    structure_type: object,
) -> DimensionEvidence:
    """Build conservative evidence from the bank's complete/single columns."""

    normalized_type = str(structure_type or "").strip()
    if normalized_type not in PARTICIPATING_STRUCTURE_TYPES:
        return DimensionEvidence(state="skip")
    full = parse_long_width(long_width)
    single = normalize_single_dimension(single_side)
    if full and single:
        return DimensionEvidence(state="conflict")
    if full:
        if not dimensions_consistent_with_structure(full, normalized_type):
            return DimensionEvidence(state="conflict")
        return DimensionEvidence(full=full, state="full")
    if single:
        return DimensionEvidence(single=single, state="single")
    return DimensionEvidence(state="none")


def dimension_evidence_from_normalized(
    normalized: Mapping[str, Any] | None,
    structure_type: object,
) -> DimensionEvidence:
    """Build query evidence from normalized V5.2 model output."""

    result = dict(normalized or {})
    if result.get("dimensions_verified") is not True:
        return DimensionEvidence(state="none")
    state = str(result.get("dimension_state") or "").strip()
    if state in {"skip", "conflict", "none"}:
        return DimensionEvidence(state=state)
    if state == "full":
        full = {
            "long": str(result.get("long") or "").strip(),
            "width": str(result.get("width") or "").strip(),
            "long_width": str(result.get("long_width") or "").strip(),
        }
        if dimensions_consistent_with_structure(full, structure_type):
            return DimensionEvidence(full=full, state="full")
        return DimensionEvidence(state="conflict")
    if state == "single":
        single = normalize_single_dimension(result.get("single_side"))
        return DimensionEvidence(single=single, state="single" if single else "none")

    # Compatibility for saved V5/V5.2 results written before dimension_state.
    full = parse_long_width(result.get("long_width"))
    if full and dimensions_consistent_with_structure(full, structure_type):
        return DimensionEvidence(full=full, state="full")
    for key in ("single_side", "code_span", "code_height", "total_span", "total_height"):
        single = normalize_single_dimension(result.get(key))
        if single:
            return DimensionEvidence(single=single, state="single")
    return DimensionEvidence(state="none")


def compares_width(structure_type: object) -> bool:
    """Whether ``structure_type`` participates in complete two-axis matching."""

    return str(structure_type or "").strip() in PARTICIPATING_STRUCTURE_TYPES


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
    Every participating structure compares both axes. A beam's width must be
    the literal ``0``; a frame/truss/composite width must be non-zero. Arches,
    unknown types, and type/dimension conflicts return ``skip``.
    """

    if not compares_width(structure_type) or not query or not candidate:
        return "skip"
    if not dimensions_consistent_with_structure(query, structure_type):
        return "skip"
    if not dimensions_consistent_with_structure(candidate, structure_type):
        return "skip"
    query_long = str(query.get("long") or "").strip()
    candidate_long = str(candidate.get("long") or "").strip()
    if not query_long or not candidate_long:
        return "skip"
    if query_long != candidate_long:
        return "mismatch"
    query_width = str(query.get("width") or "").strip()
    candidate_width = str(candidate.get("width") or "").strip()
    if not query_width or not candidate_width:
        return "skip"
    return "match" if query_width == candidate_width else "mismatch"


def dimension_evidence_verdict(
    query: DimensionEvidence,
    candidate: DimensionEvidence,
    structure_type: object,
) -> str:
    """Return ``match``/``mismatch``/``skip`` for complete and single evidence.

    Whenever both sides provide reliable evidence, a shared side is a match and
    no shared side is a hard mismatch. ``skip`` is reserved for missing,
    conflicting, or otherwise unusable evidence.
    """

    if not compares_width(structure_type):
        return "skip"
    if query.state == "full" and candidate.state == "full":
        return dimensions_match(query.full, candidate.full, structure_type)
    if query.state not in {"full", "single"} or candidate.state not in {"full", "single"}:
        return "skip"

    query_sides: Sequence[str]
    candidate_sides: Sequence[str]
    if query.state == "full" and query.full:
        query_sides = (str(query.full.get("long") or ""), str(query.full.get("width") or ""))
    else:
        query_sides = (str(query.single or ""),)
    if candidate.state == "full" and candidate.full:
        candidate_sides = (
            str(candidate.full.get("long") or ""),
            str(candidate.full.get("width") or ""),
        )
    else:
        candidate_sides = (str(candidate.single or ""),)
    common = (set(query_sides) & set(candidate_sides)) - {""}
    return "match" if common else "mismatch"


def filter_ranked_candidates_by_dimensions(
    candidates: list[dict[str, Any]],
    query: DimensionEvidence,
    structure_type: object,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stable positive reordering plus complete/full hard filtering.

    The original list is returned unchanged when the query is unusable or when
    every candidate would otherwise be removed. That fallback prevents one bad
    model response from turning a healthy coarse pool into a false no-match.
    """

    before = len(candidates)
    trace: dict[str, Any] = {
        "triggered": True,
        "applied": False,
        "query_state": query.state,
        "before": before,
        "after": before,
        "matches": 0,
        "mismatches": 0,
        "skipped": before,
        "fallback": False,
    }
    if query.state not in {"full", "single"}:
        return list(candidates), trace

    matches: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    mismatches = 0
    for candidate in candidates:
        evidence = dimension_evidence(
            candidate.get("long_width"),
            candidate.get("single_side"),
            structure_type,
        )
        verdict = dimension_evidence_verdict(query, evidence, structure_type)
        copied = dict(candidate)
        copied["dimension_verdict"] = verdict
        copied["dimension_state"] = evidence.state
        if verdict == "match":
            matches.append(copied)
        elif verdict == "mismatch":
            mismatches += 1
        else:
            kept.append(copied)

    filtered = matches + kept
    if not filtered and candidates:
        trace.update({"fallback": True, "mismatches": mismatches})
        return list(candidates), trace
    trace.update(
        {
            "applied": bool(matches or mismatches),
            "after": len(filtered),
            "matches": len(matches),
            "mismatches": mismatches,
            "skipped": len(kept),
        }
    )
    for rank, candidate in enumerate(filtered, 1):
        candidate["rank"] = rank
    return filtered, trace

"""A2 stage projections for the 4.3.3 runtime integration batch."""

from __future__ import annotations

import re
from typing import Any, Mapping

from tiku_agent.checkpoint_capture import A2CheckpointContextV1, build_a2_checkpoint
from tiku_agent.checkpoint_contract import (
    SECTION_CHAPTER_DECISION,
    SECTION_LOAD_OBSERVATIONS,
    SECTION_QUESTION_CONTEXT,
    SECTION_STRUCTURE_DECISION,
    STAGE_QUESTION_ANALYZED,
    IntermediateCheckpointV1,
)
from tiku_agent.tool_result import ToolOutcome, ToolResult


_LOAD_TYPES = frozenset({"集中", "均布", "弯矩"})
_STRUCTURE_TYPES = frozenset({"", "梁", "钢架", "桁架", "拱"})
_SCOPE_STATUSES = frozenset({"supported", "unsupported", "uncertain"})


def _text(value: object, *, maximum: int = 1_000) -> str:
    value = str(value or "")
    return value[:maximum]


def _symbol(value: object, fallback: str) -> str:
    candidate = str(value or "")
    return candidate if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}", candidate) else fallback


def _code(value: object, fallback: str) -> str:
    candidate = str(value or "").upper()
    return candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", candidate) else fallback


def _loads(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("analysis loads must be a list")
    normalized: list[dict[str, str]] = []
    for item in value[:50]:
        if not isinstance(item, Mapping):
            raise ValueError("analysis load must be an object")
        load_type = str(item.get("type") or "")
        if load_type not in _LOAD_TYPES:
            raise ValueError("analysis load type is not supported")
        normalized.append({"type": load_type, "raw": _text(item.get("raw"), maximum=200)})
    return tuple(normalized)


def question_analyzed_result(analysis: Mapping[str, Any]) -> dict[str, object]:
    """Project an A2 analysis payload into the frozen required sections."""

    if not isinstance(analysis, Mapping):
        raise TypeError("analysis must be a mapping")
    chapter = _text(analysis.get("chapter"), maximum=100)
    scope_status = str(analysis.get("chapter_scope_status") or "")
    if scope_status not in _SCOPE_STATUSES:
        scope_status = "supported" if chapter else "uncertain"
    structure_type = str(analysis.get("structure_type") or "")
    if structure_type not in _STRUCTURE_TYPES:
        structure_type = ""
    loads = _loads(analysis.get("loads") or [])
    chapter_reason = _code(
        analysis.get("chapter_reason_code")
        or ("CHAPTER_RESOLVED" if chapter else "CHAPTER_UNCERTAIN"),
        "CHAPTER_UNCERTAIN",
    )
    structure_reason = _code(
        analysis.get("structure_reason_code")
        or ("STRUCTURE_CLASSIFIED" if structure_type else "STRUCTURE_UNCERTAIN"),
        "STRUCTURE_UNCERTAIN",
    )
    return {
        SECTION_QUESTION_CONTEXT: {
            "analysis_schema_version": _symbol(
                analysis.get("analysis_schema_version") or "a2-analysis-v1",
                "a2-analysis-v1",
            ),
            "recognized_text_excerpt": _text(
                analysis.get("visible_problem_text")
                or analysis.get("recognized_text_excerpt"),
                maximum=1_000,
            ),
            "category": _symbol(analysis.get("category") or "structural_mechanics", "structural_mechanics"),
        },
        SECTION_CHAPTER_DECISION: {
            "chapter": chapter,
            "confidence": float(analysis.get("chapter_confidence") or 0.0),
            "source": _symbol(analysis.get("chapter_source") or "a2_analysis", "a2_analysis"),
            "scope_status": scope_status,
            "reason_code": chapter_reason,
        },
        SECTION_LOAD_OBSERVATIONS: loads,
        SECTION_STRUCTURE_DECISION: {
            "structure_type": structure_type,
            "source": _symbol(analysis.get("structure_source") or "a2_analysis", "a2_analysis"),
            "filter_applicable": bool(analysis.get("structure_filter_applicable", False)),
            "reason_code": structure_reason,
            "confidence": float(analysis.get("structure_confidence") or 0.0),
        },
    }


def build_question_analyzed_checkpoint(
    context: A2CheckpointContextV1,
    analysis_result: ToolResult,
    *,
    occurred_at: str,
    expires_at: str,
    input_digests: Mapping[str, str],
    predecessor_checkpoint_id: str = "",
) -> IntermediateCheckpointV1:
    """Build the first A2 stage checkpoint from an analysis ToolResult."""

    result = (
        question_analyzed_result(analysis_result.data)
        if analysis_result.outcome is not ToolOutcome.ERROR
        else {}
    )
    return build_a2_checkpoint(
        context,
        stage=STAGE_QUESTION_ANALYZED,
        result=result,
        occurred_at=occurred_at,
        expires_at=expires_at,
        input_digests=input_digests,
        tool_result=analysis_result,
        predecessor_checkpoint_id=predecessor_checkpoint_id,
    )


__all__ = ["build_question_analyzed_checkpoint", "question_analyzed_result"]

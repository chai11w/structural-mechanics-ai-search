"""Whitelist projections from actual A2 stage results to Checkpoint V1."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any, Mapping, Sequence

from tiku_agent.checkpoint_capture import A2CheckpointContextV1, build_a2_checkpoint
from tiku_agent.checkpoint_contract import (
    OUTCOME_SKIPPED,
    SECTION_CANDIDATE_COUNTS,
    SECTION_CANDIDATE_SCORES,
    SECTION_CHAPTER_DECISION,
    SECTION_DIMENSION_OBSERVATIONS,
    SECTION_DELIVERY,
    SECTION_FILTER_DECISIONS,
    SECTION_LOAD_OBSERVATIONS,
    SECTION_QUESTION_CONTEXT,
    SECTION_RERANK_POLICY,
    SECTION_SELECTION,
    SECTION_STRUCTURE_DECISION,
    STAGE_ANSWER_PREPARED,
    STAGE_COARSE_SEARCH_COMPLETED,
    STAGE_RERANK_COMPLETED,
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
    classified = analysis.get("classified")
    classified = classified if isinstance(classified, Mapping) else {}
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
                or analysis.get("recognized_text_excerpt")
                or classified.get("visible_problem_text"),
                maximum=1_000,
            ),
            "category": _symbol(analysis.get("category") or "structural_mechanics", "structural_mechanics"),
        },
        SECTION_CHAPTER_DECISION: {
            "chapter": chapter,
            "confidence": float(analysis["chapter_confidence"]) if analysis.get("chapter_confidence") is not None else None,
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
            "confidence": float(analysis["structure_confidence"]) if analysis.get("structure_confidence") is not None else None,
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


def _candidate_id(item: Mapping[str, Any], rank: int) -> str:
    raw = str(item.get("name") or item.get("candidate_key") or f"candidate_{rank}")
    return "cand_" + sha256(raw.replace("\\", "/").casefold().encode("utf-8")).hexdigest()


def _score_item(item: Mapping[str, Any], rank: int, *, visible: bool | None = None, bank_catalog=None, bank_chapter="") -> dict[str, object]:
    score = item.get("score")
    result: dict[str, object] = {
        "candidate_id": _candidate_id(item, rank),
        "coarse_rank": int(item.get("coarse_rank") or item.get("rank") or rank),
        "coarse_score": float(score) if score is not None else None,
        "rerank_rank": int(item["rerank_rank"]) if item.get("rerank_rank") is not None else None,
        "rerank_score": float(item["rerank_score"]) if item.get("rerank_score") is not None else None,
        "final_score": float(item["final_score"]) if item.get("final_score") is not None else None,
        "score_status": str(item.get("rerank_status") or ("completed" if score is not None else "missing")),
        "reason_code": _code(item.get("reason_code"), "SCORE_AVAILABLE"),
        "structure_type": (
            str(item.get("structure_type") or "")
            if str(item.get("structure_type") or "") in _STRUCTURE_TYPES
            else ""
        ),
        "long_width": _text(item.get("long_width"), maximum=200),
        "single_side": _text(item.get("single_side"), maximum=200),
    }
    if visible is not None:
        result["visible"] = bool(visible)
    if bank_catalog is not None:
        result["question_ref"] = bank_catalog.reference(item["path"], chapter=item.get("chapter") or bank_chapter).to_dict()
    return result


def _dimension_observations(data: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    trace = data.get("dimension_filter")
    trace = trace if isinstance(trace, Mapping) else {}
    observations: list[dict[str, str]] = []
    for kind, raw_value in (("long_width", trace.get("long_width")), ("single_side", trace.get("single_side"))):
        raw = _text(raw_value, maximum=200)
        status = "recognized" if raw else (
            "not_run" if not trace.get("triggered") else
            "conflict" if trace.get("query_state") == "conflict" else
            "uncertain" if trace.get("reason") == "recognition_failed" else "missing"
        )
        observations.append({
            "kind": kind,
            "raw": raw,
            "normalized": raw,
            "unit": "",
            "source": "dimension_filter",
            "status": status,
            "reason_code": "DIMENSION_" + status.upper(),
        })
    return tuple(observations)


def coarse_search_result(
    search_result: ToolResult,
    *,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    bank_catalog=None,
    bank_chapter="",
) -> dict[str, object]:
    """Project an A2 coarse-search result without exposing paths or raw errors."""

    if search_result.outcome is ToolOutcome.ERROR:
        return {}
    data = search_result.data if isinstance(search_result.data, Mapping) else {}
    items = list(candidates if candidates is not None else data.get("candidates") or [])
    stored = tuple(_score_item(item, index, bank_catalog=bank_catalog, bank_chapter=bank_chapter) for index, item in enumerate(items[:50], 1))
    dimension = _dimension_observations(data)
    filtered = data.get("dimension_filter")
    filtered = filtered if isinstance(filtered, Mapping) else {}
    before = int(filtered.get("before", len(items)))
    after = int(filtered.get("after", len(items)))
    counts = {
        **data["checkpoint_counts"],
        "stored_score_count": len(stored),
        "scores_truncated": len(stored) < data["checkpoint_counts"]["after_dimension_filter"],
    }
    if data.get("remaining_candidate_count") is not None:
        counts["remaining"] = max(0, int(data["remaining_candidate_count"]))
    filter_status = "applied" if filtered.get("applied") else "fallback" if filtered.get("reason") == "recognition_failed" else "skipped"
    filter_reason = _code(filtered.get("reason"), "DIMENSION_FILTER_SKIPPED")
    return {
        SECTION_DIMENSION_OBSERVATIONS: dimension,
        SECTION_CANDIDATE_COUNTS: counts,
        SECTION_FILTER_DECISIONS: ({
            "filter": "dimension",
            "status": filter_status,
            "before": before,
            "after": after,
            "reason_code": filter_reason,
            "policy_version": "dimension-v1",
        },),
        SECTION_CANDIDATE_SCORES: stored,
    }


def build_coarse_search_checkpoint(
    context: A2CheckpointContextV1,
    search_result: ToolResult,
    *,
    occurred_at: str,
    expires_at: str,
    input_digests: Mapping[str, str],
    candidates: Sequence[Mapping[str, Any]] | None = None,
    bank_catalog=None,
    bank_chapter="",
    predecessor_checkpoint_id: str = "",
) -> IntermediateCheckpointV1:
    if search_result.outcome is ToolOutcome.NEEDS_INPUT:
        search_result = ToolResult(outcome=ToolOutcome.ERROR, code=search_result.code, error_category="invalid_tool_input")
    result = coarse_search_result(search_result, candidates=candidates, bank_catalog=bank_catalog, bank_chapter=bank_chapter)
    return build_a2_checkpoint(
        context,
        stage=STAGE_COARSE_SEARCH_COMPLETED,
        result=result,
        occurred_at=occurred_at,
        expires_at=expires_at,
        input_digests=input_digests,
        tool_result=search_result,
        predecessor_checkpoint_id=predecessor_checkpoint_id,
    )


def rerank_result(
    rerank_result_value: ToolResult,
    *,
    candidates: Sequence[Mapping[str, Any]],
    threshold: float = 0.8,
    display_all_score: float = 0.95,
    fallback_limit: int = 3,
    bank_catalog=None,
    bank_chapter="",
) -> dict[str, object]:
    """Project rerank policy and visible candidate scores with bounded detail."""

    if rerank_result_value.outcome is ToolOutcome.ERROR:
        return {}
    data = rerank_result_value.data if isinstance(rerank_result_value.data, Mapping) else {}
    evidence = data["checkpoint_rerank"]
    visible_items = list(data.get("visible_candidates") or [])
    input_items = list(evidence["inputs"])
    scored_items = list(evidence["scores"])
    completed = bool(data.get("reranked")) and rerank_result_value.outcome in {
        ToolOutcome.SUCCESS,
        ToolOutcome.NO_MATCH,
    }
    fallback = rerank_result_value.outcome is ToolOutcome.PARTIAL
    scores_by_id = {_candidate_id(item, rank): {**item, "rerank_rank": rank if completed else None} for rank, item in enumerate(scored_items, 1)}
    visible_ids = {_candidate_id(item, rank) for rank, item in enumerate(visible_items, 1)}
    scores_source = [
        {**item, **scores_by_id.get(_candidate_id(item, rank), {}), "coarse_rank": item.get("coarse_rank") or item.get("rank") or rank}
        for rank, item in enumerate(input_items, 1)
    ]
    # Preserve every displayed candidate before filling the bounded diagnostic tail.
    scores_source.sort(key=lambda item: _candidate_id(item, 1) not in visible_ids)
    scores = tuple(
        _score_item(item, index, visible=(_candidate_id(item, index) in visible_ids), bank_catalog=bank_catalog, bank_chapter=bank_chapter)
        for index, item in enumerate(scores_source[:50], 1)
    )
    outcome_no_match = rerank_result_value.outcome is ToolOutcome.NO_MATCH
    return {
        SECTION_RERANK_POLICY: {
            "reranked": completed,
            "input_count": len(input_items),
            "completed_count": sum(item.get("rerank_status") not in {"failed", "timeout", "incomplete"} for item in scored_items),
            "failed_count": sum(item.get("rerank_status") in {"failed", "timeout", "incomplete"} for item in scored_items),
            "threshold": float(evidence["threshold"]),
            "display_all_score": float(evidence["display_all_score"]),
            "fallback_limit": int(evidence["fallback_limit"]),
            "fallback_used": bool(fallback),
            "reason_code": _code(rerank_result_value.code, "RERANK_FAILED"),
            "policy_version": "rerank-v1",
            "visible": 0 if outcome_no_match else len(visible_items),
            "stored_score_count": len(scores),
            "scores_truncated": len(scores) < len(scores_source),
        },
        SECTION_CANDIDATE_SCORES: scores,
    }


def build_rerank_checkpoint(
    context: A2CheckpointContextV1,
    rerank_result_value: ToolResult,
    *,
    candidates: Sequence[Mapping[str, Any]],
    bank_catalog=None,
    bank_chapter="",
    occurred_at: str,
    expires_at: str,
    input_digests: Mapping[str, str],
    predecessor_checkpoint_id: str = "",
) -> IntermediateCheckpointV1:
    skipped = rerank_result_value.code == "RERANK_SKIPPED_NO_IMAGE"
    return build_a2_checkpoint(
        context,
        stage=STAGE_RERANK_COMPLETED,
        result=rerank_result(rerank_result_value, candidates=candidates, bank_catalog=bank_catalog, bank_chapter=bank_chapter),
        occurred_at=occurred_at,
        expires_at=expires_at,
        input_digests=input_digests,
        tool_result=None if skipped else rerank_result_value,
        outcome=OUTCOME_SKIPPED if skipped else None,
        predecessor_checkpoint_id=predecessor_checkpoint_id,
    )


def answer_prepared_result(
    answer_result: ToolResult,
    *,
    selected_rank: int,
    selected_candidate: Mapping[str, Any] | None,
    candidate_generation: str,
    answer_artifact_count: int = 0,
    response_id: str = "",
    answer_refs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    """Project answer delivery, adding selection only for a real candidate."""

    data: dict[str, object] = {
        SECTION_DELIVERY: {
            "answer_artifact_count": max(0, int(answer_artifact_count)),
            "media_status": "partial" if answer_result.outcome is ToolOutcome.PARTIAL else "complete" if answer_artifact_count else "not_available",
            "delivery_code": "NO_MATCH" if answer_result.outcome is ToolOutcome.NO_MATCH else _code(
                answer_result.code,
                "ANSWER_READY" if answer_artifact_count else "NO_MATCH",
            ),
        }
    }
    if response_id:
        data[SECTION_DELIVERY]["response_id"] = response_id
    if answer_refs is not None:
        data[SECTION_DELIVERY]["answer_refs"] = list(answer_refs)
        data[SECTION_DELIVERY]["media_status"] = (
            "partial" if answer_result.outcome is ToolOutcome.PARTIAL
            else "complete" if answer_refs else "not_available"
        )
    if answer_result.outcome is not ToolOutcome.NO_MATCH and selected_candidate is not None:
        data[SECTION_SELECTION] = {
            "candidate_id": _candidate_id(selected_candidate, selected_rank),
            "selected_rank": int(selected_rank),
            "candidate_generation": candidate_generation,
            "selection_source": "user",
        }
    return data


def build_answer_prepared_checkpoint(
    context: A2CheckpointContextV1,
    answer_result: ToolResult,
    *,
    selected_rank: int,
    selected_candidate: Mapping[str, Any] | None,
    candidate_generation: str,
    occurred_at: str,
    expires_at: str,
    input_digests: Mapping[str, str],
    answer_artifacts: Sequence[Any] = (),
    answer_refs: Sequence[Mapping[str, Any]] | None = None,
    response_id: str = "",
    predecessor_checkpoint_id: str = "",
) -> IntermediateCheckpointV1:
    if answer_result.outcome is ToolOutcome.NEEDS_INPUT:
        answer_result = ToolResult(outcome=ToolOutcome.ERROR, code=answer_result.code, error_category="invalid_tool_input")
    result = answer_prepared_result(
        answer_result,
        selected_rank=selected_rank,
        selected_candidate=selected_candidate,
        candidate_generation=candidate_generation,
        answer_artifact_count=len(answer_artifacts),
        answer_refs=answer_refs,
        response_id=response_id,
    )
    return build_a2_checkpoint(
        context,
        stage=STAGE_ANSWER_PREPARED,
        result=result,
        occurred_at=occurred_at,
        expires_at=expires_at,
        input_digests=input_digests,
        artifacts=answer_artifacts,
        tool_result=answer_result,
        predecessor_checkpoint_id=predecessor_checkpoint_id,
    )
__all__ = [
    "answer_prepared_result",
    "build_answer_prepared_checkpoint",
    "build_coarse_search_checkpoint",
    "build_question_analyzed_checkpoint",
    "build_rerank_checkpoint",
    "coarse_search_result",
    "question_analyzed_result",
    "rerank_result",
]

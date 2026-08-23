"""Coarse Agent tools for structure-mechanics question-bank retrieval.

The first Agent layer is deliberately boring: each function wraps existing
project logic and returns structured data. It does not touch the current Feishu
bot runtime, and search tools avoid writing `_last_search.json`.
"""

from __future__ import annotations

import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Literal

import search
from multi_agent_pipeline import (
    AUTO_CHAPTER_MIN_CONFIDENCE,
    CHAPTER_UNKNOWN,
    QwenClassifier,
    RuleRouter,
    apply_dimension_prefilter,
    infer_structure_type_from_text,
    load_bank_excel,
    normalize_rerank_results,
    normalize_structure_type,
    resolve_effective_chapter,
    select_rerank_candidates,
    symbolic_root,
)
from tiku_shared.multi_question import (
    effective_question_chapter,
    normalize_multi_questions,
    normalize_question_key,
    prepare_multi_diagram_crops,
)
from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.tool_result import ToolOutcome, ToolResult
from tiku_shared.request_protocol import RequestAction
from tiku_shared.model_costs import submit_with_model_cost_context


BASE = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2"

STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱"}


@dataclass
class AgentToolConfig:
    """Runtime paths for the new Agent tool layer.

    Keep these paths separate from `.tmp_feishu_tiku` and the current Feishu
    bot's session/log directories.
    """

    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    session_dir: Path | None = None
    top_k: int = search.TOP_K
    rerank_top: int = search.DISPLAY_MAX_RESULTS
    global_coarse_threshold: float = 0.999
    global_final_score_threshold: float = 0.95
    global_rerank_workers: int = 10
    global_candidate_timeout_seconds: float = 15.0
    global_retry_incomplete_once: bool = True
    use_qwen_cache: bool = True
    dimension_filter_enabled: bool = False
    dimension_filter_timeout_seconds: int = 30

    @property
    def qwen_cache_path(self) -> Path:
        return self.runtime_dir / "qwen_classifier_cache.json"

    @property
    def qwen_dimension_cache_path(self) -> Path:
        return self.runtime_dir / "qwen_dimension_cache.json"

    @property
    def answer_output_dir(self) -> Path:
        return (self.session_dir or self.runtime_dir) / "answer_output"

    @property
    def multi_diagram_dir(self) -> Path:
        return (self.session_dir or self.runtime_dir) / "multi_diagrams"


def _make_qwen(config: AgentToolConfig) -> QwenClassifier:
    return QwenClassifier(
        cache_path=config.qwen_cache_path,
        dimension_cache_path=config.qwen_dimension_cache_path,
        dimension_timeout=config.dimension_filter_timeout_seconds,
        use_cache=config.use_qwen_cache,
    )


def _named_tool(name: str):
    """Guarantee that every return path identifies its tool boundary."""

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            return function(*args, **kwargs).with_tool(name)

        return wrapped

    return decorate


@_named_tool("analyze_image")
def analyze_image_tool(
    image_path: str | Path,
    *,
    chapter: str | None = "auto",
    include_layout: bool = False,
    context_text: str = "",
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Analyze a question image for layout, chapter hint, and loads.

    This is the coarse first-version tool: it can optionally call layout
    analysis, then calls the existing Qwen load/chapter classifier.
    """

    config = config or AgentToolConfig()
    qwen = _make_qwen(config)
    path = Path(image_path)
    try:
        layout = qwen.analyze_layout(path) if include_layout else {"layout": "unknown"}
        classified = (
            qwen.classify_image(path, context_text=context_text)
            if context_text
            else qwen.classify_image(path)
        )
        effective_chapter = resolve_effective_chapter(chapter, classified)
        needs_manual_chapter = effective_chapter is None
        data = {
                "image_path": str(path),
                "layout": layout,
                "classified": classified,
                "chapter": effective_chapter,
                "chapter_hint": classified.get("chapter_hint", CHAPTER_UNKNOWN),
                "chapter_confidence": classified.get("chapter_confidence", 0.0),
                "chapter_evidence": classified.get("chapter_evidence", ""),
                "chapter_auto_min_confidence": AUTO_CHAPTER_MIN_CONFIDENCE,
                "needs_manual_chapter": needs_manual_chapter,
                "loads": classified.get("loads", []),
                "load_details": classified.get("load_details", []),
            }
        if needs_manual_chapter:
            return ToolResult.needs_input(
                code="CHAPTER_REQUIRED",
                error="无法可靠判断章节，请选择第2至第8章。",
                data=data,
                next_state="WAIT_CHAPTER",
            )
        return ToolResult.success(
            code="IMAGE_ANALYZED",
            data=data,
            next_state="READY_TO_ROUTE",
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary returns structured errors.
        del exc
        return ToolResult.tool_error(
            code="IMAGE_ANALYSIS_FAILED",
            error="题图识别暂时失败，请稍后重试。",
            retryable=True,
            error_category="external_model",
        )


@_named_tool("analyze_multi_image")
def analyze_multi_image_tool(
    image_path: str | Path,
    *,
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Only detect whether an image contains multiple questions and list them."""
    config = config or AgentToolConfig()
    path = Path(image_path)
    try:
        layout = _make_qwen(config).analyze_image_scope(path)
        if layout.get("question_layout") != "multi":
            return ToolResult.success(
                code="SINGLE_QUESTION_DETECTED",
                data={"is_multi": False, "layout": layout, "single_analysis": layout.get("single_analysis"), "questions": []},
                next_state="READY_FOR_SINGLE_ANALYSIS",
            )

        return ToolResult.success(
            code="MULTI_QUESTION_DETECTED",
            data={"is_multi": True, "layout": layout, "questions": []},
            next_state="READY_FOR_MULTI_DETAILS",
        )
    except Exception as exc:  # noqa: BLE001 - keep the single-question flow usable.
        del exc
        return ToolResult.partial(
            code="MULTI_DETECTION_FALLBACK",
            data={"is_multi": False, "questions": []},
            error="多题判断未完成，已按单题流程继续。",
            next_state="READY_FOR_SINGLE_ANALYSIS",
            retryable=False,
            error_category="external_model",
        )


@_named_tool("prepare_question_units")
def prepare_question_units_tool(
    image_path: str | Path,
    questions: list[dict[str, Any]],
    *,
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """After multi is confirmed, locate each question then prepare rerank-safe crops."""
    config = config or AgentToolConfig()
    path = Path(image_path)
    try:
        layout = _make_qwen(config).analyze_layout(path)
        questions = normalize_multi_questions(layout.get("questions", []))
        if layout.get("question_layout") != "multi" or len(questions) < 2:
            return ToolResult.tool_error(
                code="MULTI_DETAIL_INVALID",
                error="多题详细识别未得到至少两道题。",
                retryable=True,
                error_category="external_model",
            )
    except Exception as exc:  # noqa: BLE001
        del exc
        return ToolResult.tool_error(
            code="MULTI_DETAIL_FAILED",
            error="多题详细识别暂时失败，请稍后重试。",
            retryable=True,
            error_category="external_model",
        )
    analyzed_questions = []
    for index, question in enumerate(questions, 1):
        item = dict(question)
        item["question_index"] = index
        item["chapter"] = effective_question_chapter(item, CHAPTERS) or ""
        analyzed_questions.append(item)
    questions = analyzed_questions
    prepared = []
    try:
        crops = prepare_multi_diagram_crops(path, questions, config.multi_diagram_dir)
    except Exception as exc:  # noqa: BLE001 - load-only retrieval stays available.
        crops = {}
        crop_error = str(exc)
    else:
        crop_error = ""

    for index, question in enumerate(questions, 1):
        item = dict(question)
        item["question_index"] = index
        item["question_image_path"] = crops.get(normalize_question_key(item.get("label")), "")
        item["chapter"] = str(item.get("chapter") or effective_question_chapter(item, CHAPTERS) or "")
        prepared.append(item)
    data = {
        "questions": prepared,
        "diagram_crops": crops,
        "has_reliable_crops": bool(crops),
    }
    if crop_error:
        return ToolResult.partial(
            code="MULTI_CROPS_UNAVAILABLE",
            data=data,
            error="部分题图裁剪未完成，仍可按题号继续。",
            next_state="WAIT_QUESTION_CHOICE",
            retryable=True,
            error_category="image_processing",
        )
    return ToolResult.success(
        code="QUESTION_UNITS_PREPARED",
        data=data,
        next_state="WAIT_QUESTION_CHOICE",
    )


@_named_tool("route_bank")
def route_bank_tool(loads: list[dict[str, Any]]) -> ToolResult:
    """Decide whether to search the main bank, symbolic bank, or review lane."""

    try:
        route, load_details = RuleRouter().route(loads)
        data = {
                "route": route.route,
                "category": route.category,
                "reason": route.reason,
                "excel_root": str(route.excel_root) if route.excel_root else "",
                "load_details": load_details,
            }
        if route.route == "needs_review":
            if route.category == "mixed_symbolic_numeric":
                return ToolResult.needs_input(
                    code="LOAD_ROUTE_MIXED_REVIEW_REQUIRED",
                    error=route.reason or "mixed symbolic and numeric load",
                    data=data,
                    next_state="WAIT_INPUT",
                    safe_facts={
                        "load_representation": "mixed",
                        "has_numeric_load": True,
                        "has_unassigned_symbolic_load": True,
                        "automatic_search_supported": False,
                    },
                    action=RequestAction.RETRY_UPLOAD,
                )
            return ToolResult.needs_input(
                code="LOAD_ROUTE_INPUT_UNUSABLE",
                error=route.reason or "荷载信息不足，无法安全选择题库。",
                data=data,
                next_state="WAIT_INPUT",
                safe_facts={
                    "load_representation": "unknown",
                    "automatic_search_supported": False,
                },
                action=RequestAction.RETRY_UPLOAD,
            )
        return ToolResult.success(
            code="BANK_ROUTE_SELECTED",
            data=data,
            next_state=(
                "READY_FOR_STRUCTURE"
                if route.route == "symbolic"
                else "READY_FOR_COARSE_SEARCH"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        del exc
        return ToolResult.tool_error(
            code="BANK_ROUTE_FAILED",
            error="题库路由暂时失败，请稍后重试。",
            retryable=False,
            error_category="internal_logic",
        )


@_named_tool("classify_structure")
def classify_structure_tool(
    image_path: str | Path | None,
    *,
    route: str,
    classified: dict[str, Any] | None = None,
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Classify structure type for symbolic-bank image searches.

    Returns an empty structure type when the route does not benefit from this
    filter, so callers can always invoke it safely.
    """

    if route != "symbolic":
        return ToolResult.success(
            code="STRUCTURE_FILTER_NOT_APPLICABLE",
            data={"structure_type": "", "source": "not_applicable", "filter_applicable": False},
            next_state="READY_FOR_COARSE_SEARCH",
        )

    text_structure = infer_structure_type_from_text(classified)
    if text_structure:
        return ToolResult.success(
            code="STRUCTURE_CLASSIFIED_FROM_TEXT",
            data={
                "structure_type": text_structure,
                "confidence": 1.0,
                "reason": "题干文字",
                "source": "text_fast_path",
                "filter_applicable": True,
            },
            next_state="READY_FOR_COARSE_SEARCH",
        )

    if not image_path:
        return ToolResult.partial(
            code="STRUCTURE_FILTER_SKIPPED_NO_IMAGE",
            data={"structure_type": "", "source": "missing_image", "filter_applicable": False},
            error="缺少题图，已跳过结构类型筛选。",
            next_state="READY_FOR_COARSE_SEARCH",
        )

    config = config or AgentToolConfig()
    try:
        structure = _make_qwen(config).classify_structure_type(image_path)
        structure_type = normalize_structure_type(structure.get("structure_type"))
        data = {
                "structure_type": structure_type,
                "confidence": structure.get("confidence", 0.0),
                "reason": structure.get("reason", ""),
                "source": "vision",
                "filter_applicable": bool(structure_type),
            }
        if not structure_type:
            return ToolResult.partial(
                code="STRUCTURE_TYPE_UNCERTAIN",
                data=data,
                error="结构类型无法可靠确定，已跳过该筛选。",
                next_state="READY_FOR_COARSE_SEARCH",
                error_category="model_uncertain",
            )
        return ToolResult.success(
            code="STRUCTURE_CLASSIFIED_FROM_IMAGE",
            data=data,
            next_state="READY_FOR_COARSE_SEARCH",
        )
    except Exception as exc:  # noqa: BLE001 - optional speed-up; search can continue.
        del exc
        return ToolResult.partial(
            code="STRUCTURE_CLASSIFICATION_FALLBACK",
            data={"structure_type": "", "source": "vision_failed", "filter_applicable": False},
            error="结构类型识别未完成，已跳过该筛选。",
            next_state="READY_FOR_COARSE_SEARCH",
            retryable=True,
            error_category="external_model",
        )


@_named_tool("coarse_search")
def coarse_search_tool(
    loads: list[dict[str, Any]],
    *,
    chapter: str,
    route: Literal["main", "symbolic"],
    structure_type: str = "",
    top_k: int | None = None,
    exclude_candidate_keys: list[str] | None = None,
    query_image_path: str | Path | None = None,
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Run read-only coarse search without writing `_last_search.json`.

    Unlike `MultiAgentCoordinator.search_loads`, this does not write the last
    search cache. Unlike `rank_bank_candidates`, it does not auto-repair live
    Excel paths.
    """

    config = config or AgentToolConfig()
    try:
        excel_root = search.ROOT if route == "main" else symbolic_root(search.ROOT)
        filter_type = normalize_structure_type(structure_type)
        scan = search.scan_chapter_candidates(
            loads,
            chapter,
            excel_root,
            structure_type=filter_type if route == "symbolic" else "",
            load_excel=load_bank_excel,
        )
        if scan is None:
            return ToolResult.needs_input(
                code="UNKNOWN_CHAPTER",
                error="指定章节不存在，请选择第2至第8章。",
                data={"chapter": chapter, "route": route},
                next_state="WAIT_CHAPTER",
            )
        excluded = {str(key).strip() for key in (exclude_candidate_keys or []) if str(key).strip()}
        scored: list[tuple[float, str, str]] = []
        for score, name in scan.scored:
            candidate_key = _candidate_key(chapter, route, name)
            if candidate_key not in excluded:
                scored.append((score, name, candidate_key))
        scored = [item for item in scored if item[0] > 0]
        top = search.select_coarse_results(scored)
        has_more = len(scored) > len(top)

        candidates = []
        dimensions_by_name = getattr(scan, "dimensions_by_name", {})
        for rank, (score, name, candidate_key) in enumerate(top, 1):
            path, resolved_name, repaired = search.resolve_question_path(
                name,
                chapter_name=chapter,
                update_excel=False,
            )
            dimension_data = dimensions_by_name.get(name, {})
            candidates.append(
                {
                    "rank": rank,
                    "path": str(path),
                    "name": resolved_name,
                    "score": score,
                    "route": route,
                    "chapter": chapter,
                    "structure_type": filter_type if scan.structure_filter_applied else "",
                    "structure_filter": scan.structure_filter_applied,
                    "path_repaired_in_memory": repaired,
                    "candidate_key": candidate_key,
                    "long_width": dimension_data.get("long_width", ""),
                    "single_side": dimension_data.get("single_side", ""),
                }
            )

        candidates, dimension_filter = apply_dimension_prefilter(
            candidates,
            enabled=config.dimension_filter_enabled,
            route=route,
            structure_type=filter_type,
            query_image_path=query_image_path,
            recognizer=_make_qwen(config) if config.dimension_filter_enabled else None,
        )

        data = {
                "chapter": chapter,
                "route": route,
                "structure_type": filter_type,
                "structure_filter_applied": scan.structure_filter_applied,
                "candidates": candidates,
                "has_more": has_more,
                "remaining_candidate_count": max(0, len(scored) - len(top)),
                "dimension_filter": dimension_filter,
            }
        if not candidates:
            return ToolResult.no_match(
                code="NO_COARSE_CANDIDATES",
                data=data,
            )
        return ToolResult.success(
            code="COARSE_CANDIDATES_FOUND",
            data=data,
            next_state="READY_FOR_RERANK",
        )
    except Exception as exc:  # noqa: BLE001
        del exc
        return ToolResult.tool_error(
            code="COARSE_SEARCH_FAILED",
            error="题库粗筛暂时失败，请稍后重试。",
            retryable=True,
            error_category="local_data",
        )


def _candidate_key(chapter: str, route: str, name: str) -> str:
    normalized_name = str(name).replace("\\", "/").strip().casefold()
    return f"{chapter}|{route}|{normalized_name}"


@_named_tool("global_search")
def global_search_tool(
    loads: list[dict[str, Any]],
    query_image_path: str | Path | None,
    *,
    route: Literal["main", "symbolic"],
    structure_type: str = "",
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Strict read-only search across every supported chapter.

    All content-deduplicated candidates with a coarse score at or above the
    configured perfect-match threshold are visually scored. Concurrency is
    bounded, but the total candidate pool and returned result count are not.
    """

    config = config or AgentToolConfig()
    if not query_image_path or not Path(query_image_path).is_file():
        return ToolResult.needs_input(
            code="GLOBAL_SEARCH_IMAGE_REQUIRED",
            error="全局搜索需要当前题图，请重新上传题目。",
            next_state="WAIT_IMAGE",
        )
    if route not in {"main", "symbolic"}:
        return ToolResult.tool_error(
            code="GLOBAL_SEARCH_UNSUPPORTED_ROUTE",
            error="当前题库路由不支持全局搜索。",
            data={"route": route},
            retryable=False,
            error_category="invalid_tool_input",
        )

    try:
        candidates = _collect_global_perfect_candidates(
            loads,
            route=route,
            structure_type=structure_type,
            threshold=config.global_coarse_threshold,
        )
        coarse_candidate_count = len(candidates)
        candidates, dimension_filter = apply_dimension_prefilter(
            candidates,
            enabled=config.dimension_filter_enabled,
            route=route,
            structure_type=normalize_structure_type(structure_type),
            query_image_path=query_image_path,
            recognizer=(
                _make_qwen(config)
                if config.dimension_filter_enabled and route == "symbolic"
                else None
            ),
        )
        if not candidates:
            return ToolResult.no_match(
                code="NO_GLOBAL_COARSE_CANDIDATES",
                data={
                    "candidates": [],
                    "coarse_candidate_count": coarse_candidate_count,
                    "rerank_candidate_count": 0,
                    "dimension_filter": dimension_filter,
                    "model_calls": 0,
                    "retry_model_calls": 0,
                },
            )

        scored = _score_global_candidates(query_image_path, candidates, config=config)
        retry_model_calls = 0
        unfinished = [
            item for item in scored if item.get("rerank_status") != "completed"
        ]
        if unfinished and config.global_retry_incomplete_once:
            originals_by_hash = {item["content_hash"]: item for item in candidates}
            retry_candidates = [
                originals_by_hash[item["content_hash"]]
                for item in unfinished
                if item.get("content_hash") in originals_by_hash
            ]
            retried = _score_global_candidates(
                query_image_path,
                retry_candidates,
                config=config,
            )
            retry_model_calls = len(retry_candidates)
            retried_by_hash = {item["content_hash"]: item for item in retried}
            scored = [
                retried_by_hash.get(item.get("content_hash"), item)
                for item in scored
            ]

        unfinished = [
            item for item in scored if item.get("rerank_status") != "completed"
        ]
        if unfinished:
            return ToolResult.partial(
                code="GLOBAL_RERANK_INCOMPLETE",
                data={
                    "coarse_candidate_count": coarse_candidate_count,
                    "rerank_candidate_count": len(candidates),
                    "dimension_filter": dimension_filter,
                    "model_calls": len(candidates) + retry_model_calls,
                    "retry_model_calls": retry_model_calls,
                    "unfinished_candidates": len(unfinished),
                },
                error="部分全局候选复筛未完成，请稍后重试。",
                next_state="ERROR",
                retryable=True,
                error_category="external_model",
            )

        visible = [
            item
            for item in scored
            if float(item.get("final_score") or 0)
            >= config.global_final_score_threshold
        ]
        visible.sort(
            key=lambda item: (
                float(item.get("final_score") or 0),
                float(item.get("score") or 0),
                float(item.get("rerank_score") or 0),
                -int(item.get("rank") or 0),
            ),
            reverse=True,
        )
        visible = _renumber(visible)
        data = {
                "candidates": visible,
                "coarse_candidate_count": coarse_candidate_count,
                "rerank_candidate_count": len(candidates),
                "dimension_filter": dimension_filter,
                "model_calls": len(candidates) + retry_model_calls,
                "retry_model_calls": retry_model_calls,
                "unfinished_candidates": 0,
            }
        if not visible:
            return ToolResult.no_match(
                code="NO_GLOBAL_RELIABLE_CANDIDATES",
                data=data,
            )
        return ToolResult.success(
            code="GLOBAL_CANDIDATES_FOUND",
            data=data,
            next_state="WAIT_CANDIDATE_CHOICE",
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary returns a safe error.
        del exc
        return ToolResult.tool_error(
            code="GLOBAL_SEARCH_FAILED",
            error="全局搜索暂时失败，请稍后重试。",
            retryable=True,
            error_category="search_pipeline",
        )


def _score_global_candidates(
    query_image_path: str | Path,
    candidates: list[dict[str, Any]],
    *,
    config: AgentToolConfig,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    workers = max(1, min(config.global_rerank_workers, len(candidates)))
    scored = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            submit_with_model_cost_context(
                executor,
                search.score_rerank_candidate,
                str(query_image_path),
                candidate,
                timeout_seconds=config.global_candidate_timeout_seconds,
                collect_timing=True,
            ): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                scored.append(future.result())
            except Exception:  # noqa: BLE001 - normalize scorer failure for one bounded retry.
                failed = dict(candidate)
                failed.update({"rerank_status": "error", "rerank_score": None})
                scored.append(failed)
    return scored


def _collect_global_perfect_candidates(
    loads: list[dict[str, Any]],
    *,
    route: Literal["main", "symbolic"],
    structure_type: str,
    threshold: float,
) -> list[dict[str, Any]]:
    excel_root = search.ROOT if route == "main" else symbolic_root(search.ROOT)
    filter_type = normalize_structure_type(structure_type)
    by_content: dict[str, dict[str, Any]] = {}

    for chapter in CHAPTERS:
        scan = search.scan_chapter_candidates(
            loads,
            chapter,
            excel_root,
            structure_type=filter_type if route == "symbolic" else "",
            load_excel=load_bank_excel,
        )
        if scan is None:
            continue
        dimensions_by_name = getattr(scan, "dimensions_by_name", {})
        for score, name in scan.scored:
            if score < threshold:
                continue
            path, resolved_name, _ = search.resolve_question_path(
                name,
                chapter_name=chapter,
                update_excel=False,
            )
            if not path.is_file():
                continue
            content_hash = _file_sha256(path)
            existing = by_content.get(content_hash)
            if existing is not None:
                existing["source_chapters"].add(chapter)
                _merge_global_dimension_metadata(
                    existing,
                    dimensions_by_name.get(name, {}),
                )
                continue
            dimension_data = dimensions_by_name.get(name, {})
            by_content[content_hash] = {
                "path": str(path),
                "name": resolved_name,
                "score": float(score),
                "route": route,
                "chapter": chapter,
                "source_chapters": {chapter},
                "content_hash": content_hash,
                "long_width": str(dimension_data.get("long_width") or "").strip(),
                "single_side": str(dimension_data.get("single_side") or "").strip(),
                "dimension_metadata_conflict": False,
            }

    candidates = sorted(
        by_content.values(),
        key=lambda item: (sorted(item["source_chapters"]), item["content_hash"]),
    )
    for rank, candidate in enumerate(candidates, 1):
        chapters = sorted(candidate["source_chapters"])
        candidate["rank"] = rank
        candidate["chapter"] = chapters[0]
        candidate["source_chapters"] = chapters
    return candidates


def _merge_global_dimension_metadata(
    candidate: dict[str, Any],
    incoming: dict[str, Any] | None,
) -> None:
    """Merge duplicate-image dimension metadata without creating a hard-delete risk."""

    if candidate.get("dimension_metadata_conflict"):
        return
    incoming = incoming or {}
    current_pair = (
        str(candidate.get("long_width") or "").strip(),
        str(candidate.get("single_side") or "").strip(),
    )
    incoming_pair = (
        str(incoming.get("long_width") or "").strip(),
        str(incoming.get("single_side") or "").strip(),
    )
    if not any(incoming_pair):
        return
    if not any(current_pair):
        candidate["long_width"], candidate["single_side"] = incoming_pair
        return
    if current_pair == incoming_pair:
        return

    # The same image has conflicting index metadata in different chapters.
    # Blank both values so the conservative dimension layer keeps it as unknown.
    candidate["long_width"] = ""
    candidate["single_side"] = ""
    candidate["dimension_metadata_conflict"] = True


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@_named_tool("rerank_candidates")
def rerank_candidates_tool(
    query_image_path: str | Path | None,
    candidates: list[dict[str, Any]],
    *,
    route: str,
    rerank_top: int = search.DISPLAY_MAX_RESULTS,
    force_rerank: bool = False,
    rerank_provider: str | None = None,
    rerank_model: str | None = None,
    max_workers: int | None = None,
    candidate_timeout_seconds: float | None = None,
    retry_timeout_seconds: float | None = None,
    retry_max_candidates: int | None = None,
    retry_max_workers: int | None = None,
    retry_failed_candidates: bool = False,
    display_by_rerank_score: bool = False,
    display_all_score: float = 0.95,
    display_fallback_top_n: int = search.DISPLAY_MAX_RESULTS,
) -> ToolResult:
    """Rerank coarse candidates and return visible candidates only.

    This tool does not answer automatically. The Agent must wait for a user
    candidate choice after this step.
    """

    if not candidates:
        return ToolResult.no_match(
            code="NO_CANDIDATES_TO_RERANK",
            data={"reranked": False, "visible_candidates": []},
        )
    coarse_candidates = (
        search.select_rerank_pool(candidates)
        if query_image_path
        else search.select_coarse_results(candidates)
    )
    if not query_image_path:
        return ToolResult.partial(
            code="RERANK_SKIPPED_NO_IMAGE",
            data={"reranked": False, "visible_candidates": _renumber(coarse_candidates), "rerank_note": "无查询图，跳过复筛"},
            error="缺少查询题图，已显示粗筛结果。",
            next_state="WAIT_CANDIDATE_CHOICE",
            error_category="missing_optional_input",
        )

    try:
        rerank_input = select_rerank_candidates(
            coarse_candidates,
            route,
            preserve_bounded_pool=True,
        )
        if not rerank_input:
            return ToolResult.success(
                code="RERANK_NOT_REQUIRED",
                data={"reranked": False, "visible_candidates": _renumber(coarse_candidates), "rerank_note": "候选未达到复筛阈值，已显示粗筛结果。"},
                next_state="WAIT_CANDIDATE_CHOICE",
            )
        rerank_options: dict[str, Any] = {"top_n": rerank_top}
        optional_policy = {
            "provider": rerank_provider,
            "model": rerank_model,
            "max_workers": max_workers,
            "candidate_timeout_seconds": candidate_timeout_seconds,
            "retry_timeout_seconds": retry_timeout_seconds,
            "retry_max_candidates": retry_max_candidates,
            "retry_max_workers": retry_max_workers,
        }
        rerank_options.update(
            {key: value for key, value in optional_policy.items() if value is not None}
        )
        if retry_failed_candidates:
            rerank_options["retry_failed_candidates"] = True
        reranked = search.rerank_candidates(
            query_image_path,
            rerank_input,
            **rerank_options,
        )
        if reranked and search.rerank_results_complete(reranked):
            displayed = (
                search.select_rerank_score_display_results(
                    reranked,
                    all_score=display_all_score,
                    fallback_limit=display_fallback_top_n,
                )
                if display_by_rerank_score
                else search.select_display_results(reranked)
            )
            visible = normalize_rerank_results(displayed)
            if not visible:
                return ToolResult.no_match(
                    code="NO_RELIABLE_RERANK_CANDIDATES",
                    data={
                        "reranked": True,
                        "visible_candidates": [],
                        "rerank_note": "复筛完成，但没有候选达到80%的可靠相似度门槛。",
                        "best_final_score": max(
                            (float(item.get("final_score") or 0) for item in reranked),
                            default=0.0,
                        ),
                    },
                    error="未找到可靠相似题。",
                    next_state="NO_MATCH",
                )
            rerank_note = ""
            outcome = ToolOutcome.SUCCESS
            code = "RERANK_COMPLETED"
        elif reranked:
            rerank_note = search.rerank_incomplete_note(reranked)
            visible = _renumber(search.mark_rerank_incomplete(coarse_candidates, rerank_note))
            outcome = ToolOutcome.PARTIAL
            code = "RERANK_INCOMPLETE_COARSE_FALLBACK"
        else:
            visible = _renumber(coarse_candidates)
            rerank_note = "视觉复筛未返回结果，已显示粗筛结果。"
            outcome = ToolOutcome.PARTIAL
            code = "RERANK_EMPTY_COARSE_FALLBACK"
        data = {
                "reranked": bool(reranked) and search.rerank_results_complete(reranked),
                "visible_candidates": visible,
                "rerank_note": rerank_note,
            }
        if outcome is ToolOutcome.PARTIAL:
            return ToolResult.partial(
                code=code,
                data=data,
                error=rerank_note,
                next_state="WAIT_CANDIDATE_CHOICE",
                retryable=True,
                error_category="external_model",
            )
        return ToolResult.success(
            code=code,
            data=data,
            next_state="WAIT_CANDIDATE_CHOICE",
        )
    except Exception as exc:  # noqa: BLE001
        del exc
        return ToolResult.tool_error(
            code="RERANK_FAILED",
            error="候选视觉复筛暂时失败，请稍后重试。",
            retryable=True,
            error_category="external_model",
        )


@_named_tool("parse_candidate_action")
def parse_candidate_action_tool(
    text: str,
    *,
    candidate_count: int,
    state: str = "WAIT_CANDIDATE_CHOICE",
) -> ToolResult:
    """Parse user action on a candidate page.

    The same text can mean different things in different states, so this parser
    is intentionally scoped to the candidate-choice state.
    """

    value = str(text).strip()
    if state != "WAIT_CANDIDATE_CHOICE":
        return ToolResult.tool_error(
            code="CANDIDATE_ACTION_INVALID_STATE",
            error="当前状态不能处理候选操作。",
            data={"state": state},
            next_state=state,
            retryable=False,
            error_category="invalid_tool_state",
        )
    if value == "0":
        return ToolResult.success(
            code="CANDIDATE_ACTION_CANCEL",
            data={"action": "cancel"},
            next_state="CANCELLED",
        )

    try:
        rank = int(value)
    except ValueError:
        return ToolResult.needs_input(
            code="CANDIDATE_NUMBER_REQUIRED",
            error="请回复候选编号，例如 1，或回复 0 取消。",
            next_state=state,
        )

    if rank < 0:
        delete_rank = abs(rank)
        if 1 <= delete_rank <= candidate_count:
            return ToolResult.success(
                code="CANDIDATE_DELETE_SELECTED",
                data={"action": "delete_candidate", "rank": delete_rank},
                next_state="PLAN_DELETE",
            )
        return ToolResult.needs_input(
            code="CANDIDATE_DELETE_RANK_OUT_OF_RANGE",
            error=f"删除编号超出范围：{delete_rank}",
            data={"rank": delete_rank, "candidate_count": candidate_count},
            next_state=state,
        )

    if 1 <= rank <= candidate_count:
        return ToolResult.success(
            code="CANDIDATE_ANSWER_SELECTED",
            data={"action": "answer", "rank": rank},
            next_state="ANSWER",
        )
    return ToolResult.needs_input(
        code="CANDIDATE_RANK_OUT_OF_RANGE",
        error=f"候选编号超出范围：{rank}",
        data={"rank": rank, "candidate_count": candidate_count},
        next_state=state,
    )


@_named_tool("answer_candidate")
def answer_candidate_tool(
    candidates: list[dict[str, Any]],
    *,
    rank: int,
    copy_to_output: bool = True,
    config: AgentToolConfig | None = None,
) -> ToolResult:
    """Return answer files for a chosen candidate.

    By default answers are copied to the new Agent runtime output directory,
    not the existing configured `answer_output`, so this tool does not disturb
    the existing Feishu/CLI answer output state.
    """

    config = config or AgentToolConfig()
    target = next((item for item in candidates if int(item.get("rank", -1)) == rank), None)
    if target is None:
        return ToolResult.needs_input(
            code="CANDIDATE_RANK_INVALID",
            error=f"候选编号不存在：{rank}",
            data={"rank": rank, "candidate_count": len(candidates)},
            next_state="WAIT_CANDIDATE_CHOICE",
        )

    try:
        answers = search.find_answer_files(target["path"])
        copied = []
        if copy_to_output:
            output_dir = config.answer_output_dir
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            for src in answers:
                dst = output_dir / src.name
                shutil.copy2(src, dst)
                copied.append(str(dst))
    except Exception as exc:  # noqa: BLE001 - isolate file-system failures.
        del exc
        return ToolResult.tool_error(
            code="ANSWER_LOOKUP_FAILED",
            error="答案文件读取暂时失败，请稍后重试。",
            data={"rank": rank},
            retryable=True,
            error_category="local_filesystem",
        )

    data = {
            "rank": rank,
            "candidate": target,
            "answer_paths": [str(path) for path in answers],
            "copied_paths": copied,
            "answer_output_dir": str(config.answer_output_dir) if copy_to_output else "",
        }
    if not answers:
        return ToolResult.no_match(
            code="ANSWER_FILES_NOT_FOUND",
            data=data,
            error="未找到该候选题对应的答案文件，请返回候选后选择其他题。",
            next_state="WAIT_CANDIDATE_CHOICE",
        )
    return ToolResult.success(
        code="ANSWER_FILES_FOUND",
        data=data,
        next_state="DONE",
    )


def _renumber(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered = []
    for rank, item in enumerate(candidates, 1):
        copied = dict(item)
        copied["rank"] = rank
        renumbered.append(copied)
    return renumbered

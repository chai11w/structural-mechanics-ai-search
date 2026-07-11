"""Coarse Agent tools for structure-mechanics question-bank retrieval.

The first Agent layer is deliberately boring: each function wraps existing
project logic and returns structured data. It does not touch the current Feishu
bot runtime, and search tools avoid writing `_last_search.json`.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import search
from multi_agent_pipeline import (
    AUTO_CHAPTER_MIN_CONFIDENCE,
    CHAPTER_UNKNOWN,
    QwenClassifier,
    RuleRouter,
    infer_structure_type_from_text,
    load_bank_excel,
    normalize_rerank_results,
    normalize_structure_type,
    resolve_effective_chapter,
    select_rerank_candidates,
    symbolic_root,
)


BASE = Path(__file__).resolve().parent.parent
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent"

STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱"}


@dataclass
class AgentToolConfig:
    """Runtime paths for the new Agent tool layer.

    Keep these paths separate from `.tmp_feishu_tiku` and the current Feishu
    bot's session/log directories.
    """

    runtime_dir: Path = DEFAULT_RUNTIME_DIR
    top_k: int = search.TOP_K
    rerank_top: int = 3
    use_qwen_cache: bool = True

    @property
    def qwen_cache_path(self) -> Path:
        return self.runtime_dir / "qwen_classifier_cache.json"

    @property
    def answer_output_dir(self) -> Path:
        return self.runtime_dir / "answer_output"


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    next_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _make_qwen(config: AgentToolConfig) -> QwenClassifier:
    return QwenClassifier(
        cache_path=config.qwen_cache_path,
        use_cache=config.use_qwen_cache,
    )


def analyze_image_tool(
    image_path: str | Path,
    *,
    chapter: str | None = "auto",
    include_layout: bool = False,
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
        classified = qwen.classify_image(path)
        effective_chapter = resolve_effective_chapter(chapter, classified)
        needs_manual_chapter = effective_chapter is None
        return ToolResult(
            ok=True,
            data={
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
            },
            next_state="WAIT_CHAPTER" if needs_manual_chapter else "READY_TO_ROUTE",
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary returns structured errors.
        return ToolResult(ok=False, error=str(exc), next_state="ERROR")


def route_bank_tool(loads: list[dict[str, Any]]) -> ToolResult:
    """Decide whether to search the main bank, symbolic bank, or review lane."""

    try:
        route, load_details = RuleRouter().route(loads)
        return ToolResult(
            ok=route.route != "needs_review",
            data={
                "route": route.route,
                "category": route.category,
                "reason": route.reason,
                "excel_root": str(route.excel_root) if route.excel_root else "",
                "load_details": load_details,
            },
            error="" if route.route != "needs_review" else route.reason,
            next_state="READY_FOR_STRUCTURE" if route.route == "symbolic" else "READY_FOR_COARSE_SEARCH",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc), next_state="ERROR")


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
        return ToolResult(
            ok=True,
            data={"structure_type": "", "source": "not_applicable", "filter_applicable": False},
            next_state="READY_FOR_COARSE_SEARCH",
        )

    text_structure = infer_structure_type_from_text(classified)
    if text_structure:
        return ToolResult(
            ok=True,
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
        return ToolResult(
            ok=True,
            data={"structure_type": "", "source": "missing_image", "filter_applicable": False},
            next_state="READY_FOR_COARSE_SEARCH",
        )

    config = config or AgentToolConfig()
    try:
        structure = _make_qwen(config).classify_structure_type(image_path)
        structure_type = normalize_structure_type(structure.get("structure_type"))
        return ToolResult(
            ok=True,
            data={
                "structure_type": structure_type,
                "confidence": structure.get("confidence", 0.0),
                "reason": structure.get("reason", ""),
                "source": "vision",
                "filter_applicable": bool(structure_type),
            },
            next_state="READY_FOR_COARSE_SEARCH",
        )
    except Exception as exc:  # noqa: BLE001 - optional speed-up; search can continue.
        return ToolResult(
            ok=True,
            data={"structure_type": "", "source": "vision_failed", "filter_applicable": False},
            error=str(exc),
            next_state="READY_FOR_COARSE_SEARCH",
        )


def coarse_search_tool(
    loads: list[dict[str, Any]],
    *,
    chapter: str,
    route: Literal["main", "symbolic"],
    structure_type: str = "",
    top_k: int | None = None,
) -> ToolResult:
    """Run read-only coarse search without writing `_last_search.json`.

    Unlike `MultiAgentCoordinator.search_loads`, this does not write the last
    search cache. Unlike `rank_bank_candidates`, it does not auto-repair live
    Excel paths.
    """

    try:
        excel_root = search.ROOT if route == "main" else symbolic_root(search.ROOT)
        df = load_bank_excel(excel_root, chapter)
        if df is None:
            return ToolResult(ok=False, error=f"Chapter not found: {chapter}", next_state="ERROR")

        filter_type = normalize_structure_type(structure_type)
        structure_filter_applied = False
        if route == "symbolic" and filter_type and "结构类型" in df.columns:
            filtered = df[df["结构类型"].astype(str) == filter_type]
            if not filtered.empty:
                df = filtered
                structure_filter_applied = True

        normalized_loads = search.normalize_query_loads(loads)
        scored: list[tuple[float, str]] = []
        for _, row in df.iterrows():
            db_loads = search._safe_parse_loads(row["荷载"])
            db_loads = search.fix_load_types(db_loads)
            score = search.compute_similarity(normalized_loads, db_loads)
            scored.append((score, str(row["题目名称"])))

        scored.sort(key=lambda item: item[0], reverse=True)
        limit = top_k or search.TOP_K
        perfect = [item for item in scored if item[0] >= 1.0]
        top = perfect if len(perfect) >= limit else perfect + [item for item in scored if item[0] < 1.0][: limit - len(perfect)]

        candidates = []
        for rank, (score, name) in enumerate([item for item in top if item[0] > 0], 1):
            path, resolved_name, repaired = search.resolve_question_path(
                name,
                chapter_name=chapter,
                update_excel=False,
            )
            candidates.append(
                {
                    "rank": rank,
                    "path": str(path),
                    "name": resolved_name,
                    "score": score,
                    "route": route,
                    "chapter": chapter,
                    "structure_type": filter_type if structure_filter_applied else "",
                    "structure_filter": structure_filter_applied,
                    "path_repaired_in_memory": repaired,
                }
            )

        return ToolResult(
            ok=True,
            data={
                "chapter": chapter,
                "route": route,
                "structure_type": filter_type,
                "structure_filter_applied": structure_filter_applied,
                "candidates": candidates,
            },
            next_state="READY_FOR_RERANK" if candidates else "NO_MATCH",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc), next_state="ERROR")


def rerank_candidates_tool(
    query_image_path: str | Path | None,
    candidates: list[dict[str, Any]],
    *,
    route: str,
    rerank_top: int = 3,
    force_rerank: bool = False,
) -> ToolResult:
    """Rerank coarse candidates and return visible candidates only.

    This tool does not answer automatically. The Agent must wait for a user
    candidate choice after this step.
    """

    if not candidates:
        return ToolResult(ok=True, data={"reranked": False, "visible_candidates": []}, next_state="NO_MATCH")
    if not query_image_path:
        return ToolResult(
            ok=True,
            data={"reranked": False, "visible_candidates": _renumber(candidates), "rerank_note": "无查询图，跳过复筛"},
            next_state="WAIT_CANDIDATE_CHOICE",
        )

    try:
        rerank_input = select_rerank_candidates(candidates, route)
        if not rerank_input:
            return ToolResult(
                ok=True,
                data={"reranked": False, "visible_candidates": _renumber(candidates), "rerank_note": "候选未达到复筛阈值，已显示粗筛结果。"},
                next_state="WAIT_CANDIDATE_CHOICE",
            )
        reranked = search.rerank_candidates(query_image_path, rerank_input, top_n=rerank_top)
        if reranked and search.rerank_results_complete(reranked):
            visible = normalize_rerank_results(reranked)
            rerank_note = ""
        elif reranked:
            rerank_note = search.rerank_incomplete_note(reranked)
            visible = _renumber(search.mark_rerank_incomplete(candidates, rerank_note))
        else:
            visible = _renumber(candidates)
            rerank_note = ""
        return ToolResult(
            ok=True,
            data={
                "reranked": bool(reranked) and search.rerank_results_complete(reranked),
                "visible_candidates": visible,
                "rerank_note": rerank_note,
            },
            next_state="WAIT_CANDIDATE_CHOICE",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(ok=False, error=str(exc), next_state="ERROR")


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
        return ToolResult(ok=False, error=f"Unsupported state for candidate action: {state}", next_state=state)
    if value == "0":
        return ToolResult(ok=True, data={"action": "cancel"}, next_state="CANCELLED")

    try:
        rank = int(value)
    except ValueError:
        return ToolResult(ok=False, error="请回复候选编号，例如 1，或回复 0 取消。", next_state=state)

    if rank < 0:
        delete_rank = abs(rank)
        if 1 <= delete_rank <= candidate_count:
            return ToolResult(
                ok=True,
                data={"action": "delete_candidate", "rank": delete_rank},
                next_state="PLAN_DELETE",
            )
        return ToolResult(ok=False, error=f"删除编号超出范围：{delete_rank}", next_state=state)

    if 1 <= rank <= candidate_count:
        return ToolResult(ok=True, data={"action": "answer", "rank": rank}, next_state="ANSWER")
    return ToolResult(ok=False, error=f"候选编号超出范围：{rank}", next_state=state)


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
    the current GUI/Feishu answer output state.
    """

    config = config or AgentToolConfig()
    target = next((item for item in candidates if int(item.get("rank", -1)) == rank), None)
    if target is None:
        return ToolResult(ok=False, error=f"候选编号不存在：{rank}", next_state="WAIT_CANDIDATE_CHOICE")

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

    return ToolResult(
        ok=bool(answers),
        data={
            "rank": rank,
            "candidate": target,
            "answer_paths": [str(path) for path in answers],
            "copied_paths": copied,
            "answer_output_dir": str(config.answer_output_dir) if copy_to_output else "",
        },
        error="" if answers else f"未找到答案文件：{target.get('path')}",
        next_state="DONE" if answers else "WAIT_CANDIDATE_CHOICE",
    )


def _renumber(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered = []
    for rank, item in enumerate(candidates, 1):
        copied = dict(item)
        copied["rank"] = rank
        renumbered.append(copied)
    return renumbered

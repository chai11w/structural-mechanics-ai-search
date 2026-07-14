"""
Multi-agent retrieval pipeline for the structure-mechanics question bank.

Qwen is used as the high-accuracy classifier at the front of the pipeline.
The local rule router chooses the target bank. Zhipu keeps the existing visual
rerank role for the final candidate list.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

import search
from scripts.classify_question_bank import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    CHAPTER_UNKNOWN,
    classify_loads,
    normalize_load_item,
    normalize_chapter_confidence,
    normalize_chapter_hint,
    qwen_analyze_image_scope,
    qwen_analyze_layout,
    qwen_extract_loads,
)
from scripts.structure_type_classifier import VALID_STRUCTURE_TYPES, qwen_structure_type


BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / ".tmp_multi_agent"
QWEN_CACHE = CACHE_DIR / "qwen_classifier_cache.json"
QWEN_CACHE_SCHEMA_VERSION = "unitless-loads-v1"
MAIN_RERANK_MIN_SCORE = 0.65
SYMBOLIC_RERANK_MIN_SCORE = 0.50
AUTO_CHAPTER_VALUES = {"", "auto", "自动", "自动识别", "自动识别章节"}
AUTO_CHAPTER_MIN_CONFIDENCE = 0.45


@dataclass
class RouteDecision:
    route: str
    category: str
    reason: str
    excel_root: Path | None


@dataclass
class PipelineResult:
    route: RouteDecision
    loads: list[dict[str, Any]]
    load_details: list[dict[str, Any]]
    results: list[dict[str, Any]]
    reranked: bool
    rerank_note: str = ""
    chapter: str | None = None
    chapter_hint: str = CHAPTER_UNKNOWN
    chapter_confidence: float = 0.0
    chapter_evidence: str = ""
    structure_type: str = ""
    structure_type_confidence: float = 0.0
    structure_type_reason: str = ""
    structure_filter_applied: bool = False


def symbolic_root(main_root: Path | None = None) -> Path:
    root = Path(main_root or search.ROOT)
    return root.parent / f"{root.name}_字母库"


class QwenClassifier:
    """High-accuracy front classifier backed by DashScope/Qwen."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        cache_path: Path = QWEN_CACHE,
        timeout: int = 180,
        use_cache: bool = True,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.cache_path = cache_path
        self.timeout = timeout
        self.use_cache = use_cache

    def classify_image(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        cache_key = self._cache_key(path)
        cache = self._load_cache() if self.use_cache else {}
        if self.use_cache and cache_key in cache:
            cached = dict(cache[cache_key])
            # Results written before the visible-problem-text gate cannot
            # prove that a chapter came from text rather than diagram shape.
            if "visible_problem_text" in cached:
                cached["from_cache"] = True
                cached.setdefault("chapter_hint", CHAPTER_UNKNOWN)
                cached.setdefault("chapter_confidence", 0.0)
                cached.setdefault("chapter_evidence", "")
                return cached

        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")

        extracted = qwen_extract_loads(
            path,
            model=self.model,
            endpoint=self.endpoint,
            api_key=api_key,
            timeout=self.timeout,
        )
        loads = [normalize_load_item(item) for item in extracted.get("loads", []) if isinstance(item, dict)]
        category, load_details = classify_loads(loads)
        result = {
            "loads": loads,
            "category": category,
            "load_details": load_details,
            "chapter_hint": normalize_chapter_hint(extracted.get("chapter_hint")),
            "chapter_confidence": normalize_chapter_confidence(extracted.get("chapter_confidence")),
            "visible_problem_text": str(extracted.get("visible_problem_text") or "").strip(),
            "chapter_evidence": str(extracted.get("chapter_evidence") or "").strip(),
            "model": self.model,
            "from_cache": False,
        }

        if self.use_cache:
            cache[cache_key] = result
            self._save_cache(cache)
        return result

    def analyze_layout(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")
        return qwen_analyze_layout(
            path,
            model=self.model,
            endpoint=self.endpoint,
            api_key=api_key,
            timeout=self.timeout,
        )

    def analyze_image_scope(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")
        return qwen_analyze_image_scope(path, model=self.model, endpoint=self.endpoint, api_key=api_key, timeout=self.timeout)

    def classify_structure_type(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")
        return qwen_structure_type(
            path,
            model=self.model,
            endpoint=self.endpoint,
            api_key=api_key,
            timeout=self.timeout,
        )

    def _cache_key(self, path: Path) -> str:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        return f"{QWEN_CACHE_SCHEMA_VERSION}:{self.model}:{digest}"

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


class RuleRouter:
    """Choose main bank, symbolic bank, or review lane from load classes."""

    MAIN_CATEGORIES = {"main_numeric", "main_assigned_symbolic"}

    def route(self, loads: list[dict[str, Any]]) -> tuple[RouteDecision, list[dict[str, Any]]]:
        normalized = [
            normalize_load_item(item)
            for item in search.normalize_query_loads(loads)
            if isinstance(item, dict)
        ]
        category, load_details = classify_loads(normalized)

        if category in self.MAIN_CATEGORIES:
            return RouteDecision("main", category, "numeric or assigned-symbol load", search.ROOT), load_details
        if category == "symbolic_unassigned":
            return RouteDecision("symbolic", category, "unassigned symbolic load", symbolic_root(search.ROOT)), load_details
        if category == "mixed_symbolic_numeric":
            return RouteDecision("needs_review", category, "mixed symbolic and numeric load", None), load_details
        return RouteDecision("needs_review", category, "empty, unknown, or unsupported load", None), load_details


class MultiAgentCoordinator:
    """Coordinate Qwen classification, rule routing, retrieval, and Zhipu rerank."""

    def __init__(
        self,
        *,
        qwen: QwenClassifier | None = None,
        router: RuleRouter | None = None,
        top_k: int | None = None,
    ) -> None:
        self.qwen = qwen or QwenClassifier()
        self.router = router or RuleRouter()
        self.top_k = top_k or search.TOP_K

    def search_image(
        self,
        image_path: str | Path,
        chapter: str | None,
        *,
        rerank: bool = True,
        rerank_top: int = search.DISPLAY_MAX_RESULTS,
        classified: dict[str, Any] | None = None,
    ) -> PipelineResult:
        classified = classified or self.qwen.classify_image(image_path)
        return self.search_loads(
            classified.get("loads", []),
            chapter,
            query_image_path=str(image_path),
            rerank=rerank,
            rerank_top=rerank_top,
            classified=classified,
        )

    def analyze_image_layout(self, image_path: str | Path) -> dict[str, Any]:
        return self.qwen.analyze_layout(image_path)

    def analyze_image_scope(self, image_path: str | Path) -> dict[str, Any]:
        return self.qwen.analyze_image_scope(image_path)

    def search_loads(
        self,
        loads: list[dict[str, Any]],
        chapter: str | None,
        *,
        query_image_path: str | None = None,
        rerank: bool = False,
        rerank_top: int = search.DISPLAY_MAX_RESULTS,
        force_rerank: bool = False,
        status_callback=None,
        classified: dict[str, Any] | None = None,
    ) -> PipelineResult:
        loads = search.normalize_query_loads(loads)
        route, load_details = self.router.route(loads)
        if route.route == "needs_review" or route.excel_root is None:
            return make_pipeline_result(route, loads, load_details, [], False, chapter, classified)

        effective_chapter = resolve_effective_chapter(chapter, classified)
        if not effective_chapter:
            needs_chapter = RouteDecision(
                "needs_chapter",
                route.category,
                "chapter auto-detection missing or low confidence",
                None,
            )
            return make_pipeline_result(needs_chapter, loads, load_details, [], False, None, classified)

        if status_callback:
            status_callback("候选检索中...")
        structure_type = ""
        structure_filter_applied = False
        if route.route == "symbolic" and query_image_path:
            text_structure = infer_structure_type_from_text(classified)
            if text_structure:
                structure_type = text_structure
                if classified is not None:
                    classified["structure_type"] = structure_type
                    classified["structure_type_confidence"] = 1.0
                    classified["structure_type_reason"] = "题干文字"
            else:
                if status_callback:
                    status_callback("结构类型识别中...")
                try:
                    structure = self.qwen.classify_structure_type(query_image_path)
                    structure_type = normalize_structure_type(structure.get("structure_type"))
                    if classified is not None:
                        classified["structure_type"] = structure_type
                        classified["structure_type_confidence"] = structure.get("confidence", 0.0)
                        classified["structure_type_reason"] = structure.get("reason", "")
                except Exception as exc:  # noqa: BLE001 - structure type is an optional speed-up.
                    print(f"WARNING: 结构类型识别失败，跳过类型筛选: {exc}")

        results = rank_bank_candidates(
            loads,
            effective_chapter,
            route.excel_root,
            self.top_k,
            structure_type=structure_type if route.route == "symbolic" else None,
        )
        structure_filter_applied = any(item.get("structure_filter") for item in results)
        reranked = False
        rerank_note = ""
        if rerank and query_image_path and results:
            rerank_input = select_rerank_candidates(results, route.route)
            if status_callback and rerank_input:
                status_callback("Zhipu复筛中...")
            if rerank_input:
                zhipu_results = search.rerank_candidates(query_image_path, rerank_input, top_n=rerank_top)
                if zhipu_results and search.rerank_results_complete(zhipu_results):
                    results = normalize_rerank_results(zhipu_results)
                    reranked = True
                    rerank_note = ""
                elif zhipu_results:
                    rerank_note = search.rerank_incomplete_note(zhipu_results)
                    fallback = search.select_incomplete_rerank_fallback(results)
                    results = search.mark_rerank_incomplete(fallback, rerank_note)

        write_last_search(results)
        return make_pipeline_result(
            route,
            loads,
            load_details,
            results,
            reranked,
            effective_chapter,
            classified,
            rerank_note=rerank_note,
            structure_filter_applied=structure_filter_applied,
        )


def is_auto_chapter(chapter: str | None) -> bool:
    if chapter is None:
        return True
    return str(chapter).strip().lower() in AUTO_CHAPTER_VALUES


def resolve_effective_chapter(chapter: str | None, classified: dict[str, Any] | None = None) -> str | None:
    if not is_auto_chapter(chapter):
        return str(chapter).strip()
    if not classified:
        return None
    chapter_hint = normalize_chapter_hint(classified.get("chapter_hint"))
    confidence = normalize_chapter_confidence(classified.get("chapter_confidence"))
    if chapter_hint != CHAPTER_UNKNOWN and confidence >= AUTO_CHAPTER_MIN_CONFIDENCE:
        return chapter_hint
    return None


def make_pipeline_result(
    route: RouteDecision,
    loads: list[dict[str, Any]],
    load_details: list[dict[str, Any]],
    results: list[dict[str, Any]],
    reranked: bool,
    chapter: str | None,
    classified: dict[str, Any] | None = None,
    *,
    rerank_note: str = "",
    structure_filter_applied: bool = False,
) -> PipelineResult:
    classified = classified or {}
    return PipelineResult(
        route,
        loads,
        load_details,
        results,
        reranked,
        rerank_note=rerank_note,
        chapter=chapter,
        chapter_hint=normalize_chapter_hint(classified.get("chapter_hint")),
        chapter_confidence=normalize_chapter_confidence(classified.get("chapter_confidence")),
        chapter_evidence=str(classified.get("chapter_evidence") or "").strip(),
        structure_type=normalize_structure_type(classified.get("structure_type")),
        structure_type_confidence=normalize_chapter_confidence(classified.get("structure_type_confidence")),
        structure_type_reason=str(classified.get("structure_type_reason") or "").strip(),
        structure_filter_applied=structure_filter_applied,
    )


def normalize_structure_type(value: object) -> str:
    text = str(value or "").strip()
    return text if text in VALID_STRUCTURE_TYPES and text != "unknown" else ""


def infer_structure_type_from_text(classified: dict[str, Any] | None) -> str:
    """Infer structure type from already-extracted problem text/evidence.

    This avoids a second image call when the problem statement already says
    "静定梁", "静定钢架", "桁架", or "拱".
    """
    if not classified:
        return ""
    text = " ".join(
        str(classified.get(key) or "")
        for key in ("chapter_evidence", "visible_text", "problem_text")
    )
    text = text.replace("刚架", "钢架").replace("行架", "桁架")
    if not text.strip():
        return ""
    if "桁架" in text:
        return "桁架"
    if "钢架" in text or "框架" in text or "刚构" in text or "门架" in text:
        return "钢架"
    if "拱" in text:
        return "拱"
    if "梁" in text:
        return "梁"
    return ""


def load_bank_excel(excel_root: Path, chapter: str) -> pd.DataFrame | None:
    xlsx_path = excel_root / f"{chapter}.xlsx"
    if not xlsx_path.exists():
        matches = list(excel_root.glob(f"*{chapter}*.xlsx"))
        if not matches:
            return None
        xlsx_path = matches[0]
    return pd.read_excel(xlsx_path)


def rank_bank_candidates(
    query_loads: list[dict[str, Any]],
    chapter: str,
    excel_root: Path,
    top_k: int,
    structure_type: str | None = None,
) -> list[dict[str, Any]]:
    df = load_bank_excel(excel_root, chapter)
    if df is None:
        return []
    filter_type = normalize_structure_type(structure_type)
    structure_filter_applied = False
    if filter_type and "结构类型" in df.columns:
        filtered = df[df["结构类型"].astype(str) == filter_type]
        if not filtered.empty:
            df = filtered
            structure_filter_applied = True

    query_loads = search.normalize_query_loads(query_loads)
    scored: list[tuple[float, str]] = []
    for _, row in df.iterrows():
        db_loads = search._safe_parse_loads(row["荷载"])
        db_loads = search.fix_load_types(db_loads)
        score = search.compute_similarity(query_loads, db_loads)
        scored.append((score, str(row["题目名称"])))

    scored.sort(key=lambda item: item[0], reverse=True)
    perfect = [item for item in scored if item[0] >= 1.0]
    top = perfect if len(perfect) >= top_k else perfect + [item for item in scored if item[0] < 1.0][:top_k - len(perfect)]
    top = [item for item in top if item[0] > 0]

    results = []
    for rank, (score, name) in enumerate(top, 1):
        path, resolved_name, _ = search.resolve_question_path(name, chapter_name=chapter, update_excel=True)
        results.append({
            "rank": rank,
            "path": str(path),
            "name": resolved_name,
            "score": score,
            "structure_type": filter_type if structure_filter_applied else "",
            "structure_filter": structure_filter_applied,
        })
    return results


def rerank_threshold_for_route(route: str) -> float:
    if route == "main":
        return MAIN_RERANK_MIN_SCORE
    if route == "symbolic":
        return SYMBOLIC_RERANK_MIN_SCORE
    return search.RERANK_MIN_LOAD_SCORE


def select_rerank_candidates(results: list[dict[str, Any]], route: str) -> list[dict[str, Any]]:
    """Keep rerank-threshold candidates; do not skip just because the pool is small."""
    threshold = rerank_threshold_for_route(route)
    selected = []
    seen_paths = set()

    for item in results:
        if not item.get("path") or item.get("score", 0) <= 0:
            continue
        if item.get("score", 0) < threshold:
            continue
        if item["path"] in seen_paths:
            continue
        selected.append({key: item[key] for key in ("rank", "path", "score", "name")})
        seen_paths.add(item["path"])

    return selected


def normalize_rerank_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for rank, item in enumerate(results, 1):
        normalized.append({
            "rank": rank,
            "path": item["path"],
            "name": item.get("name") or search._rel_path_from_question_path(item["path"]),
            "score": item.get("score", 0),
            "coarse_rank": item.get("rank"),
            "rerank_score": item.get("rerank_score"),
            "final_score": item.get("final_score"),
            "length_score": item.get("length_score"),
            "length_reason": item.get("length_reason"),
            "rerank_reason": item.get("rerank_reason"),
            "rerank_status": item.get("rerank_status"),
        })
    return normalized


def write_last_search(results: list[dict[str, Any]]) -> None:
    payload = [
        {key: value for key, value in item.items() if key != "name"}
        for item in results
    ]
    try:
        search.LAST_SEARCH_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: cannot write last search cache: {exc}")


def format_pipeline_result(result: PipelineResult) -> str:
    lines = [
        f"route={result.route.route}",
        f"category={result.route.category}",
        f"reason={result.route.reason}",
        f"chapter={result.chapter or ''}",
        f"chapter_hint={result.chapter_hint}",
        f"chapter_confidence={result.chapter_confidence:.2f}",
        "loads=" + json.dumps({"loads": result.loads}, ensure_ascii=False),
    ]
    if result.chapter_evidence:
        lines.append(f"chapter_evidence={result.chapter_evidence}")
    if result.load_details:
        details = "; ".join(f"{item['type']}:{item['raw']}->{item['load_class']}" for item in result.load_details)
        lines.append(f"load_classes={details}")

    if result.route.route == "needs_chapter":
        lines.append("needs_chapter: 请手动选择章节后重试")
        return "\n".join(lines)

    if result.route.route == "needs_review":
        lines.append("needs_review: not searching any bank")
        return "\n".join(lines)

    if not result.results:
        lines.append("无匹配结果")
        return "\n".join(lines)

    lines.append("reranked=" + str(result.reranked).lower())
    for item in result.results:
        score = item.get("final_score") if item.get("final_score") is not None else item.get("score", 0)
        lines.append(f"{item['rank']}. {item['path']}    相似度: {round(float(score) * 100)}%")
    return "\n".join(lines)

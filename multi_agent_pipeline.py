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
    classify_loads,
    normalize_load_item,
    qwen_extract_loads,
)


BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / ".tmp_multi_agent"
QWEN_CACHE = CACHE_DIR / "qwen_classifier_cache.json"


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
            cached["from_cache"] = True
            return cached

        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
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
            "model": self.model,
            "from_cache": False,
        }

        if self.use_cache:
            cache[cache_key] = result
            self._save_cache(cache)
        return result

    def _cache_key(self, path: Path) -> str:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        return f"{self.model}:{digest}"

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
        normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
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
        chapter: str,
        *,
        rerank: bool = True,
        rerank_top: int = 3,
    ) -> PipelineResult:
        classified = self.qwen.classify_image(image_path)
        return self.search_loads(
            classified.get("loads", []),
            chapter,
            query_image_path=str(image_path),
            rerank=rerank,
            rerank_top=rerank_top,
        )

    def search_loads(
        self,
        loads: list[dict[str, Any]],
        chapter: str,
        *,
        query_image_path: str | None = None,
        rerank: bool = False,
        rerank_top: int = 3,
    ) -> PipelineResult:
        route, load_details = self.router.route(loads)
        if route.route == "needs_review" or route.excel_root is None:
            return PipelineResult(route, loads, load_details, [], False)

        results = rank_bank_candidates(loads, chapter, route.excel_root, self.top_k)
        reranked = False
        if rerank and query_image_path and results:
            rerank_input = [
                {"rank": item["rank"], "path": item["path"], "score": item["score"], "name": item["name"]}
                for item in results
                if item["score"] >= search.RERANK_MIN_LOAD_SCORE
            ]
            zhipu_results = search.rerank_candidates(query_image_path, rerank_input, top_n=rerank_top)
            if zhipu_results:
                results = normalize_rerank_results(zhipu_results)
                reranked = True

        write_last_search(results)
        return PipelineResult(route, loads, load_details, results, reranked)


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
) -> list[dict[str, Any]]:
    df = load_bank_excel(excel_root, chapter)
    if df is None:
        return []

    query_loads = search.fix_load_types([dict(item) for item in query_loads])
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
        })
    return results


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
        "loads=" + json.dumps({"loads": result.loads}, ensure_ascii=False),
    ]
    if result.load_details:
        details = "; ".join(f"{item['type']}:{item['raw']}->{item['load_class']}" for item in result.load_details)
        lines.append(f"load_classes={details}")

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

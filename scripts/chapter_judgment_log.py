"""Append-only Feishu chapter failure logs for prompt/rule iteration.

The log is intentionally lightweight JSONL. It records only valuable samples:
Feishu auto chapter recognition failed, then the user manually supplied the
correct chapter.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = BASE / "data" / "feishu_chapter_failure_log.jsonl"


def append_chapter_judgment_log(
    *,
    source: str,
    image_path: str | Path | None = None,
    requested_chapter: str | None = None,
    final_chapter: str | None = None,
    decision_mode: str = "",
    classified: dict[str, Any] | None = None,
    loads: list[dict[str, Any]] | None = None,
    route: str = "",
    category: str = "",
    result_count: int | None = None,
    log_path: Path = DEFAULT_LOG_PATH,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one failed-auto/manual-chapter observation.

    Logging must never break search/store flows, so all filesystem errors are
    swallowed by callers through this helper.
    """
    normalized_image_path = normalize_path(image_path)
    if not normalized_image_path or Path(normalized_image_path).name.startswith("mock-"):
        return
    classified = classified or {}
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "image_path": normalized_image_path,
        "requested_chapter": requested_chapter or "",
        "final_chapter": final_chapter or "",
        "decision_mode": decision_mode,
        "chapter_hint": str(classified.get("chapter_hint") or ""),
        "chapter_confidence": classified.get("chapter_confidence", ""),
        "chapter_evidence": str(classified.get("chapter_evidence") or ""),
        "loads": loads or classified.get("loads") or [],
        "route": route,
        "category": category,
    }
    if result_count is not None:
        record["result_count"] = result_count
    if extra:
        record.update(extra)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def normalize_path(path: str | Path | None) -> str:
    return "" if path is None else str(path).replace("\\", "/")


def decision_mode_for(requested_chapter: str | None, final_chapter: str | None) -> str:
    requested = str(requested_chapter or "").strip().lower()
    if requested in {"", "auto", "自动", "自动识别", "自动识别章节"}:
        return "auto" if final_chapter else "needs_manual"
    return "manual"

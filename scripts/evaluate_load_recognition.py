"""Evaluate the production A2 load classifier on a versioned image manifest."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
from typing import Any, Iterable

import search
from multi_agent_pipeline import QwenClassifier
from scripts.classify_question_bank import clean_raw, normalize_load_item


BASE = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = BASE / "experiments" / "load_recognition_eval_20" / "manifest.json"
SCHEMA_VERSION = "load-recognition-eval-v1"


def _safe_image_path(root: Path, relative: str) -> Path:
    rel = Path(str(relative or ""))
    if rel.is_absolute():
        raise ValueError("sample path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / rel).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("sample path escapes image root")
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def load_manifest(path: Path, image_root: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("manifest samples must be a non-empty array")
    seen: set[str] = set()
    loaded: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("manifest sample must be an object")
        sample_id = str(sample.get("id") or "").strip()
        if not sample_id or sample_id in seen:
            raise ValueError("sample ids must be non-empty and unique")
        seen.add(sample_id)
        expected = sample.get("expected_loads")
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"{sample_id}: expected_loads must be non-empty")
        normalized_expected = [normalize_load_item(item) for item in expected]
        if any(item["type"] not in {"集中", "均布", "弯矩"} or not item["raw"] for item in normalized_expected):
            raise ValueError(f"{sample_id}: invalid expected load")
        row = dict(sample)
        row["image_path"] = _safe_image_path(image_root, str(sample.get("path") or ""))
        row["expected_loads"] = normalized_expected
        loaded.append(row)
    return loaded


def _canonical_raw(raw: object) -> str:
    return (
        clean_raw(raw)
        .replace("^{2}", "²")
        .replace("^2", "²")
        .replace("ℓ", "l")
        .replace(" ", "")
        .casefold()
    )


def exact_signature(loads: Iterable[dict[str, Any]]) -> Counter[tuple[str, str]]:
    normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
    return Counter((item["type"], _canonical_raw(item["raw"])) for item in normalized)


def retrieval_signature(loads: Iterable[dict[str, Any]]) -> Counter[tuple[str, str]]:
    normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
    return Counter(
        (item["type"], search.normalize_raw(item["raw"], item["type"]))
        for item in normalized
    )


def type_signature(loads: Iterable[dict[str, Any]]) -> Counter[str]:
    normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
    return Counter(item["type"] for item in normalized)


def compare_loads(expected: list[dict[str, Any]], predicted: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        "exact_match": exact_signature(expected) == exact_signature(predicted),
        "retrieval_match": retrieval_signature(expected) == retrieval_signature(predicted),
        "type_match": type_signature(expected) == type_signature(predicted),
    }


def _evaluate_one(classifier: QwenClassifier, sample: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = classifier.classify_image(sample["image_path"])
        predicted = [
            normalize_load_item(item)
            for item in result.get("loads", [])
            if isinstance(item, dict)
        ]
        comparisons = compare_loads(sample["expected_loads"], predicted)
        return {
            "id": sample["id"],
            "stratum": sample.get("stratum", ""),
            "bank": sample.get("bank", ""),
            "book": sample.get("book", ""),
            "excel_row": sample.get("excel_row"),
            "path": sample["path"],
            "expected_loads": sample["expected_loads"],
            "predicted_loads": predicted,
            **comparisons,
            "chapter_hint": result.get("chapter_hint", ""),
            "from_cache": bool(result.get("from_cache")),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - preserve every sample outcome.
        return {
            "id": sample["id"],
            "stratum": sample.get("stratum", ""),
            "bank": sample.get("bank", ""),
            "book": sample.get("book", ""),
            "excel_row": sample.get("excel_row"),
            "path": sample["path"],
            "expected_loads": sample["expected_loads"],
            "predicted_loads": [],
            "exact_match": False,
            "retrieval_match": False,
            "type_match": False,
            "chapter_hint": "",
            "from_cache": False,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def evaluate(samples: list[dict[str, Any]], *, max_workers: int, timeout: int) -> list[dict[str, Any]]:
    classifier = QwenClassifier(use_cache=False, timeout=timeout)
    workers = max(1, min(4, int(max_workers)))
    if workers == 1:
        return [_evaluate_one(classifier, sample) for sample in samples]
    indexed: dict[Any, int] = {}
    rows: list[dict[str, Any] | None] = [None] * len(samples)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="load-eval") as executor:
        for index, sample in enumerate(samples):
            indexed[executor.submit(_evaluate_one, classifier, sample)] = index
        for future in as_completed(indexed):
            rows[indexed[future]] = future.result()
    return [row for row in rows if row is not None]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if not row["error"]]
    strata: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = strata.setdefault(
            row["stratum"],
            {"total": 0, "completed": 0, "exact_matches": 0, "retrieval_matches": 0, "type_matches": 0},
        )
        bucket["total"] += 1
        if not row["error"]:
            bucket["completed"] += 1
            bucket["exact_matches"] += int(row["exact_match"])
            bucket["retrieval_matches"] += int(row["retrieval_match"])
            bucket["type_matches"] += int(row["type_match"])
    return {
        "total": len(rows),
        "completed": len(completed),
        "failures": len(rows) - len(completed),
        "exact_matches": sum(int(row["exact_match"]) for row in completed),
        "retrieval_matches": sum(int(row["retrieval_match"]) for row in completed),
        "type_matches": sum(int(row["type_match"]) for row in completed),
        "strata": strata,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate production A2 load recognition on a fixed manifest")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--image-root", type=Path, default=search.ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def _portable_manifest_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(BASE.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def main() -> int:
    args = build_parser().parse_args()
    manifest = args.manifest.resolve()
    image_root = args.image_root.resolve()
    samples = load_manifest(manifest, image_root)
    if args.validate_only:
        print(json.dumps({"manifest": str(manifest), "samples": len(samples), "valid": True}, ensure_ascii=False))
        return 0
    if args.output is None:
        raise SystemExit("--output is required unless --validate-only is used")
    started = time.perf_counter()
    rows = evaluate(samples, max_workers=args.max_workers, timeout=args.timeout)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "manifest": _portable_manifest_path(manifest),
        "model": QwenClassifier(use_cache=False).model,
        "cache_enabled": False,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "summary": summarize(rows),
        "cases": rows,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"output={output}")
    return 0 if payload["summary"]["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

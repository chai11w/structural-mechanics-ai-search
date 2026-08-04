"""Run zhipu (bigmodel) vision recognition for the structure-dimension experiment.

Fallback path used when the ``zhipu-vision`` MCP server is unavailable: this
script calls the OpenAI-compatible bigmodel ``chat/completions`` endpoint
directly with the same ``DIMENSION_PROMPT`` as the evaluator harness, and writes
results in the MCP-results format that
``scripts/evaluate_structure_dimensions.py`` merges via ``--mcp-results``.

The 10 sample images are submitted concurrently (the model supports parallel
requests). The API key is read from the ``ZHIPU_API_KEY`` environment variable
only; it is never accepted from a configuration file or the command line.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.classify_question_bank import image_to_data_url, parse_model_json  # noqa: E402
from scripts.evaluate_structure_dimensions import (  # noqa: E402
    DIMENSION_PROMPT,
    load_manifest,
    normalize_provider_result,
)

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4.6v"
DEFAULT_WORKERS = 10
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


def call_zhipu(
    image_path: Path,
    *,
    api_key: str,
    endpoint: str,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    """Submit one question image to zhipu vision and return the normalized row."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DIMENSION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    {"type": "text", "text": "只输出JSON。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 2048,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    message = data["choices"][0]["message"]
    content = str(message.get("content") or "")
    if not content.strip():
        # glm-4.6v returns the final answer in ``content``; an empty one means
        # the model stopped mid-reasoning (``reasoning_content`` populated but no
        # finished JSON). This is transient under concurrency, so the caller
        # retries instead of recording a permanent failure.
        reasoning = str(message.get("reasoning_content") or "")
        raise RuntimeError(f"empty model content; reasoning={reasoning[:200]}")
    try:
        normalized = normalize_provider_result(parse_model_json(content))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse model output as JSON: {content[:300]}") from exc
    return {
        "raw_content": content,
        "reasoning": str(message.get("reasoning_content") or "")[:300],
        "usage": data.get("usage") or {},
        "normalized": normalized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call zhipu vision directly for the structure-dimension experiment."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=BASE / "experiments" / "structure_dimension_eval" / "samples.json",
    )
    parser.add_argument("--root", type=Path, required=True, help="question-bank root")
    parser.add_argument("--output", type=Path, required=True, help="path to write mcp_results.json")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    api_key = os.environ.get("ZHIPU_API_KEY", "")
    if not api_key:
        raise SystemExit("ZHIPU_API_KEY missing; refusing to read a key from configuration files")

    root = args.root.resolve()
    samples = load_manifest(args.manifest.resolve(), root)

    def run_one(sample: Any) -> dict[str, Any]:
        image_path = root / sample.relative_path
        started = time.perf_counter()
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                row = call_zhipu(
                    image_path,
                    api_key=api_key,
                    endpoint=args.endpoint,
                    model=args.model,
                    timeout=args.timeout,
                )
                row["sample_id"] = sample.sample_id
                row["seconds"] = round(time.perf_counter() - started, 3)
                row["attempts"] = attempt
                return row
            except Exception as exc:  # noqa: BLE001 - retry empty/parse failures, keep the last one
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
        return {
            "sample_id": sample.sample_id,
            "error": last_error,
            "seconds": round(time.perf_counter() - started, 3),
            "attempts": MAX_ATTEMPTS,
        }

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, sample): sample.sample_id for sample in samples}
        for future in concurrent.futures.as_completed(futures):
            sample_id = futures[future]
            row = future.result()
            results.append(row)
            if "error" in row:
                print(f"zhipu failed {sample_id}: {row['error'][:160]}")
            else:
                print(f"zhipu ok {sample_id}")
    results.sort(key=lambda row: row["sample_id"])
    ok_count = sum(1 for row in results if "error" not in row)
    print(f"{ok_count}/{len(results)} zhipu ok")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"mcp_results={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

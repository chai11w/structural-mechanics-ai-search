"""Run the draft outer-routing prompt against isolated evaluation images."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import urllib.request

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_shared.image_payload import image_to_model_data_url
from tiku_agent.image_triage import finalize_route, observation_from_model_text
from tiku_agent.image_triage_8897 import (
    finalize_route_8897,
    finalize_route_8897_v1,
    finalize_route_8897_v2,
    observation_from_model_text_8897,
    observation_from_model_text_8897_v1,
    observation_from_model_text_8897_v2,
)


EVAL_ROOT = BASE / "experiments" / "complex_image_eval"
MANIFEST_PATH = EVAL_ROOT / "manifest.json"
PROMPT_PATH = EVAL_ROOT / "observation_prompt_scratch.md"
PROMPT_8897_PATH = EVAL_ROOT / "observation_prompt_8897_boundary.md"
PROMPT_8897_V1_PATH = EVAL_ROOT / "observation_prompt_8897_boundary_v1.md"
PROMPT_8897_V2_PATH = EVAL_ROOT / "observation_prompt_8897_boundary_v2.md"
PROMPT_8897_V3_PATH = EVAL_ROOT / "observation_prompt_8897_boundary_v3.md"
DEFAULT_OUTPUT = BASE / ".tmp_complex_image_routing_eval" / "results.json"

QWEN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_MODEL = "qwen3.7-plus"
ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODEL = "glm-4.6v"

_ROUTE_PATTERN = re.compile(r"(?<![A-Za-z0-9])A([123])(?![A-Za-z0-9])", re.IGNORECASE)


def load_local_config() -> dict:
    config: dict = {}
    for name in ("config.json", "config.local.json"):
        path = BASE / name
        if path.is_file():
            config.update(json.loads(path.read_text(encoding="utf-8")))
    return config


def load_prompt(path: Path = PROMPT_PATH) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"```text\s*(.*?)\s*```", content, flags=re.DOTALL)
    if not match:
        raise ValueError(f"提示词文件缺少 text 代码块: {path}")
    return match.group(1).strip()


def extract_route(content: str) -> str | None:
    match = _ROUTE_PATTERN.search(str(content or ""))
    return f"A{match.group(1)}" if match else None


def resolve_samples(*, include_question_bank: bool, config: dict) -> list[dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    bank_root = Path(config.get("root", "")) if config.get("root") else None
    samples = []
    for sample in manifest["samples"]:
        relative_path = sample.get("relative_path")
        expected_route = sample.get("expected_route")
        if not relative_path or not expected_route:
            continue
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"非法评测图片相对路径: {relative_path}")
        if sample["source_kind"] == "question_bank":
            if not include_question_bank:
                continue
            if bank_root is None:
                raise RuntimeError("题库根目录未配置")
            image_path = bank_root.joinpath(*relative.parts)
        else:
            image_path = EVAL_ROOT.joinpath(*relative.parts)
        if not image_path.is_file():
            raise FileNotFoundError(f"评测图片不存在: {sample['id']}")
        samples.append(
            {
                "id": sample["id"],
                "expected_route": expected_route,
                "label_status": sample.get("label_status"),
                "source_kind": sample["source_kind"],
                "image_path": image_path,
            }
        )
    return samples


def resolve_labeled_directory(root: Path) -> list[dict]:
    """Load a small external A1/A2/A3 folder without copying its images."""

    samples: list[dict] = []
    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    for expected_route in ("A1", "A2", "A3"):
        folder = root / expected_route
        if not folder.is_dir():
            raise FileNotFoundError(f"标注目录缺少 {expected_route}: {folder}")
        for image_path in sorted(folder.iterdir()):
            if image_path.is_file() and image_path.suffix.lower() in allowed:
                samples.append(
                    {
                        "id": f"{expected_route}/{image_path.name}",
                        "expected_route": expected_route,
                        "label_status": "user_labeled",
                        "source_kind": "labeled_directory",
                        "image_path": image_path,
                    }
                )
    if not samples:
        raise FileNotFoundError(f"标注目录没有图片: {root}")
    return samples


def provider_settings(provider: str, config: dict) -> tuple[str, str, str]:
    if provider == "qwen":
        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or config.get("dashscope_api_key", "")
        return QWEN_ENDPOINT, str(config.get("dashscope_model") or QWEN_MODEL), api_key
    if provider == "zhipu":
        api_key = (
            os.environ.get("ZHIPUAI_API_KEY", "")
            or os.environ.get("ZAI_API_KEY", "")
            or config.get("zhipuai_api_key", "")
        )
        return ZHIPU_ENDPOINT, str(config.get("zhipu_rerank_model") or ZHIPU_MODEL), api_key
    raise ValueError(f"未知模型提供方: {provider}")


def build_payload(provider: str, model: str, prompt: str, image_path: Path) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_model_data_url(image_path)}},
                    {"type": "text", "text": "请按要求完成第一次分流。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 1600,
    }
    if provider == "qwen":
        payload["enable_thinking"] = False
    else:
        payload["thinking"] = {"type": "disabled"}
    return payload


def call_model(
    provider: str,
    sample: dict,
    *,
    prompt: str,
    config: dict,
    timeout: int,
    route_policy: str = "legacy",
) -> dict:
    endpoint, model, api_key = provider_settings(provider, config)
    if not api_key:
        raise RuntimeError(f"{provider} 密钥未配置")
    payload = build_payload(provider, model, prompt, sample["image_path"])
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = datetime.now(timezone.utc)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    content = data["choices"][0]["message"].get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    route = extract_route(content)
    if route_policy in {"8897-boundary", "8897-v3"}:
        observation = observation_from_model_text_8897(content)
        final_route = finalize_route_8897(observation)
    elif route_policy == "8897-v2":
        observation = observation_from_model_text_8897_v2(content)
        final_route = finalize_route_8897_v2(observation)
    elif route_policy == "8897-v1":
        observation = observation_from_model_text_8897_v1(content)
        final_route = finalize_route_8897_v1(observation)
    else:
        observation = observation_from_model_text(content)
        final_route = finalize_route(observation)
    return {
        "sample_id": sample["id"],
        "provider": provider,
        "model": model,
        "expected_route": sample["expected_route"],
        "label_status": sample["label_status"],
        "suggested_route": route,
        "final_route": final_route,
        "question_count": observation.question_count,
        "original_structure_count": observation.original_structure_count,
        "auxiliary_diagram_count": observation.auxiliary_diagram_count,
        "has_actual_load_evidence": observation.has_actual_load_evidence,
        "image_recoverable": observation.image_recoverable,
        "image_boundary_clear": getattr(observation, "image_boundary_clear", None),
        "route_policy": route_policy,
        "has_ambiguity": observation.has_ambiguity,
        "route_matches_label": route == sample["expected_route"],
        "final_route_matches_label": final_route == sample["expected_route"],
        "elapsed_seconds": round(elapsed, 3),
        "usage": data.get("usage") or {},
        "raw_content": content,
        "error": None,
    }


def write_results(path: Path, *, prompt_sha256: str, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt_sha256": prompt_sha256,
        "results": sorted(results, key=lambda item: (item["sample_id"], item["provider"])),
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="独立比较智谱和千问的复杂题图初步分流")
    parser.add_argument("--providers", nargs="+", choices=("qwen", "zhipu"), default=("qwen", "zhipu"))
    parser.add_argument("--include-question-bank", action="store_true", help="加入清单中的暂定 A2 题库单题")
    parser.add_argument("--labeled-dir", type=Path, help="读取包含 A1/A2/A3 子目录的外部标注集")
    parser.add_argument(
        "--route-policy",
        choices=("legacy", "8897-v1", "8897-v2", "8897-v3", "8897-boundary"),
        default="legacy",
        help="选择代码复核门禁；默认保持既有规则",
    )
    parser.add_argument("--prompt", type=Path, help="覆盖分流提示词文件")
    parser.add_argument("--sample-id", action="append", dest="sample_ids", help="只运行指定图片编号，可重复使用")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    config = load_local_config()
    prompt_path = args.prompt or (
        {
            "8897-boundary": PROMPT_8897_V3_PATH,
            "8897-v1": PROMPT_8897_V1_PATH,
            "8897-v2": PROMPT_8897_V2_PATH,
            "8897-v3": PROMPT_8897_V3_PATH,
        }.get(args.route_policy, PROMPT_PATH)
    )
    prompt = load_prompt(prompt_path)
    samples = (
        resolve_labeled_directory(args.labeled_dir.resolve())
        if args.labeled_dir
        else resolve_samples(include_question_bank=args.include_question_bank, config=config)
    )
    if args.sample_ids:
        requested = set(args.sample_ids)
        available = {sample["id"] for sample in samples}
        missing = requested - available
        if missing:
            parser.error(f"找不到指定评测图片: {', '.join(sorted(missing))}")
        samples = [sample for sample in samples if sample["id"] in requested]
    import hashlib

    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    results: list[dict] = []
    lock = threading.Lock()
    jobs = [(provider, sample) for sample in samples for provider in args.providers]

    def run_job(provider: str, sample: dict) -> dict:
        try:
            return call_model(
                provider,
                sample,
                prompt=prompt,
                config=config,
                timeout=args.timeout,
                route_policy=args.route_policy,
            )
        except Exception as exc:  # noqa: BLE001 - each model failure belongs in the report.
            return {
                "sample_id": sample["id"],
                "provider": provider,
                "model": provider_settings(provider, config)[1],
                "expected_route": sample["expected_route"],
                "label_status": sample["label_status"],
                "suggested_route": None,
                "final_route": None,
                "route_matches_label": False,
                "final_route_matches_label": False,
                "elapsed_seconds": None,
                "usage": {},
                "raw_content": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(run_job, provider, sample): (provider, sample) for provider, sample in jobs}
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
                write_results(args.output, prompt_sha256=prompt_sha256, results=results)
            status = result.get("final_route") or result["suggested_route"] or "失败"
            print(f"{result['provider']:6} {result['sample_id']}: {status}", flush=True)

    success = sum(1 for item in results if not item["error"])
    print(f"完成 {success}/{len(results)} 次模型调用，结果保存在 {args.output}")
    return 0 if success == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

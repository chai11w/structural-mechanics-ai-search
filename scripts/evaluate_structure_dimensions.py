"""Run an isolated Qwen/MCP structural-dimension comparison experiment.

This utility never touches the question-bank indexes or production runtime state.
It reads a versioned sample manifest, submits each selected image to Qwen (optional),
merges separately captured MCP vision responses, and writes review-only artifacts to
an ignored output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.classify_question_bank import (  # noqa: E402
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    image_to_data_url,
    parse_model_json,
    tracked_qwen_request,
)

VALID_STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱", "组合结构", "unknown"}
DEFAULT_MANIFEST = BASE / "experiments" / "structure_dimension_eval" / "samples.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_structure_dimension_eval"

DIMENSION_PROMPT = """你是结构力学题图的结构尺寸识别器。只看主承重骨架，不解题，不提取荷载。

请输出这道题主承重骨架的结构大类、总跨和总高度。总跨是骨架最左至最右的总水平跨度：多跨结构必须把同方向连续分段相加。总高度是骨架最低至最高的总竖向高度。只使用图中明确给出的尺寸、符号尺寸及可直接相加的同类尺寸；不得使用图片像素、纸面空白、文字高度或荷载箭头估算尺寸。

忽略荷载箭头、荷载文字、尺寸标注线本身、题号、节点字母、支座细节和图像留白。单条水平梁的总高度为 0。若总跨或总高度不能从图中可靠得到，对应值必须为 null，不要猜测。

结构类型只能为：梁、钢架、桁架、拱、组合结构、unknown。
- 梁：单跨梁、多跨梁、悬臂梁等主要由梁构件组成的结构。
- 钢架：由梁柱刚结组成的 L/T/门式/多跨框架等。
- 桁架：由多根直杆组成三角形或网格杆系。
- 拱：以拱形曲线或拱轴为主要承重构件。
- 组合结构：梁、桁架、拉杆、钢架等不同骨架单元混合，且不能以单一类别完整描述。

总跨和总高度必须是已化简的单一无单位表达式字符串：
- 6 m 写为 "6m"；三段各为 l 的总跨写为 "3l"；无符号数字写为 "6"；单条水平梁的高度写为 "0"。
- 只能输出一个数值系数加可选的一个相同尺寸单位/符号，例如 "2.5m"、"3l"、"0"。
- 不得输出加号、范围、约等号、解释或图片像素。

严格只输出 JSON，不要输出 Markdown：
{"structure_type":"梁|钢架|桁架|拱|组合结构|unknown","total_span":"6m|null","total_height":"3m|null","confidence":0.0,"reason":"不超过20字"}"""

_DIMENSION_RE = re.compile(
    r"^(?:(?P<coefficient>(?:0|[1-9]\d*)(?:\.\d+)?)?(?P<symbol>[A-Za-z]+)|(?P<number>0|[1-9]\d*(?:\.\d+)?))$"
)


@dataclass(frozen=True)
class Dimension:
    raw: str
    coefficient: Fraction
    symbol: str


@dataclass(frozen=True)
class Sample:
    sample_id: str
    expected_structure_type: str
    relative_path: str
    selection_note: str


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def normalize_dimension(value: object) -> Dimension | None:
    """Normalize one simplified total-span/total-height expression.

    The provider contract permits only a non-negative decimal coefficient with an
    optional alphabetic unit/symbol. Rejecting all other algebra avoids inventing
    a dimension key for expressions whose relation is not mechanically known.
    """

    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text or text.lower() in {"null", "unknown", "未知", "不确定"}:
        return None
    text = text.replace("米", "m")
    match = _DIMENSION_RE.fullmatch(text)
    if not match:
        return None
    coefficient_text = match.group("coefficient") or match.group("number") or "1"
    coefficient = Fraction(coefficient_text)
    if coefficient < 0:
        return None
    return Dimension(raw=text, coefficient=coefficient, symbol=(match.group("symbol") or ""))


def format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def size_key(total_span: object, total_height: object) -> str:
    """Return a scale-invariant span:height key, or ``unknown``.

    A flat structure has a meaningful special key. For non-flat structures, both
    values must use the same explicitly written unit/symbol; this prevents false
    equivalence between, for example, ``3l`` and ``2h``.
    """

    span = normalize_dimension(total_span)
    height = normalize_dimension(total_height)
    if span is None or height is None or span.coefficient <= 0:
        return "unknown"
    if height.coefficient == 0:
        return "flat"
    if span.symbol != height.symbol:
        return "unknown"
    ratio = span.coefficient / height.coefficient
    return f"{format_fraction(ratio)}:1"


def normalize_provider_result(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Put Qwen or MCP output into the shared, conservative report schema."""

    source = dict(value or {})
    structure_type = str(source.get("structure_type") or "unknown").strip()
    if structure_type not in VALID_STRUCTURE_TYPES:
        structure_type = "unknown"
    total_span = normalize_dimension(source.get("total_span"))
    total_height = normalize_dimension(source.get("total_height"))
    try:
        confidence = max(0.0, min(1.0, float(source.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "structure_type": structure_type,
        "total_span": total_span.raw if total_span else None,
        "total_height": total_height.raw if total_height else None,
        "size_key": size_key(total_span.raw if total_span else None, total_height.raw if total_height else None),
        "confidence": confidence,
        "reason": str(source.get("reason") or "").strip()[:200],
    }


def load_manifest(path: Path, root: Path) -> list[Sample]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema_version") or 0) != 1:
        raise ValueError("unsupported manifest schema_version")
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("manifest must contain a non-empty samples list")

    samples: list[Sample] = []
    seen_ids: set[str] = set()
    for item in raw_samples:
        if not isinstance(item, Mapping):
            raise ValueError("manifest samples must be objects")
        sample_id = str(item.get("id") or "").strip()
        relative_path = str(item.get("path") or "").replace("\\", "/").strip()
        if not sample_id or sample_id in seen_ids:
            raise ValueError(f"duplicate or empty sample id: {sample_id!r}")
        if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise ValueError(f"sample {sample_id} must have a safe relative path")
        if "答案" in Path(relative_path).parts:
            raise ValueError(f"sample {sample_id} must not use an answer image")
        image_path = root / relative_path
        if not image_path.is_file():
            raise FileNotFoundError(f"sample {sample_id} missing: {image_path}")
        seen_ids.add(sample_id)
        samples.append(
            Sample(
                sample_id=sample_id,
                expected_structure_type=str(item.get("expected_structure_type") or "").strip(),
                relative_path=relative_path,
                selection_note=str(item.get("selection_note") or "").strip(),
            )
        )
    return samples


def build_qwen_request(image_path: Path, *, model: str) -> dict[str, Any]:
    return {
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
        "max_tokens": 256,
        "enable_thinking": False,
    }


def call_qwen(
    image_path: Path,
    *,
    api_key: str,
    endpoint: str,
    model: str,
    timeout: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    request = build_qwen_request(image_path, model=model)
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    provider_request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    response = tracked_qwen_request(
        provider_request,
        timeout=timeout,
        model=model,
        call_type="qwen_structure_dimension_eval",
    )
    content = str(response["choices"][0]["message"]["content"])
    parsed = parse_model_json(content)
    return normalize_provider_result(parsed), dict(response.get("usage") or {}), content


def load_mcp_results(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        raw_items = data.get("results", data)
    else:
        raw_items = data
    if isinstance(raw_items, Mapping):
        return {
            str(sample_id): dict(item) if isinstance(item, Mapping) else {"raw_content": str(item)}
            for sample_id, item in raw_items.items()
        }
    if not isinstance(raw_items, list):
        raise ValueError("MCP results must be an object keyed by sample id or a results list")
    results: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("MCP result list entries must be objects")
        sample_id = str(item.get("sample_id") or item.get("id") or "").strip()
        if not sample_id or sample_id in results:
            raise ValueError(f"duplicate or missing MCP sample id: {sample_id!r}")
        results[sample_id] = dict(item)
    return results


def load_saved_provider_results(path: Path, provider: str) -> dict[str, dict[str, Any]]:
    """Extract one provider's rows from a prior evaluator ``results.json`` run."""

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_rows = data.get("results") if isinstance(data, Mapping) else None
    if not isinstance(raw_rows, list):
        raise ValueError("saved provider results must contain a results list")
    results: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise ValueError("saved provider result rows must be objects")
        sample_id = str(row.get("sample_id") or "").strip()
        value = row.get(provider)
        if not sample_id or sample_id in results:
            raise ValueError(f"duplicate or missing saved sample id: {sample_id!r}")
        if value is not None:
            if not isinstance(value, Mapping):
                raise ValueError(f"saved {provider} result for {sample_id} must be an object")
            result = dict(value)
            raw_content = result.get("raw_content")
            if raw_content:
                try:
                    result["normalized"] = normalize_provider_result(parse_model_json(str(raw_content)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            results[sample_id] = result
    return results


def mcp_results_template(samples: list[Sample]) -> dict[str, Any]:
    return {
        "results": [
            {
                "sample_id": sample.sample_id,
                "structure_type": "unknown",
                "total_span": None,
                "total_height": None,
                "confidence": 0.0,
                "reason": "",
            }
            for sample in samples
        ]
    }


def provider_cell(result: Mapping[str, Any] | None) -> str:
    if not result:
        return "未提供"
    if result.get("error"):
        return f"失败：{markdown_escape(str(result['error']))}"
    normalized = result.get("normalized") or {}
    return "<br>".join(
        [
            f"类型：{markdown_escape(str(normalized.get('structure_type') or 'unknown'))}",
            f"总跨：{markdown_escape(str(normalized.get('total_span') or 'unknown'))}",
            f"总高：{markdown_escape(str(normalized.get('total_height') or 'unknown'))}",
            f"size_key：{markdown_escape(str(normalized.get('size_key') or 'unknown'))}",
            f"置信度：{float(normalized.get('confidence') or 0):.2f}",
            f"理由：{markdown_escape(str(normalized.get('reason') or ''))}",
        ]
    )


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def comparable_type(result: Mapping[str, Any] | None) -> str | None:
    value = str(((result or {}).get("normalized") or {}).get("structure_type") or "unknown")
    return value if value != "unknown" else None


def comparable_size_key(result: Mapping[str, Any] | None) -> str | None:
    value = str(((result or {}).get("normalized") or {}).get("size_key") or "unknown")
    return value if value != "unknown" else None


def agreement(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, str]:
    left_type, right_type = comparable_type(left), comparable_type(right)
    left_size, right_size = comparable_size_key(left), comparable_size_key(right)
    type_agreement = "一致" if left_type and left_type == right_type else "无法比较" if not (left_type and right_type) else "不一致"
    size_agreement = "一致" if left_size and left_size == right_size else "无法比较" if not (left_size and right_size) else "不一致"
    return {"structure_type": type_agreement, "size_key": size_agreement}


def write_markdown_report(output_path: Path, payload: Mapping[str, Any]) -> None:
    rows = payload["results"]
    lines = ["# 结构总跨/总高度双模型对照实验", ""]
    lines.append("- 目的：比较 Qwen 与 MCP 视觉模型对主承重骨架结构类型、总跨、总高度及归一 `size_key` 的识别结果。")
    lines.append("- 尺寸口径：总跨为主骨架最左至最右的总水平跨度；总高度为主骨架最低至最高的总竖向高度。多跨同类尺寸应相加；单条水平梁总高度为 `0`。")
    lines.append("- `size_key`：总跨 : 总高度的约分比例；总高为 0 时为 `flat`；不同尺寸单位/符号、未知或无法可靠合并时为 `unknown`。")
    lines.append("- 这是一份模型间一致性对照，不把目录类别或两模型一致直接表述为正确性。请在“人工裁决”栏逐图判定。")
    lines.append("")
    summary = payload["summary"]
    lines.append(f"- 样本：{summary['total']}；Qwen 成功：{summary['qwen_success']}；MCP 成功：{summary['mcp_success']}；MCP 未提供：{summary['mcp_missing']}")
    lines.append(f"- 可比较结构类型一致：{summary['type_agreement_count']}/{summary['type_comparable_count']}；可比较 size_key 一致：{summary['size_agreement_count']}/{summary['size_comparable_count']}")
    lines.append("")
    lines.append("| 样本 | 题图 | 目录类别（非尺寸真值） | Qwen | MCP | 结构类型 | size_key | 人工裁决 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in rows:
        image_path = Path(row["image_path"]).as_posix()
        lines.append(
            "| {sample_id} | ![{sample_id}]({image_path}) | {expected} | {qwen} | {mcp} | {type_agreement} | {size_agreement} | 待填写 |".format(
                sample_id=markdown_escape(row["sample_id"]),
                image_path=image_path,
                expected=markdown_escape(row["expected_structure_type"] or "未标注"),
                qwen=provider_cell(row.get("qwen")),
                mcp=provider_cell(row.get("mcp")),
                type_agreement=row["agreement"]["structure_type"],
                size_agreement=row["agreement"]["size_key"],
            )
        )
    lines.append("")
    lines.append("## 运行信息")
    lines.append("")
    lines.append(f"- generated_at: `{payload['generated_at']}`")
    lines.append(f"- qwen_model: `{payload['qwen_model']}`")
    lines.append(f"- manifest: `{payload['manifest']}`")
    lines.append(f"- prompt_version: `{payload['prompt_version']}`")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def build_payload(
    samples: list[Sample],
    *,
    root: Path,
    qwen_results: Mapping[str, Mapping[str, Any]],
    mcp_results: Mapping[str, Mapping[str, Any]],
    qwen_model: str,
    manifest: Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sample in samples:
        qwen = dict(qwen_results[sample.sample_id]) if sample.sample_id in qwen_results else None
        mcp = dict(mcp_results[sample.sample_id]) if sample.sample_id in mcp_results else None
        if mcp is not None and "normalized" not in mcp and "error" not in mcp:
            mcp["normalized"] = normalize_provider_result(mcp)
        row = {
            "sample_id": sample.sample_id,
            "expected_structure_type": sample.expected_structure_type,
            "selection_note": sample.selection_note,
            "relative_path": sample.relative_path,
            "image_path": str(root / sample.relative_path),
            "qwen": qwen,
            "mcp": mcp,
            "agreement": agreement(qwen, mcp),
        }
        rows.append(row)

    qwen_success = sum(1 for row in rows if row["qwen"] and not row["qwen"].get("error"))
    mcp_success = sum(1 for row in rows if row["mcp"] and not row["mcp"].get("error"))
    type_comparable = [row for row in rows if row["agreement"]["structure_type"] != "无法比较"]
    size_comparable = [row for row in rows if row["agreement"]["size_key"] != "无法比较"]
    return {
        "schema_version": 1,
        "generated_at": now_utc(),
        "manifest": str(manifest),
        "root": str(root),
        "prompt_version": "structure-total-span-height-v1",
        "qwen_model": qwen_model,
        "results": rows,
        "summary": {
            "total": len(rows),
            "qwen_success": qwen_success,
            "mcp_success": mcp_success,
            "mcp_missing": sum(1 for row in rows if row["mcp"] is None),
            "type_comparable_count": len(type_comparable),
            "type_agreement_count": sum(1 for row in type_comparable if row["agreement"]["structure_type"] == "一致"),
            "size_comparable_count": len(size_comparable),
            "size_agreement_count": sum(1 for row in size_comparable if row["agreement"]["size_key"] == "一致"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Qwen and MCP results for structural total span and height.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, required=True, help="question-bank root; no images are copied")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--mcp-results", type=Path, default=None, help="JSON captured from MCP vision calls")
    parser.add_argument("--reuse-qwen-results", type=Path, default=None, help="prior evaluator results.json whose Qwen rows are reused without another request")
    parser.add_argument("--skip-qwen", action="store_true")
    parser.add_argument("--qwen-model", default=DEFAULT_MODEL)
    parser.add_argument("--qwen-endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--qwen-timeout", type=int, default=90)
    args = parser.parse_args()

    root = args.root.resolve()
    samples = load_manifest(args.manifest.resolve(), root)
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    qwen_results: dict[str, dict[str, Any]] = {}
    if args.reuse_qwen_results:
        qwen_results = load_saved_provider_results(args.reuse_qwen_results, "qwen")
    elif not args.skip_qwen:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise SystemExit("DASHSCOPE_API_KEY missing; refusing to read a key from configuration files")
        for index, sample in enumerate(samples, 1):
            image_path = root / sample.relative_path
            started = time.perf_counter()
            try:
                normalized, usage, raw_content = call_qwen(
                    image_path,
                    api_key=api_key,
                    endpoint=args.qwen_endpoint,
                    model=args.qwen_model,
                    timeout=args.qwen_timeout,
                )
                qwen_results[sample.sample_id] = {
                    "normalized": normalized,
                    "usage": usage,
                    "raw_content": raw_content,
                    "seconds": round(time.perf_counter() - started, 3),
                }
                print(f"{index:02d}/{len(samples)} qwen ok {sample.sample_id}")
            except Exception as exc:  # noqa: BLE001 - preserve per-image failure in an evaluation report.
                qwen_results[sample.sample_id] = {
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.perf_counter() - started, 3),
                }
                print(f"{index:02d}/{len(samples)} qwen failed {sample.sample_id}: {type(exc).__name__}")

    mcp_results = load_mcp_results(args.mcp_results)
    payload = build_payload(
        samples,
        root=root,
        qwen_results=qwen_results,
        mcp_results=mcp_results,
        qwen_model=args.qwen_model,
        manifest=args.manifest.resolve(),
    )
    (output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(output_dir / "comparison.md", payload)
    (output_dir / "mcp_prompt.txt").write_text(DIMENSION_PROMPT, encoding="utf-8")
    (output_dir / "mcp_results_template.json").write_text(
        json.dumps(mcp_results_template(samples), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"results={output_dir / 'results.json'}")
    print(f"comparison={output_dir / 'comparison.md'}")
    print(f"mcp_prompt={output_dir / 'mcp_prompt.txt'}")
    print(f"mcp_template={output_dir / 'mcp_results_template.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

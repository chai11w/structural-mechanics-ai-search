"""Run an isolated Qwen/MCP structural-dimension comparison experiment.

This utility never touches the question-bank indexes or production runtime state.
It reads a versioned sample manifest, submits each selected image to Qwen (optional),
merges separately captured MCP vision responses, and writes review-only artifacts to
an ignored output directory, including a local original-image review page.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
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

from dimensions import Dimension, canonical_dimensions, dimension_text, normalize_dimension, normalized_dimension_symbol, sum_dimension_segments  # noqa: E402

VALID_STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱", "组合结构", "unknown"}
DEFAULT_MANIFEST = BASE / "experiments" / "structure_dimension_eval" / "samples.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_structure_dimension_eval"

DIMENSION_PROMPT_VERSION = "structure-dimension-segment-transcription-v5"

DIMENSION_PROMPT = """你是结构力学题图的结构类型与外围尺寸识别器。只看主承重骨架，不解题；严格只输出 JSON。

目标：
- structure_type 只能是 梁、钢架、桁架、拱、组合结构、unknown。
- total_span 是骨架最左端到最右端的水平总长；total_height 是最低端到最高端的竖直总高。程序之后自行取长×宽。

尺寸规则：
1. 只使用图中明确写出的尺寸标注。禁止按像素、纸面比例、文字、荷载箭头或支座估算。
2. 沿能覆盖外围总长的一条标注链，从左到右原样抄入 horizontal_segments；沿能覆盖外围总高的一条标注链，从下到上原样抄入 vertical_segments。每段一个元素，不合并、不拆分、不写求和式。平行位置若有多条尺寸链，只选能包住整个主骨架、总尺寸更长的外侧尺寸链；忽略较短的内侧尺寸链，绝不能把多条链相加。
3. 原样保留每段的数字、字母和单位，例如 "a"、"a/2"、"2a"、"6m"。不要统一字母或删除单位，程序会归一化并求和。某段存在但读不清时写 null。
4. total_span、total_height 是你对两轴总长的独立判断，只能写一个简单值或 null。分段与总长不一致时照实输出，禁止为凑一致而增删、改写分段或总长。
5. 水平直杆的 vertical_segments 为 []、total_height 为 "0"；竖直直杆的 horizontal_segments 为 []、total_span 为 "0"。
6. 拱高没有明确标注时写 null，禁止计算或猜测。任何尺寸不可靠时宁可写 null。

输出格式：
{"structure_type":"梁|钢架|桁架|拱|组合结构|unknown","horizontal_segments":["a","a/2",null],"vertical_segments":[],"total_span":"3a|null","total_height":"0|null","confidence":0.0,"reason":"不超过20字"}"""


@dataclass(frozen=True)
class Sample:
    sample_id: str
    expected_structure_type: str
    relative_path: str
    selection_note: str


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def normalize_provider_result(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Put Qwen or MCP output into the shared, conservative report schema.

    When the model transcribes ``horizontal_segments`` / ``vertical_segments``,
    the code sums each axis itself and uses that sum as the authoritative value;
    the model's stated ``total_span`` / ``total_height`` become a cross-check only.
    ``dimensions_verified`` is True only when every segment parsed and the code
    sum agrees with the model's estimate, so a hard filter can safely skip rows
    the model read inconsistently.
    """

    source = dict(value or {})
    structure_type = str(source.get("structure_type") or "unknown").strip()
    if structure_type not in VALID_STRUCTURE_TYPES:
        structure_type = "unknown"

    horizontal_segments = list(source.get("horizontal_segments") or [])
    vertical_segments = list(source.get("vertical_segments") or [])
    h_sum = sum_dimension_segments(horizontal_segments)
    v_sum = sum_dimension_segments(vertical_segments)

    model_span = normalize_dimension(source.get("total_span"))
    model_height = normalize_dimension(source.get("total_height"))
    code_span = h_sum["dimension"] if h_sum["error"] is None else None
    code_height = v_sum["dimension"] if v_sum["error"] is None else None

    def consistent(code_dim: Dimension | None, model_dim: Dimension | None) -> bool | None:
        if code_dim is None or model_dim is None:
            return None
        return dimension_text(code_dim) == dimension_text(model_dim)

    span_consistent = consistent(code_span, model_span)
    height_consistent = consistent(code_height, model_height)

    span_dim = code_span if code_span is not None else model_span
    height_dim = code_height if code_height is not None else model_height
    dimensions = canonical_dimensions(
        dimension_text(span_dim) if span_dim else None,
        dimension_text(height_dim) if height_dim else None,
    )
    try:
        confidence = max(0.0, min(1.0, float(source.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    h_present = len(horizontal_segments) > 0
    v_present = len(vertical_segments) > 0

    def axis_verified(present: bool, code_ok: bool, consistent: bool | None) -> bool:
        if not present:
            return True
        return code_ok and consistent is not False

    verified = (
        axis_verified(h_present, h_sum["error"] is None, span_consistent)
        and axis_verified(v_present, v_sum["error"] is None, height_consistent)
        and (h_present or v_present)
    )
    return {
        "structure_type": structure_type,
        "total_span": dimension_text(model_span) if model_span else None,
        "total_height": dimension_text(model_height) if model_height else None,
        "long": dimensions["long"] if dimensions else None,
        "width": dimensions["width"] if dimensions else None,
        "long_width": dimensions["long_width"] if dimensions else "unknown",
        "confidence": confidence,
        "reason": str(source.get("reason") or "").strip()[:200],
        "horizontal_segments": horizontal_segments,
        "vertical_segments": vertical_segments,
        "code_span": dimension_text(code_span) if code_span is not None else None,
        "code_height": dimension_text(code_height) if code_height is not None else None,
        "span_consistent": span_consistent,
        "height_consistent": height_consistent,
        "dimensions_verified": verified,
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
                    result["normalized"] = normalize_provider_result(result.get("normalized"))
            elif "error" not in result:
                result["normalized"] = normalize_provider_result(result.get("normalized"))
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


def consistency_label(value: object) -> str:
    if value is True:
        return "✓一致"
    if value is False:
        return "✗不一致"
    return "无法核对"


def segments_text(value: object) -> str:
    segments = list(value or [])
    if not segments:
        return "—"
    return "、".join(str(segment) for segment in segments)


def code_sums_text(normalized: Mapping[str, Any]) -> str:
    parts = []
    if normalized.get("code_span") is not None:
        parts.append(f"水平{normalized['code_span']}")
    if normalized.get("code_height") is not None:
        parts.append(f"竖直{normalized['code_height']}")
    return "、".join(parts) if parts else "—"


def axis_consistency_text(normalized: Mapping[str, Any]) -> str:
    labels = []
    if normalized.get("span_consistent") is not None:
        labels.append(f"水平{consistency_label(normalized['span_consistent'])}")
    if normalized.get("height_consistent") is not None:
        labels.append(f"竖直{consistency_label(normalized['height_consistent'])}")
    return "、".join(labels) if labels else "无法核对"


def provider_cell(result: Mapping[str, Any] | None) -> str:
    if not result:
        return "未提供"
    if result.get("error"):
        return f"失败：{markdown_escape(str(result['error']))}"
    normalized = result.get("normalized") or {}
    return "<br>".join(
        [
            f"类型：{markdown_escape(str(normalized.get('structure_type') or 'unknown'))}",
            f"水平各段：{markdown_escape(segments_text(normalized.get('horizontal_segments')))}",
            f"竖直各段：{markdown_escape(segments_text(normalized.get('vertical_segments')))}",
            f"代码求和：{markdown_escape(code_sums_text(normalized))}",
            f"模型总长：{markdown_escape(str(normalized.get('total_span') or 'unknown'))}",
            f"模型总高：{markdown_escape(str(normalized.get('total_height') or 'unknown'))}",
            f"核对：{markdown_escape(axis_consistency_text(normalized))}",
            f"长×宽：{markdown_escape(str(normalized.get('long_width') or 'unknown'))}",
            f"置信度：{float(normalized.get('confidence') or 0):.2f}",
            f"理由：{markdown_escape(str(normalized.get('reason') or ''))}",
        ]
    )


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def comparable_type(result: Mapping[str, Any] | None) -> str | None:
    value = str(((result or {}).get("normalized") or {}).get("structure_type") or "unknown")
    return value if value != "unknown" else None


def comparable_long_width(result: Mapping[str, Any] | None) -> str | None:
    value = str(((result or {}).get("normalized") or {}).get("long_width") or "unknown")
    return value if value != "unknown" else None


def agreement(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, str]:
    left_type, right_type = comparable_type(left), comparable_type(right)
    left_dimensions, right_dimensions = comparable_long_width(left), comparable_long_width(right)
    type_agreement = "一致" if left_type and left_type == right_type else "无法比较" if not (left_type and right_type) else "不一致"
    dimensions_agreement = "一致" if left_dimensions and left_dimensions == right_dimensions else "无法比较" if not (left_dimensions and right_dimensions) else "不一致"
    return {"structure_type": type_agreement, "long_width": dimensions_agreement}


def write_markdown_report(output_path: Path, payload: Mapping[str, Any]) -> None:
    rows = payload["results"]
    lines = ["# 结构总跨/总高度双模型对照实验", ""]
    lines.append("- 目的：比较 Qwen 与 MCP 视觉模型对主承重骨架结构类型、总跨、总高度及归一长×宽的识别结果。")
    lines.append("- 尺寸口径：总跨为主骨架最左至最右的总水平跨度；总高度为主骨架最低至最高的总竖向高度。多跨同类尺寸应相加；单条水平梁总高度为 `0`。")
    lines.append("- 归一长×宽：长为总跨与总高度中的较大值，宽为较小值；符号尺寸中的字母统一为 `L`，物理单位保留；未知或不能可靠合并时为 `unknown`。不计算比例。")
    lines.append("- 原题图与两侧结果也可在同目录 `review.html` 逐图查看；这是一份模型间一致性对照，不把目录类别或两模型一致直接表述为正确性。")
    lines.append("")
    summary = payload["summary"]
    lines.append(f"- 样本：{summary['total']}；Qwen 成功：{summary['qwen_success']}；MCP 成功：{summary['mcp_success']}；MCP 未提供：{summary['mcp_missing']}")
    lines.append(f"- 可比较结构类型一致：{summary['type_agreement_count']}/{summary['type_comparable_count']}；可比较长×宽一致：{summary['long_width_agreement_count']}/{summary['long_width_comparable_count']}")
    lines.append("")
    lines.append("| 样本 | 题图 | 目录类别（非尺寸真值） | Qwen | MCP | 结构类型 | 长×宽 | 人工裁决 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in rows:
        image_path = row.get("review_image_path") or Path(row["image_path"]).resolve().as_uri()
        lines.append(
            "| {sample_id} | ![{sample_id}]({image_path}) | {expected} | {qwen} | {mcp} | {type_agreement} | {dimensions_agreement} | 待填写 |".format(
                sample_id=markdown_escape(row["sample_id"]),
                image_path=image_path,
                expected=markdown_escape(row["expected_structure_type"] or "未标注"),
                qwen=provider_cell(row.get("qwen")),
                mcp=provider_cell(row.get("mcp")),
                type_agreement=row["agreement"]["structure_type"],
                dimensions_agreement=row["agreement"]["long_width"],
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


def review_provider_html(result: Mapping[str, Any] | None) -> str:
    if not result:
        return "<p class=\"unavailable\">未提供</p>"
    if result.get("error"):
        return f"<p class=\"error\">失败：{html.escape(str(result['error']))}</p>"
    normalized = result.get("normalized") or {}
    fields = (
        ("结构类型", normalized.get("structure_type") or "unknown"),
        ("水平各段", segments_text(normalized.get("horizontal_segments"))),
        ("竖直各段", segments_text(normalized.get("vertical_segments"))),
        ("代码求和", code_sums_text(normalized)),
        ("模型总长", normalized.get("total_span") or "unknown"),
        ("模型总高", normalized.get("total_height") or "unknown"),
        ("交叉核对", axis_consistency_text(normalized)),
        ("长×宽", normalized.get("long_width") or "unknown"),
        ("置信度", f"{float(normalized.get('confidence') or 0):.2f}"),
        ("理由", normalized.get("reason") or ""),
    )
    return "<dl>" + "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(value))}</dd>" for label, value in fields
    ) + "</dl>"


def write_review_html(output_path: Path, payload: Mapping[str, Any]) -> None:
    cards = []
    for row in payload["results"]:
        image_path = html.escape(str(row["review_image_path"]), quote=True)
        cards.append(
            """
<section class="card">
  <h2>{sample_id}</h2>
  <p class="meta">目录类别（非尺寸真值）：{expected}。{note}</p>
  <div class="content">
    <figure><img src="{image_path}" alt="{sample_id} 原题图"><figcaption>原题图</figcaption></figure>
    <div class="results">
      <article><h3>Qwen</h3>{qwen}</article>
      <article><h3>MCP</h3>{mcp}</article>
      <article><h3>对照 / 人工裁决</h3><dl><dt>结构类型</dt><dd>{type_agreement}</dd><dt>长×宽</dt><dd>{dimensions_agreement}</dd><dt>人工裁决</dt><dd>待填写</dd></dl></article>
    </div>
  </div>
</section>""".format(
                sample_id=html.escape(str(row["sample_id"])),
                expected=html.escape(str(row["expected_structure_type"] or "未标注")),
                note=html.escape(str(row["selection_note"] or "")),
                image_path=image_path,
                qwen=review_provider_html(row.get("qwen")),
                mcp=review_provider_html(row.get("mcp")),
                type_agreement=html.escape(row["agreement"]["structure_type"]),
                dimensions_agreement=html.escape(row["agreement"]["long_width"]),
            )
        )
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>结构尺寸双模型逐图审阅</title>
<style>
body{{margin:0;background:#f4f6f8;color:#1f2937;font:16px/1.5 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1500px;margin:auto;padding:24px}}h1{{margin-top:0}}.notice{{background:#fff7ed;border-left:4px solid #f97316;padding:12px 16px}}.card{{background:white;border:1px solid #d8dee8;border-radius:10px;margin:20px 0;padding:20px;box-shadow:0 1px 2px #0000000b}}.card h2{{margin:0}}.meta{{color:#4b5563}}.content{{display:grid;grid-template-columns:minmax(380px,1fr) minmax(480px,1.3fr);gap:20px;align-items:start}}figure{{margin:0;background:#f8fafc;border:1px solid #e2e8f0;padding:12px}}img{{display:block;width:100%;height:auto;max-height:760px;object-fit:contain;background:white}}figcaption{{text-align:center;color:#475569;margin-top:8px}}.results{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}article{{background:#f8fafc;border:1px solid #e2e8f0;padding:12px;min-height:180px}}article h3{{margin:0 0 8px}}dl{{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0}}dt{{font-weight:700;color:#475569}}dd{{margin:0;word-break:break-word}}.error{{color:#b91c1c}}.unavailable{{color:#64748b}}@media(max-width:900px){{.content{{grid-template-columns:1fr}}.results{{grid-template-columns:1fr}}}}
</style></head><body><main><h1>结构总跨 / 总高度：双模型逐图审阅</h1><p class="notice">每张原题图与 Qwen、MCP 结果同页相邻展示。长=max(总跨度, 总高度)，宽=min(总跨度, 总高度)；字母变量统一为 L，不计算比例。MCP 无结果或失败会如实显示。</p>{cards}</main></body></html>""".format(cards="\n".join(cards))
    output_path.write_text(document, encoding="utf-8")


def copy_review_images(samples: list[Sample], *, root: Path, output_dir: Path) -> dict[str, str]:
    """Copy validated originals into the ignored review artifact directory."""

    images_dir = output_dir / "original_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    review_paths: dict[str, str] = {}
    for sample in samples:
        source = root / sample.relative_path
        suffix = source.suffix.lower() or ".img"
        target = images_dir / f"{sample.sample_id}{suffix}"
        shutil.copy2(source, target)
        review_paths[sample.sample_id] = target.relative_to(output_dir).as_posix()
    return review_paths


def build_payload(
    samples: list[Sample],
    *,
    root: Path,
    qwen_results: Mapping[str, Mapping[str, Any]],
    mcp_results: Mapping[str, Mapping[str, Any]],
    qwen_model: str,
    manifest: Path,
    review_image_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    review_image_paths = review_image_paths or {}
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
            "review_image_path": review_image_paths.get(sample.sample_id),
            "qwen": qwen,
            "mcp": mcp,
            "agreement": agreement(qwen, mcp),
        }
        rows.append(row)

    qwen_success = sum(1 for row in rows if row["qwen"] and not row["qwen"].get("error"))
    mcp_success = sum(1 for row in rows if row["mcp"] and not row["mcp"].get("error"))
    type_comparable = [row for row in rows if row["agreement"]["structure_type"] != "无法比较"]
    long_width_comparable = [row for row in rows if row["agreement"]["long_width"] != "无法比较"]
    return {
        "schema_version": 3,
        "generated_at": now_utc(),
        "manifest": str(manifest),
        "root": str(root),
        "prompt_version": DIMENSION_PROMPT_VERSION,
        "qwen_model": qwen_model,
        "results": rows,
        "summary": {
            "total": len(rows),
            "qwen_success": qwen_success,
            "mcp_success": mcp_success,
            "mcp_missing": sum(1 for row in rows if row["mcp"] is None),
            "type_comparable_count": len(type_comparable),
            "type_agreement_count": sum(1 for row in type_comparable if row["agreement"]["structure_type"] == "一致"),
            "long_width_comparable_count": len(long_width_comparable),
            "long_width_agreement_count": sum(1 for row in long_width_comparable if row["agreement"]["long_width"] == "一致"),
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
    review_image_paths = copy_review_images(samples, root=root, output_dir=output_dir)
    payload = build_payload(
        samples,
        root=root,
        qwen_results=qwen_results,
        mcp_results=mcp_results,
        qwen_model=args.qwen_model,
        manifest=args.manifest.resolve(),
        review_image_paths=review_image_paths,
    )
    (output_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(output_dir / "comparison.md", payload)
    write_review_html(output_dir / "review.html", payload)
    (output_dir / "mcp_prompt.txt").write_text(DIMENSION_PROMPT, encoding="utf-8")
    (output_dir / "mcp_results_template.json").write_text(
        json.dumps(mcp_results_template(samples), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"results={output_dir / 'results.json'}")
    print(f"comparison={output_dir / 'comparison.md'}")
    print(f"review={output_dir / 'review.html'}")
    print(f"original_images={output_dir / 'original_images'}")
    print(f"mcp_prompt={output_dir / 'mcp_prompt.txt'}")
    print(f"mcp_template={output_dir / 'mcp_results_template.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

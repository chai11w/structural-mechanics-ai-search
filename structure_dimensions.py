"""Shared V5.2 structural-dimension recognition for experiments and retrieval."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from dimensions import (
    Dimension,
    canonical_dimensions,
    dimension_text,
    normalize_dimension,
    sum_dimension_segments,
)
from scripts.classify_question_bank import (
    image_to_data_url,
    parse_model_json,
    tracked_qwen_request,
)


VALID_STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱", "组合结构", "unknown"}
DIMENSION_PROMPT_VERSION = "structure-dimension-segment-transcription-v5.2"

DIMENSION_PROMPT = """你是结构力学题图的外围尺寸转录器。结构类型已由上游确定并在用户消息中提供，禁止重新判断或修改结构类型。只看主承重骨架，不解题；严格只输出 JSON。

目标：
- total_span 是骨架最左端到最右端的水平总长；total_height 是最低端到最高端的竖直总高。程序之后自行取长×宽。

尺寸规则：
1. 只使用图中明确写出的尺寸标注。禁止按像素、纸面比例、文字、荷载箭头或支座估算。
2. 沿能覆盖外围总长的一条标注链，从左到右原样抄入 horizontal_segments；沿能覆盖外围总高的一条标注链，从下到上原样抄入 vertical_segments。每段一个元素，不合并、不拆分、不写求和式。平行位置若有多条尺寸链，只选能包住整个主骨架、总尺寸更长的外侧尺寸链；忽略较短的内侧尺寸链，绝不能把多条链相加。
3. 原样保留每段的数字、字母和单位，例如 "a"、"a/2"、"2a"、"6m"。不要统一字母或删除单位，程序会归一化并求和。某段存在但读不清时写 null。
4. total_span、total_height 是你对两轴总长的独立判断，只能写一个简单值或 null。分段与总长不一致时照实输出，禁止为凑一致而增删、改写分段或总长。
5. 只有已知类型为梁且主骨架确为单一直杆时，未延伸方向才可写 "0"。已知类型为钢架、桁架、组合结构时，绝不允许任何轴写 "0"；未标注的轴必须写 null。
6. 已知类型为拱时不参与尺寸识别：两组 segments 均为 []，total_span、total_height 均写 null。任何尺寸不可靠时宁可写 null。

输出格式：
{"horizontal_segments":["a","a/2",null],"vertical_segments":[],"total_span":"3a|null","total_height":"0|null","confidence":0.0,"reason":"不超过20字"}"""


def normalize_provider_result(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize provider output and explicitly classify full/single/conflict."""

    source = dict(value or {})
    structure_type = str(source.get("structure_type") or "unknown").strip()
    if structure_type not in VALID_STRUCTURE_TYPES:
        structure_type = "unknown"

    if structure_type == "拱":
        horizontal_segments: list[Any] = []
        vertical_segments: list[Any] = []
    else:
        horizontal_segments = list(source.get("horizontal_segments") or [])
        vertical_segments = list(source.get("vertical_segments") or [])
    h_sum = sum_dimension_segments(horizontal_segments)
    v_sum = sum_dimension_segments(vertical_segments)

    model_span = normalize_dimension(source.get("total_span")) if structure_type != "拱" else None
    model_height = normalize_dimension(source.get("total_height")) if structure_type != "拱" else None
    code_span = h_sum["dimension"] if h_sum["error"] is None else None
    code_height = v_sum["dimension"] if v_sum["error"] is None else None

    zero_axis_discarded = False
    if structure_type in {"钢架", "桁架", "组合结构"}:
        if model_span is not None and dimension_text(model_span) == "0":
            model_span = None
            zero_axis_discarded = True
        if model_height is not None and dimension_text(model_height) == "0":
            model_height = None
            zero_axis_discarded = True
        if code_span is not None and dimension_text(code_span) == "0":
            code_span = None
            zero_axis_discarded = True
        if code_height is not None and dimension_text(code_height) == "0":
            code_height = None
            zero_axis_discarded = True

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
    type_conflict = bool(
        structure_type == "梁" and dimensions and dimensions.get("width") != "0"
    )
    if type_conflict:
        dimensions = None

    try:
        confidence = max(0.0, min(1.0, float(source.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    h_present = len(horizontal_segments) > 0
    v_present = len(vertical_segments) > 0

    def axis_verified(present: bool, code_ok: bool, is_consistent: bool | None) -> bool:
        if not present:
            return True
        return code_ok and is_consistent is not False

    verified = (
        axis_verified(h_present, h_sum["error"] is None, span_consistent)
        and axis_verified(v_present, v_sum["error"] is None, height_consistent)
        and (h_present or v_present)
    )

    positive_axes = []
    for axis in (span_dim, height_dim):
        if axis is None or axis.coefficient <= 0:
            continue
        text = dimension_text(axis)
        if text not in positive_axes:
            positive_axes.append(text)

    if structure_type == "拱":
        dimension_state = "skip"
        single_side = None
    elif type_conflict:
        dimension_state = "conflict"
        single_side = None
    elif dimensions:
        dimension_state = "full"
        single_side = None
    elif len(positive_axes) == 1:
        dimension_state = "single"
        single_side = positive_axes[0]
    elif len(positive_axes) > 1:
        dimension_state = "conflict"
        single_side = None
    else:
        dimension_state = "none"
        single_side = None

    return {
        "structure_type": structure_type,
        "total_span": dimension_text(model_span) if model_span else None,
        "total_height": dimension_text(model_height) if model_height else None,
        "long": dimensions["long"] if dimensions else None,
        "width": dimensions["width"] if dimensions else None,
        "long_width": dimensions["long_width"] if dimensions else "unknown",
        "single_side": single_side,
        "dimension_state": dimension_state,
        "dimension_type_conflict": type_conflict,
        "zero_axis_discarded": zero_axis_discarded,
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


def build_qwen_request(
    image_path: Path,
    *,
    model: str,
    known_structure_type: str,
) -> dict[str, Any]:
    known_type = known_structure_type if known_structure_type in VALID_STRUCTURE_TYPES else "unknown"
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": DIMENSION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    {
                        "type": "text",
                        "text": f"已知结构类型：{known_type}。只识别尺寸，只输出JSON。",
                    },
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
    known_structure_type: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    known_type = known_structure_type if known_structure_type in VALID_STRUCTURE_TYPES else "unknown"
    if known_type == "拱":
        raw_content = '{"skipped":"known arch"}'
        return normalize_provider_result({"structure_type": "拱"}), {}, raw_content
    request = build_qwen_request(
        image_path,
        model=model,
        known_structure_type=known_type,
    )
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
        call_type="qwen_structure_dimension",
    )
    content = str(response["choices"][0]["message"]["content"])
    parsed = dict(parse_model_json(content))
    parsed["structure_type"] = known_type
    return normalize_provider_result(parsed), dict(response.get("usage") or {}), content

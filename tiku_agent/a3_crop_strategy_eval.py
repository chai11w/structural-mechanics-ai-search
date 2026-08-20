"""Isolated helpers for comparing A3 automatic-crop strategies."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageOps

from scripts.classify_question_bank import DEFAULT_ENDPOINT, parse_model_json
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call


DIRECT_GROUNDING_SCHEMA_VERSION = "a3-direct-grounding-v1"
PADDLE_BINDING_SCHEMA_VERSION = "a3-paddle-binding-v1"


@dataclass(frozen=True)
class QwenJsonResponse:
    payload: dict[str, Any]
    raw_text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def usage_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def build_direct_grounding_prompt() -> str:
    """Return the shared prompt used by both grounding models."""

    return f"""你是结构力学题图的直接裁剪定位器。请在原图上直接定位可以送入 A2 检索的原始受力结构图，不使用外部版面候选，也不求解题目。

目标：
- 每个 target 只对应一个可独立检索的原始结构图。
- bbox 必须完整包含该结构的全部杆件、节点、支座、外荷载、荷载作用范围/方向、图中可见荷载标注和必要尺寸。
- bbox 可以保留少量空白和紧邻题号，但不能混入相邻题目的结构图、内力图、单位力图、解答图或无关内容。
- 同页多个独立题分别输出；公共题干下多个受力子图也分别输出。
- 同一道题的弯矩图、剪力图、轴力图、位移图、单位力图等辅助图不能当作 target。
- 图片不相关、只有残缺结构、主体/支座/荷载无法确认，或无法安全绑定时不要勉强画框，使用 review_required 或 no_searchable_target。

坐标：
- bbox 使用归一化整数 [x1,y1,x2,y2]，左上为 0,0，右下为 1000,1000。
- 必须满足 0 <= x1 < x2 <= 1000 且 0 <= y1 < y2 <= 1000。

只输出一个原始 JSON 对象，不要 Markdown、代码围栏或解释文字：
{{
  "schema_version":"{DIRECT_GROUNDING_SCHEMA_VERSION}",
  "page_status":"grounded|review_required|no_searchable_target",
  "targets":[{{
    "target_id":"t001",
    "question_label":"只填写实际可见题号，没有则留空",
    "group_label":"公共父题号或独立题号，没有则留空",
    "bbox":[0,0,1000,1000],
    "review_required":false,
    "reason_codes":[],
    "binding_evidence":"一句可见、可核对的绑定依据"
  }}],
  "unknowns":[]
}}

规则：
- targets 为空时 page_status 不能是 grounded。
- 只有所有 target 都可安全自动裁剪时 page_status 才能是 grounded。
- 任一 target 的完整性或绑定不确定时将其 review_required 设为 true，并把 page_status 设为 review_required。
- target_id 按阅读顺序使用 t001、t002……，不得重复。
- reason_codes 和 unknowns 只写简短稳定原因，不输出思维过程。"""


def build_paddle_binding_prompt(candidates: Sequence[Mapping[str, Any]]) -> str:
    """Build the semantic binding prompt over immutable Paddle candidates."""

    candidate_json = json.dumps(list(candidates), ensure_ascii=False, separators=(",", ":"))
    return f"""你是结构力学题图的版面候选绑定器。第一张图是原图，第二张图是 PP-StructureV3 候选框叠加图。候选框 JSON 如下：
{candidate_json}

你的任务只是在这些候选框中选择并绑定可送入 A2 的原始受力结构图。不能自行创造新坐标，也不能修改候选框坐标。

选择规则：
- 每个 binding 对应一个可独立检索的原始结构图。
- candidate_ids 可以包含一个框，也可以包含多个框；最终裁剪范围是所选框的最小外接矩形。
- 只有当所选框并集完整包含结构、全部支座、全部外荷载及其方向/范围/标注，并且没有混入另一个独立图时，才可 auto_ready。
- 弯矩图、剪力图、轴力图、位移图、单位力图、纯文字和无关图片不能单独成为 binding。
- 找不到可靠候选时 bindings 输出 []，page_status 输出 review_required 或 no_searchable_target。
- candidate_ids 只能逐字使用候选 JSON 中已有的 candidate_id，不得编造。

只输出原始 JSON：
{{
  "schema_version":"{PADDLE_BINDING_SCHEMA_VERSION}",
  "page_status":"auto_ready|review_required|no_searchable_target",
  "bindings":[{{
    "binding_id":"b001",
    "question_label":"只填写实际可见题号，没有则留空",
    "candidate_ids":["p001"],
    "review_required":false,
    "reason_codes":[],
    "binding_evidence":"一句可见、可核对的依据"
  }}],
  "unknowns":[]
}}

bindings 为空时 page_status 不能是 auto_ready；任何 binding 不确定时 review_required 必须为 true，page_status 必须为 review_required。"""


def call_qwen_json(
    image_paths: Sequence[str | Path],
    *,
    prompt: str,
    model: str,
    user_text: str = "只输出 JSON。",
    api_key: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: float = 180.0,
    max_tokens: int = 3000,
    call_type: str = "qwen_a3_crop_eval",
) -> QwenJsonResponse:
    """Call DashScope once, with no automatic retry, and validate JSON syntax."""

    key = str(api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY is not configured")
    content: list[dict[str, Any]] = []
    for image_path in image_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": image_to_model_data_url(
                        Path(image_path), normalize_orientation=True
                    )
                },
            }
        )
    content.append({"type": "text", "text": user_text})
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": max(256, int(max_tokens)),
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        str(endpoint).strip() or DEFAULT_ENDPOINT,
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def request_data() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"Qwen HTTP {exc.code}: {detail}") from exc
        if not isinstance(value, dict):
            raise ValueError("model response must be an object")
        return value

    data = timed_model_call(
        request_data,
        provider="dashscope",
        model=model,
        call_type=call_type,
        usage_getter=lambda value: value.get("usage", {}),
        request_id_getter=lambda value: str(value.get("request_id") or value.get("id") or ""),
    )
    try:
        raw_content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("invalid Qwen response envelope") from exc
    raw_text = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, ensure_ascii=False)
    parsed = parse_model_json(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("Qwen JSON output must be an object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return QwenJsonResponse(
        payload=parsed,
        raw_text=raw_text,
        model=model,
        prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
        completion_tokens=_non_negative_int(usage.get("completion_tokens")),
        total_tokens=_non_negative_int(usage.get("total_tokens")),
    )


def call_mimo_json(
    image_paths: Sequence[str | Path],
    *,
    prompt: str,
    model: str,
    user_text: str = "只输出 JSON。",
    api_key: str | None = None,
    endpoint: str = "https://api.xiaomimimo.com/v1/chat/completions",
    timeout_seconds: float = 180.0,
    max_tokens: int = 3000,
    call_type: str = "mimo_a3_crop_eval",
) -> QwenJsonResponse:
    """Call MiMo's OpenAI-compatible multimodal endpoint once."""

    key = str(api_key or os.environ.get("MIMO_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("MIMO_API_KEY is not configured")
    image_urls = [
        image_to_model_data_url(Path(image_path), normalize_orientation=True)
        for image_path in image_paths
    ]
    # MiMo's Pro model exposes multimodal input through Responses API; the
    # standard model remains on the OpenAI Chat Completions-compatible route.
    use_responses = model == "mimo-v2.5-pro"
    if use_responses:
        endpoint = "https://api.xiaomimimo.com/v1/responses"
        input_content = [
            {"type": "input_image", "image_url": url} for url in image_urls
        ]
        input_content.append({"type": "input_text", "text": user_text})
        request_payload = {
            "model": model,
            "instructions": prompt,
            "input": [{"role": "user", "content": input_content}],
            "max_output_tokens": max(256, int(max_tokens)),
            "stream": False,
            "reasoning": {"effort": "none"},
        }
    else:
        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        ]
        content.append({"type": "text", "text": user_text})
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_completion_tokens": max(256, int(max_tokens)),
            "thinking": {"type": "disabled"},
        }
    request = urllib.request.Request(
        str(endpoint).strip(),
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"api-key": key, "Content-Type": "application/json"},
        method="POST",
    )

    def request_data() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"MiMo HTTP {exc.code}: {detail}") from exc
        if not isinstance(value, dict):
            raise ValueError("model response must be an object")
        return value

    data = timed_model_call(
        request_data,
        provider="mimo",
        model=model,
        call_type=call_type,
        usage_getter=lambda value: value.get("usage", {}),
        request_id_getter=lambda value: str(value.get("id") or ""),
    )
    if use_responses:
        raw_content = data.get("output_text")
        if not raw_content:
            for item in data.get("output", []):
                for part in item.get("content", []) if isinstance(item, dict) else []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        raw_content = part.get("text")
                        break
                if raw_content:
                    break
        if not raw_content:
            raise ValueError("invalid MiMo Responses response envelope")
    else:
        try:
            raw_content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("invalid MiMo response envelope") from exc
    raw_text = raw_content if isinstance(raw_content, str) else json.dumps(raw_content, ensure_ascii=False)
    parsed = parse_model_json(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("MiMo JSON output must be an object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return QwenJsonResponse(
        payload=parsed,
        raw_text=raw_text,
        model=str(data.get("model") or model),
        prompt_tokens=_non_negative_int(usage.get("prompt_tokens") or usage.get("input_tokens")),
        completion_tokens=_non_negative_int(usage.get("completion_tokens") or usage.get("output_tokens")),
        total_tokens=_non_negative_int(usage.get("total_tokens")),
    )


def parse_direct_grounding(payload: object) -> dict[str, Any]:
    """Validate and normalize one direct-grounding response."""

    root = _object(payload, "grounding output")
    _require_keys(root, {"schema_version", "page_status", "targets", "unknowns"}, "grounding output")
    if root["schema_version"] != DIRECT_GROUNDING_SCHEMA_VERSION:
        raise ValueError("unsupported direct-grounding schema")
    status = _enum(root["page_status"], {"grounded", "review_required", "no_searchable_target"}, "page_status")
    raw_targets = _list(root["targets"], "targets")
    unknowns = _string_list(root["unknowns"], "unknowns")
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_targets):
        item = _object(value, f"targets[{index}]")
        _require_keys(
            item,
            {"target_id", "question_label", "group_label", "bbox", "review_required", "reason_codes", "binding_evidence"},
            f"targets[{index}]",
        )
        target_id = _text(item["target_id"], "target_id", allow_empty=False)
        if target_id in seen:
            raise ValueError("target_id values must be unique")
        seen.add(target_id)
        if not isinstance(item["review_required"], bool):
            raise ValueError("review_required must be boolean")
        targets.append(
            {
                "target_id": target_id,
                "question_label": _text(item["question_label"], "question_label"),
                "group_label": _text(item["group_label"], "group_label"),
                "bbox": list(_normalized_bbox(item["bbox"])),
                "review_required": item["review_required"],
                "reason_codes": _string_list(item["reason_codes"], "reason_codes"),
                "binding_evidence": _text(item["binding_evidence"], "binding_evidence"),
            }
        )
    if status == "grounded" and (not targets or any(item["review_required"] for item in targets)):
        raise ValueError("grounded status requires non-review targets")
    if not targets and status == "grounded":
        raise ValueError("empty targets cannot be grounded")
    if any(item["review_required"] for item in targets) and status != "review_required":
        raise ValueError("review target requires review_required page status")
    return {
        "schema_version": DIRECT_GROUNDING_SCHEMA_VERSION,
        "page_status": status,
        "targets": targets,
        "unknowns": unknowns,
    }


def parse_paddle_binding(
    payload: object,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate bindings and derive immutable candidate-union boxes."""

    candidate_map: dict[str, tuple[float, float, float, float]] = {}
    for index, candidate in enumerate(candidates):
        candidate_id = _text(candidate.get("candidate_id"), f"candidates[{index}].candidate_id", allow_empty=False)
        if candidate_id in candidate_map:
            raise ValueError("candidate ids must be unique")
        candidate_map[candidate_id] = _pixel_bbox(candidate.get("bbox"), f"candidates[{index}].bbox")
    root = _object(payload, "Paddle binding output")
    _require_keys(root, {"schema_version", "page_status", "bindings", "unknowns"}, "Paddle binding output")
    if root["schema_version"] != PADDLE_BINDING_SCHEMA_VERSION:
        raise ValueError("unsupported Paddle binding schema")
    status = _enum(root["page_status"], {"auto_ready", "review_required", "no_searchable_target"}, "page_status")
    raw_bindings = _list(root["bindings"], "bindings")
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, value in enumerate(raw_bindings):
        item = _object(value, f"bindings[{index}]")
        _require_keys(
            item,
            {"binding_id", "question_label", "candidate_ids", "review_required", "reason_codes", "binding_evidence"},
            f"bindings[{index}]",
        )
        binding_id = _text(item["binding_id"], "binding_id", allow_empty=False)
        if binding_id in seen:
            raise ValueError("binding_id values must be unique")
        seen.add(binding_id)
        candidate_ids = _string_list(item["candidate_ids"], "candidate_ids")
        if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("candidate_ids must be a non-empty unique list")
        unknown_ids = [value for value in candidate_ids if value not in candidate_map]
        if unknown_ids:
            raise ValueError(f"unknown candidate id: {unknown_ids[0]}")
        if not isinstance(item["review_required"], bool):
            raise ValueError("review_required must be boolean")
        selected = [candidate_map[value] for value in candidate_ids]
        bbox = [
            min(value[0] for value in selected),
            min(value[1] for value in selected),
            max(value[2] for value in selected),
            max(value[3] for value in selected),
        ]
        bindings.append(
            {
                "binding_id": binding_id,
                "question_label": _text(item["question_label"], "question_label"),
                "candidate_ids": candidate_ids,
                "bbox": bbox,
                "review_required": item["review_required"],
                "reason_codes": _string_list(item["reason_codes"], "reason_codes"),
                "binding_evidence": _text(item["binding_evidence"], "binding_evidence"),
            }
        )
    if status == "auto_ready" and (not bindings or any(item["review_required"] for item in bindings)):
        raise ValueError("auto_ready requires non-review bindings")
    if any(item["review_required"] for item in bindings) and status != "review_required":
        raise ValueError("review binding requires review_required page status")
    return {
        "schema_version": PADDLE_BINDING_SCHEMA_VERSION,
        "page_status": status,
        "bindings": bindings,
        "unknowns": _string_list(root["unknowns"], "unknowns"),
    }


def export_normalized_boxes(
    image_path: str | Path,
    items: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    *,
    id_field: str,
    normalized_1000: bool,
) -> dict[str, Any]:
    """Export crops plus a stable overlay for grounding or Paddle bindings."""

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(Path(image_path).resolve(strict=True)) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    overlay = source.copy()
    draw = ImageDraw.Draw(overlay)
    artifacts: list[dict[str, Any]] = []
    width, height = source.size
    line_width = max(2, min(width, height) // 300)
    for index, item in enumerate(items):
        item_id = _text(item.get(id_field), id_field, allow_empty=False)
        if normalized_1000:
            nx1, ny1, nx2, ny2 = _normalized_bbox(item.get("bbox"))
            bbox = (
                max(0, min(width - 1, round(nx1 * width / 1000))),
                max(0, min(height - 1, round(ny1 * height / 1000))),
                max(1, min(width, round(nx2 * width / 1000))),
                max(1, min(height, round(ny2 * height / 1000))),
            )
        else:
            px1, py1, px2, py2 = _pixel_bbox(item.get("bbox"), "bbox")
            bbox = (
                max(0, min(width - 1, round(px1))),
                max(0, min(height - 1, round(py1))),
                max(1, min(width, round(px2))),
                max(1, min(height, round(py2))),
            )
        crop_path = target_dir / f"{index + 1:02d}_{_safe_name(item_id)}.jpg"
        source.crop(bbox).save(crop_path, quality=94)
        review = bool(item.get("review_required", False))
        color = "#d97706" if review else "#16803a"
        draw.rectangle(bbox, outline=color, width=line_width)
        draw.text((bbox[0] + 4, bbox[1] + 4), item_id, fill=color)
        artifacts.append(
            {
                "id": item_id,
                "bbox_pixels": list(bbox),
                "crop_path": str(crop_path),
                "review_required": review,
            }
        )
    overlay_path = target_dir / "overlay.jpg"
    overlay.save(overlay_path, quality=92)
    return {"overlay": str(overlay_path), "crops": artifacts}


def render_paddle_candidate_overlay(
    image_path: str | Path,
    candidates: Sequence[Mapping[str, Any]],
    output_path: str | Path,
) -> Path:
    """Render candidate ids for the semantic binding model and human review."""

    with Image.open(Path(image_path).resolve(strict=True)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    draw = ImageDraw.Draw(image)
    line_width = max(2, min(image.size) // 300)
    for candidate in candidates:
        candidate_id = _text(candidate.get("candidate_id"), "candidate_id", allow_empty=False)
        x1, y1, x2, y2 = _pixel_bbox(candidate.get("bbox"), "bbox")
        box = (round(x1), round(y1), round(x2), round(y2))
        draw.rectangle(box, outline="#1f77b4", width=line_width)
        draw.text((box[0] + 4, box[1] + 4), candidate_id, fill="#1f77b4")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=92)
    return target


def write_json(path: str | Path, payload: object) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _require_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{field} keys mismatch; missing={missing}, extra={extra}")


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return value


def _text(value: object, field: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    text = value.strip()
    if not allow_empty and not text:
        raise ValueError(f"{field} cannot be empty")
    return text


def _string_list(value: object, field: str) -> list[str]:
    values = _list(value, field)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{field} must contain non-empty strings")
    return [item.strip() for item in values]


def _enum(value: object, choices: set[str], field: str) -> str:
    text = _text(value, field, allow_empty=False)
    if text not in choices:
        raise ValueError(f"unsupported {field}: {text}")
    return text


def _normalized_bbox(value: object) -> tuple[int, int, int, int]:
    values = _list(value, "bbox")
    if len(values) != 4 or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
        raise ValueError("bbox must contain four numbers")
    parsed = tuple(round(float(item)) for item in values)
    x1, y1, x2, y2 = parsed
    if min(parsed) < 0 or max(parsed) > 1000 or x2 <= x1 or y2 <= y1:
        raise ValueError("bbox must be ordered within 0..1000")
    return x1, y1, x2, y2


def _pixel_bbox(value: object, field: str) -> tuple[float, float, float, float]:
    values = _list(value, field)
    if len(values) != 4 or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
        raise ValueError(f"{field} must contain four numbers")
    parsed = tuple(float(item) for item in values)
    x1, y1, x2, y2 = parsed
    if min(parsed) < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError(f"{field} must be an ordered non-negative box")
    return x1, y1, x2, y2


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
    return cleaned[:80] or "item"

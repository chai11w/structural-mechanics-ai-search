"""Standalone semantic region mapping for the first A3 decomposition step."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal, Protocol, cast
import urllib.request
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps

from scripts.classify_question_bank import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    parse_model_json,
)
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call


A3_REGION_MAP_SCHEMA_VERSION = "a3-region-map-v1"
RegionMapStatus = Literal["ready", "uncertain", "failed"]
GroupRelationship = Literal[
    "independent_question",
    "shared_subquestions",
    "single_question_multiple_diagrams",
    "unknown",
]
RegionContentType = Literal["diagram", "text_only", "unknown"]

GROUP_RELATIONSHIPS = {
    "independent_question",
    "shared_subquestions",
    "single_question_multiple_diagrams",
    "unknown",
}
REGION_CONTENT_TYPES = {"diagram", "text_only", "unknown"}


class A3RegionMapError(RuntimeError):
    """A safe region-map failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class A3RegionGroup:
    """One independent question or one shared-stem question group."""

    group_id: str
    parent_question_label: str = ""
    relationship: GroupRelationship = "unknown"
    visible_stem_text: str = ""

    def __post_init__(self) -> None:
        if not self.group_id.strip():
            raise ValueError("region group_id is required")
        if self.relationship not in GROUP_RELATIONSHIPS:
            raise ValueError(f"unsupported group relationship: {self.relationship}")

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "parent_question_label": self.parent_question_label,
            "relationship": self.relationship,
            "visible_stem_text": self.visible_stem_text,
        }


@dataclass(frozen=True)
class A3CoarseRegion:
    """One coarse page area for later region-local OpenCV processing."""

    region_id: str
    group_id: str
    visible_labels: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    content_type: RegionContentType = "unknown"
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.region_id.strip() or not self.group_id.strip():
            raise ValueError("region_id and group_id are required")
        if self.content_type not in REGION_CONTENT_TYPES:
            raise ValueError(f"unsupported region content type: {self.content_type}")
        if any(not label.strip() for label in self.visible_labels):
            raise ValueError("visible region labels cannot be empty")
        if len(set(self.visible_labels)) != len(self.visible_labels):
            raise ValueError("visible region labels must be unique")
        x1, y1, x2, y2 = self.bbox
        if min(x1, y1) < 0 or max(x2, y2) > 100 or x2 <= x1 or y2 <= y1:
            raise ValueError("region bbox must be an ordered percentage box")

    def to_dict(self) -> dict[str, object]:
        return {
            "region_id": self.region_id,
            "group_id": self.group_id,
            "visible_labels": list(self.visible_labels),
            "bbox": list(self.bbox),
            "content_type": self.content_type,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class A3RegionMap:
    """Normalized first-step page map; it contains no chapter or load data."""

    groups: tuple[A3RegionGroup, ...]
    regions: tuple[A3CoarseRegion, ...]
    unknowns: tuple[str, ...] = ()
    schema_version: str = A3_REGION_MAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != A3_REGION_MAP_SCHEMA_VERSION:
            raise ValueError(f"unsupported region map schema: {self.schema_version}")
        group_ids = [group.group_id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("region group ids must be unique")
        region_ids = [region.region_id for region in self.regions]
        if len(set(region_ids)) != len(region_ids):
            raise ValueError("region ids must be unique")
        known_groups = set(group_ids)
        if any(region.group_id not in known_groups for region in self.regions):
            raise ValueError("region references an unknown group")

    @property
    def diagram_regions(self) -> tuple[A3CoarseRegion, ...]:
        return tuple(
            region for region in self.regions if region.content_type == "diagram"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "groups": [group.to_dict() for group in self.groups],
            "regions": [region.to_dict() for region in self.regions],
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class A3RegionModelResponse:
    raw_text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class A3RegionObserver(Protocol):
    def observe(self, image_path: Path) -> A3RegionModelResponse: ...


@dataclass(frozen=True)
class A3RegionMapResult:
    status: RegionMapStatus
    observation: A3RegionMap | None = None
    reason_codes: tuple[str, ...] = ()
    raw_response_path: str = ""
    normalized_json_path: str = ""
    overlay_path: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"ready", "uncertain", "failed"}:
            raise ValueError(f"unsupported region map status: {self.status}")
        if self.status == "ready" and self.observation is None:
            raise ValueError("ready region map requires an observation")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": A3_REGION_MAP_SCHEMA_VERSION,
            "status": self.status,
            "region_map": self.observation.to_dict() if self.observation else None,
            "reason_codes": list(self.reason_codes),
            "artifacts": {
                "raw_model_response": self.raw_response_path,
                "normalized_json": self.normalized_json_path,
                "overlay": self.overlay_path,
            },
            "model": self.model,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
        }


def build_a3_region_map_prompt() -> str:
    """Build the page-mapping prompt without doing later A3/A2 work."""

    return f"""你是结构力学复杂题图的版面区域识别助手。这是拆题的第一步，只识别题目分组和粗略区域，不裁图、不求解、不搜索题库，只输出 JSON。

输出格式：
{{
  "schema_version":"{A3_REGION_MAP_SCHEMA_VERSION}",
  "groups":[{{
    "group_id":"g1",
    "parent_question_label":"5-2",
    "relationship":"independent_question|shared_subquestions|single_question_multiple_diagrams|unknown",
    "visible_stem_text":"只原样抄写实际可见的公共题干，没有则留空"
  }}],
  "regions":[{{
    "region_id":"r1",
    "group_id":"g1",
    "visible_labels":["(a)"],
    "bbox":[0,0,100,100],
    "content_type":"diagram|text_only|unknown",
    "notes":"简短说明区域边界依据"
  }}],
  "unknowns":[]
}}

分组规则：
- 同页不等于同题。多个独立大题号分别建立 group，relationship 为 independent_question。
- 一个公共题干下的 (a)(b)(c)(d) 等子题放在同一 group，relationship 为 shared_subquestions。
- 同一道题包含原结构图、内力图、单位力图等多个图时放在同一 group，relationship 为 single_question_multiple_diagrams。本阶段不要判断各图具体角色。
- relationship 描述实际输出的 regions 之间的关系。一个 group 只有一个 region 时不能写 single_question_multiple_diagrams，应写 independent_question。
- parent_question_label 填可见的大题号或父题号；看不清可以留空，不能编造。

区域规则：
- bbox 必须使用原图百分比坐标 [x1,y1,x2,y2]，四个数都只能取 0 到 100；禁止输出像素坐标。只需给后续 OpenCV 一个可靠粗分区，不要求贴边精确。
- 每个 diagram 区域只能包含一个可独立辨认的结构力学图。一个框不能同时包住两个并列或上下排列的结构图。
- 特别检查 (a)(b)(c)(d) 等相邻子图；例如 (c) 和 (d) 必须输出两个 region，不能合并。
- visible_labels 只填写该区域紧邻的局部题号或图号。没有局部标签时输出 []；不要把父题号重复塞入每个区域。
- 如果一个候选框确实覆盖多个局部标签，visible_labels 必须全部列出并在 unknowns 说明，不能隐瞒；代码会阻止它通过第一步。
- 纯题干但没有图的独立题可输出 text_only。尺寸线、装订条、页眉、页脚、水印和纯图名不要单独建立 region。
- 页面边缘只有子图标签、零散荷载箭头或尺寸，主体承重结构不可辨认时，不建立 region。边缘残图只有在至少能看清一段相连的主体杆件及其支座或关键节点时才建立 region。
- diagram 的 bbox 只包住本图、紧邻局部标签和必要尺寸，必须在下一道大题或下一小题的题干之前停止，不能侵入下一题文字。
- 图名属于紧邻的题，不能跨到下一题。页首只露出上一题图时，可为该可见图建立独立区域。
- 题目边界不清时使用 unknown，不要靠上下顺序强行绑定。

禁止输出章节、荷载、结构类型、图角色、解题方法推断、A2 数据或 Markdown。只输出 JSON。"""


def parse_a3_region_map(
    payload: object,
    *,
    image_size: tuple[int, int] | None = None,
) -> A3RegionMap:
    """Parse and validate one model-produced region map."""

    if not isinstance(payload, dict):
        raise ValueError("region map must be an object")
    version = str(payload.get("schema_version") or "").strip()
    if version != A3_REGION_MAP_SCHEMA_VERSION:
        raise ValueError(f"unsupported region map schema: {version or 'missing'}")
    raw_groups = payload.get("groups")
    raw_regions = payload.get("regions")
    raw_unknowns = payload.get("unknowns", [])
    if not isinstance(raw_groups, list) or not isinstance(raw_regions, list):
        raise ValueError("region map requires groups and regions arrays")
    if not isinstance(raw_unknowns, list):
        raise ValueError("region map unknowns must be an array")

    parsed_bboxes = [
        _parse_percentage_bbox(raw.get("bbox"))
        if isinstance(raw, dict)
        else None
        for raw in raw_regions
    ]
    pixel_bbox_mode = any(
        bbox is not None and max(bbox) > 100 for bbox in parsed_bboxes
    )
    if pixel_bbox_mode:
        if image_size is None:
            raise ValueError("pixel region bboxes require the source image size")
        parsed_bboxes = [
            _pixel_bbox_to_percentages(bbox, image_size)
            if bbox is not None
            else None
            for bbox in parsed_bboxes
        ]

    groups: list[A3RegionGroup] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("region group must be an object")
        relationship = str(raw.get("relationship") or "unknown").strip()
        if relationship not in GROUP_RELATIONSHIPS:
            raise ValueError(f"unsupported group relationship: {relationship}")
        groups.append(
            A3RegionGroup(
                group_id=str(raw.get("group_id") or "").strip(),
                parent_question_label=str(
                    raw.get("parent_question_label") or ""
                ).strip(),
                relationship=cast(GroupRelationship, relationship),
                visible_stem_text=str(raw.get("visible_stem_text") or "").strip(),
            )
        )

    regions: list[A3CoarseRegion] = []
    for index, raw in enumerate(raw_regions):
        if not isinstance(raw, dict):
            raise ValueError("coarse region must be an object")
        labels = raw.get("visible_labels", [])
        if not isinstance(labels, list):
            raise ValueError("visible_labels must be an array")
        bbox = parsed_bboxes[index]
        if bbox is None:
            raise ValueError("coarse region must be an object")
        content_type = str(raw.get("content_type") or "unknown").strip()
        if content_type not in REGION_CONTENT_TYPES:
            raise ValueError(f"unsupported region content type: {content_type}")
        regions.append(
            A3CoarseRegion(
                region_id=str(raw.get("region_id") or "").strip(),
                group_id=str(raw.get("group_id") or "").strip(),
                visible_labels=tuple(
                    str(value).strip() for value in labels if str(value).strip()
                ),
                bbox=bbox,
                content_type=cast(RegionContentType, content_type),
                notes=str(raw.get("notes") or "").strip(),
            )
        )

    diagram_counts: dict[str, int] = {}
    for region in regions:
        if region.content_type == "diagram":
            diagram_counts[region.group_id] = (
                diagram_counts.get(region.group_id, 0) + 1
            )
    normalized_groups = tuple(
        A3RegionGroup(
            group_id=group.group_id,
            parent_question_label=group.parent_question_label,
            relationship=(
                "independent_question"
                if group.relationship == "single_question_multiple_diagrams"
                and diagram_counts.get(group.group_id, 0) == 1
                else group.relationship
            ),
            visible_stem_text=group.visible_stem_text,
        )
        for group in groups
    )

    return A3RegionMap(
        groups=normalized_groups,
        regions=tuple(regions),
        unknowns=tuple(
            str(value).strip() for value in raw_unknowns if str(value).strip()
        ),
        schema_version=version,
    )


class QwenA3RegionObserver:
    """Call Qwen once on the full page and preserve its unparsed response."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.api_key = str(api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT
        self.model = str(model).strip() or DEFAULT_MODEL
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def observe(self, image_path: Path) -> A3RegionModelResponse:
        if not self.api_key:
            raise A3RegionMapError("dashscope_not_configured")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_a3_region_map_prompt()},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_to_model_data_url(
                                    image_path, normalize_orientation=True
                                )
                            },
                        },
                        {"type": "text", "text": "只输出 JSON。"},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 2400,
            "enable_thinking": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def request_data() -> dict:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                return json.loads(response.read().decode("utf-8"))

        data = timed_model_call(
            request_data,
            provider="dashscope",
            model=self.model,
            call_type="qwen_a3_region_map",
            usage_getter=lambda value: value.get("usage", {}),
            provider_request_id_getter=lambda value: str(
                value.get("request_id") or value.get("id") or ""
            ),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise A3RegionMapError("invalid_region_model_response") from exc
        raw_text = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        )
        usage = data.get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return A3RegionModelResponse(
            raw_text=raw_text,
            model=self.model,
            prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
            completion_tokens=_non_negative_int(usage.get("completion_tokens")),
            total_tokens=_non_negative_int(usage.get("total_tokens")),
        )


class A3RegionMapLogger:
    """Record only first-step metrics, never raw text or local image paths."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, result: A3RegionMapResult, *, seconds: float) -> None:
        observation = result.observation
        record = {
            "schema_version": A3_REGION_MAP_SCHEMA_VERSION,
            "status": result.status,
            "group_count": len(observation.groups) if observation else 0,
            "region_count": len(observation.regions) if observation else 0,
            "diagram_region_count": (
                len(observation.diagram_regions) if observation else 0
            ),
            "unknown_count": len(observation.unknowns) if observation else 0,
            "reason_codes": list(result.reason_codes),
            "seconds": round(max(0.0, seconds), 3),
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class A3RegionMapRuntime:
    """Run only semantic page mapping and emit human-inspectable artifacts."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        observer: A3RegionObserver | None = None,
    ) -> None:
        self.root = Path(runtime_dir).resolve()
        self.tasks_dir = self.root / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.observer = observer or QwenA3RegionObserver()
        self.logger = A3RegionMapLogger(self.root / "a3_region_map.jsonl")

    def map_page(
        self,
        image_path: str | Path,
        *,
        observation: A3RegionMap | None = None,
    ) -> A3RegionMapResult:
        started = time.perf_counter()
        task_dir = self.tasks_dir / uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=True)
        model_response: A3RegionModelResponse | None = None
        raw_path = ""
        normalized_path = ""
        overlay_path = ""
        try:
            source = Path(image_path).resolve(strict=True)
            if observation is None:
                model_response = self.observer.observe(source)
                raw_file = task_dir / "raw_model_response.txt"
                raw_file.write_text(model_response.raw_text, encoding="utf-8")
                raw_path = str(raw_file)
                try:
                    with Image.open(source) as source_image:
                        image_size = ImageOps.exif_transpose(source_image).size
                    observation = parse_a3_region_map(
                        parse_model_json(model_response.raw_text),
                        image_size=image_size,
                    )
                except (ValueError, json.JSONDecodeError) as exc:
                    raise A3RegionMapError("invalid_region_map_schema") from exc

            normalized_file = task_dir / "region_map.json"
            normalized_file.write_text(
                json.dumps(observation.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            normalized_path = str(normalized_file)
            overlay_file = task_dir / "region_overlay.jpg"
            render_a3_region_overlay(source, observation, overlay_file)
            overlay_path = str(overlay_file)
            reason_codes = assess_a3_region_map(observation)
            result = A3RegionMapResult(
                status="ready" if not reason_codes else "uncertain",
                observation=observation,
                reason_codes=("region_map_validated",)
                if not reason_codes
                else reason_codes,
                raw_response_path=raw_path,
                normalized_json_path=normalized_path,
                overlay_path=overlay_path,
                **_model_fields(model_response),
            )
        except A3RegionMapError as exc:
            result = A3RegionMapResult(
                status=(
                    "uncertain"
                    if exc.reason_code == "invalid_region_map_schema"
                    else "failed"
                ),
                reason_codes=(exc.reason_code,),
                raw_response_path=raw_path,
                normalized_json_path=normalized_path,
                overlay_path=overlay_path,
                **_model_fields(model_response),
            )
        except (OSError, ValueError):
            result = A3RegionMapResult(
                status="failed",
                reason_codes=("region_map_io_failed",),
                raw_response_path=raw_path,
                normalized_json_path=normalized_path,
                overlay_path=overlay_path,
                **_model_fields(model_response),
            )
        except Exception:  # noqa: BLE001 - isolate provider failures from callers.
            result = A3RegionMapResult(
                status="failed",
                reason_codes=("region_map_failed",),
                raw_response_path=raw_path,
                normalized_json_path=normalized_path,
                overlay_path=overlay_path,
                **_model_fields(model_response),
            )
        self.logger.append(result, seconds=time.perf_counter() - started)
        return result


def assess_a3_region_map(observation: A3RegionMap) -> tuple[str, ...]:
    """Apply deterministic gates before a region map may enter local cropping."""

    reasons: list[str] = []
    if not observation.diagram_regions:
        reasons.append("no_diagram_regions")
    if observation.unknowns:
        reasons.append("region_map_has_unknowns")
    if any(group.relationship == "unknown" for group in observation.groups):
        reasons.append("unknown_group_relationship")
    if any(region.content_type == "unknown" for region in observation.regions):
        reasons.append("unknown_region_content")
    if any(
        region.content_type == "diagram" and len(region.visible_labels) > 1
        for region in observation.regions
    ):
        reasons.append("region_contains_multiple_labels")
    if _has_duplicate_local_labels(observation):
        reasons.append("duplicate_region_label")
    if _has_overlapping_diagram_regions(observation.diagram_regions):
        reasons.append("overlapping_diagram_regions")
    return tuple(dict.fromkeys(reasons))


def render_a3_region_overlay(
    image_path: str | Path,
    observation: A3RegionMap,
    output_path: str | Path,
) -> Path:
    """Draw stable ASCII region/group ids for manual first-step review."""

    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    colors = (
        "#d62728",
        "#1f77b4",
        "#2ca02c",
        "#9467bd",
        "#ff7f0e",
        "#17becf",
    )
    group_colors = {
        group.group_id: colors[index % len(colors)]
        for index, group in enumerate(observation.groups)
    }
    line_width = max(2, min(image.size) // 280)
    for region in observation.regions:
        x1, y1, x2, y2 = _percentage_bbox_to_pixels(region.bbox, image.size)
        color = group_colors.get(region.group_id, "#d62728")
        draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
        label = f"{region.region_id}/{region.group_id}"
        text_box = draw.textbbox((x1, y1), label)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        label_y = max(0, y1 - text_height - 6)
        draw.rectangle(
            (x1, label_y, x1 + text_width + 8, label_y + text_height + 6),
            fill=color,
        )
        draw.text((x1 + 4, label_y + 3), label, fill="white")
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=92)
    return target


def _parse_percentage_bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("region bbox must contain four values")
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("region bbox values must be numeric")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("region bbox values must be numeric") from exc
        parsed.append(round(number, 4))
    return cast(tuple[float, float, float, float], tuple(parsed))


def _pixel_bbox_to_percentages(
    bbox: tuple[float, float, float, float],
    image_size: tuple[int, int],
) -> tuple[float, float, float, float]:
    """Normalize a consistently pixel-based model response using the source size."""

    width, height = image_size
    if width <= 0 or height <= 0:
        raise ValueError("source image size must be positive")
    x1, y1, x2, y2 = bbox
    if min(bbox) < 0 or x2 > width or y2 > height:
        raise ValueError("pixel region bbox is outside the source image")
    return (
        round(x1 * 100 / width, 4),
        round(y1 * 100 / height, 4),
        round(x2 * 100 / width, 4),
        round(y2 * 100 / height, 4),
    )


def _percentage_bbox_to_pixels(
    bbox: tuple[float, float, float, float], image_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    width, height = image_size
    x1, y1, x2, y2 = bbox
    return (
        max(0, min(width - 1, round(x1 * width / 100))),
        max(0, min(height - 1, round(y1 * height / 100))),
        max(1, min(width, round(x2 * width / 100))),
        max(1, min(height, round(y2 * height / 100))),
    )


def _has_duplicate_local_labels(observation: A3RegionMap) -> bool:
    labels: list[tuple[str, str]] = []
    for region in observation.diagram_regions:
        labels.extend((region.group_id, label) for label in region.visible_labels)
    return len(labels) != len(set(labels))


def _has_overlapping_diagram_regions(
    regions: tuple[A3CoarseRegion, ...],
) -> bool:
    for index, left in enumerate(regions):
        for right in regions[index + 1 :]:
            if _intersection_over_union(left.bbox, right.bbox) >= 0.5:
                return True
    return False


def _intersection_over_union(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _model_fields(response: A3RegionModelResponse | None) -> dict[str, object]:
    return {
        "model": response.model if response else "",
        "prompt_tokens": response.prompt_tokens if response else 0,
        "completion_tokens": response.completion_tokens if response else 0,
        "total_tokens": response.total_tokens if response else 0,
    }


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

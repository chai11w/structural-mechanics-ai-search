"""Fixed A3 image decomposition without invoking the A2 search pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Callable, Protocol, cast
import urllib.request
from uuid import uuid4

from PIL import Image, ImageDraw

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - guarded at runtime.
    cv2 = None
    np = None

from scripts.classify_question_bank import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    guard_chapter_prediction,
    normalize_chapter_confidence,
    normalize_chapter_hint,
    parse_model_json,
    safe_crop,
    safe_filename_part,
    find_diagram_blocks_cv,
)
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call

from .image_contracts import (
    A3_CHAPTER_SCOPES,
    A3_DECOMPOSITION_SCHEMA_VERSION,
    A3_DIAGRAM_ROLES,
    A3DecompositionResult,
    A3DiagramObservation,
    A3PageObservation,
    ChapterHint,
    ChapterScope,
    DiagramRole,
    ProblemGroup,
    SearchUnit,
)


MAX_CANDIDATE_BLOCKS = 24


class A3DecompositionError(RuntimeError):
    """A safe internal failure with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CandidateBlock:
    block_id: int
    bbox: tuple[int, int, int, int]
    crop_path: Path


@dataclass(frozen=True)
class CandidateSet:
    blocks: tuple[CandidateBlock, ...]
    contact_sheet_path: Path


@dataclass(frozen=True)
class A3ObserverResult:
    observation: A3PageObservation
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class A3Observer(Protocol):
    def observe(
        self,
        image_path: Path,
        candidates: tuple[CandidateBlock, ...],
        contact_sheet_path: Path,
    ) -> A3ObserverResult: ...


def parse_a3_observation(payload: object) -> A3PageObservation:
    """Strictly parse model JSON and reuse the existing chapter guard."""

    if not isinstance(payload, dict):
        raise ValueError("A3 observation must be an object")
    version = str(payload.get("schema_version") or "").strip()
    if version != A3_DECOMPOSITION_SCHEMA_VERSION:
        raise ValueError(f"unsupported A3 schema: {version or 'missing'}")

    raw_groups = payload.get("groups")
    raw_diagrams = payload.get("diagrams")
    if not isinstance(raw_groups, list) or not isinstance(raw_diagrams, list):
        raise ValueError("A3 observation requires groups and diagrams arrays")

    groups: list[ProblemGroup] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("problem group must be an object")
        source_text = str(raw.get("shared_stem_text") or "").strip()
        chapter_evidence = str(raw.get("chapter_evidence") or "").strip()
        chapter_value = normalize_chapter_hint(raw.get("chapter_hint"))
        chapter_confidence = normalize_chapter_confidence(
            raw.get("chapter_confidence")
        )
        chapter_value, chapter_confidence, chapter_evidence = guard_chapter_prediction(
            chapter_value,
            chapter_confidence,
            chapter_evidence,
            source_text,
        )
        scope_text = str(raw.get("chapter_scope") or "question_group").strip()
        if scope_text not in A3_CHAPTER_SCOPES:
            raise ValueError(f"unsupported chapter scope: {scope_text}")
        member_labels = raw.get("member_labels")
        if not isinstance(member_labels, list):
            raise ValueError("problem group member_labels must be an array")
        groups.append(
            ProblemGroup(
                group_id=str(raw.get("group_id") or "").strip(),
                parent_question_label=str(
                    raw.get("parent_question_label") or ""
                ).strip(),
                member_labels=tuple(str(value).strip() for value in member_labels),
                shared_stem_text=source_text,
                shared_chapter_hint=ChapterHint(
                    value=chapter_value,
                    scope=cast(ChapterScope, scope_text),
                    source_text=source_text,
                    evidence=chapter_evidence,
                    confidence=chapter_confidence,
                ),
            )
        )

    diagrams: list[A3DiagramObservation] = []
    for raw in raw_diagrams:
        if not isinstance(raw, dict):
            raise ValueError("diagram observation must be an object")
        role = str(raw.get("role") or "unknown").strip()
        if role not in A3_DIAGRAM_ROLES:
            raise ValueError(f"unsupported diagram role: {role}")
        try:
            block_id = int(raw.get("block_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("diagram block_id must be an integer") from exc
        diagrams.append(
            A3DiagramObservation(
                block_id=block_id,
                role=cast(DiagramRole, role),
                group_id=str(raw.get("group_id") or "").strip(),
                question_label=str(raw.get("question_label") or "").strip(),
                notes=str(raw.get("notes") or "").strip(),
            )
        )

    unknowns = payload.get("unknowns", [])
    if not isinstance(unknowns, list):
        raise ValueError("unknowns must be an array")
    return A3PageObservation(
        groups=tuple(groups),
        diagrams=tuple(diagrams),
        unknowns=tuple(str(value).strip() for value in unknowns if str(value).strip()),
        schema_version=version,
    )


def build_a3_observation_prompt(candidate_count: int) -> str:
    """Describe A3-only work; chapter output is revalidated by existing code."""

    return f"""你是结构力学复杂题图拆解助手。原图和一张 OpenCV 候选图块联系表会同时提供。
联系表共有 {candidate_count} 个 block。只盘点、分组和判断图的角色，不求解，不搜索题库，只输出 JSON。

输出格式：
{{
  "schema_version":"{A3_DECOMPOSITION_SCHEMA_VERSION}",
  "groups":[{{
    "group_id":"g1",
    "parent_question_label":"5-2",
    "member_labels":["(a)","(b)"],
    "shared_stem_text":"原样抄写实际可见的公共题干，没有则留空",
    "chapter_hint":"2静定结构|3静定结构位移|4力法|5位移法|6力矩分配|7矩阵位移|8影响线|unknown",
    "chapter_scope":"question_group|search_unit|page|unknown",
    "chapter_confidence":0.0,
    "chapter_evidence":"原样引用可见文字，没有则留空"
  }}],
  "diagrams":[{{
    "block_id":1,
    "role":"original_structure|auxiliary_unit_load|internal_force_diagram|deformation_diagram|dimension_or_annotation|irrelevant|unknown",
    "group_id":"g1",
    "question_label":"(a)",
    "notes":"简短可核对说明"
  }}],
  "unknowns":[]
}}

规则：
- diagrams 必须逐一覆盖联系表中的每个 block_id，不能自造 block，也不能重复。
- 可独立搜题的每个原结构图都标为 original_structure；内力图、变形图、单位力图和局部标注不能标为原结构。
- 原结构 + 内力图 + 单位力图属于一个可检索单元；a/b/c/d 中多个独立原结构属于多个可选择单元。
- 同页不等于同题。只有题号、共享题干和版面关系明确时才能放入同一个 group。
- original_structure 必须填写 group_id 和 question_label，并且 label 必须出现在该 group 的 member_labels 中。
- 章节只根据实际可见题干或方法文字判断，不能根据结构外形、支座、EI 或荷载猜测。没有明确文字时输出 unknown，置信度不高于 0.5。
- 不输出荷载和结构类型；这些由 A2 对选中裁剪图重新识别。
- 不输出 Markdown 或解释。"""


class QwenA3Observer:
    """One structured Qwen call over the full page and numbered CV blocks."""

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

    def observe(
        self,
        image_path: Path,
        candidates: tuple[CandidateBlock, ...],
        contact_sheet_path: Path,
    ) -> A3ObserverResult:
        if not self.api_key:
            raise A3DecompositionError("dashscope_not_configured")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": build_a3_observation_prompt(len(candidates)),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_to_model_data_url(image_path)},
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_to_model_data_url(contact_sheet_path)
                            },
                        },
                        {"type": "text", "text": "第一张是原图，第二张是编号候选块。只输出 JSON。"},
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
            call_type="qwen_a3_decomposition",
            usage_getter=lambda value: value.get("usage", {}),
            request_id_getter=lambda value: str(
                value.get("request_id") or value.get("id") or ""
            ),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise A3DecompositionError("invalid_model_response") from exc
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        try:
            observation = parse_a3_observation(parse_model_json(content))
        except (ValueError, json.JSONDecodeError) as exc:
            raise A3DecompositionError("invalid_observation_schema") from exc
        usage = data.get("usage") if isinstance(data, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return A3ObserverResult(
            observation=observation,
            model=self.model,
            prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
            completion_tokens=_non_negative_int(usage.get("completion_tokens")),
            total_tokens=_non_negative_int(usage.get("total_tokens")),
        )


def detect_candidate_blocks(image_path: Path, output_dir: Path) -> CandidateSet:
    """Create deterministic candidate crops and a numbered contact sheet."""

    if cv2 is None or np is None:
        raise A3DecompositionError("opencv_unavailable")
    image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise A3DecompositionError("image_decode_failed")
    boxes = find_diagram_blocks_cv(image)
    if not boxes:
        raise A3DecompositionError("no_candidate_blocks")
    if len(boxes) > MAX_CANDIDATE_BLOCKS:
        raise A3DecompositionError("too_many_candidate_blocks")

    output_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[CandidateBlock] = []
    with Image.open(image_path).convert("RGB") as source:
        for box in boxes:
            x, y, width, height, _area = box
            crop = safe_crop(
                source,
                [x, y, x + width, y + height],
                padding_ratio=0.06,
                min_size_px=16,
            )
            if crop is None:
                continue
            block_id = len(blocks) + 1
            crop_path = output_dir / f"block_{block_id}.jpg"
            crop.save(crop_path, quality=94)
            blocks.append(
                CandidateBlock(
                    block_id=block_id,
                    bbox=(x, y, x + width, y + height),
                    crop_path=crop_path,
                )
            )
    if not blocks:
        raise A3DecompositionError("candidate_crop_failed")
    contact_sheet = _build_block_contact_sheet(
        [block.crop_path for block in blocks], output_dir / "contact_sheet.jpg"
    )
    return CandidateSet(tuple(blocks), contact_sheet)


def build_decomposition_result(
    observation: A3PageObservation,
    candidates: tuple[CandidateBlock, ...],
    output_dir: Path,
) -> A3DecompositionResult:
    """Bind validated original-structure blocks to SearchUnits."""

    candidate_by_id = {candidate.block_id: candidate for candidate in candidates}
    observed_ids = {diagram.block_id for diagram in observation.diagrams}
    expected_ids = set(candidate_by_id)
    reason_codes: list[str] = []
    ambiguous = bool(observation.unknowns)
    if observed_ids != expected_ids:
        ambiguous = True
        reason_codes.append("candidate_coverage_mismatch")
    if any(diagram.role == "unknown" for diagram in observation.diagrams):
        ambiguous = True
        reason_codes.append("unknown_diagram_role")

    groups = {group.group_id: group for group in observation.groups}
    originals = sorted(
        (
            diagram
            for diagram in observation.diagrams
            if diagram.role == "original_structure"
            and diagram.block_id in candidate_by_id
        ),
        key=lambda diagram: diagram.block_id,
    )
    duplicate_keys = {
        key
        for key in ((item.group_id, item.question_label) for item in originals)
        if sum(
            1
            for other in originals
            if (other.group_id, other.question_label) == key
        )
        > 1
    }
    if duplicate_keys:
        ambiguous = True
        reason_codes.append("duplicate_original_binding")

    quality_by_block: dict[int, tuple[str, ...]] = {}
    for diagram in originals:
        quality_flags = tuple(
            _candidate_quality_flags(candidate_by_id[diagram.block_id])
        )
        quality_by_block[diagram.block_id] = quality_flags
        if quality_flags:
            ambiguous = True
            reason_codes.extend(quality_flags)

    output_dir.mkdir(parents=True, exist_ok=True)
    units: list[SearchUnit] = []
    for index, diagram in enumerate(originals, 1):
        candidate = candidate_by_id[diagram.block_id]
        group = groups[diagram.group_id]
        quality_flags = quality_by_block[diagram.block_id]
        filename = f"unit_{index}_{safe_filename_part(diagram.question_label)}.jpg"
        final_path = output_dir / filename
        shutil.copy2(candidate.crop_path, final_path)
        units.append(
            SearchUnit(
                unit_id=f"unit_{index}",
                question_label=diagram.question_label,
                parent_question_label=group.parent_question_label,
                group_id=group.group_id,
                stem_text=group.shared_stem_text,
                primary_diagram_path=str(final_path),
                primary_diagram_bbox=candidate.bbox,
                source_block_id=candidate.block_id,
                chapter_hint=group.shared_chapter_hint,
                quality_flags=quality_flags,
                requires_user_confirmation=ambiguous,
            )
        )

    reasons = tuple(dict.fromkeys(reason_codes))
    if ambiguous:
        return A3DecompositionResult(
            status="uncertain",
            search_units=tuple(units),
            reason_codes=reasons or ("observation_has_unknowns",),
            unknowns=observation.unknowns,
        )
    if not units:
        return A3DecompositionResult(
            status="no_unit",
            reason_codes=("no_original_structure",),
        )
    if len(units) == 1:
        return A3DecompositionResult(
            status="single_ready",
            search_units=tuple(units),
            reason_codes=("decomposition_validated",),
        )
    return A3DecompositionResult(
        status="multiple_wait_choice",
        search_units=tuple(units),
        reason_codes=("multiple_units_require_selection",),
    )


class A3DecompositionLogger:
    """Append non-sensitive metrics without image paths, prompt text, or stem text."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(
        self,
        result: A3DecompositionResult,
        *,
        candidate_count: int,
        seconds: float,
        observer: A3ObserverResult | None = None,
    ) -> None:
        record = {
            "schema_version": result.schema_version,
            "status": result.status,
            "candidate_count": candidate_count,
            "unit_count": len(result.search_units),
            "reason_codes": list(result.reason_codes),
            "unknown_count": len(result.unknowns),
            "seconds": round(max(0.0, seconds), 3),
            "model": observer.model if observer else "",
            "prompt_tokens": observer.prompt_tokens if observer else 0,
            "completion_tokens": observer.completion_tokens if observer else 0,
            "total_tokens": observer.total_tokens if observer else 0,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class A3DecompositionRuntime:
    """Isolated Phase 2 runtime with no dependency on the search pipeline."""

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        observer: A3Observer | None = None,
        detector: Callable[[Path, Path], CandidateSet] = detect_candidate_blocks,
    ) -> None:
        self.root = Path(runtime_dir).resolve()
        self.tasks_dir = self.root / "tasks"
        self.incoming_dir = self.root / "incoming"
        self.sessions_dir = self.root / "sessions"
        for directory in (self.tasks_dir, self.incoming_dir, self.sessions_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.observer = observer or QwenA3Observer()
        self.detector = detector
        self.logger = A3DecompositionLogger(self.root / "a3_decomposition.jsonl")

    def decompose(
        self,
        image_path: str | Path,
        *,
        observation: A3PageObservation | None = None,
    ) -> A3DecompositionResult:
        started = time.perf_counter()
        task_dir = self.tasks_dir / uuid4().hex
        candidates: CandidateSet | None = None
        observer_result: A3ObserverResult | None = None
        try:
            source = Path(image_path).resolve(strict=True)
            candidates = self.detector(source, task_dir / "candidates")
            if observation is None:
                observer_result = self.observer.observe(
                    source, candidates.blocks, candidates.contact_sheet_path
                )
                observation = observer_result.observation
            result = build_decomposition_result(
                observation, candidates.blocks, task_dir / "units"
            )
        except A3DecompositionError as exc:
            result = A3DecompositionResult(
                status="uncertain", reason_codes=(exc.reason_code,)
            )
        except (OSError, ValueError) as exc:
            reason = (
                "invalid_observation_schema"
                if isinstance(exc, ValueError)
                else "decomposition_io_failed"
            )
            result = A3DecompositionResult(status="uncertain", reason_codes=(reason,))
        except Exception:  # noqa: BLE001 - isolate model/provider failures from callers.
            result = A3DecompositionResult(
                status="uncertain", reason_codes=("decomposition_failed",)
            )
        self.logger.append(
            result,
            candidate_count=len(candidates.blocks) if candidates else 0,
            seconds=time.perf_counter() - started,
            observer=observer_result,
        )
        return result


def _candidate_quality_flags(candidate: CandidateBlock) -> list[str]:
    x1, y1, x2, y2 = candidate.bbox
    flags: list[str] = []
    if x2 - x1 < 100 or y2 - y1 < 60:
        flags.append("crop_too_small")
    try:
        with Image.open(candidate.crop_path) as crop:
            crop.verify()
    except OSError:
        flags.append("crop_unreadable")
    return flags


def _build_block_contact_sheet(
    block_paths: list[Path], output_path: Path
) -> Path:
    """Build the same numbered review sheet without importing the search stack."""

    thumb_width, thumb_height, columns = 380, 260, 2
    rows = max(1, (len(block_paths) + columns - 1) // columns)
    sheet = Image.new("RGB", (thumb_width * columns, thumb_height * rows), "white")
    for index, path in enumerate(block_paths, 1):
        with Image.open(path).convert("RGB") as image:
            image.thumbnail((thumb_width - 20, thumb_height - 42))
            tile = Image.new("RGB", (thumb_width, thumb_height), "white")
            tile.paste(image, ((thumb_width - image.width) // 2, 34))
        ImageDraw.Draw(tile).text((10, 8), f"block_{index}", fill="black")
        sheet.paste(
            tile,
            ((index - 1) % columns * thumb_width, (index - 1) // columns * thumb_height),
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)
    return output_path


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

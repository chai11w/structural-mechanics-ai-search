"""Pure multi-question image handling shared across user-facing entry points."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - optional multi-question diagram crops.
    cv2 = None
    np = None

import search
from scripts.classify_question_bank import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    find_diagram_blocks_cv,
    image_to_data_url,
    parse_model_json,
    safe_crop,
)


BLOCK_FILTER_PROMPT = """你是结构力学图块筛选助手。图片里有若干编号 block。
请只判断哪些 block 是完整或接近完整的结构力学结构图/受力图。

返回 true 的情况:
- 包含梁、刚架、桁架、杆件、支座、荷载箭头、弯矩箭头等主体结构。
- 图形基本完整，即使缺少少量边缘也可以。

返回 false 的情况:
- 纯题干文字、页眉页脚、水印。
- 只有尺寸线、图名、题号。
- 只有局部支座或局部标注。
- 残缺到看不出主体结构。

只输出 JSON:
{"structure_blocks":[2,3,4],"rejected_blocks":[1,5,6],"reason":"简短说明"}"""


def normalize_multi_questions(raw_questions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_questions, list):
        return []
    questions = []
    for index, raw in enumerate(raw_questions, 1):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or index).strip() or str(index)
        loads = [item for item in raw.get("loads", []) if isinstance(item, dict)]
        questions.append({
            "label": label,
            "key": normalize_question_key(label),
            "bbox": raw.get("bbox"),
            "loads": loads,
            "chapter_hint": str(raw.get("chapter_hint") or "unknown").strip(),
            "chapter_confidence": safe_float(raw.get("chapter_confidence")),
            "chapter_evidence": str(raw.get("chapter_evidence") or "").strip(),
        })
    return questions


def effective_question_chapter(question: dict[str, Any], chapters: Iterable[str]) -> str | None:
    hint = str(question.get("chapter_hint") or "").strip()
    confidence = safe_float(question.get("chapter_confidence"))
    return hint if hint in set(chapters) and confidence >= 0.8 else None


def prepare_multi_diagram_crops(
    image_path: Path | str,
    questions: list[dict[str, Any]],
    output_root: Path | str,
) -> dict[str, str]:
    try:
        if cv2 is None or np is None:
            return {}
        source = Path(image_path)
        image = cv2.imdecode(np.fromfile(str(source), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return {}
        boxes = find_diagram_blocks_cv(image)
        if not boxes:
            return {}
        output_dir = Path(output_root) / f"{int(time.time() * 1000)}"
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[Path] = []
        probable_structure_paths: list[Path] = []
        with Image.open(source).convert("RGB") as pil:
            for index, box in enumerate(boxes, 1):
                x, y, w, h, _area = box
                crop = safe_crop(pil, [x, y, x + w, y + h], padding_ratio=0.06)
                if crop is None:
                    continue
                crop_path = output_dir / f"block_{index}_diagram.jpg"
                crop.save(crop_path, quality=94)
                candidates.append(crop_path)
                if is_probable_structure_block(box, image.shape):
                    probable_structure_paths.append(crop_path)

        if not candidates:
            return {}
        print(
            "multi diagram crops: "
            f"questions={len(questions)} cv_blocks={len(candidates)} "
            f"local_structure_blocks={len(probable_structure_paths)}",
            file=sys.stderr,
            flush=True,
        )
        if len(probable_structure_paths) == len(questions):
            return finalize_ordered_multi_crops(questions, probable_structure_paths, output_dir)

        api_key = os.environ.get("DASHSCOPE_API_KEY", "") or search.cfg.get("dashscope_api_key", "")
        if api_key:
            try:
                qwen_structure_paths = qwen_filter_structure_blocks(candidates, output_dir, api_key)
            except Exception as exc:  # noqa: BLE001 - fallback to load-only multi search.
                print(f"multi diagram qwen block filter failed: {exc}", file=sys.stderr, flush=True)
                qwen_structure_paths = []
            if len(qwen_structure_paths) == len(questions):
                return finalize_ordered_multi_crops(questions, qwen_structure_paths, output_dir)

        print("multi diagram crops: no reliable binding; fallback to load-only search", file=sys.stderr, flush=True)
        return {}
    except Exception as exc:  # noqa: BLE001 - crop is optional; fall back to non-reranked multi search.
        print(f"multi diagram crop failed: {exc}", file=sys.stderr, flush=True)
        return {}


def is_probable_structure_block(box: tuple[int, int, int, int, int], image_shape: tuple[int, ...]) -> bool:
    x, _y, w, h, area = box
    image_height, image_width = image_shape[:2]
    area_ratio = area / max(1, image_width * image_height)
    height_ratio = h / max(1, image_height)
    width_ratio = w / max(1, image_width)
    aspect_ratio = w / max(1, h)
    fill_ratio = area / max(1, w * h)
    return not (
        height_ratio < 0.08
        or area_ratio < 0.012
        or width_ratio < 0.18
        or aspect_ratio > 8
        or (fill_ratio > 0.75 and height_ratio < 0.12)
        or (x > image_width * 0.68 and width_ratio < 0.16)
    )


def finalize_ordered_multi_crops(
    questions: list[dict[str, Any]], selected_paths: list[Path], output_dir: Path
) -> dict[str, str]:
    crops: dict[str, str] = {}
    for index, (question, selected_path) in enumerate(zip(questions, selected_paths), 1):
        label = str(question.get("label") or index).strip()
        final_path = output_dir / f"question_{safe_filename_part(label)}_diagram.jpg"
        if selected_path != final_path:
            final_path.write_bytes(selected_path.read_bytes())
        crops[normalize_question_key(label)] = str(final_path)
    return crops


def qwen_filter_structure_blocks(block_paths: list[Path], output_dir: Path, api_key: str) -> list[Path]:
    if not block_paths:
        return []
    contact_sheet = build_block_contact_sheet(block_paths, output_dir / "block_contact_sheet.jpg")
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": BLOCK_FILTER_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_to_data_url(contact_sheet)}},
                {"type": "text", "text": "只输出JSON。"},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 256,
        "enable_thinking": False,
    }
    request = urllib.request.Request(
        DEFAULT_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    indexes = parse_model_json(data["choices"][0]["message"]["content"]).get("structure_blocks", [])
    selected = []
    if isinstance(indexes, list):
        for value in indexes:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(block_paths):
                selected.append(block_paths[index - 1])
    print(f"multi diagram qwen block filter: selected={len(selected)} seconds={time.perf_counter() - started:.2f}", file=sys.stderr, flush=True)
    return selected


def build_block_contact_sheet(block_paths: list[Path], output_path: Path) -> Path:
    thumb_width, thumb_height, columns = 380, 260, 2
    rows = max(1, (len(block_paths) + columns - 1) // columns)
    sheet = Image.new("RGB", (thumb_width * columns, thumb_height * rows), "white")
    for index, path in enumerate(block_paths, 1):
        with Image.open(path).convert("RGB") as image:
            image.thumbnail((thumb_width - 20, thumb_height - 42))
            tile = Image.new("RGB", (thumb_width, thumb_height), "white")
            tile.paste(image, ((thumb_width - image.width) // 2, 34))
        ImageDraw.Draw(tile).text((10, 8), f"block_{index}", fill="black")
        sheet.paste(tile, ((index - 1) % columns * thumb_width, (index - 1) // columns * thumb_height))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)
    return output_path


def normalize_question_key(value: object) -> str:
    text = re.sub(r"第|题|图|[()（）\s]", "", str(value or "").strip().lower())
    number = chinese_question_number_to_int(text)
    return str(number) if number is not None else text


def chinese_question_number_to_int(text: str) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits and digits[text] > 0:
        return digits[text]
    if text == "十":
        return 10
    if "十" not in text:
        return None
    left, right = text.split("十", 1)
    if left == "":
        tens = 1
    elif left in digits and digits[left] > 0:
        tens = digits[left]
    else:
        return None
    if right == "":
        ones = 0
    elif right in digits:
        ones = digits[right]
    else:
        return None
    return tens * 10 + ones


def safe_filename_part(value: object) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(value or "").strip())
    return text.strip("_") or "question"


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

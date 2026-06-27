"""
Classify the live structure-mechanics image bank into numeric vs symbolic rows.

This is intentionally read-only for the existing Excel indexes. It scans images
under the configured ROOT, calls DashScope Qwen for fresh load extraction, and
writes review artifacts under an output directory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from io import BytesIO
from datetime import datetime
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - optional CV crop experiment.
    cv2 = None
    np = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


BASE = Path(__file__).resolve().parent.parent
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DEFAULT_ROOT = Path(r"D:\桌面\答疑、帮做\结构力学\帮做")
DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配"]
CHAPTER_UNKNOWN = "unknown"
CHAPTER_TRIGGER_WORDS = {
    "2静定结构": ["静定结构", "静定刚架", "静定钢架"],
    "3静定结构位移": ["静定结构位移", "图乘法"],
    "4力法": ["力法"],
    "5位移法": ["位移法", "转角位移"],
    "6力矩分配": ["力矩分配", "弯矩分配"],
}

SYSTEM_PROMPT = """你是结构力学题目荷载提取与分类助手。请从题目图片中识别真实外部荷载，只输出JSON。

输出格式:
{
  "loads":[{"type":"集中|均布|弯矩","raw":"图中原始标注"}],
  "chapter_hint":"2静定结构|3静定结构位移|4力法|5位移法|6力矩分配|unknown",
  "chapter_confidence":0到1之间的小数,
  "chapter_evidence":"用于判断章节的题目文字或理由"
}

章节识别规则:
- 只允许从 2静定结构、3静定结构位移、4力法、5位移法、6力矩分配、unknown 中选择。
- 优先根据题目文字、方法名、题干说明判断章节，不要只根据结构图形猜章节。
- 对 4力法、5位移法、6力矩分配 必须看到明确方法文字或题干要求；不能因为结构是连续梁、超静定结构、标注 EI/刚度、支座较多，就推测为某种方法。
- 如果图片里只有结构图、荷载、支座、尺寸、EI 等信息，没有明确方法名或题干说明，chapter_hint 必须为 unknown。
- 文字出现“力矩分配法”或明确要求力矩分配，chapter_hint 为 6力矩分配。
- 文字出现“力法”或明确要求用力法计算，chapter_hint 为 4力法。
- 文字出现“位移法”或“转角位移方程”，chapter_hint 为 5位移法。
- 文字出现“静定结构位移”或明确要求用图乘法计算位移/转角，chapter_hint 为 3静定结构位移。
- 仅出现“求位移”“求转角”不能单独判断为 3静定结构位移，因为超静定结构位移计算也可能出现在力法章节。
- 只有题干明确出现“静定结构”“静定刚架/钢架”等文字时，才可判断为 2静定结构；仅要求作内力图、弯矩图、剪力图不能单独判断为 2静定结构，因为超静定结构也可能要求作内力图。
- 没有明确章节或方法线索时，chapter_hint 为 unknown，chapter_confidence 不要高于 0.5。

荷载类型只有三种:
- 集中: 集中力，如 10kN, P, F=20kN, F1=40kN, ql, 2P
- 均布: 分布荷载，如 20kN/m, q=20kN/m, q, 2P/a
- 弯矩: 力偶/集中弯矩，如 10kN·m, M=20kN·m, Pa, ql²

必须注意赋值:
- 如果题干或图片说明给出符号赋值，例如 P=40kN、q=20kN/m、F1=40kN、M=20kN·m，raw 必须保留赋值形式。
- 如果等号右侧仍是符号表达式，例如 F1=2ql、F2=2ql，raw 也保留完整形式，但它仍属于未赋值符号表达。
- F、P、Fp、F_P、F1、F2 等标在箭头旁的水平或竖向外力都必须提取，即使没有数值单位。
- 每一个独立外荷载都必须输出一条记录；不要把相同标注的多个荷载合并。比如结构上方一个 q、下方一个 q，应输出两个 {"type":"均布","raw":"q"}。

不要提取:
- 刚度 EI、EA、2EI
- 尺寸 l、L、a、b、h、4m、l/2
- 节点或杆件编号 A、B、C、1、2、3
- 支座反力 RA、RB、FA、FB、HA、VA、MA
- 公式结果 ql²/8、ql/2

无荷载时 loads 输出 []，但仍要输出章节字段。不要输出解释、Markdown或代码块。"""


LAYOUT_PROMPT = """你是结构力学题目图片分析助手。请判断图片中是一道题还是多道题，只输出JSON。

输出格式:
{
  "question_layout":"single|multi|uncertain",
  "reason":"简短说明，引用你看到的题号、分隔线或版面线索",
  "questions":[
    {
      "label":"1",
      "bbox":[0,0,100,100],
      "loads":[{"type":"集中|均布|弯矩","raw":"图中原始标注"}],
      "chapter_hint":"2静定结构|3静定结构位移|4力法|5位移法|6力矩分配|unknown",
      "chapter_confidence":0到1之间的小数,
      "chapter_evidence":"用于判断章节的题目文字或理由"
    }
  ]
}

判断规则:
- 如果图片只有一个完整题目，question_layout 输出 single，questions 只放 1 个元素。
- 如果图片明确包含多个独立题目，例如看到多个题号、多个独立结构图、明显分隔线或上下/左右分栏，question_layout 输出 multi。
- 如果只是同一道题的多个小问、一个题干配多个步骤，或边界不清，question_layout 输出 uncertain。
- bbox 必须框住对应小题的完整题目块：从本题题号/题干文字开始，到下一题题号/题干文字之前；如果页首只露出上一题图，则框住可见的上一题图。
- 图名属于对应小题，例如“题六图”属于第六题，不能放进第七题；“题七图”属于第七题，不能放进第八题。
- bbox 使用原图百分比坐标，格式为 [x1,y1,x2,y2]，取值 0 到 100，尽量框住对应小题的题干、结构图和图名。
- 多题时每个 questions 元素只提取该小题自己的荷载，不要把其他小题荷载混入。
- 每一个独立外荷载都必须输出一条记录；不要把相同标注的多个荷载合并。比如某小题结构上方一个 q、下方一个 q，该小题 loads 应输出两个 {"type":"均布","raw":"q"}。

章节识别规则:
- 只允许从 2静定结构、3静定结构位移、4力法、5位移法、6力矩分配、unknown 中选择。
- 优先根据题目文字、方法名、题干说明判断章节，不要只根据结构图形猜章节。
- 对 4力法、5位移法、6力矩分配 必须看到明确方法文字或题干要求；不能因为结构是连续梁、超静定结构、标注 EI/刚度、支座较多，就推测为某种方法。
- 如果图片里只有结构图、荷载、支座、尺寸、EI 等信息，没有明确方法名或题干说明，chapter_hint 必须为 unknown。
- 只有题干明确出现“静定结构”“静定刚架/钢架”等文字时，才可判断为 2静定结构；仅要求作内力图、弯矩图、剪力图不能单独判断为 2静定结构，因为超静定结构也可能要求作内力图。
- 没有明确章节或方法线索时，chapter_hint 为 unknown，chapter_confidence 不要高于 0.5。

荷载类型只有三种:
- 集中: 集中力，如 10kN, P, F=20kN, F1=40kN, ql, 2P
- 均布: 分布荷载，如 20kN/m, q=20kN/m, q, 2P/a
- 弯矩: 力偶/集中弯矩，如 10kN·m, M=20kN·m, Pa, ql²

不要提取:
- 刚度 EI、EA、2EI
- 尺寸 l、L、a、b、h、4m、l/2
- 节点或杆件编号 A、B、C、1、2、3
- 支座反力 RA、RB、FA、FB、HA、VA、MA
- 公式结果 ql²/8、ql/2

不要输出解释、Markdown或代码块。"""


DIAGRAM_PROMPT = """你是结构力学题目图片定位助手。请在整张图片中定位指定题号的“结构图/受力图”本身，只输出JSON。

指定题号: {question_label}

输出格式:
{{
  "question_label":"{question_label}",
  "found":true,
  "matched_diagram_label":"题{question_label}图",
  "diagram_bbox":[0,0,100,100],
  "confidence":0到1之间的小数,
  "reason":"简短说明你依据哪个题号、图名或图形定位"
}}

定位规则:
- 只框结构图/受力图本身，包括杆件、支座、荷载箭头、尺寸线和图名；不要框整段题干文字。
- 必须优先寻找与指定题号匹配的图名，例如指定题号 6 就找“题六图”，指定题号 7 就找“题七图”。
- 图名通常在结构图下方；找到匹配图名后，框住该图名正上方的结构图和该图名本身。
- 不要把“题五图”当成第六题，不要把“题六图”当成第七题，不要把“题七图”当成第八题。
- 如果没有看到匹配图名，再根据题干题号和最近的下方结构图定位。
- 如果指定题号只在页首露出上一题图，没有完整题干，也可以只框可见的结构图。
- 不要把上一题或下一题的图框进去。
- bbox 可以使用原图百分比坐标 [x1,y1,x2,y2]，也可以使用像素坐标；程序会自动归一化。
- 如果无法可靠定位，或只找到了相邻题号的图名，found 输出 false，diagram_bbox 输出 [0,0,100,100]，confidence 不高于 0.4。

不要输出解释、Markdown或代码块。"""


VERIFY_DIAGRAM_PROMPT = """你是结构力学题图核验助手。请判断这张裁剪图是否对应指定题目的结构图，只输出JSON。

指定题号: {question_label}
期望荷载: {expected_loads}
章节线索: {chapter_hint}

输出格式:
{{
  "is_match": true,
  "confidence": 0到1之间的小数,
  "visible_loads": ["图中可见荷载"],
  "reason": "简短说明"
}}

判断规则:
- 只判断裁剪图是否是该题的结构图/受力图，不要求包含完整题干。
- 如果期望荷载在图中基本可见，且结构图完整或接近完整，is_match 输出 true。
- 如果明显是相邻题的图、只剩局部图、或缺少主要结构/主要荷载，is_match 输出 false。
- 不要因为章节线索不在裁剪图中就判 false；章节线索通常来自整页题干。
- 不要输出解释、Markdown或代码块。"""


def normalize_chapter_hint(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    if not text or text.lower() in {"unknown", "none", "null", "不确定", "无法判断"}:
        return CHAPTER_UNKNOWN

    for chapter in CHAPTERS:
        if text == chapter:
            return chapter
    if re.fullmatch(r"[2-6]", text):
        for chapter in CHAPTERS:
            if chapter.startswith(text):
                return chapter

    normalized = text.replace("第", "").replace("章", "")
    keyword_map = [
        ("力矩分配", "6力矩分配"),
        ("转角位移", "5位移法"),
        ("位移法", "5位移法"),
        ("力法", "4力法"),
        ("静定结构位移", "3静定结构位移"),
        ("图乘法", "3静定结构位移"),
        ("静定", "2静定结构"),
    ]
    for keyword, chapter in keyword_map:
        if keyword in normalized:
            return chapter
    for chapter in CHAPTERS:
        if chapter in normalized or normalized in chapter:
            return chapter
    return CHAPTER_UNKNOWN


def normalize_chapter_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def quoted_evidence_segments(text: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.findall(r"[“\"'‘](.*?)[”\"'’]", text or "")
        if segment.strip()
    ]


def has_explicit_chapter_evidence(chapter_hint: str, chapter_evidence: str) -> bool:
    if chapter_hint == CHAPTER_UNKNOWN:
        return True
    triggers = CHAPTER_TRIGGER_WORDS.get(chapter_hint, [])
    if not triggers:
        return False
    quoted_text = " ".join(quoted_evidence_segments(chapter_evidence))
    if not quoted_text:
        return False
    if chapter_hint == "2静定结构" and ("不静定" in quoted_text or "超静定" in quoted_text):
        return False
    return any(trigger in quoted_text for trigger in triggers)


def guard_chapter_prediction(chapter_hint: str, confidence: float, evidence: str) -> tuple[str, float, str]:
    if chapter_hint == CHAPTER_UNKNOWN:
        return chapter_hint, min(confidence, 0.5), evidence
    if has_explicit_chapter_evidence(chapter_hint, evidence):
        return chapter_hint, confidence, evidence
    guarded = (
        f"{evidence}；未看到可直接引用的明确章节/方法文字，自动降级为unknown。"
        if evidence
        else "未看到可直接引用的明确章节/方法文字"
    )
    return CHAPTER_UNKNOWN, min(confidence, 0.49), guarded


def load_config() -> dict:
    cfg: dict = {}
    for name in ("config.json", "config.local.json"):
        path = BASE / name
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return cfg


def configured_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("root") or DEFAULT_ROOT)


def scan_images(root: Path, chapters: list[str] | None = None) -> list[Path]:
    selected = chapters or CHAPTERS
    images: list[Path] = []
    for chapter in selected:
        chapter_dir = root / chapter
        if not chapter_dir.is_dir():
            continue
        for current, dirs, files in os.walk(chapter_dir):
            dirs[:] = [
                d for d in dirs
                if "答案" not in d and not d.startswith(".") and not d.startswith("_")
            ]
            for file_name in files:
                path = Path(current) / file_name
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(path)
    return sorted(images, key=lambda p: str(p).lower())


def image_to_data_url(path: Path, upscale_min_side: int = 900) -> str:
    mime = "image/jpeg"
    try:
        img = Image.open(path).convert("RGB")
        shortest = min(img.size)
        if shortest and shortest < upscale_min_side:
            scale = upscale_min_side / shortest
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=94)
        raw = buf.getvalue()
    except Exception:  # noqa: BLE001
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        raw = path.read_bytes()
    data = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{data}"


def qwen_extract_loads(image_path: Path, *, model: str, endpoint: str, api_key: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    {"type": "text", "text": "只输出JSON。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 1024,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    usage = data.get("usage", {})
    chapter_hint = normalize_chapter_hint(parsed.get("chapter_hint"))
    chapter_confidence = normalize_chapter_confidence(parsed.get("chapter_confidence"))
    chapter_evidence = str(parsed.get("chapter_evidence") or "").strip()
    if chapter_hint == CHAPTER_UNKNOWN and not chapter_evidence:
        chapter_evidence = "未识别到明确章节线索"
    chapter_hint, chapter_confidence, chapter_evidence = guard_chapter_prediction(
        chapter_hint,
        chapter_confidence,
        chapter_evidence,
    )
    return {
        "loads": parsed.get("loads", []),
        "chapter_hint": chapter_hint,
        "chapter_confidence": chapter_confidence,
        "chapter_evidence": chapter_evidence,
        "raw_content": content,
        "usage": usage,
    }


def qwen_analyze_layout(image_path: Path, *, model: str, endpoint: str, api_key: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LAYOUT_PROMPT},
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
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    result = normalize_layout_result(parsed, image_size=image_size(image_path))
    result["raw_content"] = content
    result["usage"] = data.get("usage", {})
    return result


def qwen_locate_diagram(
    image_path: Path,
    *,
    question_label: str,
    model: str,
    endpoint: str,
    api_key: str,
    timeout: int,
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DIAGRAM_PROMPT.format(question_label=question_label)},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    {"type": "text", "text": "只输出JSON。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 1024,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    result = normalize_diagram_result(parsed, question_label, image_size=image_size(image_path))
    result["raw_content"] = content
    result["usage"] = data.get("usage", {})
    return result


def qwen_verify_diagram(
    diagram_path: Path,
    *,
    question_label: str,
    expected_loads: list[dict],
    chapter_hint: str,
    model: str,
    endpoint: str,
    api_key: str,
    timeout: int,
) -> dict:
    loads_text = "、".join(
        f"{item.get('type', '')}:{item.get('raw', '')}"
        for item in expected_loads
        if isinstance(item, dict)
    ) or "未识别"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": VERIFY_DIAGRAM_PROMPT.format(
                    question_label=question_label,
                    expected_loads=loads_text,
                    chapter_hint=chapter_hint or "unknown",
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(diagram_path)}},
                    {"type": "text", "text": "只输出JSON。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 512,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    confidence = normalize_chapter_confidence(parsed.get("confidence"))
    return {
        "is_match": bool(parsed.get("is_match")) and confidence >= 0.65,
        "confidence": confidence,
        "visible_loads": parsed.get("visible_loads", []),
        "reason": str(parsed.get("reason") or "").strip(),
        "raw_content": content,
        "usage": data.get("usage", {}),
    }


def image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:  # noqa: BLE001
        return None


def normalize_layout_result(parsed: dict, image_size: tuple[int, int] | None = None) -> dict:
    layout = str(parsed.get("question_layout") or "").strip().lower()
    if layout not in {"single", "multi", "uncertain"}:
        layout = "uncertain"
    questions = parsed.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    normalized_questions = []
    for index, item in enumerate(questions, 1):
        if not isinstance(item, dict):
            continue
        chapter_hint = normalize_chapter_hint(item.get("chapter_hint"))
        chapter_confidence = normalize_chapter_confidence(item.get("chapter_confidence"))
        chapter_evidence = str(item.get("chapter_evidence") or "").strip()
        if chapter_hint == CHAPTER_UNKNOWN and not chapter_evidence:
            chapter_evidence = "未识别到明确章节线索"
        chapter_hint, chapter_confidence, chapter_evidence = guard_chapter_prediction(
            chapter_hint,
            chapter_confidence,
            chapter_evidence,
        )
        normalized_questions.append(
            {
                "label": normalize_question_label(item.get("label"), index),
                "bbox": normalize_bbox(item.get("bbox"), image_size=image_size),
                "loads": [
                    normalize_load_item(load)
                    for load in item.get("loads", [])
                    if isinstance(load, dict)
                ],
                "chapter_hint": chapter_hint,
                "chapter_confidence": chapter_confidence,
                "chapter_evidence": chapter_evidence,
            }
        )

    if layout == "single" and len(normalized_questions) > 1:
        normalized_questions = normalized_questions[:1]
    if layout == "multi" and len(normalized_questions) < 2:
        layout = "uncertain"
    if layout == "single" and not normalized_questions:
        normalized_questions = [
            {
                "label": "1",
                "bbox": [0, 0, 100, 100],
                "loads": [],
                "chapter_hint": CHAPTER_UNKNOWN,
                "chapter_confidence": 0.0,
                "chapter_evidence": "未识别到明确章节线索",
            }
        ]

    return {
        "question_layout": layout,
        "reason": str(parsed.get("reason") or "").strip(),
        "questions": normalized_questions,
    }


def normalize_diagram_result(parsed: dict, question_label: str, image_size: tuple[int, int] | None = None) -> dict:
    confidence = normalize_chapter_confidence(parsed.get("confidence"))
    found = bool(parsed.get("found")) and confidence > 0.4
    return {
        "question_label": str(parsed.get("question_label") or question_label).strip() or str(question_label),
        "found": found,
        "diagram_bbox": normalize_bbox(parsed.get("diagram_bbox"), image_size=image_size),
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def normalize_question_label(value: object, fallback: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text or str(fallback)


def normalize_bbox(value: object, image_size: tuple[int, int] | None = None) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        return [0, 0, 100, 100]
    raw_numbers = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            number = 0
        raw_numbers.append(number)

    if image_size and any(number > 100 for number in raw_numbers):
        width, height = image_size
        if width > 0 and height > 0:
            raw_numbers = [
                raw_numbers[0] / width * 100,
                raw_numbers[1] / height * 100,
                raw_numbers[2] / width * 100,
                raw_numbers[3] / height * 100,
            ]

    bbox = [max(0, min(100, int(round(number)))) for number in raw_numbers]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return [0, 0, 100, 100]
    return bbox


def parse_model_json(content: str) -> dict:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def normalize_load_item(item: dict) -> dict:
    typ = str(item.get("type", "")).strip()
    raw = clean_raw(item.get("raw", ""))
    aliases = {
        "集中力": "集中",
        "点荷载": "集中",
        "均布荷载": "均布",
        "分布荷载": "均布",
        "分布力": "均布",
        "力偶": "弯矩",
        "集中弯矩": "弯矩",
        "集中力偶": "弯矩",
    }
    typ = aliases.get(typ, typ)
    return {"type": typ, "raw": raw}


def clean_raw(raw: object) -> str:
    text = str(raw or "").strip()
    text = text.replace("$", "")
    text = re.sub(r"\\left|\\right", "", text)
    text = text.replace("\\,", "")
    text = re.sub(r"\\mathrm\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"([A-Za-z])_\{?([0-9A-Za-z]+)\}?", r"\1\2", text)
    text = text.replace("^2", "²").replace("^{2}", "²")
    text = text.replace("^3", "³").replace("^{3}", "³")
    text = text.replace("\\cdot", "·").replace("\\times", "×")
    text = re.sub(r"\s+", "", text)
    return text


UNIT_PATTERNS = [
    r"kN[·.\-*×]?m",
    r"N[·.\-*×]?m",
    r"kN/m",
    r"N/m",
    r"kN",
    r"(?<=\d)k\b",
    r"\bN\b",
]


def strip_units(text: str) -> str:
    value = text
    for pattern in UNIT_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.I)
    return value


def symbolic_residue(raw: str) -> str:
    text = clean_raw(raw)
    if "=" in text:
        left, right = text.split("=", 1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", left):
            text = right
    text = strip_units(text)
    text = re.sub(r"\d+(?:\.\d+)?", "", text)
    text = re.sub(r"[=+\-*/×·().,，:：\[\]{}]", "", text)
    text = text.replace("²", "").replace("³", "")
    return text.strip()


def has_load_unit(raw: str) -> bool:
    return any(re.search(pattern, raw, flags=re.I) for pattern in UNIT_PATTERNS)


def is_numeric_assignment(raw: str) -> bool:
    text = clean_raw(raw)
    if "=" not in text:
        return False
    left, right = text.split("=", 1)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", left):
        return False
    if not re.search(r"\d", right) or not has_load_unit(right):
        return False
    return not re.search(r"[A-Za-z]", symbolic_residue(right))


def assignment_symbol(raw: str) -> str | None:
    text = clean_raw(raw)
    if not is_numeric_assignment(text):
        return None
    left = text.split("=", 1)[0]
    return left.lower()


def symbolic_residue_letters(raw: str) -> str:
    residue = symbolic_residue(raw).lower()
    return re.sub(r"[^a-z]", "", residue)


def classify_raw(raw: str) -> str:
    text = clean_raw(raw)
    if is_numeric_assignment(text):
        return "assigned_numeric"
    residue = symbolic_residue(text)
    if residue and re.search(r"[A-Za-z]", residue):
        return "symbolic_unassigned"
    if re.search(r"\d", text) and has_load_unit(text):
        return "numeric"
    return "unknown"


def classify_loads(loads: list[dict]) -> tuple[str, list[dict]]:
    normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
    assigned_symbols = {
        symbol for symbol in (assignment_symbol(item["raw"]) for item in normalized) if symbol
    }
    details = []
    for item in normalized:
        load_class = classify_raw(item["raw"])
        if load_class == "symbolic_unassigned" and assigned_symbols:
            letters = symbolic_residue_letters(item["raw"])
            if letters in assigned_symbols:
                load_class = "assigned_numeric"
        details.append({**item, "load_class": load_class})

    if not details:
        return "needs_review", details

    classes = {item["load_class"] for item in details}
    if "unknown" in classes:
        return "needs_review", details
    has_symbolic = "symbolic_unassigned" in classes
    has_main = bool(classes.intersection({"numeric", "assigned_numeric"}))
    if has_symbolic and has_main:
        return "mixed_symbolic_numeric", details
    if has_symbolic:
        return "symbolic_unassigned", details
    if "assigned_numeric" in classes:
        return "main_assigned_symbolic", details
    return "main_numeric", details


def load_existing_excel_index(root: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for xlsx in sorted(root.glob("*.xlsx")):
        if xlsx.stem.startswith("_"):
            continue
        try:
            df = pd.read_excel(xlsx)
        except Exception:
            continue
        if not {"题目名称", "荷载"}.issubset(df.columns):
            continue
        for row_index, row in df.iterrows():
            rel = str(row["题目名称"]).replace("\\", "/")
            index[rel] = {
                "xlsx": xlsx.name,
                "row": row_index + 2,
                "old_loads": row["荷载"],
            }
    return index


def save_artifacts(records: list[dict], output_dir: Path, root: Path, sample_size: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "classification_results.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for r in records:
        rows.append({
            "chapter": r["chapter"],
            "category": r["category"],
            "in_main_excel": r["in_main_excel"],
            "excel_row": r.get("excel_row", ""),
            "question": r["rel_path"],
            "loads": json.dumps({"loads": r["loads"]}, ensure_ascii=False),
            "load_classes": "; ".join(f"{x['type']}:{x['raw']}->{x['load_class']}" for x in r["load_details"]),
            "error": r.get("error", ""),
        })
    df = pd.DataFrame(rows)
    xlsx_path = output_dir / "classification_results.xlsx"
    df.to_excel(xlsx_path, index=False)

    for category, part in df.groupby("category", sort=True):
        safe_name = category.replace("/", "_")
        part.to_csv(output_dir / f"{safe_name}.csv", index=False, encoding="utf-8-sig")

    save_markdown(records, output_dir, root)
    save_contact_sheet(records, output_dir, root, sample_size)


def save_markdown(records: list[dict], output_dir: Path, root: Path) -> None:
    counts: dict[str, int] = {}
    for r in records:
        counts[r["category"]] = counts.get(r["category"], 0) + 1

    lines = ["# 题库图片重新识别分类报告", ""]
    lines.append(f"- root: `{root}`")
    lines.append(f"- total_images_processed: {len(records)}")
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines.append("")
    lines.append("## Samples By Category")
    lines.append("")

    for category in sorted(counts):
        lines.append(f"### {category}")
        subset = [r for r in records if r["category"] == category][:20]
        for r in subset:
            loads = json.dumps({"loads": r["loads"]}, ensure_ascii=False)
            lines.append(f"- `{r['rel_path']}` | in_main={r['in_main_excel']} | {loads}")
        lines.append("")

    (output_dir / "classification_report.md").write_text("\n".join(lines), encoding="utf-8")


def save_contact_sheet(records: list[dict], output_dir: Path, root: Path, sample_size: int) -> None:
    if not records or sample_size <= 0:
        return
    rng = random.Random(20260623)
    selected: list[dict] = []
    categories = sorted({r["category"] for r in records})
    per_category = max(1, sample_size // max(1, len(categories)))
    for category in categories:
        subset = [r for r in records if r["category"] == category]
        selected.extend(rng.sample(subset, min(per_category, len(subset))))
    if len(selected) < sample_size:
        rest = [r for r in records if r not in selected]
        selected.extend(rng.sample(rest, min(sample_size - len(selected), len(rest))))

    thumb_w, thumb_h = 240, 170
    label_h = 82
    cols = 3
    rows = (len(selected) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for idx, record in enumerate(selected):
        x = (idx % cols) * thumb_w
        y = (idx // cols) * (thumb_h + label_h)
        img_path = root / record["rel_path"]
        try:
            img = Image.open(img_path).convert("RGB")
            img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
            sheet.paste(img, (x + (thumb_w - img.width) // 2, y))
        except Exception as exc:  # noqa: BLE001
            draw.text((x + 4, y + 4), f"image error: {exc}", fill=(180, 0, 0), font=font)

        loads = "; ".join(f"{i['type']}:{i['raw']}" for i in record["load_details"])
        label = f"{idx+1}. {record['category']}\n{record['rel_path']}\n{loads[:90]}"
        draw.multiline_text((x + 4, y + thumb_h + 4), label, fill=(0, 0, 0), font=font, spacing=2)

    sheet.save(output_dir / "sample_contact_sheet.jpg", quality=92)


def analyze_layout_command(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else configured_root()
    image_path = Path(args.layout_image)
    if not image_path.is_absolute():
        image_path = root / image_path
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY is not set", file=sys.stderr)
        return 2
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 2

    if args.diagram_question:
        result = qwen_locate_diagram(
            image_path,
            question_label=args.diagram_question,
            model=args.model,
            endpoint=args.endpoint,
            api_key=api_key,
            timeout=args.timeout,
        )
        if args.save_diagram_crop:
            save_diagram_crop(
                image_path,
                result,
                Path(args.save_diagram_crop),
                padding_percent=args.diagram_crop_padding,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.cv_diagram_question:
        result = stable_cv_diagram_command(args, image_path, api_key)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = qwen_analyze_layout(
        image_path,
        model=args.model,
        endpoint=args.endpoint,
        api_key=api_key,
        timeout=args.timeout,
    )
    if args.save_layout_crops:
        save_layout_crops(
            image_path,
            result,
            Path(args.save_layout_crops),
            padding_percent=args.layout_crop_padding,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def save_diagram_crop(
    image_path: Path,
    diagram_result: dict,
    output_dir: Path,
    *,
    padding_percent: float = 2.0,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diagram.json").write_text(
        json.dumps(diagram_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not diagram_result.get("found"):
        return
    bbox = diagram_result.get("diagram_bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return
    with Image.open(image_path).convert("RGB") as img:
        crop = safe_crop(img, percent_bbox_to_pixels(bbox, img.size), padding_ratio=padding_percent / 100)
        if crop is None:
            return
        label = safe_filename_part(diagram_result.get("question_label") or "question")
        crop.save(output_dir / f"question_{label}_diagram.jpg", quality=94)


def save_layout_crops(
    image_path: Path,
    layout_result: dict,
    output_dir: Path,
    *,
    padding_percent: float = 2.0,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "layout.json").write_text(
        json.dumps(layout_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with Image.open(image_path).convert("RGB") as img:
        for index, question in enumerate(layout_result.get("questions", []), 1):
            if not isinstance(question, dict):
                continue
            bbox = question.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            crop = safe_crop(img, percent_bbox_to_pixels(bbox, img.size), padding_ratio=padding_percent / 100)
            if crop is None:
                continue
            label = safe_filename_part(question.get("label") or index)
            crop.save(output_dir / f"question_{label}.jpg", quality=94)


def stable_cv_diagram_command(args: argparse.Namespace, image_path: Path, api_key: str) -> dict:
    timings: dict[str, float] = {}
    started = time.perf_counter()
    layout = qwen_analyze_layout(
        image_path,
        model=args.model,
        endpoint=args.endpoint,
        api_key=api_key,
        timeout=args.timeout,
    )
    timings["layout_seconds"] = round(time.perf_counter() - started, 2)

    output_dir = Path(args.save_cv_diagram_crop) if args.save_cv_diagram_crop else None
    started = time.perf_counter()
    cv_result = locate_diagram_by_cv(
        image_path,
        layout,
        args.cv_diagram_question,
        output_dir=output_dir,
    )
    timings["cv_seconds"] = round(time.perf_counter() - started, 2)

    verify_result = None
    question = find_layout_question(layout, args.cv_diagram_question)
    crop_path = None
    if output_dir and cv_result.get("found"):
        label = safe_filename_part(cv_result.get("question_label") or "question")
        crop_path = output_dir / f"question_{label}_cv_diagram.jpg"
    if args.verify_cv_diagram and question and crop_path and crop_path.exists():
        started = time.perf_counter()
        verify_result = qwen_verify_diagram(
            crop_path,
            question_label=args.cv_diagram_question,
            expected_loads=question.get("loads", []),
            chapter_hint=str(question.get("chapter_hint") or "unknown"),
            model=args.model,
            endpoint=args.endpoint,
            api_key=api_key,
            timeout=args.timeout,
        )
        timings["verify_seconds"] = round(time.perf_counter() - started, 2)

    return {
        "question_label": args.cv_diagram_question,
        "layout": {
            "question_layout": layout.get("question_layout"),
            "questions": [
                {
                    "label": q.get("label"),
                    "loads": q.get("loads"),
                    "chapter_hint": q.get("chapter_hint"),
                    "chapter_confidence": q.get("chapter_confidence"),
                }
                for q in layout.get("questions", [])
                if isinstance(q, dict)
            ],
        },
        "cv": cv_result,
        "verify": verify_result,
        "crop_path": str(crop_path) if crop_path else None,
        "timings": {**timings, "total_seconds": round(sum(timings.values()), 2)},
    }


def find_layout_question(layout_result: dict, question_label: str) -> dict | None:
    key = normalize_question_label_for_match(question_label)
    for question in layout_result.get("questions", []):
        if isinstance(question, dict) and normalize_question_label_for_match(question.get("label")) == key:
            return question
    return None


def locate_diagram_by_cv(
    image_path: Path,
    layout_result: dict,
    question_label: str,
    *,
    output_dir: Path | None = None,
) -> dict:
    if cv2 is None or np is None:
        raise RuntimeError("opencv-python and numpy are required for CV diagram cropping")
    image = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"failed to read image: {image_path}")
    boxes = find_diagram_blocks_cv(image)
    questions = [q for q in layout_result.get("questions", []) if isinstance(q, dict)]
    labels = [str(q.get("label") or "").strip() for q in questions if str(q.get("label") or "").strip()]
    target_key = normalize_question_label_for_match(question_label)
    target_index = None
    for index, label in enumerate(labels):
        if normalize_question_label_for_match(label) == target_key:
            target_index = index
            break

    if target_index is None or target_index >= len(boxes):
        result = {
            "question_label": question_label,
            "found": False,
            "diagram_bbox": [0, 0, 100, 100],
            "pixel_bbox": None,
            "labels": labels,
            "blocks": len(boxes),
            "reason": "未能按题号顺序匹配图块。",
        }
    else:
        x, y, w, h, _area = boxes[target_index]
        result = {
            "question_label": question_label,
            "found": True,
            "diagram_bbox": pixel_bbox_to_percent([x, y, x + w, y + h], (image.shape[1], image.shape[0])),
            "pixel_bbox": [int(x), int(y), int(x + w), int(y + h)],
            "matched_index": target_index + 1,
            "labels": labels,
            "blocks": len(boxes),
            "reason": "按 layout 题号顺序与 CV 图块从上到下顺序匹配。",
        }

    if output_dir:
        save_cv_diagram_debug(image_path, boxes, result, output_dir)
    return result


def find_diagram_blocks_cv(image) -> list[tuple[int, int, int, int, int]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (0, 0), 21)
    norm = cv2.divide(gray, blur, scale=255)
    thresh = cv2.adaptiveThreshold(
        norm,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        11,
    )
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 5))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, close_kernel, iterations=2)
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (35, 9))
    dilated = cv2.dilate(closed, dilate_kernel, iterations=1)
    num, _labels, stats, _centroids = cv2.connectedComponentsWithStats(dilated, 8)
    height, width = gray.shape
    candidates: list[tuple[int, int, int, int, int]] = []
    for index in range(1, num):
        x, y, w, h, area = [int(value) for value in stats[index]]
        if area < 1000:
            continue
        if y < height * 0.12 and w > width * 0.5:
            continue
        if w < width * 0.08 or h < height * 0.025:
            continue
        if w > width * 0.85 and h < height * 0.06:
            continue
        if h < height * 0.06 and area / max(1, w * h) > 0.8:
            continue
        if w > width * 0.80 and y > height * 0.88:
            continue
        if w / max(1, h) > 20:
            continue
        candidates.append((x, y, w, h, area))
    return sorted(candidates, key=lambda box: (box[1], box[0]))


def save_cv_diagram_debug(
    image_path: Path,
    boxes: list[tuple[int, int, int, int, int]],
    result: dict,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pil = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(pil)
    for index, (x, y, w, h, _area) in enumerate(boxes, 1):
        draw.rectangle([x, y, x + w, y + h], outline="red", width=4)
        draw.text((x, y), str(index), fill="red")
    pil.save(output_dir / "cv_blocks.jpg", quality=94)
    (output_dir / "cv_diagram.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if result.get("found") and result.get("pixel_bbox"):
        with Image.open(image_path).convert("RGB") as img:
            crop = safe_crop(img, result["pixel_bbox"], padding_ratio=0.06)
            if crop is not None:
                label = safe_filename_part(result.get("question_label") or "question")
                crop.save(output_dir / f"question_{label}_cv_diagram.jpg", quality=94)


def normalize_question_label_for_match(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"第|题|图|[()\uff08\uff09\s]", "", text)
    chinese_digits = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    return chinese_digits.get(text, text)


def pixel_bbox_to_percent(bbox: list[int], image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    x1, y1, x2, y2 = bbox
    return [
        max(0, min(100, round(x1 / width * 100))),
        max(0, min(100, round(y1 / height * 100))),
        max(0, min(100, round(x2 / width * 100))),
        max(0, min(100, round(y2 / height * 100))),
    ]


def percent_bbox_to_pixels(bbox: list, image_size: tuple[int, int]) -> list[float]:
    try:
        x1, y1, x2, y2 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        x1, y1, x2, y2 = 0.0, 0.0, 100.0, 100.0
    width, height = image_size
    return [x1 / 100 * width, y1 / 100 * height, x2 / 100 * width, y2 / 100 * height]


def safe_crop(
    image: Image.Image,
    bbox: list[float] | tuple[float, float, float, float],
    *,
    padding_ratio: float = 0.08,
    min_size_px: int = 5,
) -> Image.Image | None:
    """Crop from an original-image pixel bbox with expansion, clamping, and inversion repair."""
    if len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None

    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    width = x2 - x1
    height = y2 - y1
    if width < min_size_px or height < min_size_px:
        return None

    pad_x = max(width * max(0.0, padding_ratio), 2.0)
    pad_y = max(height * max(0.0, padding_ratio), 2.0)
    x1 -= pad_x
    x2 += pad_x
    y1 -= pad_y
    y2 += pad_y

    left = max(0, min(image.width - 1, int(round(x1))))
    top = max(0, min(image.height - 1, int(round(y1))))
    right = max(left + 1, min(image.width, int(round(x2))))
    bottom = max(top + 1, min(image.height, int(round(y2))))

    if right - left < min_size_px or bottom - top < min_size_px:
        return None
    return image.crop((left, top, right, bottom))


def safe_filename_part(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", text)
    return text.strip("_") or "question"


def classify_images(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else configured_root()
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY is not set", file=sys.stderr)
        return 2

    if args.image:
        images = []
        for raw_image in args.image:
            p = Path(raw_image)
            if not p.is_absolute():
                p = root / raw_image
            images.append(p)
    else:
        chapters = args.chapter or CHAPTERS
        images = scan_images(root, chapters)
        if args.only_missing or args.only_existing:
            existing = load_existing_excel_index(root)
            if args.only_missing:
                images = [p for p in images if str(p.relative_to(root)).replace("\\", "/") not in existing]
            if args.only_existing:
                images = [p for p in images if str(p.relative_to(root)).replace("\\", "/") in existing]
    if args.shuffle:
        random.Random(args.seed).shuffle(images)
    if args.limit:
        images = images[:args.limit]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else BASE / ".tmp_symbol_sheets" / f"classify_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_path = output_dir / "classification_working.json"

    existing_index = load_existing_excel_index(root)
    records: list[dict] = []
    if args.resume and working_path.exists():
        records = json.loads(working_path.read_text(encoding="utf-8"))
        for record in records:
            category, load_details = classify_loads(record.get("loads", []))
            if record.get("error"):
                category = "needs_review"
            record["category"] = category
            record["load_details"] = load_details
        working_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    done = {r["rel_path"] for r in records}
    processed_new = 0

    print(f"root={root}")
    print(f"images={len(images)}")
    print(f"output_dir={output_dir}")
    print(f"model={args.model}")

    for index, image_path in enumerate(images, 1):
        rel_path = str(image_path.relative_to(root)).replace("\\", "/")
        if rel_path in done:
            continue
        if args.max_new and processed_new >= args.max_new:
            break
        started = time.time()
        error = ""
        loads: list[dict] = []
        raw_content = ""
        chapter_hint = CHAPTER_UNKNOWN
        chapter_confidence = 0.0
        chapter_evidence = ""
        try:
            extracted = qwen_extract_loads(
                image_path,
                model=args.model,
                endpoint=args.endpoint,
                api_key=api_key,
                timeout=args.timeout,
            )
            loads = extracted["loads"]
            chapter_hint = extracted.get("chapter_hint", CHAPTER_UNKNOWN)
            chapter_confidence = extracted.get("chapter_confidence", 0.0)
            chapter_evidence = extracted.get("chapter_evidence", "")
            raw_content = extracted.get("raw_content", "")
        except Exception as exc:  # noqa: BLE001 - keep batch jobs running on per-image failures.
            error = str(exc)
        category, load_details = classify_loads(loads)
        if error:
            category = "needs_review"

        chapter = rel_path.split("/", 1)[0]
        old = existing_index.get(rel_path, {})
        record = {
            "chapter": chapter,
            "rel_path": rel_path,
            "category": category,
            "chapter_hint": chapter_hint,
            "chapter_confidence": chapter_confidence,
            "chapter_evidence": chapter_evidence,
            "loads": [normalize_load_item(item) for item in loads if isinstance(item, dict)],
            "load_details": load_details,
            "in_main_excel": rel_path in existing_index,
            "excel_file": old.get("xlsx", ""),
            "excel_row": old.get("row", ""),
            "old_loads": old.get("old_loads", ""),
            "seconds": round(time.time() - started, 2),
            "error": error,
            "raw_content": raw_content,
        }
        records.append(record)
        done.add(rel_path)
        processed_new += 1

        print(
            f"[{index}/{len(images)}] {category} | {rel_path} | "
            f"{json.dumps({'loads': record['loads'], 'chapter_hint': chapter_hint}, ensure_ascii=False)} | {record['seconds']}s"
        )
        working_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.sleep:
            time.sleep(args.sleep)

    save_artifacts(records, output_dir, root, args.sample_size)
    print(f"done={len(records)}")
    print(f"report={output_dir / 'classification_report.md'}")
    print(f"xlsx={output_dir / 'classification_results.xlsx'}")
    print(f"contact_sheet={output_dir / 'sample_contact_sheet.jpg'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen classify full question-bank images without editing live Excel.")
    parser.add_argument("--root", help="question bank root; defaults to config root")
    parser.add_argument("--layout-image", help="analyze whether one image contains single or multiple questions; prints JSON")
    parser.add_argument("--save-layout-crops", help="directory to save crops when using --layout-image")
    parser.add_argument("--layout-crop-padding", type=float, default=2.0, help="padding percent around each layout crop")
    parser.add_argument("--diagram-question", help="with --layout-image, locate only this question's structure/load diagram")
    parser.add_argument("--save-diagram-crop", help="directory to save the diagram crop when using --diagram-question")
    parser.add_argument("--diagram-crop-padding", type=float, default=2.0, help="padding percent around the diagram crop")
    parser.add_argument("--cv-diagram-question", help="with --layout-image, locate this question's diagram using layout order + OpenCV blocks")
    parser.add_argument("--save-cv-diagram-crop", help="directory to save the OpenCV diagram crop/debug image")
    parser.add_argument("--verify-cv-diagram", action="store_true", help="verify the OpenCV crop with Qwen")
    parser.add_argument("--chapter", action="append", help="chapter to scan; may be repeated")
    parser.add_argument("--image", action="append", help="single image path to classify; may be repeated")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", help="directory for reports")
    parser.add_argument("--limit", type=int, default=0, help="only process first N images after filtering")
    parser.add_argument("--max-new", type=int, default=0, help="process at most N images not already in the working file")
    parser.add_argument("--shuffle", action="store_true", help="shuffle image order before applying --limit")
    parser.add_argument("--seed", type=int, default=20260623)
    parser.add_argument("--only-missing", action="store_true", help="only classify images not present in live Excel")
    parser.add_argument("--only-existing", action="store_true", help="only classify images already present in live Excel")
    parser.add_argument("--resume", action="store_true", help="resume from classification_working.json in output dir")
    parser.add_argument("--sleep", type=float, default=0.0, help="sleep seconds between API calls")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sample-size", type=int, default=12, help="number of images in contact sheet")
    args = parser.parse_args()
    if args.layout_image:
        return analyze_layout_command(args)
    return classify_images(args)


if __name__ == "__main__":
    raise SystemExit(main())

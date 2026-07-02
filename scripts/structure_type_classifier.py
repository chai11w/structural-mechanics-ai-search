"""Coarse structure type classifier shared by experiments and retrieval."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from scripts.classify_question_bank import image_to_data_url, parse_model_json


VALID_STRUCTURE_TYPES = {"梁", "钢架", "桁架", "拱", "unknown"}

STRUCTURE_TYPE_PROMPT = """你是结构力学题图粗分类器。只判断结构图的大类，不解题，不判断荷载数值。

只输出 JSON，不要输出 Markdown：
{"structure_type":"梁|钢架|桁架|拱|unknown","confidence":0.0,"reason":"不超过12字"}

分类规则：
- 梁：只包含横向或斜向梁构件；单跨梁、多跨梁、悬臂梁也归为梁。短小支座符号、支座小竖线、滚轮/铰支座画法不算竖向结构杆，仍归梁。
- 钢架：有明显柱和梁组成的刚架/框架/门架外形，包含直角框架、折线框架、闭口框架、组合结构。只有当连接到梁上的竖向杆很长、明显不是支座符号，而是结构杆、柱、竖向弹性杆、树干状分支时，才归钢架；判断为组合结构也归钢架。
- 拱：以拱形曲线或拱轴为主要承重构件的结构。
- 桁架：由多根直杆组成三角形或网格杆系，杆件多以铰接杆系形式出现。
- unknown：图片不清楚、结构图不完整，或无法可靠判断。

只根据结构几何外形判断，不要因为题号、节点字母、尺寸、EI、荷载箭头、支座画法改变分类。"""


def qwen_structure_type(image_path: Path, *, endpoint: str, model: str, api_key: str, timeout: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": STRUCTURE_TYPE_PROMPT},
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
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    structure_type = str(parsed.get("structure_type") or "unknown").strip()
    if structure_type not in VALID_STRUCTURE_TYPES:
        structure_type = "unknown"
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "structure_type": structure_type,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip(),
        "raw_content": content,
        "usage": data.get("usage", {}),
    }

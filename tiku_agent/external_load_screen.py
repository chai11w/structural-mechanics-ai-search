"""Fast external-load screening for uploaded question images."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.request
from typing import Any

import search
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call


DEFAULT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_MODEL = "glm-4.6v"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_QWEN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_QWEN_MODEL = "qwen3.7-plus"
NO_EXTERNAL_LOAD_MESSAGE = (
    "未识别到图片中的外荷载，暂时无法检索。"
    "请重新上传外荷载清晰可见的题图。"
)
EXTERNAL_LOAD_PROMPT = """你是结构力学图片入口筛查器。判断图中是否有作用在梁、柱、桁架等结构杆件或节点上的真实外部荷载。
真实外荷载包括：直接作用于结构的单个直箭头、成排分布箭头、弧形力偶箭头。必须看见荷载图形与结构相连，不能只凭 P、F、q、M、kN 等文字或单位。
尺寸/坐标箭头、支座反力、内力图、单位荷载图、公式、纯文字、软件界面、风景和墙面都不算。明确看见或疑似有外荷载输出 yes；只有明确没有外荷载才输出 no。
请快速判断。只输出一个单词：yes 或 no。不要解释，不要 JSON，不要 Markdown。"""


class ImageSearchCancelled(RuntimeError):
    """The external-load branch won before the search branch committed."""


class ZhipuExternalLoadScreen:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT
        self.model = str(model).strip() or DEFAULT_MODEL
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def __call__(self, image_path: str | Path) -> str:
        api_key = (
            os.environ.get("ZHIPUAI_API_KEY", "")
            or os.environ.get("ZAI_API_KEY", "")
            or search.ZHIPUAI_API_KEY
        )
        if not api_key:
            raise RuntimeError("ZHIPUAI_API_KEY is not configured")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTERNAL_LOAD_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_to_model_data_url(image_path)},
                        },
                        {"type": "text", "text": "只输出 yes 或 no。"},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 32,
            "thinking": {"type": "disabled"},
        }

        def request_model() -> dict[str, Any]:
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = str(data["choices"][0]["message"].get("content") or "").strip()
            normalized = content.lower().strip("`*_.,:;!?。！？\n\r \t")
            if normalized.startswith("yes"):
                verdict = "yes"
            elif normalized.startswith("no"):
                verdict = "no"
            else:
                raise RuntimeError("external-load screen returned an unexpected response")
            return {
                "verdict": verdict,
                "usage": data.get("usage") or {},
                "request_id": str(data.get("request_id") or data.get("id") or ""),
            }

        result = timed_model_call(
            request_model,
            provider="zhipu",
            model=self.model,
            call_type="external_load_screen",
            usage_getter=lambda value: value.get("usage") or {},
            provider_request_id_getter=lambda value: str(value.get("request_id") or ""),
        )
        return str(result["verdict"])


class QwenExternalLoadScreen:
    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        model: str = DEFAULT_QWEN_MODEL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = str(endpoint).strip() or DEFAULT_QWEN_ENDPOINT
        self.model = str(model).strip() or DEFAULT_QWEN_MODEL
        self.api_key = str(
            api_key
            or os.environ.get("DASHSCOPE_API_KEY", "")
            or search.DASHSCOPE_API_KEY
        ).strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def __call__(self, image_path: str | Path) -> str:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": EXTERNAL_LOAD_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_to_model_data_url(image_path)},
                        },
                        {"type": "text", "text": "只输出 yes 或 no。"},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 32,
            "enable_thinking": False,
        }

        def request_model() -> dict[str, Any]:
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = str(data["choices"][0]["message"].get("content") or "").strip()
            normalized = content.lower().strip("`*_.,:;!?。！？\n\r \t")
            if normalized.startswith("yes"):
                verdict = "yes"
            elif normalized.startswith("no"):
                verdict = "no"
            else:
                raise RuntimeError("external-load screen returned an unexpected response")
            return {
                "verdict": verdict,
                "usage": data.get("usage") or {},
                "request_id": str(data.get("request_id") or data.get("id") or ""),
            }

        result = timed_model_call(
            request_model,
            provider="dashscope",
            model=self.model,
            call_type="external_load_screen",
            usage_getter=lambda value: value.get("usage") or {},
            provider_request_id_getter=lambda value: str(value.get("request_id") or ""),
        )
        return str(result["verdict"])

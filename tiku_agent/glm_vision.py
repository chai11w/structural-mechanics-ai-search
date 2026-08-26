"""Small GLM multimodal JSON client shared by A3 production and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Sequence
import urllib.error
import urllib.request

from scripts.classify_question_bank import parse_model_json
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call


DEFAULT_GLM_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_GLM_MODEL = "glm-5v-turbo"


@dataclass(frozen=True)
class GlmJsonResponse:
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


def call_glm_json(
    image_paths: Sequence[str | Path],
    *,
    prompt: str,
    model: str = DEFAULT_GLM_MODEL,
    user_text: str = "只输出 JSON。",
    api_key: str | None = None,
    endpoint: str = DEFAULT_GLM_ENDPOINT,
    timeout_seconds: float = 180.0,
    max_tokens: int = 3000,
    call_type: str = "glm_multimodal_json",
) -> GlmJsonResponse:
    """Call GLM's multimodal Chat Completions endpoint once."""

    key = str(
        api_key
        or os.environ.get("ZHIPUAI_API_KEY", "")
        or os.environ.get("ZAI_API_KEY", "")
    ).strip()
    if not key:
        raise RuntimeError("ZHIPUAI_API_KEY is not configured")
    content: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": image_to_model_data_url(
                    Path(image_path),
                    normalize_orientation=True,
                )
            },
        }
        for image_path in image_paths
    ]
    content.append({"type": "text", "text": str(user_text)})
    request_payload = {
        "model": str(model).strip() or DEFAULT_GLM_MODEL,
        "messages": [
            {"role": "system", "content": str(prompt)},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": max(256, int(max_tokens)),
        "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        str(endpoint).strip() or DEFAULT_GLM_ENDPOINT,
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def request_data() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(1.0, float(timeout_seconds)),
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"GLM HTTP {exc.code}: {detail}") from exc
        if not isinstance(value, dict):
            raise ValueError("model response must be an object")
        return value

    data = timed_model_call(
        request_data,
        provider="zhipu",
        model=request_payload["model"],
        call_type=str(call_type),
        usage_getter=lambda value: value.get("usage", {}),
        provider_request_id_getter=lambda value: str(value.get("id") or ""),
    )
    try:
        raw_content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("invalid GLM response envelope") from exc
    raw_text = (
        raw_content
        if isinstance(raw_content, str)
        else json.dumps(raw_content, ensure_ascii=False)
    )
    parsed = parse_model_json(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError("GLM JSON output must be an object")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return GlmJsonResponse(
        payload=parsed,
        raw_text=raw_text,
        model=str(data.get("model") or request_payload["model"]),
        prompt_tokens=_non_negative_int(
            usage.get("prompt_tokens") or usage.get("input_tokens")
        ),
        completion_tokens=_non_negative_int(
            usage.get("completion_tokens") or usage.get("output_tokens")
        ),
        total_tokens=_non_negative_int(usage.get("total_tokens")),
    )


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

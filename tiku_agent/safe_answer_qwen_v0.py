"""DashScope Qwen adapter for offline safe-answer evaluation only."""

from __future__ import annotations

import json
import os
import urllib.request

from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from tiku_agent.safe_answer_generator_v0 import SafeAnswerModelRequestV0


class QwenSafeAnswerClientV0:
    """Translate the provider-neutral request into one bounded Qwen call."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.model = model
        self.endpoint = endpoint

    def __call__(self, request: SafeAnswerModelRequestV0) -> str:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not set")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.prompt.system_prompt},
                {"role": "user", "content": request.prompt.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "enable_thinking": False,
        }
        http_request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            http_request,
            timeout=request.timeout_seconds,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Qwen returned an invalid response shape") from exc
        if not isinstance(content, str):
            raise RuntimeError("Qwen returned non-text content")
        return content

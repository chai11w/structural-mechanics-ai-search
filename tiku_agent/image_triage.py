"""Pure-code safety routing for the isolated 8890 image triage stage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import urllib.request

from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call
from .image_contracts import ImageTriageHandoff, ImageTriageObservation, Route


BASE = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = BASE / "experiments" / "complex_image_eval" / "observation_prompt_scratch.md"
DEFAULT_QWEN_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_QWEN_MODEL = "qwen3.7-plus"

_EXPLICIT_ROUTE = re.compile(
    r"(?:建议|推荐)?\s*(?:路线|分流)\s*[:：]?\s*[`* _-]*(A[123])\b",
    re.IGNORECASE,
)
_BARE_ROUTE = re.compile(r"^\s*[`* _-]*(A[123])\s*[`* _-]*(?:$|[：:，,。.!！])", re.IGNORECASE)
_CHINESE_COUNTS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class QwenImageTriageResult:
    """One observation plus the non-sensitive usage needed for shadow evaluation."""

    observation: ImageTriageObservation
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def load_triage_prompt(path: str | Path = DEFAULT_PROMPT_PATH) -> str:
    content = Path(path).read_text(encoding="utf-8")
    match = re.search(r"```text\s*(.*?)\s*```", content, flags=re.DOTALL)
    if not match:
        raise ValueError(f"提示词文件缺少 text 代码块: {path}")
    return match.group(1).strip()


def parse_route_candidate(text: str) -> Route:
    """Read the model's first-pass route without accepting A30-like tokens."""

    content = str(text or "")
    explicit = [match.group(1).upper() for match in _EXPLICIT_ROUTE.finditer(content)]
    if explicit:
        if len(set(explicit)) != 1:
            raise ValueError("模型回答包含互相冲突的分流建议")
        return explicit[0]  # type: ignore[return-value]

    first_line = content.splitlines()[0] if content.splitlines() else content
    match = _BARE_ROUTE.match(first_line)
    if match:
        return match.group(1).upper()  # type: ignore[return-value]
    raise ValueError("模型回答缺少明确的 A1/A2/A3 分流建议")


def _summary_field(text: str, label: str) -> str | None:
    match = re.search(
        rf"^[\s>*`*_]*{re.escape(label)}\s*[:：]\s*([^\r\n]+)",
        str(text or ""),
        flags=re.MULTILINE,
    )
    if not match:
        return None
    return match.group(1).strip().strip("`*_ ")


def _summary_count(text: str, label: str) -> int | None:
    value = _summary_field(text, label)
    if not value or "不确定" in value:
        return None
    match = re.search(r"\d+", value)
    if match:
        return int(match.group(0))
    for word, count in _CHINESE_COUNTS.items():
        if word in value:
            return count
    return None


def _summary_bool(text: str, label: str, *, positive: tuple[str, ...], negative: tuple[str, ...]) -> bool | None:
    value = _summary_field(text, label)
    if not value or "不确定" in value:
        return None
    if any(token in value for token in negative):
        return False
    if any(token in value for token in positive):
        return True
    return None


def observation_from_model_text(text: str) -> ImageTriageObservation:
    """Keep the open-ended model explanation while extracting only its route."""

    content = str(text or "").strip()
    actual_load = _summary_bool(
        content,
        "真实外荷载",
        positive=("明确", "有"),
        negative=("没有", "无"),
    )
    recoverable = _summary_bool(
        content,
        "图片完整性",
        positive=("完整",),
        negative=("残缺", "不完整"),
    )
    structure_content = _summary_bool(
        content,
        "结构力学内容",
        positive=("有",),
        negative=("无",),
    )
    return ImageTriageObservation(
        route_candidate=parse_route_candidate(content),
        evidence=(content,) if content else (),
        question_count=_summary_count(content, "题目数量"),
        original_structure_count=_summary_count(content, "原结构图数量"),
        auxiliary_diagram_count=_summary_count(content, "辅助图数量"),
        has_actual_load_evidence=actual_load,
        has_structure_content=structure_content,
        image_recoverable=recoverable,
        has_ambiguity=any(value is None for value in (actual_load, recoverable, structure_content)),
        raw_text=content,
    )


class QwenImageTriage:
    """Call the selected Qwen vision model for a first-pass observation only."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        model: str = DEFAULT_QWEN_MODEL,
        timeout_seconds: float = 120.0,
        prompt_path: str | Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        self.api_key = str(api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.endpoint = str(endpoint).strip() or DEFAULT_QWEN_ENDPOINT
        self.model = str(model).strip() or DEFAULT_QWEN_MODEL
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.prompt_path = Path(prompt_path)

    def observe(self, image_path: str | Path) -> ImageTriageObservation:
        return self.observe_with_metadata(image_path).observation

    def observe_with_metadata(self, image_path: str | Path) -> QwenImageTriageResult:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": load_triage_prompt(self.prompt_path)},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_to_model_data_url(image_path)}},
                        {"type": "text", "text": "请按要求完成第一次分流。"},
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 1600,
            "enable_thinking": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        def request_data() -> dict:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))

        data = timed_model_call(
            request_data,
            provider="dashscope",
            model=self.model,
            call_type="qwen_image_triage",
            usage_getter=lambda value: value.get("usage", {}),
            request_id_getter=lambda value: str(value.get("request_id") or value.get("id") or ""),
        )
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("千问分流返回缺少模型回答") from exc
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return QwenImageTriageResult(
            observation=observation_from_model_text(content),
            model=self.model,
            prompt_tokens=_non_negative_int(usage.get("prompt_tokens")),
            completion_tokens=_non_negative_int(usage.get("completion_tokens")),
            total_tokens=_non_negative_int(usage.get("total_tokens")),
        )


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def finalize_route(observation: ImageTriageObservation) -> Route:
    """Apply only high-risk A2/A1 guards; uncertain cases fall back to A3."""

    # A structural diagram with an explicit no-load observation is a terminal
    # A1 case. It must not be handed to A2 or left for A3 to invent a load.
    if observation.has_actual_load_evidence is False:
        return "A1"

    if observation.route_candidate == "A2":
        a2_facts = (
            observation.question_count == 1,
            observation.original_structure_count == 1,
            observation.auxiliary_diagram_count == 0,
            observation.has_actual_load_evidence is True,
            observation.image_recoverable is True,
            observation.has_ambiguity is False,
        )
        if not all(a2_facts):
            return "A3"

    if observation.route_candidate == "A1":
        if observation.has_structure_content is not False:
            return "A3"
    return observation.route_candidate


def build_handoff(
    source_image_path: str,
    observation: ImageTriageObservation,
) -> ImageTriageHandoff:
    """Create the branch payload without invoking any model or downstream tool."""

    route = finalize_route(observation)
    if route == "A1":
        next_action = "stop"
    elif route == "A2":
        next_action = "existing_search"
    else:
        next_action = "a3_processing"
    return ImageTriageHandoff(
        route=route,
        source_image_path=str(source_image_path),
        observation=observation,
        next_action=next_action,
        reason=observation.evidence,
    )

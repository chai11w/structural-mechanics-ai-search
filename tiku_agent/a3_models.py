"""Bounded Qwen clients for the isolated A3 manual-crop MVP."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
import urllib.request

from scripts.classify_question_bank import (
    CHAPTER_UNKNOWN,
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    classify_loads,
    guard_chapter_prediction,
    normalize_chapter_confidence,
    normalize_chapter_hint,
    normalize_load_item,
    request_json_with_retry,
)
from tiku_agent.a3_page_parser import A3PageUnderstanding, parse_a3_page_understanding
from tiku_shared.image_payload import image_to_model_data_url
from tiku_shared.model_costs import timed_model_call


PROMPT_DIR = Path(__file__).with_name("prompts")
A3_PAGE_PROMPT_PATH = PROMPT_DIR / "a3_page_understanding_v2.txt"
A3_CROP_COMPARE_PROMPT_PATH = PROMPT_DIR / "a3_crop_compare_v2.txt"
A3_UNIT_ANALYSIS_PROMPT_PATH = PROMPT_DIR / "a3_unit_analysis_v1.txt"
CROP_COMPARE_SCHEMA_VERSION = "a3-crop-compare-v2"
UNIT_ANALYSIS_SCHEMA_VERSION = "a3-unit-analysis-v1"
VALID_LOAD_TYPES = {"集中", "均布", "弯矩"}
CROP_COMPARE_CHECK_KEYS = (
    "selected_diagram_match",
    "single_target_diagram",
    "structure_complete",
    "supports_complete",
    "external_loads_complete",
    "image_clear",
)


class A3ModelError(RuntimeError):
    """Raised when an A3 model call or its bounded response is unusable."""


@dataclass(frozen=True)
class CropCompareResult:
    selected_unit_id: str
    verdict: str
    checks: Mapping[str, bool | None]
    schema_version: str = CROP_COMPARE_SCHEMA_VERSION

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"


@dataclass(frozen=True)
class A3UnitAnalysis:
    loads: tuple[dict[str, str], ...]
    chapter_hint: str
    chapter_confidence: float
    chapter_evidence: str
    category: str
    load_details: tuple[dict[str, Any], ...]
    schema_version: str = UNIT_ANALYSIS_SCHEMA_VERSION


class A3PageObserver(Protocol):
    def observe(self, image_path: Path) -> A3PageUnderstanding: ...


class A3CropVerifier(Protocol):
    def verify(
        self,
        original_page_path: Path,
        crop_path: Path,
        selected_unit: Mapping[str, Any],
        page_understanding: Mapping[str, Any],
    ) -> CropCompareResult: ...


class A3UnitAnalyzer(Protocol):
    def analyze(self, crop_path: Path, context_text: str) -> A3UnitAnalysis: ...


def load_prompt(path: str | Path) -> str:
    prompt = Path(path).read_text(encoding="utf-8").strip()
    if not prompt:
        raise A3ModelError(f"empty prompt: {path}")
    return prompt


def parse_crop_compare_result(payload: str | Mapping[str, Any], *, expected_unit_id: str) -> CropCompareResult:
    data = _raw_json_object(payload)
    if set(data) != {
        "schema_version",
        "selected_unit_id",
        "verdict",
        "checks",
    }:
        raise A3ModelError("invalid crop comparison fields")
    if data.get("schema_version") != CROP_COMPARE_SCHEMA_VERSION:
        raise A3ModelError("unsupported crop comparison schema")
    selected_unit_id = str(data.get("selected_unit_id") or "").strip()
    if not selected_unit_id or selected_unit_id != str(expected_unit_id).strip():
        raise A3ModelError("crop comparison unit binding mismatch")
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in {"verified", "review_required"}:
        raise A3ModelError("invalid crop comparison verdict")
    raw_checks = data.get("checks")
    if not isinstance(raw_checks, Mapping) or set(raw_checks) != set(CROP_COMPARE_CHECK_KEYS):
        raise A3ModelError("invalid crop comparison checks")
    checks: dict[str, bool | None] = {}
    for key in CROP_COMPARE_CHECK_KEYS:
        value = raw_checks.get(key)
        if value is not None and not isinstance(value, bool):
            raise A3ModelError("crop comparison checks must be boolean or null")
        checks[key] = value
    all_checks_pass = all(value is True for value in checks.values())
    if (verdict == "verified") != all_checks_pass:
        raise A3ModelError("crop comparison verdict and checks disagree")
    return CropCompareResult(
        selected_unit_id=selected_unit_id,
        verdict=verdict,
        checks=checks,
    )


def parse_unit_analysis(payload: str | Mapping[str, Any], *, context_text: str) -> A3UnitAnalysis:
    data = _raw_json_object(payload)
    allowed = {
        "schema_version",
        "loads",
        "chapter_hint",
        "chapter_confidence",
        "chapter_evidence",
    }
    if set(data) != allowed:
        raise A3ModelError("invalid A3 unit analysis fields")
    if data.get("schema_version") != UNIT_ANALYSIS_SCHEMA_VERSION:
        raise A3ModelError("unsupported A3 unit analysis schema")
    raw_loads = data.get("loads")
    if not isinstance(raw_loads, list):
        raise A3ModelError("loads must be an array")
    loads: list[dict[str, str]] = []
    for item in raw_loads:
        if not isinstance(item, Mapping) or set(item) != {"type", "raw"}:
            raise A3ModelError("invalid load item")
        normalized = normalize_load_item(dict(item))
        if normalized["type"] not in VALID_LOAD_TYPES or not normalized["raw"]:
            raise A3ModelError("unsupported or empty load item")
        loads.append(normalized)
    chapter_hint = normalize_chapter_hint(data.get("chapter_hint"))
    chapter_confidence = normalize_chapter_confidence(data.get("chapter_confidence"))
    chapter_evidence = str(data.get("chapter_evidence") or "").strip()
    chapter_hint, chapter_confidence, chapter_evidence = guard_chapter_prediction(
        chapter_hint,
        chapter_confidence,
        chapter_evidence,
        str(context_text or "").strip(),
    )
    category, load_details = classify_loads(loads)
    return A3UnitAnalysis(
        loads=tuple(loads),
        chapter_hint=chapter_hint,
        chapter_confidence=chapter_confidence,
        chapter_evidence=chapter_evidence,
        category=category,
        load_details=tuple(load_details),
    )


class _QwenVisionClient:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.model = str(model).strip() or DEFAULT_MODEL
        self.endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT
        self.api_key = str(api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def _call(
        self,
        *,
        system_prompt: str,
        user_content: list[dict[str, Any]],
        max_tokens: int,
        call_type: str,
    ) -> str:
        if not self.api_key:
            raise A3ModelError("dashscope_not_configured")
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "enable_thinking": False,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def request_data() -> dict[str, Any]:
            return request_json_with_retry(request, timeout=int(self.timeout_seconds))

        data = timed_model_call(
            request_data,
            provider="dashscope",
            model=self.model,
            call_type=call_type,
            usage_getter=lambda value: value.get("usage", {}),
            request_id_getter=lambda value: str(value.get("request_id") or value.get("id") or ""),
            attempt_count_getter=lambda value: int(getattr(value, "client_attempt_count", 1) or 1),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise A3ModelError("invalid qwen response") from exc
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return content


class QwenA3PageObserver(_QwenVisionClient):
    def __init__(self, *, prompt_path: str | Path = A3_PAGE_PROMPT_PATH, max_attempts: int = 2, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompt_path = Path(prompt_path)
        self.max_attempts = min(2, max(1, int(max_attempts)))

    def observe(self, image_path: Path) -> A3PageUnderstanding:
        return self.observe_with_diagnostics(image_path)

    def observe_with_diagnostics(
        self,
        image_path: Path,
        *,
        on_validation_error: Callable[[int, Exception], None] | None = None,
    ) -> A3PageUnderstanding:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            retry_instruction = ""
            if attempt and last_error is not None:
                retry_instruction = (
                    "上一输出未通过 schema 校验："
                    f"{type(last_error).__name__}: {str(last_error)[:240]}。"
                    "请修正该错误并重新完整输出。"
                )
            content = self._call(
                system_prompt=load_prompt(self.prompt_path),
                user_content=[
                    {"type": "image_url", "image_url": {"url": image_to_model_data_url(image_path)}},
                    {
                        "type": "text",
                        "text": "请理解这张完整页面并只输出规定 JSON。"
                        + retry_instruction,
                    },
                ],
                max_tokens=5000,
                call_type="qwen_a3_page_understanding",
            )
            try:
                return parse_a3_page_understanding(content)
            except Exception as exc:  # noqa: BLE001 - one bounded schema retry.
                last_error = exc
                if on_validation_error is not None:
                    try:
                        on_validation_error(attempt + 1, exc)
                    except Exception:  # noqa: BLE001 - diagnostics must not break correction.
                        pass
        raise A3ModelError("invalid A3 page understanding output") from last_error


class QwenA3CropVerifier(_QwenVisionClient):
    def __init__(self, *, prompt_path: str | Path = A3_CROP_COMPARE_PROMPT_PATH, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompt_path = Path(prompt_path)

    def verify(
        self,
        original_page_path: Path,
        crop_path: Path,
        selected_unit: Mapping[str, Any],
        page_understanding: Mapping[str, Any],
    ) -> CropCompareResult:
        unit_id = str(selected_unit.get("unit_id") or "").strip()
        if not unit_id:
            raise A3ModelError("selected unit id is required")
        content = self._call(
            system_prompt=load_prompt(self.prompt_path),
            user_content=[
                {"type": "image_url", "image_url": {"url": image_to_model_data_url(original_page_path)}},
                {"type": "image_url", "image_url": {"url": image_to_model_data_url(crop_path)}},
                {
                    "type": "text",
                    "text": json.dumps(
                        {"selected_unit": dict(selected_unit), "page_understanding": dict(page_understanding)},
                        ensure_ascii=False,
                    ),
                },
            ],
            max_tokens=300,
            call_type="qwen_a3_crop_compare",
        )
        return parse_crop_compare_result(content, expected_unit_id=unit_id)


class QwenA3UnitAnalyzer(_QwenVisionClient):
    def __init__(self, *, prompt_path: str | Path = A3_UNIT_ANALYSIS_PROMPT_PATH, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.prompt_path = Path(prompt_path)

    def analyze(self, crop_path: Path, context_text: str) -> A3UnitAnalysis:
        clean_context = str(context_text or "").strip()
        content = self._call(
            system_prompt=load_prompt(self.prompt_path),
            user_content=[
                {"type": "image_url", "image_url": {"url": image_to_model_data_url(crop_path)}},
                {
                    "type": "text",
                    "text": "context_text:\n" + clean_context + "\n只输出规定 JSON。",
                },
            ],
            max_tokens=1000,
            call_type="qwen_a3_unit_analysis",
        )
        return parse_unit_analysis(content, context_text=clean_context)


def _raw_json_object(payload: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    text = str(payload or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise A3ModelError("model output must be one raw JSON object")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise A3ModelError("invalid model JSON") from exc
    if not isinstance(data, dict):
        raise A3ModelError("model output must be an object")
    return data

"""Small, side-effect-free guard for text that is about to reach a user.

The watchdog deliberately runs after business rendering and does not inspect or
change Agent state.  ``observe`` is the default mode so it can be deployed for
coverage measurement before any text is blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Literal


WatchdogMode = Literal["observe", "enforce"]
WatchdogAction = Literal["pass", "polish", "replace"]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stack_trace", re.compile(r"\bTraceback\b|\b(?:Error|Exception)\b", re.I)),
    ("raw_json", re.compile(r"(?:^|\s)[{\[]\s*[\"']|[}\]](?:\s|$)")),
    ("local_path", re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|tmp|var|opt|workspace)/)")),
    ("credential", re.compile(r"(?:api[_ -]?key|token|secret|password|密码|密钥|凭据)", re.I)),
    ("prompt_disclosure", re.compile(r"(?:system\s*prompt|prompt|reasoning|思维链|提示词)", re.I)),
    ("command", re.compile(r"(?:^|\s)(?:python|powershell|cmd|bash|curl|git)\s+[^。！？\n]+", re.I)),
)
_MEDIA_DELIVERY_CLAIM = re.compile(
    r"(?:答案|候选|图片|题图).{0,10}(?:发你|发送|返回|展示|显示)|"
    r"(?:发你|发送|返回|展示|显示).{0,10}(?:答案|候选|图片|题图)"
)


@dataclass(frozen=True)
class OutputWatchdogResult:
    text: str
    action: WatchdogAction
    reasons: tuple[str, ...] = ()
    original_length: int = 0


def inspect_output(
    text: str | None,
    *,
    expected_media: int = 0,
    delivered_media: int = 0,
) -> OutputWatchdogResult:
    """Classify final text without changing it or performing I/O."""

    raw = str(text or "")
    value = raw.strip()
    reasons: list[str] = []
    if not value:
        reasons.append("empty")
    if len(value) > 2000:
        reasons.append("overlong")
    if _CONTROL_RE.search(value):
        reasons.append("control_character")
    if re.search(r"[ \t]{2,}", value):
        reasons.append("repeated_whitespace")
    for reason, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(value):
            reasons.append(reason)
    if (
        expected_media > 0
        and delivered_media < expected_media
        and _MEDIA_DELIVERY_CLAIM.search(value)
    ):
        reasons.append("media_delivery_mismatch")
    dangerous = any(
        reason not in {"empty", "overlong", "control_character", "repeated_whitespace"}
        for reason in reasons
    )
    return OutputWatchdogResult(
        text=value,
        action="replace" if dangerous or not value else ("polish" if reasons else "pass"),
        reasons=tuple(dict.fromkeys(reasons)),
        original_length=len(value),
    )


def guard_output(
    text: str | None,
    *,
    mode: WatchdogMode = "observe",
    polisher: Callable[[str], str] | None = None,
    fallback: str = "这次没查成功，请稍后重试。",
) -> OutputWatchdogResult:
    """Inspect text and optionally enforce the result.

    In observe mode the original text is always returned.  In enforce mode a
    dangerous result is replaced; a mild result may be passed to a caller-owned
    bounded polisher, and failed/invalid polishing falls back safely.
    """

    if mode not in {"observe", "enforce"}:
        raise ValueError("mode must be 'observe' or 'enforce'")
    result = inspect_output(text)
    if mode == "observe" or result.action == "pass":
        return result
    if result.action == "polish" and polisher is not None:
        try:
            polished = str(polisher(result.text) or "").strip()
            checked = inspect_output(polished)
            if checked.action == "pass":
                return OutputWatchdogResult(polished, "polish", result.reasons, result.original_length)
        except Exception:  # caller-owned model must never break delivery
            pass
    return OutputWatchdogResult(str(fallback).strip(), "replace", result.reasons, result.original_length)

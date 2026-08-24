"""Fail-open, deterministic observation of public Agent output.

This module never changes the text it receives and never calls a model. It is
intended to run beside the public Web/A3 serialization boundary while an
output contract is being observed in a real service.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
from threading import Lock
from threading import Thread
import time
from typing import Any


OUTPUT_CATEGORIES = frozenset({"normal", "awkward", "dangerous"})
MAX_PREVIEW_CHARS = 280

_DANGEROUS_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("traceback", re.compile(r"\btraceback\b|\bFile \"[^\"]+\", line \d+", re.IGNORECASE)),
    ("exception", re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_.]{0,80}(?:Error|Exception)|Internal Server Error|Connection refused|stack trace)\b", re.IGNORECASE)),
    ("english_error_marker", re.compile(r"\b(?:ERROR|FATAL|FAILED|EXCEPTION)\s*[:\-]", re.IGNORECASE)),
    ("windows_path", re.compile(r"\b[A-Za-z]:[\\/][^\s<>\"']*")),
    ("network_path", re.compile(r"(?:https?://|\\\\)[^\s<>\"']+", re.IGNORECASE)),
    ("unix_path", re.compile(r"(?:^|\s)/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)", re.IGNORECASE)),
    ("credential", re.compile(r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|secret|cookie)\b(?:\s*[:=]\s*[^\s,;}]+|\s+[A-Za-z0-9._-]{8,})?", re.IGNORECASE)),
    ("internal_field", re.compile(r"\b(?:raw_model_output|safe_facts|last_error|last_intent|candidate_generation|_a3_media_guard|debug|reasoning|prompt|stack_trace|request_id|search_id|protocol_code)\b", re.IGNORECASE)),
    ("json_internal_field", re.compile(r"[\"'](?:error|data|safe_facts|raw_model_output|reason|traceback)[\"']\s*:")),
    ("serialized_internal_field", re.compile(r"\b(?:error|data|reason|status|layer)\s*[:=]\s*[^\s,;}]+", re.IGNORECASE)),
)

_AWKWARD_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("overlong", re.compile(r".")),
    ("repeated_punctuation", re.compile(r"[!?！？。]{3,}|[、,]{4,}")),
    ("unbounded_english", re.compile(r"\b[A-Za-z]{24,}\b")),
)


def _redact(text: str) -> str:
    """Remove dangerous spans before a preview can reach the observation log."""

    redacted = text
    for rule, pattern in _DANGEROUS_RULES:
        redacted = pattern.sub(f"<redacted:{rule}>", redacted)
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[:MAX_PREVIEW_CHARS]


def _text_preview(text: str, *, dangerous: bool) -> str:
    clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if dangerous:
        return "<dangerous output redacted>"
    clean = re.sub(r"\s+", " ", clean)
    return clean[:MAX_PREVIEW_CHARS]


def classify_output(text: str) -> tuple[str, tuple[str, ...]]:
    """Classify final public text without changing or normalizing it."""

    value = str(text or "")
    dangerous_rules = tuple(
        rule for rule, pattern in _DANGEROUS_RULES if pattern.search(value)
    )
    if dangerous_rules:
        return "dangerous", dangerous_rules

    awkward_rules: list[str] = []
    if len(value) > 180:
        awkward_rules.append("overlong")
    for rule, pattern in _AWKWARD_RULES:
        if rule != "overlong" and pattern.search(value):
            awkward_rules.append(rule)
    if awkward_rules:
        return "awkward", tuple(dict.fromkeys(awkward_rules))
    return "normal", ()


class OutputWatchdog:
    """Append-only output observer that is deliberately fail-open."""

    def __init__(
        self,
        root: str | Path,
        *,
        enabled: bool = True,
        queue_size: int = 2048,
    ) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / "output_watchdog.jsonl"
        self.enabled = bool(enabled)
        self._lock = Lock()
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=max(1, int(queue_size)))
        self._worker: Thread | None = None
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001 - observation must not block service boot.
                self.enabled = False
        if self.enabled:
            self._worker = Thread(
                target=self._write_loop,
                name="tiku-output-watchdog",
                daemon=True,
            )
            self._worker.start()

    def observe(
        self,
        text: str,
        *,
        intent: str = "",
        protocol_code: str = "",
        media_status: str = "",
        endpoint: str = "public",
        session_id: str = "",
    ) -> dict[str, Any] | None:
        """Classify and append one sample; return the in-memory observation."""

        category, rules = classify_output(text)
        sample: dict[str, Any] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "rules": list(rules),
            "text_length": len(str(text or "")),
            "text_hash": hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:20],
            "preview": _text_preview(text, dangerous=category == "dangerous"),
            "intent": str(intent or "")[:80],
            "protocol_code": str(protocol_code or "")[:80],
            "media_status": str(media_status or "")[:32],
            "endpoint": str(endpoint or "public")[:32],
        }
        clean_session = str(session_id or "").strip()
        if clean_session:
            sample["session_hash"] = hashlib.sha256(clean_session.encode("utf-8")).hexdigest()[:16]
        if not self.enabled:
            return sample
        try:
            self._queue.put_nowait(sample)
        except Full:
            pass
        return sample

    def flush(self, *, timeout_seconds: float = 2.0) -> bool:
        """Wait for queued observations during tests and controlled shutdowns."""

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def _write_loop(self) -> None:
        while True:
            try:
                sample = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                self._append(sample)
            finally:
                self._queue.task_done()

    def _append(self, sample: dict[str, Any]) -> None:
        try:
            line = json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
        except Exception:  # noqa: BLE001 - fail-open: never affect public output.
            pass


def observe_output(
    observer: object,
    text: str,
    **metadata: object,
) -> dict[str, Any] | None:
    observe = getattr(observer, "observe", None)
    if not callable(observe):
        return None
    try:
        return observe(text, **metadata)
    except Exception:  # noqa: BLE001 - public response must always pass through.
        return None


def observe_public_output(
    runtime: object,
    text: str,
    *,
    intent: str = "",
    protocol_code: str = "",
    media_status: str = "",
    endpoint: str = "public",
    session_id: str = "",
) -> dict[str, Any] | None:
    """Invoke a runtime-attached observer without making it a runtime contract."""

    return observe_output(
        getattr(runtime, "output_watchdog", None),
        text,
        intent=intent,
        protocol_code=protocol_code,
        media_status=media_status,
        endpoint=endpoint,
        session_id=session_id,
    )

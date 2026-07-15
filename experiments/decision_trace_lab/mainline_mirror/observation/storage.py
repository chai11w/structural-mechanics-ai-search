from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4


FORBIDDEN_KEYS = {
    "text", "user_text", "raw", "prompt", "response", "model_response",
    "image_path", "answer_path", "candidate", "headers", "cookie",
    "api_key", "token", "password", "secret",
}
EVENT_TYPES = {
    "turn_started", "intent_decided", "authorization_checked", "tool_started",
    "tool_completed", "state_transition", "turn_completed",
}
VERDICTS = {"correct", "incorrect", "uncertain"}
NO_MATCH = {"reasonable_no_match", "false_no_match", "uncertain_no_match"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def trace_key(session_id: str) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]


def _privacy_issues(value: Any, path: str = "$") -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if str(key).lower() in FORBIDDEN_KEYS:
                issues.append({"code": "privacy_forbidden_key", "path": child})
            issues.extend(_privacy_issues(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(_privacy_issues(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        if len(value) > 500:
            issues.append({"code": "privacy_oversized_string", "path": path})
        if ":\\" in value or value.startswith(("/home/", "/Users/", "/tmp/", "/var/")):
            issues.append({"code": "privacy_absolute_path", "path": path})
    return issues


@dataclass
class TurnSink:
    store: "ObservationStore"
    trace_id: str
    turn_id: str
    sequence: int = 0

    def emit(self, event_type: str, payload: dict[str, Any], *, duration_ms: int | None = None) -> dict[str, Any] | None:
        self.sequence += 1
        event = {
            "schema_version": 2,
            "trace_id": self.trace_id,
            "turn_id": self.turn_id,
            "event_id": uuid4().hex,
            "sequence": self.sequence,
            "event_type": event_type,
            "recorded_at": _now(),
            "payload": payload,
        }
        if duration_ms is not None:
            event["duration_ms"] = max(0, int(duration_ms))
        return self.store.append_event(event)


class ObservationStore:
    """Append-only sidecar. Every public write is fail-open for the Agent."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.traces_path = self.data_dir / "traces.jsonl"
        self.labels_path = self.data_dir / "labels.jsonl"
        self.diagnostics_path = self.data_dir / "diagnostics.jsonl"
        self._lock = threading.Lock()

    def new_turn(self, session_id: str) -> TurnSink:
        return TurnSink(self, trace_key(session_id), uuid4().hex)

    def append_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        try:
            issues = _privacy_issues(event)
            if issues:
                self._diagnose("trace_rejected_privacy", {"codes": sorted({x["code"] for x in issues})})
                return None
            self._append(self.traces_path, event)
            return event
        except Exception as exc:  # noqa: BLE001 - mandatory fail-open boundary.
            self._diagnose("trace_write_failed", {"error_kind": type(exc).__name__})
            return None

    def append_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("target_id") or "").strip()
        dimension = str(payload.get("dimension") or "result_interpretation").strip()
        verdict = str(payload.get("verdict") or "").strip()
        no_match = str(payload.get("no_match_classification") or "").strip()
        if not target_id or verdict not in VERDICTS or (no_match and no_match not in NO_MATCH):
            raise ValueError("invalid label")
        rows = [
            row for row in self.labels()
            if row.get("target_id") == target_id and row.get("dimension") == dimension
        ]
        current = max(rows, key=lambda row: int(row.get("label_revision") or 0), default=None)
        optional = {
            key: str(payload.get(key) or "").strip()
            for key in ("expected", "reason", "error_category")
        }
        if current is not None and all(
            str(current.get(key) or "") == value
            for key, value in {"verdict": verdict, "no_match_classification": no_match, **optional}.items()
        ):
            return {**current, "unchanged": True}
        revision = 1 + max(
            (int(row.get("label_revision") or 0) for row in rows),
            default=0,
        )
        label = {
            "label_id": uuid4().hex,
            "target_type": str(payload.get("target_type") or "event"),
            "target_id": target_id,
            "dimension": dimension,
            "verdict": verdict,
            "no_match_classification": no_match,
            "label_revision": revision,
            "labeled_at": _now(),
        }
        label.update({key: value for key, value in optional.items() if value})
        if _privacy_issues(label):
            raise ValueError("label rejected by privacy policy")
        self._append(self.labels_path, label)
        return label

    def latest_labels(self, target_ids: set[str] | None = None) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.labels():
            target_id = str(row.get("target_id") or "")
            if target_ids is not None and target_id not in target_ids:
                continue
            key = (target_id, str(row.get("dimension") or ""))
            if int(row.get("label_revision") or 0) >= int(latest.get(key, {}).get("label_revision") or 0):
                latest[key] = row
        return list(latest.values())

    def events(self, *, trace_id: str = "", turn_id: str = "") -> list[dict[str, Any]]:
        rows = _read_jsonl(self.traces_path)
        if trace_id:
            rows = [row for row in rows if row.get("trace_id") == trace_id]
        if turn_id:
            rows = [row for row in rows if row.get("turn_id") == turn_id]
        return sorted(rows, key=lambda row: (str(row.get("recorded_at") or ""), int(row.get("sequence") or 0)))

    def labels(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.labels_path)

    def turns(self, trace_id: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self.events(trace_id=trace_id):
            grouped[str(event.get("turn_id") or "")].append(event)
        result = []
        for turn_id, events in grouped.items():
            completed = next((e for e in reversed(events) if e.get("event_type") == "turn_completed"), None)
            result.append({
                "turn_id": turn_id,
                "recorded_at": events[0].get("recorded_at", ""),
                "event_count": len(events),
                "phase": (completed or {}).get("payload", {}).get("phase_after", ""),
                "response_type": (completed or {}).get("payload", {}).get("response_type", ""),
                "issues": [issue for issue in scan_events(events) if issue.get("turn_id") in {None, turn_id}],
            })
        return sorted(result, key=lambda row: row["recorded_at"], reverse=True)

    def summary(self, trace_id: str = "") -> dict[str, Any]:
        events = self.events(trace_id=trace_id)
        latest = {str(row.get("target_id") or ""): row for row in self.latest_labels()}
        eligible = [e for e in events if e.get("event_type") in {"intent_decided", "tool_completed", "turn_completed"}]
        counts = Counter(latest.get(str(e.get("event_id")), {}).get("verdict", "unlabeled") for e in eligible)
        return {"key_items": len(eligible), "reviewed": len(eligible) - counts["unlabeled"], "verdicts": dict(counts)}

    def scan(self, trace_id: str = "") -> list[dict[str, Any]]:
        return scan_events(self.events(trace_id=trace_id))

    def _append(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _diagnose(self, code: str, payload: dict[str, Any]) -> None:
        try:
            self._append(self.diagnostics_path, {"recorded_at": _now(), "code": code, "payload": payload})
        except Exception:
            pass


def scan_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    turns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        turn_id = str(event.get("turn_id") or "")
        turns[turn_id].append(event)
        if event.get("event_type") not in EVENT_TYPES:
            issues.append({"code": "unknown_event", "turn_id": turn_id, "event_id": event.get("event_id")})
        issues.extend({**issue, "turn_id": turn_id, "event_id": event.get("event_id")} for issue in _privacy_issues(event))
        if event.get("event_type") == "state_transition" and event.get("payload", {}).get("automatic_check") == "fail":
            issues.append({"code": "embedded_automatic_check_failed", "turn_id": turn_id, "event_id": event.get("event_id")})
    for turn_id, rows in turns.items():
        rows.sort(key=lambda row: int(row.get("sequence") or 0))
        if [row.get("sequence") for row in rows] != list(range(1, len(rows) + 1)):
            issues.append({"code": "sequence_not_contiguous", "turn_id": turn_id})
        types = Counter(row.get("event_type") for row in rows)
        if types["turn_started"] != 1:
            issues.append({"code": "turn_started_cardinality", "turn_id": turn_id})
        if types["turn_completed"] != 1:
            issues.append({"code": "turn_completed_cardinality", "turn_id": turn_id})
        completed_turn = next((row for row in rows if row.get("event_type") == "turn_completed"), None)
        if completed_turn is not None:
            expected_authorizations = int(completed_turn.get("payload", {}).get("authorization_count") or 0)
            if types["authorization_checked"] != expected_authorizations:
                issues.append({
                    "code": "authorization_trace_count_mismatch",
                    "turn_id": turn_id,
                    "expected": expected_authorizations,
                    "recorded": types["authorization_checked"],
                })
        started = Counter((r.get("payload", {}).get("tool_name"), r.get("payload", {}).get("call_index")) for r in rows if r.get("event_type") == "tool_started")
        completed = Counter((r.get("payload", {}).get("tool_name"), r.get("payload", {}).get("call_index")) for r in rows if r.get("event_type") == "tool_completed")
        if started != completed:
            issues.append({"code": "tool_pair_mismatch", "turn_id": turn_id})
    return issues


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"event_type": "invalid_json"})
    return rows

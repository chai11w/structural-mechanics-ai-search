"""Plan and apply bounded retention maintenance for Agent runtime evidence.

Planning is strictly read-only. Applying a plan is deliberately separate and
requires the caller to supply the exact plan hash plus an external backup root.
Sources without an approved policy are reported but never mutated.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from typing import Mapping, Sequence

from .sqlite_reader import readonly_connection, table_columns


RETENTION_PLAN_SCHEMA_VERSION = 1
A3_PAGE_ERROR_RETENTION_DAYS = 30

_FEEDBACK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
_TRACE_ID_RE = re.compile(r"^trace_[0-9a-f]{32}$")

_RUNTIME_LAYOUTS: dict[str, dict[str, tuple[str, ...]]] = {
    "8790": {
        "task_logs": ("a2/task_logs.jsonl", "task_logs.jsonl"),
        "output_watchdog": ("output_watchdog/output_watchdog.jsonl",),
        "service_logs": (
            "tiku_8790.out.log",
            "tiku_8790.err.log",
            "watchdog_8790.status",
        ),
    },
    "8896": {
        "task_logs": ("a2/task_logs.jsonl", "task_logs.jsonl"),
        "output_watchdog": ("output_watchdog/output_watchdog.jsonl",),
        "service_logs": (
            "service_logs/tiku_8896.out.log",
            "service_logs/tiku_8896.err.log",
            "service_logs/watchdog_8896.status",
        ),
    },
}

_MUTABLE_FILES = {
    "responses": "responses.sqlite3",
    "feedback_cases": "feedback.sqlite3",
    "a3_page_errors": "a3_sessions.sqlite3",
}


class RetentionError(RuntimeError):
    """Raised before maintenance can cross a declared safety boundary."""


def build_retention_plan(
    runtime_root: str | Path,
    *,
    runtime_name: str,
    repository_root: str | Path,
    as_of: datetime | str | None = None,
    now: datetime | str | None = None,
    future_report_only: bool = False,
) -> dict[str, object]:
    """Build a deterministic, non-mutating retention plan for one runtime."""

    name = _validated_runtime_name(runtime_name)
    repository = _resolved_directory(repository_root, "repository root")
    runtime = _resolved_directory(runtime_root, "runtime root")
    if not _is_relative_to(runtime, repository):
        raise RetentionError("runtime root must stay inside the repository")
    current = _current_timestamp(now)
    timestamp = _aware_timestamp(as_of, "as_of") if as_of is not None else current
    is_future = timestamp > current
    if is_future and not future_report_only:
        raise RetentionError(
            "future as_of is report-only and cannot create an applyable plan"
        )
    timestamp_text = timestamp.isoformat()

    feedback_action, feedback_holds, feedback_issues = _plan_feedback(
        runtime, timestamp_text
    )
    response_action, response_issues = _plan_responses(
        runtime, timestamp_text, feedback_holds
    )
    page_error_action, page_error_issues = _plan_page_errors(runtime, timestamp)

    report_only = [
        _sqlite_report(
            runtime,
            source="trace_events",
            relative="trace_events.sqlite3",
            table="trace_events",
        )
    ]
    for source in ("task_logs", "output_watchdog", "service_logs"):
        for relative in _RUNTIME_LAYOUTS[name][source]:
            report_only.append(_file_report(runtime, source=source, relative=relative))

    exclusions = [
        _excluded_file(runtime, "model_costs", "model_costs.sqlite3"),
        _excluded_file(runtime, "model_costs_legacy_a2", "a2/model_costs.sqlite3"),
        {
            "source": "feedback_metadata",
            "file": "feedback.sqlite3:message_feedback",
            "policy": "excluded",
            "reason": "feedback rows, review fields, and legacy v7 metadata remain readable",
        },
        {
            "source": "control_admin_audit",
            "file": "8795/control.sqlite3",
            "policy": "excluded",
            "reason": "control data and administrator audit are never maintenance inputs",
        },
    ]
    actions = [response_action, feedback_action, page_error_action]
    if is_future:
        actions = [
            {
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"candidates", "held_response_ids"}
                },
                "policy": "report_only",
            }
            for item in actions
        ]
    issues = [*feedback_issues, *response_issues, *page_error_issues]
    plan: dict[str, object] = {
        "schema_version": RETENTION_PLAN_SCHEMA_VERSION,
        "mode": "report_only" if is_future else "plan",
        "runtime_name": name,
        "runtime_root": str(runtime),
        "repository_root": str(repository),
        "as_of": timestamp_text,
        "actions": actions,
        "report_only": report_only,
        "exclusions": exclusions,
        "issues": list(dict.fromkeys(issues)),
        "summary": {
            "candidate_count": sum(int(item["candidate_count"]) for item in actions),
            "estimated_logical_bytes": sum(
                int(item["estimated_logical_bytes"]) for item in actions
            ),
            "policy_missing_count": sum(
                1 for item in report_only if item["policy"] == "policy_missing"
            ),
            "sqlite_disk_note": (
                "SQLite row estimates are logical bytes; DELETE does not shrink database files "
                "and this maintenance never runs VACUUM."
            ),
        },
    }
    plan["plan_hash"] = retention_plan_hash(plan)
    return plan


def retention_plan_hash(plan: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 of a plan, excluding its hash field."""

    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_retention_plan(
    path: str | Path,
    plan: Mapping[str, object],
    *,
    now: datetime | str | None = None,
) -> None:
    """Write an explicitly requested plan file without touching runtime state."""

    _require_applyable_as_of(plan, now=now)
    target = Path(path)
    if target.exists():
        raise RetentionError("retention plan output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_retention_plan(
    path: str | Path, *, now: datetime | str | None = None
) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionError("retention plan is unreadable") from exc
    if not isinstance(value, dict):
        raise RetentionError("retention plan must be a JSON object")
    _require_applyable_as_of(value, now=now)
    return value


def apply_retention_plan(
    plan: Mapping[str, object],
    *,
    expected_plan_hash: str,
    repository_root: str | Path,
    backup_root: str | Path,
    allowed_runtime_roots: Sequence[str | Path],
    runtime_stopped_confirmed: bool = False,
    now: datetime | str | None = None,
) -> dict[str, object]:
    """Apply one confirmed plan after verified, repository-external backups."""

    validated = _validated_plan(plan, expected_plan_hash, now=now)
    if runtime_stopped_confirmed is not True:
        raise RetentionError("runtime stop confirmation is required before apply")
    repository = _resolved_directory(repository_root, "repository root")
    runtime = _resolved_directory(str(validated["runtime_root"]), "runtime root")
    if str(validated["repository_root"]) != str(repository):
        raise RetentionError("plan repository root does not match this checkout")
    allowed = {_resolved_directory(value, "allowed runtime root") for value in allowed_runtime_roots}
    if runtime not in allowed:
        raise RetentionError("plan runtime root is not explicitly allowed")
    if not _is_relative_to(runtime, repository):
        raise RetentionError("runtime root escaped the repository")

    backup_base = Path(backup_root).resolve(strict=False)
    if (
        backup_base == repository
        or _is_relative_to(backup_base, repository)
        or _is_relative_to(repository, backup_base)
    ):
        raise RetentionError("backup root must be outside the repository")
    nearest = _nearest_existing_parent(backup_base)
    if _is_reparse_or_symlink(nearest):
        raise RetentionError("backup root ancestry cannot be a link or reparse point")

    plan_hash = str(validated["plan_hash"])
    date_folder = str(validated["as_of"])[:10]
    target = (
        backup_base
        / date_folder
        / f"retention_{validated['runtime_name']}_{plan_hash[:16]}"
    ).resolve(strict=False)
    if not _is_relative_to(target, backup_base) or target == backup_base:
        raise RetentionError("backup target escaped the approved backup root")
    result_path = target / "result.json"
    if target.exists():
        if result_path.is_file():
            previous = _read_json_object(result_path)
            if previous.get("plan_hash") == plan_hash and previous.get("status") == "applied":
                return {**previous, "status": "already_applied"}
        raise RetentionError("backup target already exists without a completed matching result")

    try:
        _validate_action_contract(validated, runtime)
        _preflight_current_candidates(validated, runtime)
    except RetentionError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RetentionError("retention plan structure is invalid") from exc

    required_bytes = _backup_size_estimate(validated, runtime)
    if shutil.disk_usage(nearest).free < required_bytes:
        raise RetentionError("insufficient free space for verified backups")

    target.mkdir(parents=True, exist_ok=False)
    try:
        _write_json(target / "plan.json", validated)
        backups = _create_verified_backups(validated, runtime, target)
        _write_json(
            target / "backup_manifest.json",
            {
                "plan_hash": plan_hash,
                "created_at": datetime.now(UTC).isoformat(),
                "files": backups,
            },
        )
        response_count = _apply_response_candidates(validated, runtime)
        feedback_count = _apply_feedback_candidates(validated, runtime)
        page_error_count = _apply_page_error_candidates(validated, runtime)
        result: dict[str, object] = {
            "status": "applied",
            "plan_hash": plan_hash,
            "runtime_name": validated["runtime_name"],
            "backup_dir": str(target),
            "applied_at": datetime.now(UTC).isoformat(),
            "changed": {
                "responses": response_count,
                "feedback_cases": feedback_count,
                "a3_page_errors": page_error_count,
            },
        }
        _write_json(result_path, result)
        return result
    except Exception as exc:
        _write_json(
            target / "failure.json",
            {
                "plan_hash": plan_hash,
                "failed_at": datetime.now(UTC).isoformat(),
                "error_type": type(exc).__name__,
            },
        )
        raise


def format_retention_plan(plan: Mapping[str, object]) -> str:
    report_only = str(plan.get("mode") or "") == "report_only"
    title = "保留预测（仅报告）" if report_only else "保留计划"
    hash_label = "报告哈希" if report_only else "计划哈希"
    lines = [
        f"{title}: runtime={plan['runtime_name']} as_of={plan['as_of']}",
        f"{hash_label}: {plan['plan_hash']}",
    ]
    for item in plan.get("actions", []):
        assert isinstance(item, Mapping)
        lines.append(
            f"- {item['source']}: count={item['candidate_count']} "
            f"cutoff={item['cutoff']} file={item['file']} "
            f"estimated={item['estimated_logical_bytes']}B"
        )
    for item in plan.get("report_only", []):
        assert isinstance(item, Mapping)
        lines.append(
            f"- {item['source']}: policy_missing count={item['record_count']} "
            f"file={item['file']} bytes={item['file_bytes']}"
        )
    lines.append("排除: model costs、feedback metadata、8795 control/admin audit")
    lines.append(str(plan["summary"]["sqlite_disk_note"]))  # type: ignore[index]
    return "\n".join(lines)


def retention_plan_report(plan: Mapping[str, object]) -> dict[str, object]:
    """Return the public view without candidate identifiers or absolute paths."""

    summary_fields = (
        "candidate_count",
        "estimated_logical_bytes",
        "policy_missing_count",
        "sqlite_disk_note",
    )
    action_fields = (
        "source",
        "policy",
        "status",
        "file",
        "cutoff",
        "candidate_count",
        "held_count",
        "estimated_logical_bytes",
    )
    report_fields = (
        "source",
        "policy",
        "status",
        "file",
        "cutoff",
        "record_count",
        "file_bytes",
        "estimated_logical_bytes",
    )
    exclusion_fields = ("source", "policy", "status", "file", "file_bytes")
    return {
        "schema_version": plan["schema_version"],
        "mode": plan["mode"],
        "runtime_name": plan["runtime_name"],
        "as_of": plan["as_of"],
        "plan_hash": plan["plan_hash"],
        "actions": [
            {key: item[key] for key in action_fields if key in item}
            for item in plan.get("actions", [])
            if isinstance(item, Mapping)
        ],
        "report_only": [
            {key: item[key] for key in report_fields if key in item}
            for item in plan.get("report_only", [])
            if isinstance(item, Mapping)
        ],
        "exclusions": [
            {key: item[key] for key in exclusion_fields if key in item}
            for item in plan.get("exclusions", [])
            if isinstance(item, Mapping)
        ],
        "issue_count": len(plan.get("issues", [])),
        "summary": {
            key: plan["summary"][key]  # type: ignore[index]
            for key in summary_fields
            if key in plan["summary"]  # type: ignore[operator]
        },
    }


def _plan_feedback(
    runtime: Path, as_of: str
) -> tuple[dict[str, object], set[str], list[str]]:
    relative = "feedback.sqlite3"
    path = _source_path(runtime, relative)
    base = _action("feedback_cases", relative, as_of)
    if not path.is_file():
        return {**base, "status": "missing"}, set(), []
    required = {
        "feedback_id",
        "rated_response_id",
        "conversation_json",
        "case_expires_at",
        "case_purged_at",
        "updated_at",
    }
    issues: list[str] = []
    candidates: list[dict[str, object]] = []
    holds: set[str] = set()
    try:
        with readonly_connection(path) as connection:
            if not required.issubset(table_columns(connection, "message_feedback")):
                return {**base, "status": "schema_mismatch"}, holds, [
                    "feedback_cases:schema_mismatch"
                ]
            rows = connection.execute(
                "SELECT feedback_id, rated_response_id, conversation_json, "
                "case_expires_at, case_purged_at, updated_at "
                "FROM message_feedback WHERE case_purged_at = '' "
                "AND case_expires_at != '' ORDER BY case_expires_at, feedback_id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise RetentionError("feedback retention plan query failed") from exc

    for row in rows:
        feedback_id = str(row["feedback_id"])
        response_id = str(row["rated_response_id"] or "")
        expires_at = str(row["case_expires_at"])
        if expires_at > as_of:
            if _RESPONSE_ID_RE.fullmatch(response_id):
                holds.add(response_id)
            continue
        if not _FEEDBACK_ID_RE.fullmatch(feedback_id):
            issues.append(f"feedback_cases:invalid_feedback_id:{feedback_id[:32]}")
            continue
        case_relative = f"feedback_cases/{feedback_id}"
        case_dir = _source_path(runtime, case_relative, allow_missing=True)
        media_files: list[dict[str, object]] = []
        if case_dir.exists():
            if not case_dir.is_dir():
                issues.append(f"feedback_cases:not_directory:{feedback_id}")
                continue
            try:
                media_files = [
                    {
                        "file": item.relative_to(runtime).as_posix(),
                        "bytes": item.stat().st_size,
                        "sha256": _sha256_file(item),
                    }
                    for item in _regular_files(case_dir)
                ]
            except (OSError, RetentionError):
                issues.append(f"feedback_cases:unsafe_media_tree:{feedback_id}")
                continue
        conversation = str(row["conversation_json"] or "[]")
        candidates.append(
            {
                "feedback_id": feedback_id,
                "rated_response_id": response_id,
                "case_expires_at": expires_at,
                "updated_at": str(row["updated_at"]),
                "conversation_bytes": len(conversation.encode("utf-8")),
                "media_dir": case_relative if case_dir.is_dir() else "",
                "media_files": media_files,
            }
        )
    estimated = sum(
        int(item["conversation_bytes"])
        + sum(int(media["bytes"]) for media in item["media_files"])
        for item in candidates
    )
    return (
        {
            **base,
            "candidate_count": len(candidates),
            "estimated_logical_bytes": estimated,
            "candidates": candidates,
        },
        holds,
        issues,
    )


def _plan_responses(
    runtime: Path, as_of: str, feedback_holds: set[str]
) -> tuple[dict[str, object], list[str]]:
    relative = "responses.sqlite3"
    path = _source_path(runtime, relative)
    base = _action("responses", relative, as_of)
    if not path.is_file():
        return {**base, "status": "missing"}, []
    required = {"response_id", "trace_id", "expires_at"}
    try:
        with readonly_connection(path) as connection:
            if not required.issubset(table_columns(connection, "public_responses")):
                return {**base, "status": "schema_mismatch"}, [
                    "responses:schema_mismatch"
                ]
            rows = connection.execute(
                "SELECT response_id, trace_id, expires_at FROM public_responses "
                "WHERE expires_at <= ? ORDER BY expires_at, response_id",
                (as_of,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RetentionError("response retention plan query failed") from exc
    held = [str(row["response_id"]) for row in rows if str(row["response_id"]) in feedback_holds]
    candidates = [
        {
            "response_id": str(row["response_id"]),
            "trace_id": str(row["trace_id"]),
            "expires_at": str(row["expires_at"]),
        }
        for row in rows
        if str(row["response_id"]) not in feedback_holds
    ]
    return (
        {
            **base,
            "candidate_count": len(candidates),
            "held_count": len(held),
            "held_response_ids": held,
            "estimated_logical_bytes": sum(_row_estimate(item) for item in candidates),
            "candidates": candidates,
        },
        [],
    )


def _plan_page_errors(
    runtime: Path, as_of: datetime
) -> tuple[dict[str, object], list[str]]:
    relative = "a3_sessions.sqlite3"
    cutoff = (as_of - timedelta(days=A3_PAGE_ERROR_RETENTION_DAYS)).isoformat()
    path = _source_path(runtime, relative)
    base = _action("a3_page_errors", relative, cutoff)
    if not path.is_file():
        return {**base, "status": "missing"}, []
    required = {"event_id", "created_at"}
    try:
        with readonly_connection(path) as connection:
            if not required.issubset(table_columns(connection, "a3_page_errors")):
                return {**base, "status": "schema_mismatch"}, [
                    "a3_page_errors:schema_mismatch"
                ]
            rows = connection.execute(
                "SELECT event_id, created_at FROM a3_page_errors "
                "WHERE created_at < ? ORDER BY event_id",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise RetentionError("page-error retention plan query failed") from exc
    candidates = [
        {"event_id": int(row["event_id"]), "created_at": str(row["created_at"])}
        for row in rows
    ]
    return (
        {
            **base,
            "candidate_count": len(candidates),
            "estimated_logical_bytes": sum(_row_estimate(item) for item in candidates),
            "candidates": candidates,
        },
        [],
    )


def _action(source: str, relative: str, cutoff: str) -> dict[str, object]:
    return {
        "source": source,
        "policy": "approved",
        "status": "ok",
        "file": relative,
        "cutoff": cutoff,
        "candidate_count": 0,
        "estimated_logical_bytes": 0,
        "candidates": [],
    }


def _sqlite_report(
    runtime: Path, *, source: str, relative: str, table: str
) -> dict[str, object]:
    path = _source_path(runtime, relative)
    count = 0
    status = "missing"
    if path.is_file():
        try:
            with readonly_connection(path) as connection:
                if table_columns(connection, table):
                    count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    status = "ok"
                else:
                    status = "schema_mismatch"
        except sqlite3.Error:
            status = "query_failed"
    return {
        "source": source,
        "policy": "policy_missing",
        "status": status,
        "file": relative,
        "cutoff": None,
        "record_count": count,
        "file_bytes": path.stat().st_size if path.is_file() else 0,
        "estimated_logical_bytes": 0,
    }


def _file_report(runtime: Path, *, source: str, relative: str) -> dict[str, object]:
    path = _source_path(runtime, relative)
    return {
        "source": source,
        "policy": "policy_missing",
        "status": "ok" if path.is_file() else "missing",
        "file": relative,
        "cutoff": None,
        "record_count": _line_count(path) if path.is_file() else 0,
        "file_bytes": path.stat().st_size if path.is_file() else 0,
        "estimated_logical_bytes": 0,
    }


def _excluded_file(runtime: Path, source: str, relative: str) -> dict[str, object]:
    path = _source_path(runtime, relative)
    return {
        "source": source,
        "file": relative,
        "policy": "excluded",
        "reason": "historical fee ledger is never part of retention cleanup",
        "status": "present" if path.is_file() else "missing",
        "file_bytes": path.stat().st_size if path.is_file() else 0,
    }


def _validated_plan(
    plan: Mapping[str, object],
    expected_plan_hash: str,
    *,
    now: datetime | str | None = None,
) -> dict[str, object]:
    value = dict(plan)
    required = {
        "schema_version",
        "mode",
        "runtime_name",
        "runtime_root",
        "repository_root",
        "as_of",
        "actions",
        "issues",
        "plan_hash",
    }
    if not required.issubset(value):
        raise RetentionError("retention plan is missing required fields")
    if value.get("schema_version") != RETENTION_PLAN_SCHEMA_VERSION:
        raise RetentionError("unsupported retention plan schema")
    if value.get("mode") != "plan":
        raise RetentionError("retention input is not a plan")
    supplied = str(value.get("plan_hash") or "")
    calculated = retention_plan_hash(value)
    expected = str(expected_plan_hash or "").strip().lower()
    if not expected or expected != supplied or supplied != calculated:
        raise RetentionError("retention plan hash confirmation failed")
    _validated_runtime_name(str(value.get("runtime_name") or ""))
    _require_applyable_as_of(value, now=now)
    issues = value.get("issues")
    if not isinstance(issues, list) or issues:
        raise RetentionError("retention plan has unresolved source issues")
    return value


def _validate_action_contract(plan: Mapping[str, object], runtime: Path) -> None:
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise RetentionError("plan actions are invalid")
    seen: set[str] = set()
    for raw in actions:
        if not isinstance(raw, dict):
            raise RetentionError("plan action is invalid")
        source = str(raw.get("source") or "")
        expected_file = _MUTABLE_FILES.get(source)
        if expected_file is None or source in seen:
            raise RetentionError("plan contains an unexpected or duplicate action")
        if raw.get("policy") != "approved" or str(raw.get("file")) != expected_file:
            raise RetentionError("plan action crossed its approved policy")
        _source_path(runtime, expected_file)
        if not isinstance(raw.get("candidates"), list):
            raise RetentionError("plan candidates are invalid")
        if int(raw.get("candidate_count") or 0) != len(raw["candidates"]):
            raise RetentionError("plan candidate count does not match its snapshot")
        _validate_action_candidates(source, raw, plan)
        seen.add(source)
    if seen != set(_MUTABLE_FILES):
        raise RetentionError("plan is missing a required action")


def _validate_action_candidates(
    source: str, action: Mapping[str, object], plan: Mapping[str, object]
) -> None:
    candidates = action["candidates"]
    if not isinstance(candidates, list):
        raise RetentionError("plan candidates are invalid")
    cutoff = str(action.get("cutoff") or "")
    if source in {"responses", "feedback_cases"} and cutoff != str(plan["as_of"]):
        raise RetentionError("plan action cutoff does not match its approved policy")
    if source == "a3_page_errors":
        expected = (
            _aware_timestamp(str(plan["as_of"]), "as_of")
            - timedelta(days=A3_PAGE_ERROR_RETENTION_DAYS)
        ).isoformat()
        if cutoff != expected:
            raise RetentionError("page-error cutoff does not match its approved policy")
    seen_response_ids: set[str] = set()
    seen_feedback_ids: set[str] = set()
    seen_event_ids: set[int] = set()
    for item in candidates:
        if not isinstance(item, dict):
            raise RetentionError("plan candidate is invalid")
        if source == "responses":
            response_id = str(item.get("response_id") or "")
            if not _RESPONSE_ID_RE.fullmatch(response_id):
                raise RetentionError("plan contains an invalid response candidate")
            if response_id in seen_response_ids:
                raise RetentionError("plan contains duplicate response_id candidates")
            seen_response_ids.add(response_id)
            if not _TRACE_ID_RE.fullmatch(str(item.get("trace_id") or "")):
                raise RetentionError("plan contains an invalid trace candidate")
            if str(item.get("expires_at") or "") > cutoff:
                raise RetentionError("plan contains an unexpired response candidate")
        elif source == "feedback_cases":
            feedback_id = str(item.get("feedback_id") or "")
            if not _FEEDBACK_ID_RE.fullmatch(feedback_id):
                raise RetentionError("plan contains an invalid feedback candidate")
            if feedback_id in seen_feedback_ids:
                raise RetentionError("plan contains duplicate feedback_id candidates")
            seen_feedback_ids.add(feedback_id)
            if str(item.get("case_expires_at") or "") > cutoff:
                raise RetentionError("plan contains an unexpired feedback candidate")
            media_dir = str(item.get("media_dir") or "")
            expected_dir = f"feedback_cases/{feedback_id}"
            if media_dir not in {"", expected_dir}:
                raise RetentionError("feedback media directory does not match its row")
            media_files = item.get("media_files")
            if not isinstance(media_files, list):
                raise RetentionError("feedback media snapshot is invalid")
            for media in media_files:
                if (
                    not isinstance(media, dict)
                    or not str(media.get("file") or "").startswith(expected_dir + "/")
                    or type(media.get("bytes")) is not int
                    or int(media["bytes"]) < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256") or ""))
                ):
                    raise RetentionError("feedback media snapshot is invalid")
        elif source == "a3_page_errors":
            event_id = item.get("event_id")
            if type(event_id) is not int or int(event_id) <= 0:
                raise RetentionError("plan contains an invalid page-error candidate")
            if event_id in seen_event_ids:
                raise RetentionError("plan contains duplicate event_id candidates")
            seen_event_ids.add(event_id)
            if str(item.get("created_at") or "") >= cutoff:
                raise RetentionError("plan contains a retained page-error candidate")


def _preflight_current_candidates(plan: Mapping[str, object], runtime: Path) -> None:
    actions = _actions_by_source(plan)
    _preflight_responses(actions["responses"], runtime)
    _preflight_feedback(actions["feedback_cases"], runtime)
    _preflight_page_errors(actions["a3_page_errors"], runtime)


def _preflight_responses(action: Mapping[str, object], runtime: Path) -> None:
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return
    path = _source_path(runtime, str(action["file"]))
    if not path.is_file():
        raise RetentionError("planned response database is missing")
    with readonly_connection(path) as connection:
        for item in candidates:
            row = connection.execute(
                "SELECT trace_id, expires_at FROM public_responses WHERE response_id = ?",
                (item["response_id"],),
            ).fetchone()
            if row is None or str(row["trace_id"]) != item["trace_id"] or str(
                row["expires_at"]
            ) != item["expires_at"]:
                raise RetentionError("response plan drift detected")


def _preflight_feedback(action: Mapping[str, object], runtime: Path) -> None:
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return
    path = _source_path(runtime, str(action["file"]))
    if not path.is_file():
        raise RetentionError("planned feedback database is missing")
    with readonly_connection(path) as connection:
        for item in candidates:
            row = connection.execute(
                "SELECT case_expires_at, case_purged_at, updated_at FROM message_feedback "
                "WHERE feedback_id = ?",
                (item["feedback_id"],),
            ).fetchone()
            if (
                row is None
                or str(row["case_expires_at"]) != item["case_expires_at"]
                or str(row["case_purged_at"] or "")
                or str(row["updated_at"]) != item["updated_at"]
            ):
                raise RetentionError("feedback plan drift detected")
            _verify_media_snapshot(runtime, item)


def _preflight_page_errors(action: Mapping[str, object], runtime: Path) -> None:
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return
    path = _source_path(runtime, str(action["file"]))
    if not path.is_file():
        raise RetentionError("planned page-error database is missing")
    with readonly_connection(path) as connection:
        for item in candidates:
            row = connection.execute(
                "SELECT created_at FROM a3_page_errors WHERE event_id = ?",
                (item["event_id"],),
            ).fetchone()
            if row is None or str(row["created_at"]) != item["created_at"]:
                raise RetentionError("page-error plan drift detected")


def _verify_media_snapshot(runtime: Path, item: Mapping[str, object]) -> None:
    relative = str(item.get("media_dir") or "")
    expected = {
        str(value["file"]): (int(value["bytes"]), str(value["sha256"]))
        for value in item.get("media_files", [])  # type: ignore[union-attr]
    }
    if not relative:
        if expected:
            raise RetentionError("feedback media plan is inconsistent")
        return
    directory = _source_path(runtime, relative)
    if not directory.is_dir():
        raise RetentionError("feedback media directory drift detected")
    actual = {
        path.relative_to(runtime).as_posix(): (path.stat().st_size, _sha256_file(path))
        for path in _regular_files(directory)
    }
    if actual != expected:
        raise RetentionError("feedback media snapshot drift detected")


def _backup_size_estimate(plan: Mapping[str, object], runtime: Path) -> int:
    total = 1024 * 1024
    for action in plan["actions"]:  # type: ignore[index]
        if int(action["candidate_count"]) <= 0:
            continue
        path = _source_path(runtime, str(action["file"]))
        if path.is_file():
            total += path.stat().st_size
        if action["source"] == "feedback_cases":
            total += sum(
                int(media["bytes"])
                for item in action["candidates"]
                for media in item["media_files"]
            )
    return total


def _create_verified_backups(
    plan: Mapping[str, object], runtime: Path, target: Path
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for action in plan["actions"]:  # type: ignore[index]
        if int(action["candidate_count"]) <= 0:
            continue
        source = _source_path(runtime, str(action["file"]))
        destination = target / "sqlite" / str(action["file"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        _backup_sqlite(source, destination)
        records.append(_backup_record(destination, target, "sqlite"))

    feedback = _actions_by_source(plan)["feedback_cases"]
    for item in feedback["candidates"]:  # type: ignore[index]
        relative = str(item.get("media_dir") or "")
        if not relative:
            continue
        source_dir = _source_path(runtime, relative)
        destination_dir = target / "media" / relative
        destination_dir.mkdir(parents=True, exist_ok=False)
        for source_file in _regular_files(source_dir):
            relative_file = source_file.relative_to(source_dir)
            destination_file = destination_dir / relative_file
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination_file)
            if _sha256_file(source_file) != _sha256_file(destination_file):
                raise RetentionError("feedback media backup verification failed")
            records.append(_backup_record(destination_file, target, "media"))
    return records


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    try:
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=5)
        ) as source_connection:
            source_connection.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(destination)) as destination_connection:
                with destination_connection:
                    source_connection.backup(destination_connection)
                    result = destination_connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()
                    if result is None or str(result[0]).lower() != "ok":
                        raise RetentionError("SQLite backup integrity check failed")
    except sqlite3.Error as exc:
        raise RetentionError("SQLite backup failed") from exc


def _apply_response_candidates(plan: Mapping[str, object], runtime: Path) -> int:
    action = _actions_by_source(plan)["responses"]
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return 0
    path = _source_path(runtime, str(action["file"]))
    changed = 0
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            for item in candidates:
                cursor = connection.execute(
                    "DELETE FROM public_responses WHERE response_id = ? AND trace_id = ? "
                    "AND expires_at = ? AND expires_at <= ?",
                    (
                        item["response_id"],
                        item["trace_id"],
                        item["expires_at"],
                        action["cutoff"],
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise RetentionError("response changed after its verified backup")
                changed += 1
            connection.commit()
    return changed


def _apply_feedback_candidates(plan: Mapping[str, object], runtime: Path) -> int:
    action = _actions_by_source(plan)["feedback_cases"]
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return 0
    path = _source_path(runtime, str(action["file"]))
    changed = 0
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in candidates:
                    row = connection.execute(
                        "SELECT case_expires_at, case_purged_at, updated_at FROM message_feedback "
                        "WHERE feedback_id = ?",
                        (item["feedback_id"],),
                    ).fetchone()
                    if row is None or str(row["case_purged_at"] or ""):
                        raise RetentionError("feedback changed after its verified backup")
                    if (
                        str(row["case_expires_at"]) != item["case_expires_at"]
                        or str(row["updated_at"]) != item["updated_at"]
                        or str(row["case_expires_at"]) > str(action["cutoff"])
                    ):
                        raise RetentionError("feedback changed after its verified backup")
                    relative = str(item.get("media_dir") or "")
                    if relative:
                        _verify_media_snapshot(runtime, item)
                        _remove_media_directory(runtime, relative)
                    cursor = connection.execute(
                        "UPDATE message_feedback SET conversation_json = '[]', "
                        "case_purged_at = ?, updated_at = ? WHERE feedback_id = ? "
                        "AND case_expires_at = ? AND case_purged_at = '' AND updated_at = ?",
                        (
                            plan["as_of"],
                            plan["as_of"],
                            item["feedback_id"],
                            item["case_expires_at"],
                            item["updated_at"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RetentionError(
                            "feedback update lost its verified ownership"
                        )
                    changed += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    return changed


def _apply_page_error_candidates(plan: Mapping[str, object], runtime: Path) -> int:
    action = _actions_by_source(plan)["a3_page_errors"]
    candidates = list(action["candidates"])  # type: ignore[arg-type]
    if not candidates:
        return 0
    path = _source_path(runtime, str(action["file"]))
    changed = 0
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN IMMEDIATE")
            for item in candidates:
                cursor = connection.execute(
                    "DELETE FROM a3_page_errors WHERE event_id = ? AND created_at = ? "
                    "AND created_at < ?",
                    (item["event_id"], item["created_at"], action["cutoff"]),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise RetentionError("page error changed after its verified backup")
                changed += 1
            connection.commit()
    return changed


def _remove_media_directory(runtime: Path, relative: str) -> None:
    target = _source_path(runtime, relative)
    if target.parent != _source_path(runtime, "feedback_cases", allow_missing=True):
        raise RetentionError("feedback media target escaped its case root")
    if target.is_dir():
        list(_regular_files(target))
        shutil.rmtree(target)


def _actions_by_source(plan: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(item["source"]): item
        for item in plan["actions"]  # type: ignore[index]
    }


def _source_path(runtime: Path, relative: str, *, allow_missing: bool = True) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or not relative or ".." in raw.parts:
        raise RetentionError("runtime-relative path is invalid")
    candidate = runtime.joinpath(*raw.parts)
    if _is_reparse_or_symlink(candidate):
        raise RetentionError("runtime source cannot be a link or reparse point")
    resolved = candidate.resolve(strict=False)
    if not _is_relative_to(resolved, runtime) or resolved == runtime:
        raise RetentionError("runtime source escaped its root")
    if not allow_missing and not resolved.exists():
        raise RetentionError("required runtime source is missing")
    return resolved


def _regular_files(root: Path) -> list[Path]:
    if _is_reparse_or_symlink(root):
        raise RetentionError("media root cannot be a link or reparse point")
    result: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink() or _is_reparse_or_symlink(path):
                    raise RetentionError("media tree contains a link or reparse point")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    result.append(path)
                else:
                    raise RetentionError("media tree contains a non-regular entry")
    return sorted(result, key=lambda value: value.as_posix())


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _resolved_directory(path: str | Path, name: str) -> Path:
    raw = Path(path)
    if _is_reparse_or_symlink(raw):
        raise RetentionError(f"{name} cannot be a link or reparse point")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise RetentionError(f"{name} does not exist") from exc
    if not resolved.is_dir():
        raise RetentionError(f"{name} must be a directory")
    return resolved


def _validated_runtime_name(value: str) -> str:
    clean = str(value or "").strip()
    if clean not in _RUNTIME_LAYOUTS:
        raise RetentionError("runtime must be exactly 8790 or 8896")
    return clean


def _current_timestamp(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _aware_timestamp(now, "now")


def _require_applyable_as_of(
    plan: Mapping[str, object], *, now: datetime | str | None = None
) -> datetime:
    if plan.get("mode") != "plan":
        raise RetentionError("retention report is not an applyable plan")
    timestamp = _aware_timestamp(str(plan.get("as_of") or ""), "as_of")
    if timestamp > _current_timestamp(now):
        raise RetentionError("future as_of is report-only and cannot be applied")
    return timestamp


def _aware_timestamp(value: datetime | str, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise RetentionError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise RetentionError(f"{name} must include timezone")
    return parsed.astimezone(UTC)


def _line_count(path: Path) -> int:
    count = 0
    last = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    if path.stat().st_size and last != b"\n":
        count += 1
    return count


def _row_estimate(value: Mapping[str, object]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _backup_record(path: Path, target: Path, kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "file": path.relative_to(target).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise RetentionError("backup root has no existing parent")
        current = parent
    if not current.is_dir():
        raise RetentionError("backup root ancestry is not a directory")
    return current


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionError("maintenance result is unreadable") from exc
    if not isinstance(value, dict):
        raise RetentionError("maintenance result is invalid")
    return value


__all__ = [
    "A3_PAGE_ERROR_RETENTION_DAYS",
    "RETENTION_PLAN_SCHEMA_VERSION",
    "RetentionError",
    "apply_retention_plan",
    "build_retention_plan",
    "format_retention_plan",
    "load_retention_plan",
    "retention_plan_report",
    "retention_plan_hash",
    "write_retention_plan",
]

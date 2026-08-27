"""Read-only secondary evidence collectors for diagnostic packages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .sqlite_reader import readonly_connection, table_columns


MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 64 * 1024
LEGACY_WINDOW_MINUTES = 5

_COST_RUN_COLUMNS = (
    "run_id",
    "trace_id",
    "session_key",
    "identity_key",
    "search_key",
    "task_kind",
    "started_at",
    "finished_at",
    "outcome",
    "call_count",
    "total_tokens",
    "estimated_cost_micros",
    "warning_codes_json",
    "schema_version",
)
_COST_CALL_COLUMNS = (
    "call_id",
    "run_id",
    "sequence",
    "provider",
    "model",
    "call_type",
    "status",
    "started_at",
    "finished_at",
    "latency_ms",
    "input_tokens",
    "image_tokens",
    "cached_tokens",
    "output_tokens",
    "total_tokens",
    "attempt_count",
    "trace_id",
    "error_kind",
    "price_version",
    "pricing_status",
    "estimated_cost_micros",
    "schema_version",
)
_TASK_COLUMNS = (
    "task_id",
    "session_key",
    "kind",
    "started_at",
    "finished_at",
    "duration_ms",
    "phase_before",
    "phase_after",
    "outcome",
    "question_count",
    "candidate_count",
    "chapter",
    "route",
    "error_kind",
    "request_id",
    "search_id",
    "identity_key",
    "status",
    "layer",
    "code",
    "retryable",
    "action",
    "schema_version",
    "trace_id",
)
_PAGE_ERROR_COLUMNS = (
    "event_id",
    "session_key",
    "search_id",
    "task_kind",
    "phase",
    "error_type",
    "error_code",
    "created_at",
)

_PUBLIC_RECORD_FIELDS: Mapping[str, tuple[str, ...]] = {
    "model_cost_runs": tuple(
        field
        for field in _COST_RUN_COLUMNS
        if field not in {"session_key", "warning_codes_json"}
    )
    + ("warning_codes",),
    "model_cost_calls": _COST_CALL_COLUMNS,
    "task_logs": tuple(
        field
        for field in _TASK_COLUMNS
        if field not in {"task_id", "session_key", "request_id"}
    ),
    "a3_page_errors": tuple(
        field for field in _PAGE_ERROR_COLUMNS if field != "session_key"
    ),
}


@dataclass(frozen=True)
class JoinContext:
    association_mode: str
    trace_ids: frozenset[str]
    identity_key: str
    search_keys: frozenset[str]
    since: str
    until: str
    anchor_timestamps: tuple[str, ...]

    @property
    def has_authoritative_anchor(self) -> bool:
        return bool(self.trace_ids)

    @property
    def legacy_window(self) -> tuple[str, str] | None:
        if self.since and self.until:
            return self.since, self.until
        parsed = [_timestamp(value) for value in self.anchor_timestamps]
        values = [value for value in parsed if value is not None]
        if not values:
            return None
        delta = timedelta(minutes=LEGACY_WINDOW_MINUTES)
        return (min(values) - delta).isoformat(), (max(values) + delta).isoformat()


@dataclass(frozen=True)
class SecondaryEvidence:
    items: tuple[dict[str, object], ...]
    sources: tuple[dict[str, object], ...]
    gaps: tuple[str, ...]
    counts: Mapping[str, int]


class SecondaryEvidenceCollector:
    """Collect cost, task and page-error evidence without touching source state."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve()

    def collect(self, context: JoinContext, *, limit: int) -> SecondaryEvidence:
        sources: list[dict[str, object]] = []
        gaps: list[str] = []

        runs, calls, cost_sources, cost_gaps = self._costs(context, limit)
        sources.extend(cost_sources)
        gaps.extend(cost_gaps)
        tasks, task_sources, task_gaps = self._tasks(context, limit)
        sources.extend(task_sources)
        gaps.extend(task_gaps)
        page_errors, page_sources, page_gaps = self._page_errors(context, limit)
        sources.extend(page_sources)
        gaps.extend(page_gaps)

        items = [
            *[_item("model_cost_runs", row) for row in runs],
            *[_item("model_cost_calls", row) for row in calls],
            *[_item("task_logs", row) for row in tasks],
            *[_item("a3_page_errors", row) for row in page_errors],
        ]
        items.sort(key=lambda item: (str(item["timestamp"]), str(item["source"])))
        return SecondaryEvidence(
            items=tuple(items),
            sources=tuple(sources),
            gaps=tuple(dict.fromkeys(gaps)),
            counts={
                "model_run_count": len(runs),
                "model_call_count": len(calls),
                "task_count": len(tasks),
                "page_error_count": len(page_errors),
            },
        )

    def _costs(
        self, context: JoinContext, limit: int
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
        list[str],
    ]:
        gaps: list[str] = []
        all_runs: list[dict[str, object]] = []
        all_calls: list[dict[str, object]] = []
        paths = (
            ("model_costs", Path("model_costs.sqlite3"), False),
            ("model_costs_legacy_a2", Path("a2/model_costs.sqlite3"), True),
        )
        source_states: dict[str, dict[str, object]] = {}

        def collect(source: str, relative: Path, association: str) -> list[dict[str, object]]:
            runs, calls, state, read_gaps = self._read_cost_source(
                context,
                limit,
                source=source,
                relative=relative,
                association=association,
            )
            source_states[source] = state
            gaps.extend(read_gaps)
            all_runs.extend(runs)
            all_calls.extend(calls)
            return runs

        def skip(source: str, relative: Path) -> None:
            path = self.runtime_root / relative
            status = "missing" if not path.is_file() else "not_selected"
            source_states[source] = _source(source, relative, status, 0)

        root_source, root_relative, _ = paths[0]
        legacy_source, legacy_relative, _ = paths[1]
        if context.association_mode == "authoritative-only":
            if context.has_authoritative_anchor:
                collect(root_source, root_relative, "trace_exact")
            else:
                skip(root_source, root_relative)
            skip(legacy_source, legacy_relative)
        elif context.association_mode == "legacy-only":
            collect(root_source, root_relative, "legacy_compatibility")
            collect(legacy_source, legacy_relative, "legacy_compatibility")
        elif context.has_authoritative_anchor:
            exact = collect(root_source, root_relative, "trace_exact")
            skip(legacy_source, legacy_relative)
            if not exact:
                root_legacy, _root_calls, root_state, root_gaps = (
                    self._read_cost_source(
                        context,
                        limit,
                        source=root_source,
                        relative=root_relative,
                        association="legacy_compatibility",
                    )
                )
                legacy_runs, _legacy_calls, legacy_state, legacy_gaps = (
                    self._read_cost_source(
                        context,
                        limit,
                        source=legacy_source,
                        relative=legacy_relative,
                        association="legacy_compatibility",
                    )
                )
                gaps.extend(root_gaps)
                gaps.extend(legacy_gaps)
                if root_gaps:
                    source_states[root_source] = root_state
                if legacy_gaps:
                    source_states[legacy_source] = legacy_state
                if root_legacy or legacy_runs:
                    gaps.append("model_costs:authoritative_evidence_missing")
        else:
            collect(root_source, root_relative, "legacy_compatibility")
            collect(legacy_source, legacy_relative, "legacy_compatibility")

        # The root ledger is authoritative when duplicate run IDs exist.
        deduped_runs: dict[str, dict[str, object]] = {}
        for row in reversed(all_runs):
            deduped_runs[str(row["run_id"])] = row
        run_ids = set(deduped_runs)
        deduped_calls: dict[tuple[str, str], dict[str, object]] = {}
        for row in reversed(all_calls):
            key = (str(row["run_id"]), str(row["call_id"]))
            if key[0] in run_ids:
                deduped_calls[key] = row
        runs = sorted(
            deduped_runs.values(), key=lambda row: (str(row["started_at"]), str(row["run_id"]))
        )
        calls = sorted(
            deduped_calls.values(),
            key=lambda row: (str(row["started_at"]), str(row["run_id"]), int(row["sequence"])),
        )
        sources = [source_states[source] for source, _relative, _legacy in paths]
        return runs, calls, sources, list(dict.fromkeys(gaps))

    def _read_cost_source(
        self,
        context: JoinContext,
        limit: int,
        *,
        source: str,
        relative: Path,
        association: str,
    ) -> tuple[
        list[dict[str, object]],
        list[dict[str, object]],
        dict[str, object],
        list[str],
    ]:
        path = self.runtime_root / relative
        if not path.is_file():
            return [], [], _source(source, relative, "missing", 0), []
        try:
            with readonly_connection(path) as connection:
                if not set(_COST_RUN_COLUMNS).issubset(
                    table_columns(connection, "model_cost_runs")
                ) or not set(_COST_CALL_COLUMNS).issubset(
                    table_columns(connection, "model_cost_calls")
                ):
                    return (
                        [],
                        [],
                        _source(source, relative, "schema_mismatch", 0),
                        [f"{source}:schema_mismatch"],
                    )
                where, parameters, join_keys_truncated = self._cost_predicate(
                    context, association, limit
                )
                if not where:
                    return [], [], _source(source, relative, "not_selected", 0), []
                rows = connection.execute(
                    f"SELECT {', '.join(_COST_RUN_COLUMNS)} FROM model_cost_runs "
                    f"WHERE {where} ORDER BY started_at ASC, run_id ASC LIMIT ?",
                    (*parameters, limit + 1),
                ).fetchall()
                runs_truncated = len(rows) > limit
                runs = [
                    _project_cost_run(row, source, association)
                    for row in rows[:limit]
                ]
                run_ids = [str(row["run_id"]) for row in runs]
                calls: list[dict[str, object]] = []
                calls_truncated = False
                if run_ids:
                    placeholders = ",".join("?" for _ in run_ids)
                    call_rows = connection.execute(
                        f"SELECT {', '.join(_COST_CALL_COLUMNS)} FROM model_cost_calls "
                        f"WHERE run_id IN ({placeholders}) "
                        "ORDER BY started_at ASC, run_id ASC, sequence ASC LIMIT ?",
                        (*run_ids, limit + 1),
                    ).fetchall()
                    calls_truncated = len(call_rows) > limit
                    calls = [
                        _project_cost_call(row, source, association)
                        for row in call_rows[:limit]
                    ]
        except sqlite3.Error:
            return (
                [],
                [],
                _source(source, relative, "query_failed", 0),
                [f"{source}:query_failed"],
            )
        truncated = runs_truncated or calls_truncated or join_keys_truncated
        return (
            runs,
            calls,
            _source(source, relative, "partial" if truncated else "ok", len(runs)),
            [f"{source}:result_truncated"] if truncated else [],
        )

    @staticmethod
    def _cost_association(
        context: JoinContext, *, force_legacy: bool
    ) -> str | None:
        if context.association_mode == "authoritative-only":
            return (
                "trace_exact"
                if context.has_authoritative_anchor and not force_legacy
                else None
            )
        if context.association_mode == "legacy-only":
            return "legacy_compatibility"
        if force_legacy:
            return (
                "legacy_compatibility"
                if not context.has_authoritative_anchor
                else None
            )
        if context.has_authoritative_anchor:
            return "trace_exact"
        return "legacy_compatibility"

    @staticmethod
    def _cost_predicate(
        context: JoinContext, association: str, limit: int
    ) -> tuple[str, tuple[object, ...], bool]:
        if association == "trace_exact":
            if not context.trace_ids:
                return "", (), False
            all_values = sorted(context.trace_ids)
            values = all_values[:limit]
            return (
                "trace_id IN (" + ",".join("?" for _ in values) + ")",
                tuple(values),
                len(all_values) > limit,
            )
        window = context.legacy_window
        if window is None:
            return "", (), False
        clauses: list[str] = ["started_at >= ?", "started_at < ?"]
        parameters: list[object] = [window[0], window[1]]
        identity = str(context.identity_key or "")
        if identity:
            clauses.append("identity_key = ?")
            parameters.append(identity)
        if context.search_keys:
            all_values = sorted(context.search_keys)
            values = all_values[:limit]
            clauses.append(
                "search_key IN (" + ",".join("?" for _ in values) + ")"
            )
            parameters.extend(values)
        elif not identity:
            return "", (), False
        return (
            " AND ".join(clauses),
            tuple(parameters),
            bool(context.search_keys and len(context.search_keys) > limit),
        )

    def _tasks(
        self, context: JoinContext, limit: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        gaps: list[str] = []
        result: list[dict[str, object]] = []
        paths = (
            ("task_logs", Path("a2/task_logs.jsonl"), False),
            ("task_logs_legacy_root", Path("task_logs.jsonl"), True),
        )
        source_states: dict[str, dict[str, object]] = {}

        def collect(source: str, relative: Path, association: str) -> list[dict[str, object]]:
            tasks, state, read_gaps = self._read_task_source(
                context,
                limit,
                source=source,
                relative=relative,
                association=association,
            )
            source_states[source] = state
            gaps.extend(read_gaps)
            result.extend(tasks)
            return tasks

        def skip(source: str, relative: Path) -> None:
            path = self.runtime_root / relative
            status = "missing" if not path.is_file() else "not_selected"
            source_states[source] = _source(source, relative, status, 0)

        active_source, active_relative, _ = paths[0]
        legacy_source, legacy_relative, _ = paths[1]
        if context.association_mode == "authoritative-only":
            if context.has_authoritative_anchor:
                collect(active_source, active_relative, "trace_exact")
            else:
                skip(active_source, active_relative)
            skip(legacy_source, legacy_relative)
        elif context.association_mode == "legacy-only":
            collect(active_source, active_relative, "legacy_compatibility")
            collect(legacy_source, legacy_relative, "legacy_compatibility")
        elif context.has_authoritative_anchor:
            exact = collect(active_source, active_relative, "trace_exact")
            skip(legacy_source, legacy_relative)
            if not exact:
                active_legacy, active_state, active_gaps = self._read_task_source(
                    context,
                    limit,
                    source=active_source,
                    relative=active_relative,
                    association="legacy_compatibility",
                )
                legacy_tasks, legacy_state, legacy_gaps = self._read_task_source(
                    context,
                    limit,
                    source=legacy_source,
                    relative=legacy_relative,
                    association="legacy_compatibility",
                )
                gaps.extend(active_gaps)
                gaps.extend(legacy_gaps)
                if active_gaps:
                    source_states[active_source] = active_state
                if legacy_gaps:
                    source_states[legacy_source] = legacy_state
                if active_legacy or legacy_tasks:
                    gaps.append("task_logs:authoritative_evidence_missing")
        else:
            collect(active_source, active_relative, "legacy_compatibility")
            collect(legacy_source, legacy_relative, "legacy_compatibility")
        deduped = {
            (str(row["task_id"]), str(row["source_file"])): row for row in result
        }
        tasks = sorted(
            deduped.values(), key=lambda row: (str(row["started_at"]), str(row["task_id"]))
        )
        sources = [source_states[source] for source, _relative, _legacy in paths]
        return tasks, sources, list(dict.fromkeys(gaps))

    def _read_task_source(
        self,
        context: JoinContext,
        limit: int,
        *,
        source: str,
        relative: Path,
        association: str,
    ) -> tuple[list[dict[str, object]], dict[str, object], list[str]]:
        path = self.runtime_root / relative
        if not path.is_file():
            return [], _source(source, relative, "missing", 0), []
        matched: list[dict[str, object]] = []
        malformed = False
        scan_boundary_exceeded = False
        source_changed = False
        try:
            with path.open("rb") as handle:
                opened_size = os.fstat(handle.fileno()).st_size
                if opened_size > MAX_JSONL_BYTES:
                    return (
                        [],
                        _source(source, relative, "limit_exceeded", 0),
                        [f"{source}:scan_limit_exceeded"],
                    )
                remaining = opened_size
                scanned = 0
                while remaining > 0:
                    raw_line = handle.readline(
                        min(MAX_JSONL_LINE_BYTES + 1, remaining)
                    )
                    if not raw_line:
                        source_changed = True
                        break
                    scanned += len(raw_line)
                    remaining -= len(raw_line)
                    if len(raw_line) > MAX_JSONL_LINE_BYTES:
                        malformed = True
                        while remaining > 0 and not raw_line.endswith(b"\n"):
                            raw_line = handle.readline(
                                min(MAX_JSONL_LINE_BYTES + 1, remaining)
                            )
                            if not raw_line:
                                source_changed = True
                                break
                            scanned += len(raw_line)
                            remaining -= len(raw_line)
                        continue
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                        malformed = True
                        continue
                    if not isinstance(value, dict) or not _task_matches(
                        value, context, association
                    ):
                        continue
                    matched.append(_project_task(value, source, association))
                    if len(matched) > limit:
                        break
                current_size = os.fstat(handle.fileno()).st_size
                scan_boundary_exceeded = current_size > opened_size
                if len(matched) <= limit and scanned < opened_size:
                    source_changed = True
        except OSError:
            return (
                [],
                _source(source, relative, "query_failed", 0),
                [f"{source}:read_failed"],
            )
        truncated = len(matched) > limit
        matched = matched[:limit]
        status = (
            "partial"
            if malformed or truncated or scan_boundary_exceeded or source_changed
            else "ok"
        )
        gaps = [f"{source}:malformed_or_oversized_line"] if malformed else []
        if truncated:
            gaps.append(f"{source}:result_truncated")
        if scan_boundary_exceeded:
            gaps.append(f"{source}:scan_limit_exceeded")
        if source_changed:
            gaps.append(f"{source}:source_changed_during_scan")
        return matched, _source(source, relative, status, len(matched)), gaps

    @staticmethod
    def _task_association(
        context: JoinContext, *, force_legacy: bool
    ) -> str | None:
        if context.association_mode == "authoritative-only":
            return (
                "trace_exact"
                if context.has_authoritative_anchor and not force_legacy
                else None
            )
        if context.association_mode == "legacy-only":
            return "legacy_compatibility"
        if force_legacy:
            return "legacy_compatibility" if not context.has_authoritative_anchor else None
        return "trace_exact" if context.has_authoritative_anchor else "legacy_compatibility"

    def _page_errors(
        self, context: JoinContext, limit: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        relative = Path("a3_sessions.sqlite3")
        source = "a3_page_errors"
        path = self.runtime_root / relative
        if context.association_mode == "authoritative-only":
            status = "missing" if not path.is_file() else "not_selected"
            return [], [_source(source, relative, status, 0)], []
        if not context.search_keys or context.legacy_window is None:
            status = "missing" if not path.is_file() else "not_selected"
            return [], [_source(source, relative, status, 0)], []
        if not path.is_file():
            return [], [_source(source, relative, "missing", 0)], []
        all_values = sorted(context.search_keys)
        values = all_values[:limit]
        window = context.legacy_window
        try:
            with readonly_connection(path) as connection:
                if not set(_PAGE_ERROR_COLUMNS).issubset(
                    table_columns(connection, "a3_page_errors")
                ):
                    return (
                        [],
                        [_source(source, relative, "schema_mismatch", 0)],
                        [f"{source}:schema_mismatch"],
                    )
                rows = connection.execute(
                    f"SELECT {', '.join(_PAGE_ERROR_COLUMNS)} FROM a3_page_errors "
                    "WHERE search_id IN ("
                    + ",".join("?" for _ in values)
                    + ") AND created_at >= ? AND created_at < ? "
                    "ORDER BY created_at ASC, event_id ASC LIMIT ?",
                    (*values, window[0], window[1], limit + 1),
                ).fetchall()
        except sqlite3.Error:
            return [], [_source(source, relative, "query_failed", 0)], [f"{source}:query_failed"]
        truncated = len(rows) > limit or len(all_values) > limit
        projected = [_project_page_error(row, source) for row in rows[:limit]]
        return (
            projected,
            [_source(source, relative, "partial" if truncated else "ok", len(projected))],
            [f"{source}:result_truncated"] if truncated else [],
        )


def _task_matches(
    value: Mapping[str, object], context: JoinContext, association: str
) -> bool:
    if association == "trace_exact":
        return str(value.get("trace_id") or "") in context.trace_ids
    window = context.legacy_window
    if window is None:
        return False
    timestamp = str(value.get("started_at") or "")
    if not (window[0] <= timestamp < window[1]):
        return False
    identity = str(value.get("identity_key") or "")
    if context.identity_key and identity != context.identity_key:
        return False
    search_id = str(value.get("search_id") or "")
    if context.search_keys and search_id not in context.search_keys:
        return False
    return bool(context.identity_key or context.search_keys)


def _project_cost_run(
    row: sqlite3.Row, source: str, association: str
) -> dict[str, object]:
    result = {column: row[column] for column in _COST_RUN_COLUMNS if column != "warning_codes_json"}
    try:
        warnings = json.loads(str(row["warning_codes_json"] or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        warnings = []
    result["warning_codes"] = (
        [str(value) for value in warnings[:16]] if isinstance(warnings, list) else []
    )
    result.update(_metadata(source, association))
    return result


def _project_cost_call(
    row: sqlite3.Row, source: str, association: str
) -> dict[str, object]:
    result = {column: row[column] for column in _COST_CALL_COLUMNS}
    result.update(_metadata(source, association))
    return result


def _project_task(
    value: Mapping[str, object], source: str, association: str
) -> dict[str, object]:
    result = {column: value.get(column, "") for column in _TASK_COLUMNS}
    result["retryable"] = bool(value.get("retryable", False))
    result.update(_metadata(source, association))
    return result


def _project_page_error(row: sqlite3.Row, source: str) -> dict[str, object]:
    result = {column: row[column] for column in _PAGE_ERROR_COLUMNS}
    result.update(_metadata(source, "legacy_compatibility"))
    return result


def _metadata(source: str, association: str) -> dict[str, object]:
    return {
        "source_file": source,
        "association": association,
        "completeness": (
            "partial" if association == "legacy_compatibility" else "complete"
        ),
    }


def _item(source: str, row: Mapping[str, object]) -> dict[str, object]:
    allowed_fields = _PUBLIC_RECORD_FIELDS[source]
    record = {
        key: row[key]
        for key in allowed_fields
        if key in row
    }
    return {
        "source": source,
        "association": str(row["association"]),
        "completeness": str(row["completeness"]),
        "timestamp": str(
            row.get("started_at") or row.get("created_at") or row.get("finished_at") or ""
        ),
        "record": record,
    }


def _source(
    name: str, relative: Path, status: str, record_count: int
) -> dict[str, object]:
    return {
        "name": name,
        "file": relative.as_posix(),
        "status": status,
        "record_count": record_count,
    }


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)

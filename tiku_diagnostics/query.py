"""Strictly read-only diagnostic queries over one Agent runtime root.

The query layer deliberately does not instantiate the product stores. Their
read methods run schema creation/migration helpers, which would violate this
module's read-only contract. Every SQLite database is opened with ``mode=ro``
and ``query_only`` and every returned field is selected from an explicit
privacy allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .evidence import JoinContext, SecondaryEvidenceCollector
from .sqlite_reader import readonly_connection, table_columns


DIAGNOSTIC_SCHEMA_VERSION = 1
MAX_DIAGNOSTIC_LIMIT = 100
MAX_DIAGNOSTIC_ASSOCIATION_ROWS = 256
MAX_IDENTITY_WINDOW_DAYS = 7
MAX_DIAGNOSTIC_OUTPUT_BYTES = 2 * 1024 * 1024
ASSOCIATION_MODES = frozenset(
    {"authoritative-only", "authoritative-first", "legacy-only"}
)

_TRACE_ID_RE = re.compile(r"^trace_[0-9a-f]{32}$")
_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
_FEEDBACK_ID_RE = re.compile(r"^(?:[0-9a-f]{32}|FB-[0-9]{8}-[A-F0-9]{10})$")
_IDENTITY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
_TRUSTED_CLIENT_TIMESTAMP_LAG = timedelta(minutes=30)

_TRACE_COLUMNS = (
    "event_id",
    "trace_id",
    "event_type",
    "occurred_at",
    "stage",
    "outcome",
    "request_id",
    "response_id",
    "session_key",
    "identity_key",
    "workflow_search_id",
    "search_id",
    "unit_id",
    "run_id",
    "call_id",
    "feedback_id",
    "rated_response_id",
    "protocol_status",
    "protocol_layer",
    "protocol_code",
    "protocol_retryable",
    "protocol_action",
    "duration_ms",
    "safe_attributes_json",
)
_RESPONSE_COLUMNS = (
    "response_id",
    "created_at",
    "expires_at",
    "trace_id",
    "identity_key",
    "session_key",
    "request_id",
    "workflow_search_id",
    "search_id",
    "unit_id",
    "status",
    "layer",
    "code",
    "retryable",
    "action",
    "phase",
    "task_revision",
    "candidate_count",
    "chapter",
    "image_route",
    "intent",
    "response_mode",
    "media_status",
    "image_count",
    "text_length",
    "duration_ms",
)
_FEEDBACK_COLUMNS = (
    "feedback_id",
    "feedback_number",
    "message_id",
    "rated_response_id",
    "identity_key",
    "session_key",
    "rating",
    "tags_json",
    "task_revision",
    "phase",
    "candidate_count",
    "search_duration_ms",
    "search_key",
    "request_id",
    "search_id",
    "status",
    "layer",
    "code",
    "chapter",
    "image_route",
    "workflow_search_id",
    "intent",
    "feedback_scope",
    "review_status",
    "archived_at",
    "case_expires_at",
    "case_purged_at",
    "created_at",
    "updated_at",
    "schema_version",
    "conversation_json",
)

_PUBLIC_RECORD_FIELDS: Mapping[str, tuple[str, ...]] = {
    "trace_events": (
        "event_id",
        "trace_id",
        "event_type",
        "occurred_at",
        "stage",
        "outcome",
        "response_id",
        "identity_key",
        "workflow_search_id",
        "search_id",
        "unit_id",
        "run_id",
        "call_id",
        "feedback_id",
        "rated_response_id",
        "protocol_status",
        "protocol_layer",
        "protocol_code",
        "protocol_retryable",
        "protocol_action",
        "duration_ms",
    ),
    "responses": tuple(
        field for field in _RESPONSE_COLUMNS if field not in {"session_key", "request_id"}
    ),
    "feedback": (
        "feedback_id",
        "feedback_number",
        "rated_response_id",
        "identity_key",
        "rating",
        "tags",
        "task_revision",
        "phase",
        "candidate_count",
        "search_duration_ms",
        "search_key",
        "search_id",
        "status",
        "layer",
        "code",
        "chapter",
        "image_route",
        "workflow_search_id",
        "intent",
        "feedback_scope",
        "review_status",
        "archived_at",
        "case_expires_at",
        "case_purged_at",
        "created_at",
        "updated_at",
        "schema_version",
        "legacy_binding",
    ),
}


class DiagnosticQueryError(ValueError):
    """A query is unsafe, unbounded, or cannot be represented by the contract."""


@dataclass(frozen=True)
class QuerySpec:
    """One bounded selector over a single runtime root."""

    trace_id: str = ""
    response_id: str = ""
    feedback_id: str = ""
    identity_key: str = ""
    since: str = ""
    until: str = ""
    limit: int = 100
    association_mode: str = "authoritative-first"

    def __post_init__(self) -> None:
        selectors = {
            "trace_id": str(self.trace_id or "").strip(),
            "response_id": str(self.response_id or "").strip(),
            "feedback_id": str(self.feedback_id or "").strip(),
            "identity_key": str(self.identity_key or "").strip(),
        }
        selected = [(name, value) for name, value in selectors.items() if value]
        if len(selected) != 1:
            raise DiagnosticQueryError("exactly one diagnostic selector is required")
        name, value = selected[0]
        validators = {
            "trace_id": _TRACE_ID_RE,
            "response_id": _RESPONSE_ID_RE,
            "feedback_id": _FEEDBACK_ID_RE,
            "identity_key": _IDENTITY_KEY_RE,
        }
        if not validators[name].fullmatch(value):
            raise DiagnosticQueryError(f"invalid {name}")
        if name == "identity_key" and value.upper().startswith("TIKU-"):
            raise DiagnosticQueryError("invitation codes are not accepted")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_DIAGNOSTIC_LIMIT:
            raise DiagnosticQueryError(
                f"limit must be between 1 and {MAX_DIAGNOSTIC_LIMIT}"
            )
        if self.association_mode not in ASSOCIATION_MODES:
            raise DiagnosticQueryError("invalid association mode")
        since = _optional_timestamp(self.since, "since")
        until = _optional_timestamp(self.until, "until")
        if bool(since) != bool(until):
            raise DiagnosticQueryError("since and until must be supplied together")
        if name == "identity_key" and not since:
            raise DiagnosticQueryError("identity queries require since and until")
        if since and until:
            if until <= since:
                raise DiagnosticQueryError("until must follow since")
            if until - since > timedelta(days=MAX_IDENTITY_WINDOW_DAYS):
                raise DiagnosticQueryError(
                    f"query window cannot exceed {MAX_IDENTITY_WINDOW_DAYS} days"
                )

    @property
    def selector(self) -> tuple[str, str]:
        for name in ("trace_id", "response_id", "feedback_id", "identity_key"):
            value = str(getattr(self, name) or "").strip()
            if value:
                return name, value
        raise DiagnosticQueryError("diagnostic selector is missing")

    @property
    def normalized_since(self) -> str:
        value = _optional_timestamp(self.since, "since")
        return value.isoformat() if value else ""

    @property
    def normalized_until(self) -> str:
        value = _optional_timestamp(self.until, "until")
        return value.isoformat() if value else ""


@dataclass(frozen=True)
class _SourceState:
    name: str
    file: str
    status: str
    record_count: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "file": self.file,
            "status": self.status,
            "record_count": self.record_count,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


def _mark_source_partial(
    states: dict[str, _SourceState],
    source: str,
    filename: str,
    *,
    reason: str,
) -> None:
    previous = states.get(source)
    states[source] = _SourceState(
        source,
        filename,
        "partial",
        record_count=previous.record_count if previous else 0,
        reason=reason,
    )


class DiagnosticQueryService:
    """Build a bounded ``summary -> timeline -> evidence`` diagnostic package."""

    def __init__(self, runtime_root: str | Path) -> None:
        root = Path(runtime_root)
        if not root.is_dir():
            raise DiagnosticQueryError("runtime root is not a directory")
        self.runtime_root = root.resolve()

    def query(self, spec: QuerySpec) -> dict[str, object]:
        if not isinstance(spec, QuerySpec):
            raise TypeError("spec must be a QuerySpec")
        source_states: dict[str, _SourceState] = {}

        feedback_rows = self._initial_feedback_rows(spec, source_states)
        response_rows = self._initial_response_rows(spec, source_states)
        trace_rows = self._initial_trace_rows(spec, source_states)

        response_ids = (
            {
                str(row.get("rated_response_id") or "")
                for row in feedback_rows
                if str(row.get("rated_response_id") or "")
            }
            if spec.association_mode != "legacy-only"
            else set()
        )
        response_ids.update(
            str(row.get("response_id") or "")
            for row in trace_rows
            if str(row.get("response_id") or "")
        )
        response_rows = _merge_rows(
            response_rows,
            self._responses_by_ids(
                response_ids, MAX_DIAGNOSTIC_ASSOCIATION_ROWS, source_states
            ),
            "response_id",
        )

        trace_ids = {
            str(row.get("trace_id") or "")
            for row in response_rows
            if str(row.get("trace_id") or "")
        }
        trace_rows = _merge_rows(
            trace_rows,
            self._traces_by_ids(
                trace_ids, MAX_DIAGNOSTIC_ASSOCIATION_ROWS, source_states
            ),
            "event_id",
        )
        feedback_ids = {
            str(row.get("feedback_id") or "")
            for row in feedback_rows
            if str(row.get("feedback_id") or "")
        }
        rated_response_ids = {
            str(row.get("rated_response_id") or "")
            for row in feedback_rows
            if str(row.get("rated_response_id") or "")
        }
        if spec.association_mode != "legacy-only":
            trace_rows = _merge_rows(
                trace_rows,
                self._traces_by_feedback(
                    feedback_ids,
                    rated_response_ids,
                    MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
                    source_states,
                ),
                "event_id",
            )

        response_ids.update(
            str(row.get("response_id") or "")
            for row in response_rows
            if str(row.get("response_id") or "")
        )
        if spec.association_mode != "legacy-only":
            feedback_rows = _merge_rows(
                feedback_rows,
                self._feedback_by_response_ids(
                    response_ids, MAX_DIAGNOSTIC_ASSOCIATION_ROWS, source_states
                ),
                "feedback_id",
            )

        terminal_trace_ids = {
            str(row.get("trace_id") or "")
            for row in trace_rows
            if str(row.get("trace_id") or "")
        }
        trace_rows = _merge_rows(
            trace_rows,
            self._terminal_traces_by_ids(
                terminal_trace_ids,
                MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
                source_states,
            ),
            "event_id",
        )

        join_context = _join_context(
            spec, trace_rows, response_rows, feedback_rows
        )
        if spec.association_mode == "legacy-only" and spec.selector[0] in {
            "trace_id",
            "response_id",
        }:
            feedback_rows = _merge_rows(
                feedback_rows,
                self._feedback_by_legacy_context(
                    join_context, MAX_DIAGNOSTIC_ASSOCIATION_ROWS, source_states
                ),
                "feedback_id",
            )

        trace_rows = _sort_rows(trace_rows, "occurred_at")
        response_rows = _sort_rows(response_rows, "created_at")
        feedback_rows = _sort_rows(feedback_rows, "created_at")
        primary_evidence = self._build_evidence(
            spec, trace_rows, response_rows, feedback_rows
        )
        secondary = SecondaryEvidenceCollector(self.runtime_root).collect(
            _join_context(spec, trace_rows, response_rows, feedback_rows),
            limit=MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
        )
        all_evidence = [*primary_evidence, *secondary.items]
        evidence = _bounded_evidence(all_evidence, spec.limit)
        output_truncated = len(all_evidence) > len(evidence)
        timeline = sorted(
            [item for item in evidence if item.get("timestamp")],
            key=lambda item: (str(item["timestamp"]), str(item["source"])),
        )[: spec.limit]
        gaps = list(
            dict.fromkeys(
                [
                    *_evidence_gaps(
                        spec,
                        trace_rows,
                        response_rows,
                        feedback_rows,
                        source_states,
                    ),
                    *secondary.gaps,
                    *(
                        ["diagnostic_output:result_truncated"]
                        if output_truncated
                        else []
                    ),
                ]
            )
        )
        selector_name, selector_value = spec.selector
        sources = [
            source_states[name].to_dict()
            for name in ("trace_events", "responses", "feedback")
            if name in source_states
        ]
        sources.extend(dict(source) for source in secondary.sources)
        package = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "query": {
                "selector": selector_name,
                "value": selector_value,
                "since": spec.normalized_since,
                "until": spec.normalized_until,
                "limit": spec.limit,
                "association_mode": spec.association_mode,
            },
            "runtime": {"label": self.runtime_root.name},
            "summary": {
                "trace_count": len({str(row["trace_id"]) for row in trace_rows}),
                "trace_event_count": len(trace_rows),
                "response_count": len(response_rows),
                "feedback_count": len(feedback_rows),
                **dict(secondary.counts),
                "authoritative_link_count": sum(
                    item["association"] != "legacy_compatibility" for item in evidence
                ),
                "legacy_compatibility_count": sum(
                    item["association"] == "legacy_compatibility" for item in evidence
                ),
                "complete": not gaps,
                "evidence_gaps": gaps,
            },
            "timeline": timeline,
            "evidence": evidence,
            "sources": sources,
        }
        encoded_size = len(
            json.dumps(
                package,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if encoded_size > MAX_DIAGNOSTIC_OUTPUT_BYTES:
            raise DiagnosticQueryError("diagnostic package exceeds hard output limit")
        return package

    def _initial_trace_rows(
        self, spec: QuerySpec, states: dict[str, _SourceState]
    ) -> list[dict[str, object]]:
        name, value = spec.selector
        if name == "trace_id":
            return self._query_table(
                source="trace_events",
                filename="trace_events.sqlite3",
                table="trace_events",
                columns=_TRACE_COLUMNS,
                where="trace_id = ?",
                parameters=(value,),
                order="occurred_at ASC, event_id ASC",
                limit=MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
                states=states,
                projector=_project_trace,
            )
        if name == "identity_key":
            return self._query_table(
                source="trace_events",
                filename="trace_events.sqlite3",
                table="trace_events",
                columns=_TRACE_COLUMNS,
                where="identity_key = ? AND occurred_at >= ? AND occurred_at < ?",
                parameters=(value, spec.normalized_since, spec.normalized_until),
                order="occurred_at ASC, event_id ASC",
                limit=MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
                states=states,
                projector=_project_trace,
            )
        return self._source_not_queried(
            "trace_events", "trace_events.sqlite3", states
        )

    def _initial_response_rows(
        self, spec: QuerySpec, states: dict[str, _SourceState]
    ) -> list[dict[str, object]]:
        name, value = spec.selector
        if name == "response_id":
            where = "response_id = ?"
            parameters: tuple[object, ...] = (value,)
        elif name == "trace_id":
            where = "trace_id = ?"
            parameters = (value,)
        elif name == "identity_key":
            where = "identity_key = ? AND created_at >= ? AND created_at < ?"
            parameters = (value, spec.normalized_since, spec.normalized_until)
        else:
            return self._source_not_queried("responses", "responses.sqlite3", states)
        return self._query_table(
            source="responses",
            filename="responses.sqlite3",
            table="public_responses",
            columns=_RESPONSE_COLUMNS,
            where=where,
            parameters=parameters,
            order="created_at ASC, response_id ASC",
            limit=MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
            states=states,
            projector=_project_response,
        )

    def _initial_feedback_rows(
        self, spec: QuerySpec, states: dict[str, _SourceState]
    ) -> list[dict[str, object]]:
        name, value = spec.selector
        if name == "feedback_id":
            where = "feedback_id = ? OR feedback_number = ?"
            parameters: tuple[object, ...] = (value, value.upper())
        elif name == "response_id":
            if spec.association_mode == "legacy-only":
                return self._source_not_queried(
                    "feedback", "feedback.sqlite3", states
                )
            where = "rated_response_id = ?"
            parameters = (value,)
        elif name == "identity_key":
            where = "identity_key = ? AND created_at >= ? AND created_at < ?"
            parameters = (value, spec.normalized_since, spec.normalized_until)
            if spec.association_mode == "authoritative-only":
                where += " AND rated_response_id != ''"
        else:
            return self._source_not_queried("feedback", "feedback.sqlite3", states)
        rows = self._query_table(
            source="feedback",
            filename="feedback.sqlite3",
            table="message_feedback",
            columns=_FEEDBACK_COLUMNS,
            where=where,
            parameters=parameters,
            order="created_at ASC, feedback_id ASC",
            limit=MAX_DIAGNOSTIC_ASSOCIATION_ROWS,
            states=states,
            projector=_project_feedback,
        )
        if name == "identity_key":
            for row in rows:
                if spec.association_mode == "legacy-only" or not str(
                    row.get("rated_response_id") or ""
                ):
                    row["_diagnostic_association"] = "legacy_compatibility"
        return rows

    def _responses_by_ids(
        self,
        values: set[str],
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        clean = sorted(value for value in values if _RESPONSE_ID_RE.fullmatch(value))
        return self._query_in(
            "responses", "responses.sqlite3", "public_responses", _RESPONSE_COLUMNS,
            "response_id", clean, "created_at ASC, response_id ASC", limit, states,
            _project_response,
        )

    def _traces_by_ids(
        self,
        values: set[str],
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        clean = sorted(value for value in values if _TRACE_ID_RE.fullmatch(value))
        return self._query_in(
            "trace_events", "trace_events.sqlite3", "trace_events", _TRACE_COLUMNS,
            "trace_id", clean, "occurred_at ASC, event_id ASC", limit, states,
            _project_trace,
        )

    def _feedback_by_response_ids(
        self,
        values: set[str],
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        clean = sorted(value for value in values if _RESPONSE_ID_RE.fullmatch(value))
        return self._query_in(
            "feedback", "feedback.sqlite3", "message_feedback", _FEEDBACK_COLUMNS,
            "rated_response_id", clean, "created_at ASC, feedback_id ASC", limit, states,
            _project_feedback,
        )

    def _feedback_by_legacy_context(
        self,
        context: JoinContext,
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        all_search_keys = sorted(context.search_keys)
        search_keys = all_search_keys[:limit]
        if not context.identity_key or not search_keys:
            return self._source_not_queried(
                "feedback", "feedback.sqlite3", states
            )
        placeholders = ",".join("?" for _ in search_keys)
        search_clause = (
            f"workflow_search_id IN ({placeholders}) OR "
            f"(feedback_scope != 'page' AND search_key IN ({placeholders}))"
        )
        rows = self._query_table(
            source="feedback",
            filename="feedback.sqlite3",
            table="message_feedback",
            columns=_FEEDBACK_COLUMNS,
            where=(
                "identity_key = ? AND ("
                + search_clause
                + ")"
            ),
            parameters=(
                context.identity_key,
                *search_keys,
                *search_keys,
            ),
            order="created_at ASC, feedback_id ASC",
            limit=limit,
            states=states,
            projector=_project_feedback,
        )
        if len(all_search_keys) > limit:
            _mark_source_partial(
                states,
                "feedback",
                "feedback.sqlite3",
                reason="join_keys_truncated",
            )
        rows = [row for row in rows if _legacy_feedback_matches(row, context)]
        for row in rows:
            row["_diagnostic_association"] = "legacy_compatibility"
        return rows

    def _terminal_traces_by_ids(
        self,
        values: set[str],
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        clean = sorted(value for value in values if _TRACE_ID_RE.fullmatch(value))
        if not clean:
            return self._source_not_queried(
                "trace_events", "trace_events.sqlite3", states
            )
        placeholders = ",".join("?" for _ in clean[:limit])
        rows = self._query_table(
            source="trace_events",
            filename="trace_events.sqlite3",
            table="trace_events",
            columns=_TRACE_COLUMNS,
            where=(
                f"trace_id IN ({placeholders}) AND "
                "event_type IN ('public_response_finalized', 'request_failed')"
            ),
            parameters=tuple(clean[:limit]),
            order="occurred_at ASC, event_id ASC",
            limit=limit,
            states=states,
            projector=_project_trace,
        )
        if len(clean) > limit:
            _mark_source_partial(
                states,
                "trace_events",
                "trace_events.sqlite3",
                reason="join_keys_truncated",
            )
        return rows

    def _traces_by_feedback(
        self,
        feedback_ids: set[str],
        response_ids: set[str],
        limit: int,
        states: dict[str, _SourceState],
    ) -> list[dict[str, object]]:
        clean_feedback = sorted(
            value for value in feedback_ids if re.fullmatch(r"[0-9a-f]{32}", value)
        )
        clean_responses = sorted(
            value for value in response_ids if _RESPONSE_ID_RE.fullmatch(value)
        )
        join_keys_truncated = (
            len(clean_feedback) > limit or len(clean_responses) > limit
        )
        clauses: list[str] = []
        parameters: list[str] = []
        if clean_feedback:
            clauses.append(
                "feedback_id IN (" + ",".join("?" for _ in clean_feedback[:limit]) + ")"
            )
            parameters.extend(clean_feedback[:limit])
        if clean_responses:
            clauses.append(
                "rated_response_id IN ("
                + ",".join("?" for _ in clean_responses[:limit])
                + ")"
            )
            parameters.extend(clean_responses[:limit])
        if not clauses:
            return self._source_not_queried(
                "trace_events", "trace_events.sqlite3", states
            )
        rows = self._query_table(
            source="trace_events",
            filename="trace_events.sqlite3",
            table="trace_events",
            columns=_TRACE_COLUMNS,
            where=" OR ".join(f"({clause})" for clause in clauses),
            parameters=tuple(parameters),
            order="occurred_at ASC, event_id ASC",
            limit=limit,
            states=states,
            projector=_project_trace,
        )
        if join_keys_truncated:
            _mark_source_partial(
                states,
                "trace_events",
                "trace_events.sqlite3",
                reason="join_keys_truncated",
            )
        return rows

    def _query_in(
        self,
        source: str,
        filename: str,
        table: str,
        columns: Sequence[str],
        field: str,
        values: Sequence[str],
        order: str,
        limit: int,
        states: dict[str, _SourceState],
        projector,
    ) -> list[dict[str, object]]:
        if not values:
            return self._source_not_queried(source, filename, states)
        placeholders = ",".join("?" for _ in values[:limit])
        rows = self._query_table(
            source=source,
            filename=filename,
            table=table,
            columns=columns,
            where=f"{field} IN ({placeholders})",
            parameters=tuple(values[:limit]),
            order=order,
            limit=limit,
            states=states,
            projector=projector,
        )
        if len(values) > limit:
            _mark_source_partial(
                states,
                source,
                filename,
                reason="join_keys_truncated",
            )
        return rows

    def _query_table(
        self,
        *,
        source: str,
        filename: str,
        table: str,
        columns: Sequence[str],
        where: str,
        parameters: Sequence[object],
        order: str,
        limit: int,
        states: dict[str, _SourceState],
        projector,
    ) -> list[dict[str, object]]:
        path = self.runtime_root / filename
        if not path.is_file():
            states[source] = _SourceState(source, filename, "missing", reason="file_missing")
            return []
        try:
            with readonly_connection(path) as connection:
                actual = table_columns(connection, table)
                missing = [column for column in columns if column not in actual]
                if missing:
                    states[source] = _SourceState(
                        source,
                        filename,
                        "schema_mismatch",
                        reason="required_columns_missing",
                    )
                    return []
                statement = (
                    f"SELECT {', '.join(columns)} FROM {table} "
                    f"WHERE {where} ORDER BY {order} LIMIT ?"
                )
                rows = connection.execute(
                    statement, (*parameters, limit + 1)
                ).fetchall()
        except sqlite3.Error:
            states[source] = _SourceState(source, filename, "query_failed", reason="sqlite_error")
            return []
        truncated = len(rows) > limit
        projected = [projector(row) for row in rows[:limit]]
        previous = states.get(source)
        record_count = max(len(projected), previous.record_count if previous else 0)
        was_partial = previous is not None and previous.status == "partial"
        states[source] = _SourceState(
            source,
            filename,
            "partial" if truncated or was_partial else "ok",
            record_count=record_count,
            reason=(
                "result_truncated"
                if truncated
                else previous.reason
                if was_partial and previous is not None
                else ""
            ),
        )
        return projected

    @staticmethod
    def _source_not_queried(
        source: str, filename: str, states: dict[str, _SourceState]
    ) -> list[dict[str, object]]:
        states.setdefault(source, _SourceState(source, filename, "not_selected"))
        return []

    def _build_evidence(
        self,
        spec: QuerySpec,
        traces: Sequence[Mapping[str, object]],
        responses: Sequence[Mapping[str, object]],
        feedback: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        selector_name, selector_value = spec.selector
        response_ids = {str(row["response_id"]) for row in responses}
        trace_ids = {str(row["trace_id"]) for row in responses}
        result: list[dict[str, object]] = []
        for row in traces:
            association = (
                "direct_selector"
                if selector_name == "trace_id" and row["trace_id"] == selector_value
                else "authoritative_trace_id"
            )
            result.append(_evidence_item("trace_events", association, row))
        for row in responses:
            if selector_name == "response_id" and row["response_id"] == selector_value:
                association = "direct_selector"
            elif selector_name == "trace_id" and row["trace_id"] == selector_value:
                association = "authoritative_trace_id"
            elif selector_name == "identity_key":
                association = "authoritative_identity_key"
            else:
                association = "authoritative_response_id"
            result.append(_evidence_item("responses", association, row))
        for row in feedback:
            rated = str(row.get("rated_response_id") or "")
            forced_association = str(
                row.get("_diagnostic_association") or ""
            )
            if selector_name == "feedback_id" and selector_value in {
                str(row["feedback_id"]), str(row["feedback_number"])
            }:
                association = "direct_selector"
            elif forced_association == "legacy_compatibility":
                association = forced_association
            elif rated and rated in response_ids:
                association = "authoritative_response_id"
            else:
                association = "legacy_compatibility"
            completeness = (
                "complete"
                if association != "legacy_compatibility"
                and rated
                and rated in response_ids
                else "partial"
            )
            result.append(_evidence_item("feedback", association, row, completeness))
        return sorted(
            result,
            key=lambda item: (str(item.get("timestamp") or ""), str(item["source"])),
        )


def _project_trace(row: sqlite3.Row) -> dict[str, object]:
    result = {column: row[column] for column in _TRACE_COLUMNS if column != "safe_attributes_json"}
    result["protocol_retryable"] = (
        None if row["protocol_retryable"] is None else bool(row["protocol_retryable"])
    )
    return result


def _project_response(row: sqlite3.Row) -> dict[str, object]:
    result = {column: row[column] for column in _RESPONSE_COLUMNS}
    result["retryable"] = bool(row["retryable"])
    return result


def _project_feedback(row: sqlite3.Row) -> dict[str, object]:
    result = {column: row[column] for column in _FEEDBACK_COLUMNS if column != "tags_json"}
    try:
        tags = json.loads(str(row["tags_json"] or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        tags = []
    result["tags"] = [str(value) for value in tags[:8]] if isinstance(tags, list) else []
    result["legacy_binding"] = not bool(str(row["rated_response_id"] or ""))
    return result


def _evidence_item(
    source: str,
    association: str,
    row: Mapping[str, object],
    completeness: str = "complete",
) -> dict[str, object]:
    timestamp = str(
        row.get("occurred_at") or row.get("created_at") or row.get("updated_at") or ""
    )
    allowed_fields = _PUBLIC_RECORD_FIELDS[source]
    return {
        "source": source,
        "association": association,
        "completeness": completeness,
        "timestamp": timestamp,
        "record": {
            key: row[key]
            for key in allowed_fields
            if key in row
        },
    }


def _evidence_gaps(
    spec: QuerySpec,
    traces: Sequence[Mapping[str, object]],
    responses: Sequence[Mapping[str, object]],
    feedback: Sequence[Mapping[str, object]],
    states: Mapping[str, _SourceState],
) -> list[str]:
    gaps: list[str] = []
    for source in ("trace_events", "responses", "feedback"):
        state = states.get(source)
        if state is not None and state.status in {"missing", "schema_mismatch", "query_failed"}:
            gaps.append(f"{source}:{state.status}")
        elif state is not None and state.status == "partial":
            gaps.append(f"{source}:result_truncated")
    selector_name, _selector_value = spec.selector
    if selector_name == "trace_id" and not traces:
        gaps.append("trace_not_found")
    if selector_name == "response_id" and not responses:
        gaps.append("response_not_found")
    if selector_name == "feedback_id" and not feedback:
        gaps.append("feedback_not_found")
    terminals: dict[str, int] = {}
    for row in traces:
        if row.get("event_type") in {"public_response_finalized", "request_failed"}:
            key = str(row.get("trace_id") or "")
            terminals[key] = terminals.get(key, 0) + 1
    trace_source_partial = (
        states.get("trace_events") is not None
        and states["trace_events"].status == "partial"
    )
    for trace_id in {str(row.get("trace_id") or "") for row in traces}:
        if terminals.get(trace_id, 0) == 0:
            if not trace_source_partial:
                gaps.append(f"terminal_missing:{trace_id}")
        elif terminals[trace_id] > 1:
            gaps.append(f"terminal_duplicate:{trace_id}")
    trace_ids = {str(row.get("trace_id") or "") for row in traces}
    for row in responses:
        trace_id = str(row.get("trace_id") or "")
        if trace_id not in trace_ids and not trace_source_partial:
            gaps.append(f"response_trace_missing:{row['response_id']}")
    response_ids = {str(row.get("response_id") or "") for row in responses}
    response_source_partial = (
        states.get("responses") is not None
        and states["responses"].status == "partial"
    )
    for row in feedback:
        rated = str(row.get("rated_response_id") or "")
        if not rated:
            gaps.append(f"legacy_feedback_unbound:{row['feedback_id']}")
        elif rated not in response_ids and not response_source_partial:
            gaps.append(f"feedback_response_missing:{row['feedback_id']}")
    return list(dict.fromkeys(gaps))


def _join_context(
    spec: QuerySpec,
    traces: Sequence[Mapping[str, object]],
    responses: Sequence[Mapping[str, object]],
    feedback: Sequence[Mapping[str, object]],
) -> JoinContext:
    rows = (*traces, *responses, *feedback)
    trace_ids = frozenset(
        str(row.get("trace_id") or "")
        for row in (*traces, *responses)
        if _TRACE_ID_RE.fullmatch(str(row.get("trace_id") or ""))
    )
    identities = {
        str(row.get("identity_key") or "")
        for row in rows
        if str(row.get("identity_key") or "")
    }
    selector_name, selector_value = spec.selector
    if selector_name == "identity_key":
        identities.add(selector_value)
    identity_key = next(iter(identities)) if len(identities) == 1 else ""
    search_keys = frozenset(
        str(row.get(field) or "")
        for row in rows
        for field in ("workflow_search_id", "search_id", "search_key")
        if str(row.get(field) or "")
    )
    anchor_timestamps = tuple(
        str(row.get(field) or "")
        for row in rows
        for field in ("occurred_at", "created_at", "updated_at")
        if str(row.get(field) or "")
    )
    return JoinContext(
        association_mode=spec.association_mode,
        trace_ids=trace_ids,
        identity_key=identity_key,
        search_keys=search_keys,
        since=spec.normalized_since,
        until=spec.normalized_until,
        anchor_timestamps=anchor_timestamps,
    )


def _legacy_feedback_matches(
    row: Mapping[str, object], context: JoinContext
) -> bool:
    if str(row.get("identity_key") or "") != context.identity_key:
        return False
    if not (_legacy_feedback_search_keys(row) & context.search_keys):
        return False
    anchor_times = [
        parsed
        for value in context.anchor_timestamps
        if (parsed := _parse_stored_timestamp(value)) is not None
    ]
    anchor_started_at = min(anchor_times) if anchor_times else None
    cutoff = _legacy_feedback_cutoff(row)
    return (
        anchor_started_at is None
        or cutoff is None
        or anchor_started_at <= cutoff
    )


def _legacy_feedback_search_keys(row: Mapping[str, object]) -> frozenset[str]:
    workflow = str(row.get("workflow_search_id") or "")
    if str(row.get("feedback_scope") or "") == "page":
        return frozenset({workflow} - {""})
    return frozenset(
        {
            workflow,
            str(row.get("search_key") or ""),
        }
        - {""}
    )


def _legacy_feedback_cutoff(row: Mapping[str, object]) -> datetime | None:
    submitted_at = _parse_stored_timestamp(row.get("created_at"))
    try:
        conversation = json.loads(str(row.get("conversation_json") or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        conversation = []
    target_id = str(row.get("message_id") or "").strip()
    target = None
    if isinstance(conversation, list):
        target = next(
            (
                item
                for item in reversed(conversation)
                if isinstance(item, dict)
                and str(
                    item.get("message_id") or item.get("messageId") or ""
                ).strip()
                == target_id
            ),
            None,
        )
    if isinstance(target, dict):
        try:
            created_at_ms = int(target.get("created_at") or target.get("createdAt") or 0)
        except (TypeError, ValueError):
            created_at_ms = 0
        if created_at_ms > 0:
            try:
                target_at = datetime.fromtimestamp(created_at_ms / 1000, UTC)
            except (OSError, OverflowError, ValueError):
                target_at = None
            if target_at is None:
                return submitted_at
            if submitted_at is None:
                return target_at
            lag = submitted_at - target_at
            if timedelta(0) <= lag <= _TRUSTED_CLIENT_TIMESTAMP_LAG:
                return target_at
    return submitted_at


def _parse_stored_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _merge_rows(
    first: Sequence[dict[str, object]],
    second: Sequence[dict[str, object]],
    key: str,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for row in (*first, *second):
        merged[str(row[key])] = row
    return list(merged.values())


def _sort_rows(
    rows: Sequence[dict[str, object]], timestamp: str
) -> list[dict[str, object]]:
    return sorted(
        rows,
        key=lambda row: (str(row[timestamp]), str(next(iter(row.values())))),
    )


def _bounded_evidence(
    items: Sequence[dict[str, object]], limit: int
) -> list[dict[str, object]]:
    ordered = sorted(
        items,
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("source") or ""),
        ),
    )
    first_by_source: dict[str, int] = {}
    for index, item in enumerate(ordered):
        first_by_source.setdefault(str(item.get("source") or ""), index)
    reserved_order = sorted(
        first_by_source.values(),
        key=lambda index: (
            0
            if ordered[index].get("association") == "direct_selector"
            else 1,
            str(ordered[index].get("timestamp") or ""),
            str(ordered[index].get("source") or ""),
        ),
    )[:limit]
    reserved = set(reserved_order)
    selected = list(reserved_order)
    selected.extend(
        index
        for index in range(len(ordered))
        if index not in reserved
    )
    return sorted(
        (ordered[index] for index in selected[:limit]),
        key=lambda item: (
            str(item.get("timestamp") or ""),
            str(item.get("source") or ""),
        ),
    )


def _optional_timestamp(value: object, name: str) -> datetime | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiagnosticQueryError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise DiagnosticQueryError(f"{name} must include timezone")
    return parsed.astimezone(UTC)

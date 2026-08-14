"""Read-only operational reporting across 8790 cost and feedback stores."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from tiku_admin.control_store import SQLiteControlStore, micros_to_cny
from tiku_agent.feedback_store import MessageFeedback, SQLiteFeedbackStore


class AdminReporter:
    def __init__(
        self,
        *,
        control_store: SQLiteControlStore,
        cost_database: str | Path,
        feedback_store: SQLiteFeedbackStore,
        task_log_path: str | Path | None = None,
    ) -> None:
        self.control_store = control_store
        self.cost_database = Path(cost_database).resolve()
        self.feedback_store = feedback_store
        self.task_log_path = Path(task_log_path).resolve() if task_log_path else None

    def overview(self) -> dict[str, object]:
        settings = self.control_store.settings()
        started_at, finished_at = _today_window(str(settings["budget_timezone"]))
        total, per_identity = self._usage(started_at, finished_at)
        invitations = self.control_store.list_invitations()
        invite_rows = []
        active_count = 0
        default_budget = int(settings["default_invite_daily_budget_micros"])
        for invitation in invitations:
            if invitation.status == "enabled" and not _is_expired(invitation.expires_at):
                active_count += 1
            usage = per_identity.get(invitation.invite_id, {})
            budget = invitation.daily_budget_micros or default_budget
            cost = int(usage.get("estimated_cost_micros", 0))
            invite_rows.append({
                **invitation.to_dict(),
                "today_searches": int(usage.get("search_count", 0)),
                "today_cost_micros": cost,
                "today_cost_cny": micros_to_cny(cost),
                "effective_budget_micros": budget,
                "effective_budget_cny": micros_to_cny(budget),
                "remaining_micros": max(0, budget - cost),
                "remaining_cny": micros_to_cny(max(0, budget - cost)),
                "last_activity_at": str(usage.get("last_activity_at", "")),
            })
        recent, _count = self.feedback_store.query_feedback(limit=5)
        pending_negative = len([
            item for item in self.feedback_store.list_feedback()
            if item.rating == "negative" and item.review_status == "pending"
        ])
        return {
            "date": datetime.now(ZoneInfo(str(settings["budget_timezone"]))).date().isoformat(),
            "today_searches": int(total["search_count"]),
            "today_cost_micros": int(total["estimated_cost_micros"]),
            "today_cost_cny": micros_to_cny(int(total["estimated_cost_micros"])),
            "active_invites": active_count,
            "pending_negative_feedback": pending_negative,
            "global_budget_micros": int(settings["global_daily_budget_micros"]),
            "global_budget_cny": micros_to_cny(int(settings["global_daily_budget_micros"])),
            "global_remaining_micros": max(
                0,
                int(settings["global_daily_budget_micros"])
                - int(total["estimated_cost_micros"]),
            ),
            "global_remaining_cny": micros_to_cny(max(
                0,
                int(settings["global_daily_budget_micros"])
                - int(total["estimated_cost_micros"]),
            )),
            "invites": invite_rows,
            "recent_feedback": self._feedback_summaries(recent),
        }

    def invitation_rows(self, *, include_archived: bool = False) -> list[dict[str, object]]:
        overview = self.overview()
        rows = list(overview["invites"])
        if not include_archived:
            return rows
        known = {str(row["invite_id"]) for row in rows}
        for invitation in self.control_store.list_invitations(include_archived=True):
            if invitation.invite_id not in known:
                rows.append({
                    **invitation.to_dict(),
                    "today_searches": 0,
                    "today_cost_micros": 0,
                    "today_cost_cny": "0.00",
                    "effective_budget_micros": 0,
                    "effective_budget_cny": "0.00",
                    "remaining_micros": 0,
                    "remaining_cny": "0.00",
                    "last_activity_at": "",
                })
        return rows

    def feedback_list(self, **filters: object) -> dict[str, object]:
        created_from, created_before = _date_window(
            str(filters.get("date") or ""),
            str(self.control_store.settings()["budget_timezone"]),
        )
        identity_status = str(filters.get("identity_status") or "").strip().lower()
        identity_keys = None
        if identity_status:
            if identity_status != "archived":
                raise ValueError("invalid identity status")
            identity_keys = [
                invitation.invite_id
                for invitation in self.control_store.list_invitations(include_archived=True)
                if invitation.status == "archived"
            ]
        items, total = self.feedback_store.query_feedback(
            rating=str(filters.get("rating") or ""),
            identity_key=str(filters.get("identity_key") or ""),
            identity_keys=identity_keys,
            chapter=str(filters.get("chapter") or ""),
            review_status=str(filters.get("review_status") or ""),
            status=str(filters.get("status") or ""),
            layer=str(filters.get("layer") or ""),
            code=str(filters.get("code") or ""),
            request_id=str(filters.get("request_id") or ""),
            search_id=str(filters.get("search_id") or ""),
            include_archived=bool(filters.get("include_archived")),
            created_from=created_from,
            created_before=created_before,
            limit=int(filters.get("limit") or 50),
            offset=int(filters.get("offset") or 0),
        )
        return {
            "items": self._feedback_summaries(items),
            "total": total,
            "chapters": self.feedback_store.list_chapters(),
        }

    def invitation_delete_blockers(self, invite_id: str) -> dict[str, int]:
        cost_runs = -1
        if self.cost_database.is_file():
            try:
                with sqlite3.connect(self.cost_database) as connection:
                    cost_runs = int(connection.execute(
                        "SELECT COUNT(*) FROM model_cost_runs WHERE identity_key = ?",
                        (str(invite_id),),
                    ).fetchone()[0])
            except sqlite3.Error:
                cost_runs = -1
        return {
            "cost_runs": cost_runs,
            "feedback": self.feedback_store.count_for_identity(invite_id),
        }

    def feedback_detail(self, feedback_id: str) -> dict[str, object] | None:
        item = self.feedback_store.get_feedback(feedback_id)
        if item is None:
            return None
        invitation = self.control_store.get_invitation(item.identity_key)
        detail = self._feedback_summaries([item])[0]
        detail.update({
            "conversation": [
                {
                    **message,
                    "images": [
                        f"/api/admin/feedback/{item.feedback_id}/media/{name}"
                        for name in message.get("images", [])
                    ],
                }
                for message in item.conversation
            ],
            "admin_note": item.admin_note,
            "case_expires_at": item.case_expires_at,
            "case_purged_at": item.case_purged_at,
            "invite_label": invitation.label if invitation else item.identity_key,
            "timeline": (
                self.request_events(search_id=item.search_id, limit=200)["items"]
                if item.search_id else []
            ),
        })
        return detail

    def request_events(self, **filters: object) -> dict[str, object]:
        rows = self._read_request_events()
        invitations = {
            item.invite_id: item
            for item in self.control_store.list_invitations(include_archived=True)
        }
        exact_filters = {
            key: str(filters.get(key) or "").strip()
            for key in (
                "identity_key", "status", "layer", "code", "request_id", "search_id"
            )
        }
        for key, value in exact_filters.items():
            if value:
                rows = [row for row in rows if str(row.get(key) or "") == value]
        rows.sort(
            key=lambda row: (str(row.get("finished_at") or ""), str(row.get("request_id") or "")),
            reverse=True,
        )
        total = len(rows)
        limit = max(1, min(200, int(filters.get("limit") or 50)))
        offset = max(0, int(filters.get("offset") or 0))
        visible = []
        for row in rows[offset:offset + limit]:
            invitation = invitations.get(str(row.get("identity_key") or ""))
            visible.append({
                **row,
                "invite_label": invitation.label if invitation else str(row.get("identity_key") or ""),
            })
        all_rows = self._read_request_events()
        return {
            "items": visible,
            "total": total,
            "statuses": sorted({str(row.get("status") or "") for row in all_rows if row.get("status")}),
            "layers": sorted({str(row.get("layer") or "") for row in all_rows if row.get("layer")}),
            "codes": sorted({str(row.get("code") or "") for row in all_rows if row.get("code")}),
        }

    def _read_request_events(self) -> list[dict[str, object]]:
        if self.task_log_path is None or not self.task_log_path.is_file():
            return []
        rows: list[dict[str, object]] = []
        try:
            lines = self.task_log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(raw, dict):
                continue
            outcome = str(raw.get("outcome") or "")
            status = str(raw.get("status") or _legacy_task_status(outcome))
            rows.append({
                "request_id": str(raw.get("request_id") or raw.get("task_id") or ""),
                "search_id": str(raw.get("search_id") or ""),
                "identity_key": str(raw.get("identity_key") or ""),
                "session_key": str(raw.get("session_key") or ""),
                "kind": str(raw.get("kind") or ""),
                "status": status,
                "layer": str(raw.get("layer") or "tool"),
                "code": str(raw.get("code") or _legacy_task_code(outcome)),
                "retryable": bool(raw.get("retryable")),
                "action": str(raw.get("action") or ""),
                "outcome": outcome,
                "phase_before": str(raw.get("phase_before") or ""),
                "phase_after": str(raw.get("phase_after") or ""),
                "chapter": str(raw.get("chapter") or ""),
                "candidate_count": max(0, int(raw.get("candidate_count") or 0)),
                "duration_ms": max(0, int(raw.get("duration_ms") or 0)),
                "error_kind": str(raw.get("error_kind") or ""),
                "started_at": str(raw.get("started_at") or ""),
                "finished_at": str(raw.get("finished_at") or ""),
            })
        return rows

    def _feedback_summaries(
        self, items: list[MessageFeedback]
    ) -> list[dict[str, object]]:
        costs = self._search_costs([item.search_key for item in items])
        invitations = {
            item.invite_id: item
            for item in self.control_store.list_invitations(include_archived=True)
        }
        result = []
        for item in items:
            summary = _feedback_summary(item)
            invitation = invitations.get(item.identity_key)
            summary.update({
                "invite_label": invitation.label if invitation else item.identity_key,
                "cost": costs.get(item.search_key, _empty_search_cost()),
            })
            result.append(summary)
        return result

    def _usage(
        self, started_at: str, finished_at: str
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        empty = {"search_count": 0, "estimated_cost_micros": 0}
        if not self.cost_database.is_file():
            return empty, {}
        try:
            with sqlite3.connect(self.cost_database) as connection:
                connection.row_factory = sqlite3.Row
                total = connection.execute(
                    """
                    SELECT COUNT(DISTINCT NULLIF(search_key, '')) AS search_count,
                           COALESCE(SUM(estimated_cost_micros), 0) AS estimated_cost_micros
                    FROM model_cost_runs
                    WHERE started_at >= ? AND started_at < ?
                    """,
                    (started_at, finished_at),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT identity_key,
                           COUNT(DISTINCT NULLIF(search_key, '')) AS search_count,
                           COALESCE(SUM(estimated_cost_micros), 0) AS estimated_cost_micros,
                           MAX(finished_at) AS last_activity_at
                    FROM model_cost_runs
                    WHERE started_at >= ? AND started_at < ? AND identity_key != ''
                    GROUP BY identity_key
                    """,
                    (started_at, finished_at),
                ).fetchall()
        except sqlite3.Error:
            return empty, {}
        return (
            {
                "search_count": int(total["search_count"] or 0),
                "estimated_cost_micros": int(total["estimated_cost_micros"] or 0),
            },
            {
                str(row["identity_key"]): {
                    "search_count": int(row["search_count"] or 0),
                    "estimated_cost_micros": int(row["estimated_cost_micros"] or 0),
                    "last_activity_at": str(row["last_activity_at"] or ""),
                }
                for row in rows
            },
        )

    def _search_cost(self, search_key: str) -> dict[str, object]:
        clean = str(search_key or "").strip()
        return self._search_costs([clean]).get(clean, _empty_search_cost())

    def _search_costs(self, search_keys: list[str]) -> dict[str, dict[str, object]]:
        keys = sorted({str(value).strip() for value in search_keys if str(value).strip()})
        if not keys or not self.cost_database.is_file():
            return {}
        placeholders = ",".join("?" for _key in keys)
        try:
            with sqlite3.connect(self.cost_database) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    f"""
                    SELECT search_key, estimated_cost_micros, call_count, started_at,
                           finished_at, warning_codes_json
                    FROM model_cost_runs WHERE search_key IN ({placeholders})
                    """,
                    keys,
                ).fetchall()
        except sqlite3.Error:
            return {}
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["search_key"]), []).append(row)
        return {key: _cost_summary(group) for key, group in grouped.items()}


def _cost_summary(rows: list[sqlite3.Row]) -> dict[str, object]:
    cost = sum(int(row["estimated_cost_micros"] or 0) for row in rows)
    warnings: list[str] = []
    for row in rows:
        try:
            warnings.extend(json.loads(str(row["warning_codes_json"] or "[]")))
        except json.JSONDecodeError:
            continue
    return {
        "estimated_cost_micros": cost,
        "estimated_cost_cny": micros_to_cny(cost),
        "model_call_count": sum(int(row["call_count"] or 0) for row in rows),
        "started_at": min(str(row["started_at"] or "") for row in rows),
        "finished_at": max(str(row["finished_at"] or "") for row in rows),
        "warning_codes": sorted(set(warnings)),
    }


def _feedback_summary(item: MessageFeedback) -> dict[str, object]:
    preview = next((message for message in item.conversation if message.get("role") == "user"), {})
    images = list(preview.get("images", [])) if isinstance(preview, dict) else []
    return {
        "feedback_id": item.feedback_id,
        "feedback_number": item.feedback_number,
        "message_id": item.message_id,
        "identity_key": item.identity_key,
        "rating": item.rating,
        "tags": list(item.tags),
        "detail": item.detail,
        "task_revision": item.task_revision,
        "phase": item.phase,
        "candidate_count": item.candidate_count,
        "search_duration_ms": item.search_duration_ms,
        "request_id": item.request_id,
        "search_id": item.search_id,
        "status": item.status,
        "layer": item.layer,
        "code": item.code,
        "chapter": item.chapter,
        "review_status": item.review_status,
        "archived_at": item.archived_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "has_case": bool(item.conversation),
        "preview_text": str(preview.get("message") or "")[:120] if isinstance(preview, dict) else "",
        "preview_image": (
            f"/api/admin/feedback/{item.feedback_id}/media/{images[0]}" if images else ""
        ),
    }


def _today_window(timezone_name: str) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


def _date_window(value: str, timezone_name: str) -> tuple[str, str]:
    clean = str(value or "").strip()
    if not clean:
        return "", ""
    selected = date.fromisoformat(clean)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(selected, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


def _is_expired(expires_at: str) -> bool:
    return bool(expires_at and datetime.fromisoformat(expires_at) <= datetime.now(UTC))


def _empty_search_cost() -> dict[str, object]:
    return {
        "estimated_cost_micros": 0,
        "estimated_cost_cny": "0.00",
        "model_call_count": 0,
        "started_at": "",
        "finished_at": "",
        "warning_codes": [],
    }


def _legacy_task_status(outcome: str) -> str:
    if outcome == "error":
        return "ERROR"
    if outcome == "no_match":
        return "NO_MATCH"
    return "SUCCESS"


def _legacy_task_code(outcome: str) -> str:
    if outcome == "error":
        return "LEGACY_TASK_ERROR"
    if outcome == "no_match":
        return "NO_MATCH"
    return "LEGACY_TASK_SUCCESS"

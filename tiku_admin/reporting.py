"""Read-only operational reporting across 8790 cost and feedback stores."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from tiku_admin.control_store import SQLiteControlStore, micros_to_cny
from tiku_agent.feedback_store import (
    FEEDBACK_SCOPES,
    MessageFeedback,
    SQLiteFeedbackStore,
    scope_feedback_conversation,
)
from tiku_shared.model_costs import estimate_cost


_STAGE_META = {
    "image_triage": ("图片分流", "判断图片进入 A1、A2 或 A3", "workflow"),
    "a3_page_understanding": ("整页理解", "识别题目、文字与结构图的对应关系", "workflow"),
    "a3_page_understanding_retry": ("整页理解重试", "重新识别整页题目关系", "workflow"),
    "a3_auto_crop_grounding": ("整页自动框选", "一次定位所有可检索题目的结构图", "workflow"),
    "a3_auto_crop_compare": ("自动裁图完整性校验", "逐题核对裁图是否完整且对应正确", "workflow"),
    "a3_auto_external_load_screen": ("裁图外荷载门禁", "逐题确认裁图包含明确外荷载", "workflow"),
    "a3_crop_compare": ("人工裁图校验", "核对人工裁剪范围与所选题目", "question"),
    "a3_external_load_screen": ("外荷载门禁", "确认题图包含明确外荷载", "question"),
    "a3_intent": ("A3 意图理解", "理解整页选题、取消范围与会话控制意图", "question"),
    "a3_verified_image": ("A2 题库检索", "识别荷载、章节并检索与复筛候选题", "question"),
    "image": ("A2 题库检索", "识别荷载、章节并检索与复筛候选题", "question"),
    "text": ("结果交互", "处理选题、追问或答案说明", "question"),
}

_CALL_LABELS = {
    "qwen_image_triage": "Qwen 图片分流",
    "qwen_image_triage_reply": "Qwen 分流说明",
    "qwen_a3_page_understanding": "Qwen 整页理解",
    "glm_a3_page_auto_crop": "GLM 整页框选",
    "qwen_a3_crop_compare": "Qwen 裁图完整性校验",
    "external_load_screen": "外荷载门禁",
    "qwen_image_classification": "Qwen 荷载与章节识别",
    "qwen_image_scope": "Qwen 图片范围识别",
    "qwen_layout_analysis": "Qwen 页面布局分析",
    "qwen_structure_type": "Qwen 结构类型识别",
    "qwen_structure_dimension": "Qwen 尺寸识别",
    "qwen_shape_rerank": "Qwen 候选复筛",
    "qwen_length_tie_break": "Qwen 长度细判",
    "zhipu_shape_rerank": "GLM 候选复筛",
    "qwen_safe_answer": "Qwen 答案说明",
    "qwen_intent_decision": "Qwen 追问意图判断",
    "qwen_a3_intent_decision": "Qwen A3 意图判断",
}

_STAGE_ORDER = {
    "image_triage": 10,
    "a3_page_understanding": 20,
    "a3_page_understanding_retry": 20,
    "a3_auto_crop_grounding": 30,
    "a3_auto_crop_compare": 40,
    "a3_auto_external_load_screen": 50,
    "a3_crop_compare": 60,
    "a3_external_load_screen": 65,
    "a3_intent": 68,
    "a3_verified_image": 70,
    "image": 70,
    "text": 80,
}

_QUESTION_SEARCH_TASK_KINDS = frozenset({"image", "a3_verified_image"})
_TRUSTED_CLIENT_TIMESTAMP_LAG = timedelta(minutes=30)

# ``image_triage`` runs once against the parent workflow search id for every
# newly uploaded page, including A1 stops and direct A2 images.  Older A3
# deployments can lack that record, so their page-scoped stages are accepted
# as fallback evidence for the same parent workflow id.
_A3_PAGE_WORKFLOW_TASK_KINDS = frozenset({
    "a3_page_understanding",
    "a3_page_understanding_retry",
    "a3_auto_crop_grounding",
    "a3_auto_crop_compare",
    "a3_auto_external_load_screen",
})


class AdminReporter:
    def __init__(
        self,
        *,
        control_store: SQLiteControlStore,
        cost_database: str | Path | None = None,
        cost_databases: Sequence[str | Path] | None = None,
        feedback_store: SQLiteFeedbackStore,
    ) -> None:
        self.control_store = control_store
        paths = [*list(cost_databases or [])]
        if cost_database is not None:
            paths.insert(0, cost_database)
        resolved: list[Path] = []
        for value in paths:
            path = Path(value).resolve()
            if path not in resolved:
                resolved.append(path)
        self.cost_databases = tuple(resolved)
        self.cost_database = resolved[0] if resolved else Path("model_costs.sqlite3").resolve()
        self.feedback_store = feedback_store

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
            question_searches = int(usage.get("question_search_count", 0))
            invite_rows.append({
                **invitation.to_dict(),
                # ``today_searches`` remains the compatibility alias for the
                # historical A2-question metric.
                "today_searches": question_searches,
                "today_question_searches": question_searches,
                "today_page_searches": int(usage.get("page_search_count", 0)),
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
            "today_searches": int(total["question_search_count"]),
            "today_question_searches": int(total["question_search_count"]),
            "today_page_searches": int(total["page_search_count"]),
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
                    "today_question_searches": 0,
                    "today_page_searches": 0,
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
        feedback_scope = str(filters.get("feedback_scope") or "").strip().lower()
        if feedback_scope and feedback_scope not in FEEDBACK_SCOPES:
            raise ValueError("invalid feedback scope")
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
            feedback_scope=feedback_scope,
            identity_key=str(filters.get("identity_key") or ""),
            identity_keys=identity_keys,
            chapter=str(filters.get("chapter") or ""),
            review_status=str(filters.get("review_status") or ""),
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
        cost_runs = 0
        readable = False
        for path in self.cost_databases:
            if not path.is_file():
                continue
            try:
                with sqlite3.connect(path) as connection:
                    cost_runs += int(connection.execute(
                        "SELECT COUNT(*) FROM model_cost_runs WHERE identity_key = ?",
                        (str(invite_id),),
                    ).fetchone()[0])
                    readable = True
            except sqlite3.Error:
                continue
        return {
            "cost_runs": cost_runs if readable else -1,
            "feedback": self.feedback_store.count_for_identity(invite_id),
        }

    def feedback_detail(self, feedback_id: str) -> dict[str, object] | None:
        item = self.feedback_store.get_feedback(feedback_id)
        if item is None:
            return None
        invitation = self.control_store.get_invitation(item.identity_key)
        detail = self._feedback_summaries([item], include_flow=True)[0]
        conversation = scope_feedback_conversation(item.conversation, item.message_id)
        detail.update({
            "conversation": [
                {
                    **message,
                    "images": [
                        f"/api/admin/feedback/{item.feedback_id}/media/{name}"
                        for name in message.get("images", [])
                    ],
                    "a3_overlay": (
                        f"/api/admin/feedback/{item.feedback_id}/media/{message.get('a3_overlay')}"
                        if message.get("a3_overlay")
                        else ""
                    ),
                }
                for message in conversation
            ],
            "admin_note": item.admin_note,
            "case_expires_at": item.case_expires_at,
            "case_purged_at": item.case_purged_at,
            "invite_label": invitation.label if invitation else item.identity_key,
        })
        return detail

    def _feedback_summaries(
        self,
        items: list[MessageFeedback],
        *,
        include_flow: bool = False,
    ) -> list[dict[str, object]]:
        search_keys = sorted({
            key
            for item in items
            for key in _feedback_cost_search_keys(item)
            if key
        })
        runs = self._load_runs(search_keys=search_keys)
        invitations = {
            item.invite_id: item
            for item in self.control_store.list_invitations(include_archived=True)
        }
        result = []
        for item in items:
            summary = _feedback_summary(item)
            invitation = invitations.get(item.identity_key)
            item_keys = _feedback_cost_search_keys(item)
            cutoff = _feedback_cost_cutoff(item)
            item_runs = [
                run
                for run in runs
                if str(run["search_key"]) in item_keys
                and str(run["identity_key"]) == item.identity_key
                and _run_started_by(run, cutoff)
            ]
            summary.update({
                "invite_label": invitation.label if invitation else item.identity_key,
                "cost": _cost_summary(
                    item_runs,
                    image_route=item.image_route,
                    include_flow=include_flow,
                ),
            })
            result.append(summary)
        return result

    def _usage(
        self, started_at: str, finished_at: str
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        runs = self._load_runs(started_at=started_at, finished_at=finished_at)
        if not runs:
            return {
                "search_count": 0,
                "question_search_count": 0,
                "page_search_count": 0,
                "estimated_cost_micros": 0,
            }, {}
        per_identity: dict[str, dict[str, object]] = {}
        for run in runs:
            identity_key = str(run["identity_key"])
            if not identity_key:
                continue
            usage = per_identity.setdefault(identity_key, {
                "question_search_keys": set(),
                "page_search_keys": set(),
                "estimated_cost_micros": 0,
                "last_activity_at": "",
            })
            search_key = str(run["search_key"])
            task_kind = str(run["task_kind"])
            if search_key and task_kind in _QUESTION_SEARCH_TASK_KINDS:
                usage["question_search_keys"].add(search_key)  # type: ignore[union-attr]
            if search_key and (
                task_kind == "image_triage"
                or task_kind in _A3_PAGE_WORKFLOW_TASK_KINDS
            ):
                usage["page_search_keys"].add(search_key)  # type: ignore[union-attr]
            usage["estimated_cost_micros"] = int(usage["estimated_cost_micros"]) + int(
                run["effective_cost_micros"]
            )
            usage["last_activity_at"] = max(
                str(usage["last_activity_at"]), str(run["finished_at"])
            )
        normalized = {}
        for key, value in per_identity.items():
            question_search_count = len(value.pop("question_search_keys"))
            page_search_count = len(value.pop("page_search_keys"))
            normalized[key] = {
                "search_count": question_search_count,
                "question_search_count": question_search_count,
                "page_search_count": page_search_count,
                **value,
            }
        total_question_searches = len({
            (str(run["identity_key"]), str(run["search_key"]))
            for run in runs
            if run["search_key"]
            and str(run["task_kind"]) in _QUESTION_SEARCH_TASK_KINDS
        })
        total_page_searches = len({
            (str(run["identity_key"]), str(run["search_key"]))
            for run in runs
            if run["search_key"]
            and (
                str(run["task_kind"]) == "image_triage"
                or str(run["task_kind"]) in _A3_PAGE_WORKFLOW_TASK_KINDS
            )
        })
        return (
            {
                "search_count": total_question_searches,
                "question_search_count": total_question_searches,
                "page_search_count": total_page_searches,
                "estimated_cost_micros": sum(int(run["effective_cost_micros"]) for run in runs),
            },
            normalized,
        )

    def _load_runs(
        self,
        *,
        started_at: str = "",
        finished_at: str = "",
        search_keys: Sequence[str] | None = None,
    ) -> list[dict[str, object]]:
        keys = sorted({str(value).strip() for value in (search_keys or []) if str(value).strip()})
        result: list[dict[str, object]] = []
        seen_run_ids: set[str] = set()
        for path in self.cost_databases:
            if not path.is_file():
                continue
            clauses: list[str] = []
            parameters: list[object] = []
            if started_at:
                clauses.append("started_at >= ?")
                parameters.append(started_at)
            if finished_at:
                clauses.append("started_at < ?")
                parameters.append(finished_at)
            if keys:
                clauses.append(f"search_key IN ({','.join('?' for _ in keys)})")
                parameters.extend(keys)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            try:
                with sqlite3.connect(path) as connection:
                    connection.row_factory = sqlite3.Row
                    rows = connection.execute(
                        "SELECT run_id, session_key, identity_key, search_key, task_kind, "
                        "started_at, finished_at, outcome, call_count, total_tokens, "
                        f"estimated_cost_micros, warning_codes_json FROM model_cost_runs {where}",
                        parameters,
                    ).fetchall()
                    calls_by_run = _read_calls(connection, [str(row["run_id"]) for row in rows])
            except sqlite3.Error:
                continue
            for row in rows:
                run_id = str(row["run_id"])
                if run_id in seen_run_ids:
                    continue
                seen_run_ids.add(run_id)
                calls = calls_by_run.get(run_id, [])
                stored_cost = int(row["estimated_cost_micros"] or 0)
                effective_cost = (
                    sum(int(call["effective_cost_micros"]) for call in calls)
                    if calls
                    else stored_cost
                )
                result.append({
                    **dict(row),
                    "calls": calls,
                    "effective_cost_micros": effective_cost,
                    "historical_reprice_applied": any(
                        bool(call["historical_reprice_applied"]) for call in calls
                    ),
                    "warning_codes": _json_list(row["warning_codes_json"]),
                })
        return sorted(result, key=lambda run: (str(run["started_at"]), str(run["run_id"])))


def _read_calls(
    connection: sqlite3.Connection,
    run_ids: Sequence[str],
) -> dict[str, list[dict[str, object]]]:
    if not run_ids:
        return {}
    try:
        rows = connection.execute(
            "SELECT call_id, run_id, sequence, provider, model, call_type, status, "
            "started_at, finished_at, latency_ms, input_tokens, image_tokens, "
            "cached_tokens, output_tokens, total_tokens, attempt_count, error_kind, "
            "price_version, pricing_status, estimated_cost_micros FROM model_cost_calls "
            f"WHERE run_id IN ({','.join('?' for _ in run_ids)}) "
            "ORDER BY started_at, sequence",
            list(run_ids),
        ).fetchall()
    except sqlite3.Error:
        return {}
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        call = dict(row)
        current = int(call["estimated_cost_micros"] or 0)
        repriced = estimate_cost(
            str(call["provider"]),
            str(call["model"]),
            {
                "input_tokens": int(call["input_tokens"] or 0),
                "cached_tokens": int(call["cached_tokens"] or 0),
                "output_tokens": int(call["output_tokens"] or 0),
            },
        )
        can_reprice = (
            str(call["pricing_status"]) != "priced"
            and str(repriced["pricing_status"]) == "priced"
        )
        call["effective_cost_micros"] = (
            int(repriced["estimated_cost_micros"]) if can_reprice else current
        )
        call["effective_price_version"] = (
            str(repriced["price_version"]) if can_reprice else str(call["price_version"])
        )
        call["effective_pricing_status"] = "repriced" if can_reprice else str(call["pricing_status"])
        call["historical_reprice_applied"] = can_reprice
        grouped.setdefault(str(call["run_id"]), []).append(call)
    return grouped


def _cost_summary(
    runs: list[dict[str, object]],
    *,
    image_route: str = "",
    include_flow: bool = False,
) -> dict[str, object]:
    if not runs:
        return _empty_search_cost(include_flow=include_flow, image_route=image_route)
    cost = sum(int(run["effective_cost_micros"]) for run in runs)
    warnings = sorted({
        warning
        for run in runs
        for warning in list(run.get("warning_codes") or [])
    })
    repriced = any(bool(run["historical_reprice_applied"]) for run in runs)
    if repriced:
        warnings = sorted({*warnings, "HISTORICAL_PRICE_RECALCULATED"})
    result: dict[str, object] = {
        "estimated_cost_micros": cost,
        "estimated_cost_cny": micros_to_cny(cost),
        "model_call_count": sum(int(run["call_count"] or 0) for run in runs),
        "started_at": min(str(run["started_at"] or "") for run in runs),
        "finished_at": max(str(run["finished_at"] or "") for run in runs),
        "warning_codes": warnings,
        "historical_reprice_applied": repriced,
    }
    if include_flow:
        route = _infer_route(runs, image_route)
        calls = [call for run in runs for call in list(run.get("calls") or [])]
        result.update({
            "route": route,
            "route_label": _route_label(route),
            "flow": _flow_summary(runs),
            "models": _model_summary(calls),
        })
    return result


def _flow_summary(runs: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    order: list[str] = []
    for run in runs:
        task_kind = str(run["task_kind"] or "unknown")
        if task_kind not in grouped:
            grouped[task_kind] = []
            order.append(task_kind)
        grouped[task_kind].append(run)
    order.sort(key=lambda key: (
        _STAGE_ORDER.get(key, 75),
        min(str(run["started_at"]) for run in grouped[key]),
    ))
    stages = []
    for index, task_kind in enumerate(order, start=1):
        stage_runs = grouped[task_kind]
        title, description, scope = _STAGE_META.get(
            task_kind,
            (task_kind.replace("_", " "), "记录该阶段的模型调用与费用", "question"),
        )
        calls = sorted(
            [call for run in stage_runs for call in list(run.get("calls") or [])],
            key=lambda call: (str(call["started_at"]), int(call["sequence"] or 0)),
        )
        stage_cost = sum(int(run["effective_cost_micros"]) for run in stage_runs)
        stages.append({
            "index": index,
            "key": task_kind,
            "title": title,
            "description": description,
            "scope": scope,
            "run_count": len(stage_runs),
            "model_call_count": sum(int(run["call_count"] or 0) for run in stage_runs),
            "estimated_cost_micros": stage_cost,
            "estimated_cost_cny": micros_to_cny(stage_cost),
            "started_at": min(str(run["started_at"] or "") for run in stage_runs),
            "finished_at": max(str(run["finished_at"] or "") for run in stage_runs),
            "status": "error" if any(str(run["outcome"]) == "error" for run in stage_runs) else "success",
            "calls": [_public_call(call) for call in calls],
        })
    return stages


def _public_call(call: dict[str, object]) -> dict[str, object]:
    call_type = str(call["call_type"])
    cost = int(call["effective_cost_micros"] or 0)
    return {
        "label": _CALL_LABELS.get(call_type, call_type.replace("_", " ")),
        "call_type": call_type,
        "provider": str(call["provider"]),
        "model": str(call["model"]),
        "status": str(call["status"]),
        "latency_ms": int(call["latency_ms"] or 0),
        "input_tokens": int(call["input_tokens"] or 0),
        "cached_tokens": int(call["cached_tokens"] or 0),
        "output_tokens": int(call["output_tokens"] or 0),
        "total_tokens": int(call["total_tokens"] or 0),
        "attempt_count": int(call["attempt_count"] or 0),
        "estimated_cost_micros": cost,
        "estimated_cost_cny": micros_to_cny(cost),
        "price_version": str(call["effective_price_version"]),
        "pricing_status": str(call["effective_pricing_status"]),
        "error_kind": str(call["error_kind"]),
    }


def _model_summary(calls: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for call in calls:
        key = (str(call["provider"]), str(call["model"]))
        item = grouped.setdefault(key, {
            "provider": key[0],
            "model": key[1],
            "call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_micros": 0,
        })
        item["call_count"] = int(item["call_count"]) + int(call["attempt_count"] or 0)
        for name in ("input_tokens", "output_tokens", "total_tokens", "estimated_cost_micros"):
            source = "effective_cost_micros" if name == "estimated_cost_micros" else name
            item[name] = int(item[name]) + int(call[source] or 0)
    result = []
    for item in grouped.values():
        item["estimated_cost_cny"] = micros_to_cny(int(item["estimated_cost_micros"]))
        result.append(item)
    return sorted(result, key=lambda item: (-int(item["estimated_cost_micros"]), str(item["model"])))


def _infer_route(runs: list[dict[str, object]], explicit: str) -> str:
    clean = str(explicit or "").strip().upper()
    if clean in {"A1", "A2", "A3"}:
        return clean
    task_kinds = {str(run["task_kind"]) for run in runs}
    if any(kind.startswith("a3_page") or kind.startswith("a3_auto") for kind in task_kinds):
        return "A3"
    if "image" in task_kinds or "a3_verified_image" in task_kinds:
        return "A2"
    if "image_triage" in task_kinds:
        return "A1"
    return ""


def _route_label(route: str) -> str:
    return {
        "A1": "A1 · 停止检索",
        "A2": "A2 · 单题直接检索",
        "A3": "A3 · 整页拆题后检索",
    }.get(route, "未识别路线")


def _feedback_summary(item: MessageFeedback) -> dict[str, object]:
    conversation = scope_feedback_conversation(item.conversation, item.message_id)
    preview = next((message for message in conversation if message.get("role") == "user"), {})
    images = list(preview.get("images", [])) if isinstance(preview, dict) else []
    return {
        "feedback_id": item.feedback_id,
        "feedback_number": item.feedback_number,
        "message_id": item.message_id,
        "rated_response_id": item.rated_response_id,
        "legacy_response_binding": not bool(item.rated_response_id),
        "identity_key": item.identity_key,
        "rating": item.rating,
        "tags": list(item.tags),
        "detail": item.detail,
        "task_revision": item.task_revision,
        "phase": item.phase,
        "candidate_count": item.candidate_count,
        "search_duration_ms": item.search_duration_ms,
        "chapter": item.chapter,
        "image_route": item.image_route,
        "workflow_search_id": item.workflow_search_id,
        "intent": item.intent,
        "feedback_scope": item.feedback_scope,
        "review_status": item.review_status,
        "archived_at": item.archived_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "has_case": bool(conversation),
        "preview_text": str(preview.get("message") or "")[:120] if isinstance(preview, dict) else "",
        "preview_image": (
            f"/api/admin/feedback/{item.feedback_id}/media/{images[0]}" if images else ""
        ),
    }


def _feedback_cost_search_keys(item: MessageFeedback) -> set[str]:
    if item.feedback_scope == "page":
        return {item.workflow_search_id} - {""}
    return {item.search_key, item.workflow_search_id} - {""}


def _feedback_cost_cutoff(item: MessageFeedback) -> datetime | None:
    submitted_at = _parse_iso_datetime(item.created_at)
    target_messages = [
        message
        for message in scope_feedback_conversation(item.conversation, item.message_id)
        if str(message.get("message_id") or message.get("messageId") or "").strip()
        == item.message_id
    ]
    if target_messages:
        try:
            created_at_ms = int(target_messages[-1].get("created_at") or 0)
        except (TypeError, ValueError):
            created_at_ms = 0
        if created_at_ms > 0:
            target_at = datetime.fromtimestamp(created_at_ms / 1000, UTC)
            if submitted_at is None:
                return target_at
            # Conversation timestamps originate from the browser.  They give
            # us a more precise boundary for the reply being reviewed, but a
            # badly skewed device clock must not erase legitimate server-side
            # model runs.  Delayed/implausible values safely fall back to the
            # server-recorded feedback submission time.
            client_lag = submitted_at - target_at
            if timedelta(0) <= client_lag <= _TRUSTED_CLIENT_TIMESTAMP_LAG:
                return target_at
    return submitted_at


def _run_started_by(run: dict[str, object], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    started_at = _parse_iso_datetime(str(run.get("started_at") or ""))
    return started_at is None or started_at <= cutoff


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _json_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


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


def _empty_search_cost(
    *,
    include_flow: bool = False,
    image_route: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "estimated_cost_micros": 0,
        "estimated_cost_cny": "0.00",
        "model_call_count": 0,
        "started_at": "",
        "finished_at": "",
        "warning_codes": [],
        "historical_reprice_applied": False,
    }
    if include_flow:
        route = str(image_route or "").strip().upper()
        result.update({
            "route": route if route in {"A1", "A2", "A3"} else "",
            "route_label": _route_label(route),
            "flow": [],
            "models": [],
        })
    return result

"""Isolated A3 manual-crop runtime layered over the existing A2 runtime."""

from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageOps

from tiku_agent.a3_intent_v1 import (
    A3ActionDecisionV1,
    A3IntentContextV1,
    A3IntentEngineV1,
    A3IntentUnitV1,
)
from tiku_agent.a3_auto_crop import (
    A3AutoCropper,
    expand_normalized_bbox,
    normalized_bbox_to_bounds,
)
from tiku_agent.a3_models import (
    A3CropVerifier,
    A3ModelError,
    A3PageObserver,
    A3UnitAnalyzer,
    CropCompareResult,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.image_triage_authority import NO_EXTERNAL_LOAD_REPLY
from tiku_agent.session_artifacts import SessionArtifacts, session_key
from tiku_agent.session_runtime import (
    AgentProtocolError,
    AgentSessionRuntime,
    ProgressReporter,
    _ExecutionGate,
)
from tiku_shared.model_costs import (
    ModelCostCollector,
    SQLiteModelCostLedger,
    model_cost_scope,
    new_run_id,
)
from tiku_shared.request_protocol import RequestProtocol, new_request_id, new_search_id
from tiku_shared.trace_context import (
    current_request_id,
    current_trace_id,
    submit_with_trace_context,
)
from tiku_shared.trace_events import bind_trace_event_dimensions, record_trace_event


A3_PHASE_IDLE = "IDLE"
A3_PHASE_UNDERSTANDING = "UNDERSTANDING_PAGE"
A3_PHASE_AUTO_GROUNDING = "AUTO_GROUNDING_PAGE"
A3_PHASE_AUTO_VALIDATING = "AUTO_VALIDATING_CROPS"
A3_PHASE_WAIT_SELECTION = "WAIT_UNIT_SELECTION"
A3_PHASE_CROP_REQUIRED = "CROP_REQUIRED"
A3_PHASE_VERIFYING = "VERIFYING_CROP"
A3_PHASE_A2_ACTIVE = "A2_ACTIVE"
A3_PHASE_COMPLETE = "COMPLETE"
A3_PHASE_ERROR = "ERROR"

_A3_PHASES = {
    A3_PHASE_IDLE,
    A3_PHASE_UNDERSTANDING,
    A3_PHASE_AUTO_GROUNDING,
    A3_PHASE_AUTO_VALIDATING,
    A3_PHASE_WAIT_SELECTION,
    A3_PHASE_CROP_REQUIRED,
    A3_PHASE_VERIFYING,
    A3_PHASE_A2_ACTIVE,
    A3_PHASE_COMPLETE,
    A3_PHASE_ERROR,
}
A3_CROP_REVIEW_MESSAGES = {
    "SELECTED_DIAGRAM_MISMATCH": "裁剪结果未通过，裁剪图与所选题目不匹配，请重新选择区域裁剪。",
    "MULTIPLE_DIAGRAMS": "裁剪结果未通过，裁剪区域包含多个结构图，请重新选择区域裁剪。",
    "EXTERNAL_LOADS_INCOMPLETE": "裁剪结果未通过，结构荷载不完整，请重新选择区域裁剪。",
    "STRUCTURE_INCOMPLETE": "裁剪结果未通过，结构图不完整，请重新选择区域裁剪。",
    "IMAGE_UNCLEAR": "裁剪结果未通过，裁剪图不清晰，请重新选择区域裁剪。",
    "CROP_UNCONFIRMED": "裁剪结果未通过，无法确认裁剪图完整，请重新选择区域裁剪。",
    "LOAD_CHECK_UNAVAILABLE": "裁剪结果暂时无法确认外荷载，请重新提交裁剪。",
    "EXTERNAL_LOADS_NOT_FOUND": "裁剪结果未通过，未识别到结构荷载，请重新选择区域裁剪。",
}
A3_CROP_REVIEW_CODES = frozenset(A3_CROP_REVIEW_MESSAGES)
_A3_PAGE_ERROR_RETENTION = timedelta(days=30)


def _merge_a3_projection(
    public_snapshot: Mapping[str, object],
    existing_projection: object,
) -> dict[str, object]:
    """Combine the A3 parent identity with any nested A2 response details."""

    projection = (
        dict(existing_projection)
        if isinstance(existing_projection, Mapping)
        else {}
    )

    # These fields describe the page/workflow that owns the child response.
    # They must come from the outer A3 state even when A2 finalized first.
    for key in (
        "session_valid",
        "has_active_image",
        "task_revision",
        "phase",
        "workflow_search_id",
        "image_route",
        "a3",
    ):
        if key in public_snapshot:
            projection[key] = public_snapshot[key]

    # Keep child search metadata when it exists; page-level snapshots may have
    # intentionally reset these values after the child has completed.
    for key in ("search_id", "chapter", "candidate_count", "candidate_generation"):
        if key not in projection or projection[key] in (None, "", 0):
            if key in public_snapshot:
                projection[key] = public_snapshot[key]
    return projection


def _capture_a3_response_snapshot(method: Callable[..., AgentResponse]):
    """Bound public work and keep response-time state under the session lock."""

    @wraps(method)
    def wrapped(
        runtime: "A3MvpRuntime",
        session_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> AgentResponse:
        clean = _clean_session_id(session_id)
        progress = kwargs.get("progress")
        error_snapshot_captured = False
        session_lock = runtime._lock(clean)
        try:
            with runtime._execution_gate.enter(progress, session_key=session_lock):
                with session_lock:
                    try:
                        response = method(runtime, clean, *args, **kwargs)
                        response.response_snapshot = dict(runtime.session_snapshot(clean))
                        response.response_projection_snapshot = _merge_a3_projection(
                            response.response_snapshot,
                            response.response_projection_snapshot,
                        )
                        response.response_media_snapshot_captured = True
                        try:
                            response.uploaded_image_path = runtime.current_image_path(clean)
                            raw_a3 = response.response_snapshot.get("a3")
                            selected = (
                                raw_a3.get("selected_unit")
                                if isinstance(raw_a3, Mapping)
                                else None
                            )
                            selected_unit_id = (
                                str(selected.get("unit_id") or "").strip()
                                if isinstance(selected, Mapping)
                                else ""
                            )
                            if (
                                isinstance(raw_a3, Mapping)
                                and raw_a3.get("phase") == A3_PHASE_A2_ACTIVE
                                and selected_unit_id
                            ):
                                response.submitted_crop_path = runtime.current_crop_path(
                                    clean, selected_unit_id
                                )
                            if response.intent == "a3_units_prepared":
                                response.feedback_overlay_path = (
                                    runtime.current_auto_crop_overlay_path(clean)
                                )
                        except Exception:  # noqa: BLE001 - do not replace the business reply.
                            pass
                        return response
                    except Exception as exc:
                        try:
                            setattr(
                                exc,
                                "response_snapshot",
                                dict(runtime.session_snapshot(clean)),
                            )
                            error_snapshot_captured = True
                        except Exception:  # noqa: BLE001 - never mask the original failure.
                            pass
                        raise
        except Exception as exc:
            if not error_snapshot_captured:
                try:
                    setattr(exc, "response_snapshot", dict(runtime.session_snapshot(clean)))
                except Exception:  # noqa: BLE001 - never mask the original failure.
                    pass
            raise

    return wrapped
_A3_MEDIA_RETRY_TEXTS = frozenset({"重试", "再试一次", "重新发送", "再发一次"})
_CHINESE_ORDINALS = {
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


@dataclass
class A3SessionState:
    session_id: str
    entry_route: str = ""
    phase: str = A3_PHASE_IDLE
    source_page_path: str = ""
    page_understanding: dict[str, Any] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)
    selected_unit_id: str = ""
    completed_unit_ids: list[str] = field(default_factory=list)
    searched_unit_ids: list[str] = field(default_factory=list)
    crop_drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_crop_enabled: bool = False
    auto_crop_page: dict[str, Any] = field(default_factory=dict)
    auto_crops: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_crop_overlay_path: str = ""
    requested_unit_ids: list[str] = field(default_factory=list)
    crop_review_required: bool = False
    crop_review_code: str = ""
    crop_review_feedback: str = ""
    task_revision: int = 0
    current_search_id: str = ""
    workflow_search_id: str = ""
    page_finished: bool = False
    pending_intent_clarification: dict[str, Any] = field(default_factory=dict)
    last_a3_intent: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    last_error_detail: str = ""

    def validate(self) -> None:
        if self.entry_route not in {"", "A1", "A2", "A3"}:
            raise ValueError(f"unknown image entry route: {self.entry_route}")
        if self.phase not in _A3_PHASES:
            raise ValueError(f"unknown A3 phase: {self.phase}")
        unit_ids = [str(item.get("unit_id") or "") for item in self.units]
        if any(not value for value in unit_ids) or len(unit_ids) != len(set(unit_ids)):
            raise ValueError("A3 unit ids must be present and unique")
        if self.selected_unit_id and self.selected_unit_id not in unit_ids:
            raise ValueError("selected A3 unit is unavailable")
        if any(value not in unit_ids for value in self.completed_unit_ids):
            raise ValueError("completed A3 unit is unavailable")
        if any(value not in unit_ids for value in self.searched_unit_ids):
            raise ValueError("searched A3 unit is unavailable")
        if any(value not in unit_ids for value in self.requested_unit_ids):
            raise ValueError("requested A3 unit is unavailable")
        if any(value not in unit_ids for value in self.auto_crops):
            raise ValueError("automatic crop is bound to an unavailable unit")
        if self.crop_review_code and self.crop_review_code not in A3_CROP_REVIEW_CODES:
            raise ValueError("unknown A3 crop review code")
        if self.crop_review_code and not self.crop_review_required:
            raise ValueError("A3 crop review code requires an active review")
        page_indexes = [int(item.get("page_index") or 0) for item in self.units]
        if any(value < 1 for value in page_indexes) or len(page_indexes) != len(set(page_indexes)):
            raise ValueError("A3 page indexes must be positive and unique")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "A3SessionState":
        values = dict(payload)
        if not values.get("entry_route") and values.get("source_page_path"):
            values["entry_route"] = "A3"
        if not values.get("workflow_search_id") and values.get("current_search_id"):
            values["workflow_search_id"] = values["current_search_id"]
        state = cls(**values)
        _ensure_stable_page_indexes(state.units)
        _refresh_unlabelled_display_labels(state.units)
        for unit in state.units:
            unit["a2_context_text"] = _question_context_text(unit)
        state.validate()
        return state

    def unit(self, unit_id: str) -> dict[str, Any] | None:
        return next(
            (dict(item) for item in self.units if item.get("unit_id") == unit_id),
            None,
        )

    @property
    def searchable_units(self) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in self.units
            if item.get("searchability") == "searchable_candidate"
        ]

    @property
    def remaining_units(self) -> list[dict[str, Any]]:
        if self.page_finished:
            return []
        closed = set(self.completed_unit_ids) | set(self.searched_unit_ids)
        return [item for item in self.searchable_units if item["unit_id"] not in closed]


class SQLiteA3SessionStore:
    def __init__(
        self,
        database_path: str | Path,
        *,
        ttl: timedelta = timedelta(hours=2),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS a3_sessions (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_a3_sessions_expires_at "
                "ON a3_sessions(expires_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS a3_page_errors (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    search_id TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_a3_page_errors_created_at "
                "ON a3_page_errors(created_at)"
            )

    def load(self, session_id: str) -> A3SessionState | None:
        clean = str(session_id or "").strip()
        if not clean:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json, expires_at FROM a3_sessions WHERE session_id = ?",
                (clean,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) <= self._timestamp():
                conn.execute("DELETE FROM a3_sessions WHERE session_id = ?", (clean,))
                return None
        return A3SessionState.from_dict(json.loads(row["state_json"]))

    def save(self, state: A3SessionState) -> None:
        state.validate()
        now = self._timestamp()
        expires = now + self.ttl
        payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO a3_sessions (session_id, state_json, updated_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (state.session_id, payload, now.isoformat(), expires.isoformat()),
            )

    def clear(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM a3_sessions WHERE session_id = ?", (str(session_id),))

    def purge_expired(self) -> list[str]:
        now = self._timestamp().isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM a3_sessions WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            session_ids = [str(row["session_id"]) for row in rows]
            if session_ids:
                conn.executemany(
                    "DELETE FROM a3_sessions WHERE session_id = ?",
                    [(value,) for value in session_ids],
                )
        return session_ids

    def record_page_error(
        self,
        state: A3SessionState,
        *,
        task_kind: str,
        diagnostic: Mapping[str, str],
    ) -> None:
        now = self._timestamp()
        cutoff = (now - _A3_PAGE_ERROR_RETENTION).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM a3_page_errors WHERE created_at < ?", (cutoff,))
            conn.execute(
                """
                INSERT INTO a3_page_errors (
                    session_key, search_id, task_kind, phase,
                    error_type, error_code, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_key(state.session_id),
                    state.current_search_id,
                    str(task_kind),
                    state.phase,
                    diagnostic.get("error_type", ""),
                    diagnostic.get("error_code", ""),
                    diagnostic.get("error_message", ""),
                    now.isoformat(),
                ),
            )

    def recent_page_errors(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded_limit = min(100, max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT search_id, task_kind, phase, error_type, error_code,
                       error_message, created_at
                FROM a3_page_errors
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _timestamp(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class A3MvpRuntime:
    """Own full-page state and delegate verified single units to the A2 runtime."""

    a3_enabled = True

    def __init__(
        self,
        *,
        store: SQLiteA3SessionStore,
        artifacts: SessionArtifacts,
        a2_runtime: AgentSessionRuntime,
        page_observer: A3PageObserver,
        crop_verifier: A3CropVerifier,
        auto_cropper: A3AutoCropper | None = None,
        auto_crop_max_workers: int = 10,
        auto_prepare_all_units: bool = False,
        unit_analyzer: A3UnitAnalyzer | None = None,
        external_load_screen: Callable[[str | Path], str] | None = None,
        image_triage_authority: object | None = None,
        cost_ledger: SQLiteModelCostLedger | None = None,
        intent_engine: A3IntentEngineV1 | None = None,
        enable_three_scope_cancel_clarification: bool = False,
        max_concurrent_tasks: int = 0,
        max_queued_tasks: int = 0,
        queue_wait_seconds: float = 90.0,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.a2_runtime = a2_runtime
        self.page_observer = page_observer
        self.crop_verifier = crop_verifier
        self.auto_cropper = auto_cropper
        self.auto_crop_max_workers = max(1, min(10, int(auto_crop_max_workers)))
        self.auto_prepare_all_units = bool(auto_prepare_all_units)
        # Kept as an injection point for older callers; A3 no longer owns
        # final load/chapter extraction after crop verification.
        self.unit_analyzer = unit_analyzer
        self.external_load_screen = external_load_screen
        self.image_triage_authority = image_triage_authority
        self.cost_ledger = cost_ledger
        self.intent_engine = intent_engine
        self.enable_three_scope_cancel_clarification = bool(
            enable_three_scope_cancel_clarification
        )
        self._execution_gate = _ExecutionGate(
            max_concurrent_tasks,
            max_queued_tasks,
            queue_wait_seconds,
        )
        self._locks = tuple(threading.RLock() for _ in range(64))

    @_capture_a3_response_snapshot
    def handle_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        clean = _clean_session_id(session_id)
        lock = self._lock(clean)
        with lock:
            self._ensure_budget(identity_key)
            previous = self.store.load(clean)
            next_revision = int(previous.task_revision if previous is not None else 0) + 1
            self._clear_locked(clean, preserve_artifacts=True)
            persisted = self.artifacts.persist_image(clean, image_path)
            workflow_search_id = new_search_id()
            state = A3SessionState(
                session_id=clean,
                entry_route="A3" if self.image_triage_authority is None else "",
                phase=A3_PHASE_UNDERSTANDING,
                source_page_path=str(persisted),
                auto_crop_enabled=self.auto_cropper is not None,
                task_revision=next_revision,
                current_search_id=workflow_search_id,
                workflow_search_id=workflow_search_id,
            )
            self.store.save(state)
            self._bind_trace_state(state, identity_key=identity_key)
            return self._route_persisted_image(
                state,
                persisted,
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
            )

    @_capture_a3_response_snapshot
    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        clean = _clean_session_id(session_id)
        with self._lock(clean):
            self._ensure_budget(identity_key)
            state = self.store.load(clean)
            if state is None:
                return AgentResponse(
                    text="先发一张题图给我吧。",
                    intent="clarification",
                    protocol=RequestProtocol.from_code("UPLOAD_REQUIRED").to_dict(),
                )
            self._bind_trace_state(state, identity_key=identity_key)
            clean_text = str(text or "").strip()
            if (
                state.phase == A3_PHASE_A2_ACTIVE
                and state.last_error in {
                    "candidate_media_delivery_failed",
                    "answer_media_delivery_failed",
                }
                and clean_text in _A3_MEDIA_RETRY_TEXTS
            ):
                response = self.a2_runtime.handle_text(
                    clean,
                    clean_text,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
                return self._after_a2_response(state, response)
            if state.entry_route == "A2":
                return self.a2_runtime.handle_text(
                    clean,
                    clean_text,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
            if state.entry_route == "A1":
                return _response(
                    "这张图没有进入题库检索，请重新上传新的题图。",
                    state,
                    intent="image_triage_stop",
                    code="UPLOAD_REQUIRED",
                )
            if self.intent_engine is not None:
                context = self._a3_intent_context(state)
                decision = self._call_model(
                    state,
                    "a3_intent",
                    lambda: self.intent_engine.decide(clean_text, context),
                    identity_key=identity_key,
                )
                handled = self._dispatch_a3_intent(
                    state,
                    decision,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
                if handled is not None:
                    return handled
            if state.phase == A3_PHASE_ERROR and clean_text in {"重试", "再试一次", "重新识别"}:
                if not state.entry_route and self.image_triage_authority is not None:
                    return self._route_persisted_image(
                        state,
                        Path(state.source_page_path),
                        identity_key=identity_key,
                        progress=progress,
                        request_id=request_id,
                    )
                return self._retry_page_understanding(
                    state,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
            if state.phase in {A3_PHASE_WAIT_SELECTION, A3_PHASE_COMPLETE}:
                unit_id, ambiguous = _resolve_unit_selection(clean_text, state.remaining_units)
                if unit_id:
                    return self._select_locked(
                        state,
                        unit_id,
                        identity_key=identity_key,
                        progress=progress,
                        request_id=request_id,
                    )
                message = (
                    "你这句话里像是选了不止一道题。当前一次只处理一道，请再选一个。"
                    if ambiguous
                    else "我还不能唯一确定你选的是哪道题。请点击题目按钮，或只说一个题号。"
                )
                return _response(
                    message,
                    state,
                    intent="a3_unit_clarification",
                    code="CLARIFICATION_REQUIRED",
                )
            if state.phase == A3_PHASE_CROP_REQUIRED:
                selected = state.unit(state.selected_unit_id) or {}
                return _response(
                    f"当前已选中「{selected.get('display_label', '这道题')}」。"
                    "在裁剪页框住一个完整结构图后提交就可以继续。",
                    state,
                    intent="a3_crop_required",
                    code="CLARIFICATION_REQUIRED",
                )
            if state.phase == A3_PHASE_A2_ACTIVE:
                child = self.a2_runtime.session_snapshot(clean)
                if not child.get("session_valid") or str(child.get("phase") or "") in {
                    "IDLE",
                    "CANCELLED",
                }:
                    state.selected_unit_id = ""
                    state.phase = (
                        A3_PHASE_WAIT_SELECTION
                        if state.remaining_units
                        else A3_PHASE_COMPLETE
                    )
                    self.store.save(state)
                    if state.phase == A3_PHASE_WAIT_SELECTION:
                        unit_id, ambiguous = _resolve_unit_selection(
                            clean_text,
                            state.remaining_units,
                        )
                        if unit_id:
                            return self._select_locked(
                                state,
                                unit_id,
                                identity_key=identity_key,
                                progress=progress,
                                request_id=request_id,
                            )
                        message = (
                            "上一道已经停止了。你这句话里像是选了不止一道题，请再选一个。"
                            if ambiguous
                            else "上一道已经停止了。请从这张图的剩余题目中选一道。"
                        )
                        return _response(
                            message,
                            state,
                            intent="a3_unit_clarification",
                            code="CLARIFICATION_REQUIRED",
                        )
                    return _response(
                        "上一道已经停止了，这张图里没有其他待处理题目。",
                        state,
                        intent="a3_complete",
                    )
                if _is_a3_reselect_request(clean_text):
                    return self._return_to_unit_selection_locked(state)
                unit_id, ambiguous = _resolve_active_unit_selection(
                    clean_text,
                    state.remaining_units,
                )
                if unit_id:
                    return self._select_locked(
                        state,
                        unit_id,
                        identity_key=identity_key,
                        progress=progress,
                        request_id=request_id,
                    )
                if ambiguous:
                    return _response(
                        "你像是想切换题目，但我不能唯一确定是哪一道。请从题目列表中选一个。",
                        state,
                        intent="a3_unit_clarification",
                        code="CLARIFICATION_REQUIRED",
                    )
                bare_rank = _active_bare_reference_rank(clean_text)
                if bare_rank is not None:
                    original_unit_id, original_ambiguous = _resolve_unit_selection(
                        clean_text,
                        state.remaining_units,
                    )
                    candidate_count = int(child.get("candidate_count") or 0)
                    candidate_valid = 1 <= bare_rank <= candidate_count
                    if original_ambiguous or (original_unit_id and candidate_valid):
                        return _response(
                            f"你是要选择候选 {bare_rank}，还是切换到图片中的第 {bare_rank} 道题？"
                            f"请说“候选 {bare_rank}”或“图片第 {bare_rank} 题”。",
                            state,
                            intent="a3_namespace_clarification",
                            code="CLARIFICATION_REQUIRED",
                        )
                    if original_unit_id:
                        return self._select_locked(
                            state,
                            original_unit_id,
                            identity_key=identity_key,
                            progress=progress,
                            request_id=request_id,
                        )
                response = self.a2_runtime.handle_text(
                    clean,
                    clean_text,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
                return self._after_a2_response(state, response)
            return _response(
                "这张图里已经没有待处理的题了。你可以继续发一张新题图。",
                state,
                intent="a3_complete",
            )

    def _a3_intent_context(self, state: A3SessionState) -> A3IntentContextV1:
        child: Mapping[str, Any] = {}
        if state.phase == A3_PHASE_A2_ACTIVE:
            child = self.a2_runtime.session_snapshot(state.session_id)
        completed = set(state.completed_unit_ids)
        searched = set(state.searched_unit_ids)
        units = tuple(
            A3IntentUnitV1(
                unit_id=str(unit["unit_id"]),
                question_index=int(unit["page_index"]),
                display_label=str(unit.get("display_label") or ""),
                completed=str(unit["unit_id"]) in completed,
                searched=str(unit["unit_id"]) in searched,
                selected=str(unit["unit_id"]) == state.selected_unit_id,
            )
            for unit in state.searchable_units
        )
        pending = state.pending_intent_clarification
        pending_scopes = tuple(
            str(value)
            for value in pending.get("options") or ()
            if str(value)
        ) if pending.get("kind") == "cancel_scope" else ()
        return A3IntentContextV1(
            phase=state.phase,
            units=units,
            child_phase=str(child.get("phase") or ""),
            candidate_count=int(child.get("candidate_count") or 0),
            page_finished=state.page_finished,
            pending_cancel_scopes=pending_scopes,
        )

    def _dispatch_a3_intent(
        self,
        state: A3SessionState,
        decision: A3ActionDecisionV1,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
    ) -> AgentResponse | None:
        state.last_a3_intent = decision.to_dict()
        action = decision.action
        if action == "defer_to_a2":
            state.pending_intent_clarification = {}
            self.store.save(state)
            return None
        if action == "clarification":
            reason = str(decision.clarification_reason or "ambiguous_action")
            if reason == "ambiguous_cancel_scope":
                options = (
                    ["finish_page", "cancel_current_unit", "continue_current"]
                    if self.enable_three_scope_cancel_clarification
                    else self._cancel_scope_options(state)
                )
                state.pending_intent_clarification = {
                    "kind": "cancel_scope",
                    "options": options,
                }
                self.store.save(state)
                labels = {
                    "cancel_current_unit": (
                        "结束当前题"
                        if self.enable_three_scope_cancel_clarification
                        else "只停止当前这道题（裁剪后的单题图）"
                    ),
                    "finish_page": "结束最初上传的整张多题图",
                    "continue_current": "继续当前题",
                }
                choices = "\n".join(
                    f"{index}. {labels[value]}"
                    for index, value in enumerate(options, start=1)
                )
                if self.enable_three_scope_cancel_clarification:
                    question = "你想结束最初上传的整张多题图，还是结束当前题？"
                else:
                    question = (
                        "你说的“这张图”是当前裁剪后的单题图，还是最初上传的整张多题图？"
                        if state.selected_unit_id
                        else "你想结束最初上传的整张多题图，还是继续当前操作？"
                    )
                return _response(
                    (
                        question + "\n" + choices
                        if self.enable_three_scope_cancel_clarification
                        else question + "我暂时没有改变当前进度。\n" + choices
                    ),
                    state,
                    intent="a3_cancel_scope_clarification",
                    code="CLARIFICATION_REQUIRED",
                )
            state.pending_intent_clarification = {}
            self.store.save(state)
            if reason == "ambiguous_number_namespace":
                return _response(
                    "这个数字可能是候选排名，也可能是原图题号。"
                    "请说“候选 2”或“图片第 2 题”。",
                    state,
                    intent="a3_namespace_clarification",
                    code="CLARIFICATION_REQUIRED",
                )
            if reason == "unit_completed":
                message = "你指定的原图题目已经处理完成，没有改选其他题目。"
                intent = "a3_unit_unavailable"
            elif reason == "unit_unavailable":
                message = "你指定的原图题目当前不能再次选择，没有改选其他题目。"
                intent = "a3_unit_unavailable"
            elif reason == "out_of_range":
                message = "原图中没有这个稳定题号，请从题目列表中选择。"
                intent = "a3_unit_clarification"
            elif state.phase == A3_PHASE_CROP_REQUIRED:
                selected = state.unit(state.selected_unit_id) or {}
                message = (
                    f"当前正在处理「{selected.get('display_label', '这道题')}」。"
                    "你可以继续裁剪；如需停止，请明确说“取消当前题”或“结束整张原图”。"
                )
                intent = "a3_crop_required"
            elif state.page_finished:
                message = "这张图的流程已经结束。你可以上传新题图，或明确说“开始新对话”。"
                intent = "a3_complete"
            else:
                message = "我还不能确定你想执行什么。请明确说题号，或说明要取消的范围。"
                intent = "a3_unit_clarification"
            return _response(
                message,
                state,
                intent=intent,
                code="CLARIFICATION_REQUIRED",
            )

        state.pending_intent_clarification = {}
        if action == "reset_session":
            search_id = state.current_search_id
            self._clear_locked(state.session_id)
            return AgentResponse(
                text="好，已清空当前会话。请发送一张新题图。",
                intent="a3_session_reset",
                protocol=RequestProtocol.from_code(
                    "REQUEST_SUCCEEDED",
                    request_id=request_id or new_request_id(),
                    search_id=search_id,
                ).to_dict(),
            )
        if action == "finish_page":
            self.a2_runtime.clear(state.session_id, preserve_artifacts=True)
            state.selected_unit_id = ""
            state.page_finished = True
            state.phase = A3_PHASE_COMPLETE
            state.crop_review_required = False
            state.crop_review_code = ""
            state.crop_review_feedback = ""
            self.store.save(state)
            return _response(
                "好，已结束最初上传的整张多题图搜题流程。当前对话记录仍然保留。",
                state,
                intent="a3_page_finished",
            )
        if action == "cancel_current_unit":
            selected = state.unit(state.selected_unit_id) or {}
            display_label = str(selected.get("display_label") or "当前题")
            self.a2_runtime.clear(state.session_id, preserve_artifacts=True)
            state.selected_unit_id = ""
            state.phase = A3_PHASE_WAIT_SELECTION if state.remaining_units else A3_PHASE_COMPLETE
            state.crop_review_required = False
            state.crop_review_code = ""
            state.crop_review_feedback = ""
            self.store.save(state)
            return _response(
                f"好，只停止了「{display_label}」。原始大图里的其他题目和当前对话都已保留。",
                state,
                intent="a3_current_unit_cancelled",
            )
        if action == "continue_current":
            self.store.save(state)
            if state.phase == A3_PHASE_CROP_REQUIRED:
                selected = state.unit(state.selected_unit_id) or {}
                message = f"好，继续处理「{selected.get('display_label', '当前题')}」，请完成裁剪后提交。"
            elif state.phase == A3_PHASE_A2_ACTIVE:
                message = "好，继续处理当前题。你可以继续选择候选。"
            elif state.phase == A3_PHASE_WAIT_SELECTION:
                message = "好，继续当前图片，请选择一道题。"
            else:
                message = "好，继续当前操作。"
            return _response(message, state, intent="a3_continue_current")
        if action == "retry_current_stage":
            self.store.save(state)
            if not state.entry_route and self.image_triage_authority is not None:
                return self._route_persisted_image(
                    state,
                    Path(state.source_page_path),
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
            return self._retry_page_understanding(
                state,
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
            )
        if action == "select_unit":
            self.store.save(state)
            unit = next(
                (
                    item
                    for item in state.searchable_units
                    if int(item.get("page_index") or 0) == int(decision.question_index or 0)
                ),
                None,
            )
            if unit is None:
                return _response(
                    "原图中没有这个稳定题号，请从题目列表中选择。",
                    state,
                    intent="a3_unit_clarification",
                    code="CLARIFICATION_REQUIRED",
                )
            return self._select_locked(
                state,
                str(unit["unit_id"]),
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
            )
        if action == "greeting":
            self.store.save(state)
            return _response("你好，我正在处理这张多题图片。你可以说题号继续。", state, intent="greeting")
        if action == "small_talk":
            self.store.save(state)
            return _response("不客气。当前图片进度已经保留。", state, intent="small_talk")
        if action == "capability_help":
            self.store.save(state)
            return _response(
                "我可以按原图稳定题号选择题目、裁剪结构图并进入题库检索。"
                "取消时请说明是当前题还是整张图。",
                state,
                intent="capability_help",
            )
        self.store.save(state)
        return _response(
            "我还不能确定你想执行什么，请再说明一下。",
            state,
            intent="a3_unit_clarification",
            code="CLARIFICATION_REQUIRED",
        )

    @staticmethod
    def _cancel_scope_options(state: A3SessionState) -> list[str]:
        options: list[str] = []
        if state.selected_unit_id and state.phase in {A3_PHASE_CROP_REQUIRED, A3_PHASE_A2_ACTIVE}:
            options.append("cancel_current_unit")
        if not state.page_finished:
            options.append("finish_page")
        options.append("continue_current")
        return options

    @_capture_a3_response_snapshot
    def select_unit(
        self,
        session_id: str,
        unit_id: str,
        *,
        task_revision: int | None = None,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        clean = _clean_session_id(session_id)
        with self._lock(clean):
            state = self.store.load(clean)
            if state is None:
                return AgentResponse(
                    text="当前题目列表已失效，请重新上传题图。",
                    intent="stale_action",
                    protocol=RequestProtocol.from_code("STALE_ACTION").to_dict(),
                )
            self._bind_trace_state(state, identity_key=identity_key)
            if task_revision is not None and int(task_revision) != state.task_revision:
                return _response(
                    "这是上一张题图的选题操作，已经失效。请使用当前题目列表。",
                    state,
                    intent="stale_action",
                    code="STALE_ACTION",
                )
            return self._select_locked(
                state,
                str(unit_id or "").strip(),
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
            )

    @_capture_a3_response_snapshot
    def prepare_units(
        self,
        session_id: str,
        unit_ids: Sequence[str],
        *,
        task_revision: int | None = None,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        del request_id
        clean = _clean_session_id(session_id)
        requested = list(dict.fromkeys(str(value or "").strip() for value in unit_ids))
        if not requested or any(not value for value in requested):
            return AgentResponse(
                text="请至少选择一道要查询的题目。",
                intent="a3_prepare_required",
                protocol=RequestProtocol.from_code("CLARIFICATION_REQUIRED").to_dict(),
            )
        with self._lock(clean):
            self._ensure_budget(identity_key)
            state = self.store.load(clean)
            if state is None or not state.auto_crop_enabled:
                return AgentResponse(
                    text="当前自动裁剪任务已失效，请重新上传题图。",
                    intent="stale_action",
                    protocol=RequestProtocol.from_code("STALE_ACTION").to_dict(),
                )
            self._bind_trace_state(state, identity_key=identity_key)
            if task_revision is not None and int(task_revision) != state.task_revision:
                return _response(
                    "这是上一张题图的准备操作，已经失效。请使用当前题目列表。",
                    state,
                    intent="stale_action",
                    code="STALE_ACTION",
                )
            remaining_ids = {str(unit["unit_id"]) for unit in state.remaining_units}
            if state.phase not in {A3_PHASE_WAIT_SELECTION, A3_PHASE_CROP_REQUIRED} or any(
                value not in remaining_ids for value in requested
            ):
                return _response(
                    "所选题目已经失效，请从当前列表重新选择。",
                    state,
                    intent="stale_action",
                    code="STALE_ACTION",
                )

            ready, manual = self._prepare_units_locked(
                state,
                requested,
                identity_key=identity_key,
                progress=progress,
            )
            text = f"已准备 {len(requested)} 道题：{ready} 道可以直接检索"
            if manual:
                text += f"，{manual} 道需要人工裁剪"
            text += "。请选择一道继续。"
            return _response(text, state, intent="a3_units_prepared")

    def _prepare_units_locked(
        self,
        state: A3SessionState,
        requested: Sequence[str],
        *,
        identity_key: str,
        progress: ProgressReporter | None,
    ) -> tuple[int, int]:
        state.requested_unit_ids = list(requested)
        state.selected_unit_id = ""
        state.phase = A3_PHASE_AUTO_VALIDATING
        self.store.save(state)
        candidates = [
            unit_id
            for unit_id in requested
            if self._auto_crop_can_validate(state, unit_id)
        ]
        if progress is not None:
            progress(
                "a3_auto_validating",
                f"正在并发校验 {len(candidates)} 张自动裁图…"
                if candidates
                else "所选题目需要人工裁剪，正在准备…",
            )

        results: dict[str, dict[str, Any]] = {}
        if candidates:
            workers = min(self.auto_crop_max_workers, len(candidates))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    submit_with_trace_context(
                        executor,
                        self._validate_auto_crop,
                        state,
                        unit_id,
                        identity_key=identity_key,
                    ): unit_id
                    for unit_id in candidates
                }
                for completed, future in enumerate(as_completed(futures), start=1):
                    unit_id = futures[future]
                    try:
                        results[unit_id] = future.result()
                    except Exception as exc:  # noqa: BLE001 - isolate one crop failure.
                        results[unit_id] = {
                            "validation_status": "manual_required",
                            "external_load_status": "error",
                            "verification_checks": {},
                            "error_type": type(exc).__name__,
                        }
                    if progress is not None:
                        progress(
                            "a3_auto_validating",
                            f"已完成 {completed}/{len(candidates)} 张自动裁图校验…",
                        )

        for unit_id in requested:
            record = state.auto_crops.setdefault(unit_id, {})
            if unit_id in results:
                record.update(results[unit_id])
            elif record.get("validation_status") != "auto_ready":
                record["validation_status"] = "manual_required"
        state.phase = A3_PHASE_WAIT_SELECTION
        self.store.save(state)
        ready = sum(
            state.auto_crops.get(unit_id, {}).get("validation_status") == "auto_ready"
            for unit_id in requested
        )
        return ready, len(requested) - ready

    def _auto_crop_can_validate(self, state: A3SessionState, unit_id: str) -> bool:
        record = state.auto_crops.get(unit_id) or {}
        path = Path(str(record.get("path") or ""))
        return (
            record.get("grounding_status") == "auto_ready"
            and record.get("validation_status") != "auto_ready"
            and path.is_file()
        )

    def _validate_auto_crop(
        self,
        state: A3SessionState,
        unit_id: str,
        *,
        identity_key: str,
    ) -> dict[str, Any]:
        selected = state.unit(unit_id)
        record = state.auto_crops.get(unit_id) or {}
        crop_path = Path(str(record.get("path") or ""))
        if selected is None or not crop_path.is_file():
            return {
                "validation_status": "manual_required",
                "external_load_status": "not_run",
                "verification_checks": {},
                "error_type": "auto_crop_unavailable",
            }
        try:
            verdict = self._call_model(
                state,
                "a3_auto_crop_compare",
                lambda: self.crop_verifier.verify(
                    Path(state.source_page_path),
                    crop_path,
                    selected,
                    state.page_understanding,
                ),
                identity_key=identity_key,
            )
        except Exception as exc:  # noqa: BLE001 - automatic crop degrades to manual.
            return {
                "validation_status": "manual_required",
                "external_load_status": "not_run",
                "verification_checks": {},
                "error_type": type(exc).__name__,
            }
        checks = dict(verdict.checks)
        if not verdict.verified:
            return {
                "validation_status": "manual_required",
                "external_load_status": "not_run",
                "verification_checks": checks,
                "error_type": "crop_review_required",
            }
        if self.external_load_screen is None:
            return {
                "validation_status": "auto_ready",
                "external_load_status": "not_configured",
                "verification_checks": checks,
                "error_type": "",
            }
        try:
            load_status = str(
                self._call_model(
                    state,
                    "a3_auto_external_load_screen",
                    lambda: self.external_load_screen(crop_path),
                    identity_key=identity_key,
                )
            ).strip().lower()
        except Exception as exc:  # noqa: BLE001 - independent gate is fail-closed.
            return {
                "validation_status": "manual_required",
                "external_load_status": "error",
                "verification_checks": checks,
                "error_type": type(exc).__name__,
            }
        return {
            "validation_status": "auto_ready" if load_status == "yes" else "manual_required",
            "external_load_status": load_status,
            "verification_checks": checks,
            "error_type": "" if load_status == "yes" else "external_load_not_confirmed",
        }

    @_capture_a3_response_snapshot
    def handle_crop(
        self,
        session_id: str,
        bounds: Mapping[str, Any],
        *,
        unit_id: str = "",
        task_revision: int | None = None,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        clean = _clean_session_id(session_id)
        with self._lock(clean):
            self._ensure_budget(identity_key)
            state = self.store.load(clean)
            if state is None or state.phase != A3_PHASE_CROP_REQUIRED:
                return AgentResponse(
                    text="这次裁剪已失效，请从当前题目列表重新选择。",
                    intent="stale_action",
                    protocol=RequestProtocol.from_code("STALE_ACTION").to_dict(),
                )
            self._bind_trace_state(state, identity_key=identity_key)
            if (
                (task_revision is not None and int(task_revision) != state.task_revision)
                or (unit_id and str(unit_id).strip() != state.selected_unit_id)
            ):
                return _response(
                    "这是上一次选题的裁剪操作，已经失效。请在当前裁剪页重新提交。",
                    state,
                    intent="stale_action",
                    code="STALE_ACTION",
                )
            selected = state.unit(state.selected_unit_id)
            if selected is None:
                raise ValueError("selected A3 unit is unavailable")
            clean_bounds = _normalize_bounds(bounds)
            crop_path = self._crop_source(state, clean_bounds)
            state.crop_drafts[state.selected_unit_id] = {
                "path": str(crop_path),
                "bounds": clean_bounds,
            }
            state.phase = A3_PHASE_VERIFYING
            state.crop_review_required = False
            state.crop_review_code = ""
            state.crop_review_feedback = ""
            self.store.save(state)
            if progress is not None:
                progress("a3_verifying", "正在核对裁剪图和所选题目…")
            try:
                verdict = self._call_model(
                    state,
                    "a3_crop_compare",
                    lambda: self.crop_verifier.verify(
                        Path(state.source_page_path),
                        crop_path,
                        selected,
                        state.page_understanding,
                    ),
                    identity_key=identity_key,
                )
            except Exception as exc:  # noqa: BLE001 - preserve the crop draft for retry.
                state.phase = A3_PHASE_CROP_REQUIRED
                state.last_error = type(exc).__name__
                self.store.save(state)
                return _response(
                    "裁剪图已保留，但这次校验服务没有完成。请稍后再提交一次。",
                    state,
                    intent="a3_crop_error",
                    code="SERVICE_UNAVAILABLE",
                )
            if not verdict.verified:
                state.phase = A3_PHASE_CROP_REQUIRED
                state.crop_review_required = True
                state.crop_review_code = _crop_review_code(verdict)
                state.crop_review_feedback = A3_CROP_REVIEW_MESSAGES[
                    state.crop_review_code
                ]
                self.store.save(state)
                return _response(
                    state.crop_review_feedback,
                    state,
                    intent="a3_crop_review_required",
                    code="CLARIFICATION_REQUIRED",
                )

            if self.external_load_screen is not None:
                try:
                    load_verdict = str(
                        self._call_model(
                            state,
                            "a3_external_load_screen",
                            lambda: self.external_load_screen(crop_path),
                            identity_key=identity_key,
                        )
                    ).strip().lower()
                except Exception as exc:  # noqa: BLE001 - do not pass an unverified crop to A2.
                    state.phase = A3_PHASE_CROP_REQUIRED
                    state.crop_review_required = True
                    state.crop_review_code = "LOAD_CHECK_UNAVAILABLE"
                    state.crop_review_feedback = A3_CROP_REVIEW_MESSAGES[
                        state.crop_review_code
                    ]
                    state.last_error = type(exc).__name__
                    self.store.save(state)
                    return _response(
                        state.crop_review_feedback,
                        state,
                        intent="a3_crop_review_required",
                        code="CLARIFICATION_REQUIRED",
                    )
                if load_verdict != "yes":
                    state.phase = A3_PHASE_CROP_REQUIRED
                    state.crop_review_required = True
                    state.crop_review_code = "EXTERNAL_LOADS_NOT_FOUND"
                    state.crop_review_feedback = A3_CROP_REVIEW_MESSAGES[
                        state.crop_review_code
                    ]
                    state.last_error = "external_load_not_confirmed"
                    self.store.save(state)
                    return _response(
                        state.crop_review_feedback,
                        state,
                        intent="a3_crop_review_required",
                        code="CLARIFICATION_REQUIRED",
                    )

            if progress is not None:
                progress("a3_analyzing_unit", "校验通过，正在结合题干识别章节和荷载…")
            context_text = _question_context_text(selected)
            state.phase = A3_PHASE_A2_ACTIVE
            state.last_error = ""
            self.store.save(state)
            response = self.a2_runtime.handle_prechecked_image(
                clean,
                crop_path,
                context_text=context_text,
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
            )
            return self._after_a2_response(state, response)

    def current_image_path(self, session_id: str) -> Path | None:
        state = self.store.load(_clean_session_id(session_id))
        if state is None or not state.source_page_path:
            return None
        path = Path(state.source_page_path).resolve()
        return path if path.is_file() else None

    def current_crop_path(self, session_id: str, unit_id: str) -> Path | None:
        state = self.store.load(_clean_session_id(session_id))
        if state is None:
            return None
        clean_unit_id = str(unit_id or "").strip()
        draft = state.crop_drafts.get(clean_unit_id) or state.auto_crops.get(clean_unit_id) or {}
        path = Path(str(draft.get("path") or "")).resolve()
        crop_dir = (self.artifacts.session_dir(state.session_id) / "crops").resolve()
        return path if path.is_file() and path.parent == crop_dir else None

    def current_auto_crop_overlay_path(self, session_id: str) -> Path | None:
        state = self.store.load(_clean_session_id(session_id))
        if state is None or not state.auto_crop_overlay_path:
            return None
        path = Path(state.auto_crop_overlay_path).resolve()
        crop_dir = (self.artifacts.session_dir(state.session_id) / "crops").resolve()
        return path if path.is_file() and path.parent == crop_dir else None

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        clean = _clean_session_id(session_id)
        auto_prepare_all_enabled = bool(
            self.auto_prepare_all_units and self.auto_cropper is not None
        )
        state = self.store.load(clean)
        if state is None:
            return {
                "session_valid": False,
                "phase": "IDLE",
                "has_active_image": False,
                "task_revision": 0,
                "candidate_generation": "",
                "candidate_count": 0,
                "chapter": "",
                "search_id": "",
                "workflow_search_id": "",
                "image_route": "",
                "a3": {
                    "enabled": True,
                    "auto_prepare_all_enabled": auto_prepare_all_enabled,
                    "phase": A3_PHASE_IDLE,
                    "units": [],
                },
            }
        if state.entry_route == "A2":
            snapshot = dict(self.a2_runtime.session_snapshot(clean))
            snapshot["session_valid"] = True
            snapshot["has_active_image"] = bool(state.source_page_path)
            snapshot["image_route"] = "A2"
            snapshot["workflow_search_id"] = (
                state.workflow_search_id or state.current_search_id
            )
            snapshot["a3"] = {
                "enabled": False,
                "auto_prepare_all_enabled": auto_prepare_all_enabled,
                "phase": A3_PHASE_IDLE,
                "units": [],
            }
            return snapshot
        if state.entry_route != "A3":
            return {
                "session_valid": True,
                "phase": state.phase,
                "has_active_image": bool(state.source_page_path),
                "task_revision": state.task_revision,
                "candidate_generation": "",
                "candidate_count": 0,
                "chapter": "",
                "search_id": state.current_search_id,
                "workflow_search_id": state.workflow_search_id or state.current_search_id,
                "image_route": state.entry_route,
                "a3": {
                    "enabled": False,
                    "auto_prepare_all_enabled": auto_prepare_all_enabled,
                    "phase": A3_PHASE_IDLE,
                    "units": [],
                },
            }
        child = self.a2_runtime.session_snapshot(clean)
        if state.phase != A3_PHASE_A2_ACTIVE:
            child = {
                "session_valid": True,
                "phase": state.phase,
                "has_active_image": bool(state.source_page_path),
                "task_revision": state.task_revision,
                "candidate_generation": "",
                "candidate_count": 0,
                "chapter": "",
                "search_id": state.current_search_id,
            }
        snapshot = dict(child)
        snapshot["session_valid"] = True
        snapshot["has_active_image"] = bool(state.source_page_path)
        snapshot["image_route"] = "A3"
        snapshot["workflow_search_id"] = state.workflow_search_id or state.current_search_id
        snapshot["a3"] = self._a3_snapshot(state)
        return snapshot

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        clean = _clean_session_id(session_id)
        if self.store.load(clean) is None:
            return None
        return self.artifacts.resolve_upload(clean, filename)

    def persist_media(self, session_id: str, source: str | Path) -> Path | None:
        clean = _clean_session_id(session_id)
        if self.store.load(clean) is None:
            return None
        persisted = self.a2_runtime.persist_media(clean, source)
        if persisted is not None:
            return persisted
        return self.artifacts.persist_media(clean, source)

    def mark_media_delivery_failed(
        self,
        session_id: str,
        *,
        expected_unit_id: str = "",
        expected_task_revision: int = 0,
        expected_candidate_generation: str = "",
        kind: str = "answer",
        snapshot_target: dict[str, object] | None = None,
    ) -> bool:
        """Keep the active A3 unit reopenable when response media was not delivered."""

        clean = _clean_session_id(session_id)
        with self._lock(clean):
            state = self.store.load(clean)
            if state is None or state.entry_route != "A3" or not state.selected_unit_id:
                return False
            if expected_unit_id and state.selected_unit_id != str(expected_unit_id):
                return False
            if expected_task_revision and state.task_revision != int(expected_task_revision):
                return False
            if expected_candidate_generation:
                child = self.a2_runtime.session_snapshot(clean)
                if str(child.get("candidate_generation") or "") != str(
                    expected_candidate_generation
                ):
                    return False
            if state.selected_unit_id in state.completed_unit_ids:
                state.completed_unit_ids = [
                    value
                    for value in state.completed_unit_ids
                    if value != state.selected_unit_id
                ]
            state.phase = A3_PHASE_A2_ACTIVE
            state.page_finished = False
            state.last_error = (
                "candidate_media_delivery_failed"
                if str(kind).strip().lower() == "candidates"
                else "answer_media_delivery_failed"
            )
            self.store.save(state)
            if snapshot_target is not None:
                snapshot_target.update(self.session_snapshot(clean))
            return True

    def resolve_media(self, session_id: str, filename: str) -> Path | None:
        clean = _clean_session_id(session_id)
        if self.store.load(clean) is None:
            return None
        persisted = self.a2_runtime.resolve_media(clean, filename, allow_preserved=True)
        if persisted is not None:
            return persisted
        return self.artifacts.resolve_media(clean, filename)

    def record_protocol_event(self, *args: Any, **kwargs: Any) -> None:
        self.a2_runtime.record_protocol_event(*args, **kwargs)

    def clear(self, session_id: str) -> None:
        clean = _clean_session_id(session_id)
        with self._lock(clean):
            self._clear_locked(clean)

    def purge_expired(self) -> None:
        expired = self.store.purge_expired()
        if expired:
            self.artifacts.clear_sessions(expired)
            for session_id in expired:
                self.a2_runtime.clear(session_id)
        self.a2_runtime.purge_expired()

    def _route_persisted_image(
        self,
        state: A3SessionState,
        persisted: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
    ) -> AgentResponse:
        self._bind_trace_state(state, identity_key=identity_key)
        record_trace_event(
            "stage_started",
            stage="image_routing",
            outcome="started",
            safe_attributes={"operation": "route_image"},
        )
        if self.image_triage_authority is not None:
            if progress is not None:
                progress("triage", "正在检查图片并决定处理路线…")
            try:
                decision = self._call_model(
                    state,
                    "image_triage",
                    lambda: self.image_triage_authority.decide_for_full_flow(persisted),
                    identity_key=identity_key,
                )
            except Exception as exc:  # noqa: BLE001 - normalize the model boundary.
                state.entry_route = ""
                state.phase = A3_PHASE_ERROR
                state.last_error = type(exc).__name__
                self.store.save(state)
                self._record_image_route_error(state, exc, identity_key=identity_key)
                raise AgentProtocolError(
                    "图片检查暂时失败，请稍后重试。",
                    code="SERVICE_UNAVAILABLE",
                ) from exc

            state.entry_route = decision.handoff.route
            state.last_error = ""
            if state.entry_route == "A1":
                state.phase = A3_PHASE_COMPLETE
                self.store.save(state)
                self._record_image_route(state, "A1", identity_key=identity_key)
                return AgentResponse(
                    text=decision.reply or "这张图片目前不适合进入结构力学题库检索，请重新上传完整清晰的题目图。",
                    state={"phase": state.phase, "current_route": "A1"},
                    intent="image_triage_stop",
                    reply_source=decision.reply_source,
                    fallback_reason=decision.fallback_reason,
                    protocol=RequestProtocol.from_code(
                        "TRIAGE_A1_STOPPED",
                        search_id=state.current_search_id,
                    ).to_dict(),
                )
            if state.entry_route == "A2":
                if self.external_load_screen is not None:
                    try:
                        load_verdict = str(
                            self._call_model(
                                state,
                                "a3_external_load_screen",
                                lambda: self.external_load_screen(persisted),
                                identity_key=identity_key,
                            )
                        ).strip().lower()
                    except Exception as exc:  # noqa: BLE001 - do not search without the gate.
                        state.entry_route = ""
                        state.phase = A3_PHASE_ERROR
                        state.last_error = type(exc).__name__
                        self.store.save(state)
                        self._record_image_route_error(
                            state,
                            exc,
                            identity_key=identity_key,
                        )
                        raise AgentProtocolError(
                            "外荷载筛查暂时失败，请稍后重试。",
                            code="SERVICE_UNAVAILABLE",
                        ) from exc
                    if load_verdict != "yes":
                        state.entry_route = "A1"
                        state.phase = A3_PHASE_COMPLETE
                        state.last_error = "external_load_not_confirmed"
                        self.store.save(state)
                        self._record_image_route(state, "A1", identity_key=identity_key)
                        return AgentResponse(
                            text=NO_EXTERNAL_LOAD_REPLY,
                            state={"phase": state.phase, "current_route": "A1"},
                            intent="image_triage_stop",
                            protocol=RequestProtocol.from_code(
                                "TRIAGE_A1_STOPPED",
                                search_id=state.current_search_id,
                            ).to_dict(),
                        )
                state.phase = A3_PHASE_A2_ACTIVE
                self.store.save(state)
                self._record_image_route(state, "A2", identity_key=identity_key)
                if progress is not None:
                    progress("searching", "图片适合直接检索，正在识别题目信息…")
                response = self.a2_runtime.handle_prechecked_image(
                    state.session_id,
                    persisted,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
                child = self.a2_runtime.session_snapshot(state.session_id)
                state.current_search_id = str(child.get("search_id") or state.current_search_id)
                self.store.save(state)
                self._bind_trace_state(state, identity_key=identity_key)
                return response

        state.entry_route = "A3"
        state.phase = A3_PHASE_UNDERSTANDING
        self.store.save(state)
        self._record_image_route(state, "A3", identity_key=identity_key)
        return self._understand_page(
            state,
            persisted,
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
        )

    def _understand_page(
        self,
        state: A3SessionState,
        persisted: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
    ) -> AgentResponse:
        if progress is not None:
            progress("a3_understanding", "正在理解整页题目和图形关系…")
        try:
            understanding = self._call_model(
                state,
                "a3_page_understanding",
                lambda: self._observe_page(
                    state,
                    persisted,
                    task_kind="a3_page_understanding",
                ),
                identity_key=identity_key,
            )
        except Exception as exc:  # noqa: BLE001 - keep the upload available for retry.
            state.phase = A3_PHASE_ERROR
            state.last_error = type(exc).__name__
            self._record_page_error(state, exc, task_kind="a3_page_understanding")
            self.store.save(state)
            return _response(
                "这次没能完成整页理解。题图已保留，你可以直接回复“重试”。",
                state,
                intent="a3_page_error",
                code="SERVICE_UNAVAILABLE",
            )

        return self._finish_page_understanding(
            state,
            understanding,
            persisted,
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
            retry=False,
        )

    def _retry_page_understanding(
        self,
        state: A3SessionState,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
    ) -> AgentResponse:
        if progress is not None:
            progress("a3_understanding", "正在重新理解整页题目…")
        try:
            understanding = self._call_model(
                state,
                "a3_page_understanding_retry",
                lambda: self._observe_page(
                    state,
                    Path(state.source_page_path),
                    task_kind="a3_page_understanding_retry",
                ),
                identity_key=identity_key,
            )
        except A3ModelError as exc:
            state.last_error = type(exc).__name__
            self._record_page_error(state, exc, task_kind="a3_page_understanding_retry")
            self.store.save(state)
            return _response(
                "这次仍然没能完成整页理解。题图还在，可以稍后继续重试。",
                state,
                intent="a3_page_error",
                code="SERVICE_UNAVAILABLE",
            )
        return self._finish_page_understanding(
            state,
            understanding,
            Path(state.source_page_path),
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
            retry=True,
        )

    def _finish_page_understanding(
        self,
        state: A3SessionState,
        understanding: Any,
        persisted: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
        retry: bool,
    ) -> AgentResponse:
        state.page_understanding = understanding.to_dict(include_derived=True)
        state.units = _flatten_units(state.page_understanding)
        state.selected_unit_id = ""
        state.requested_unit_ids = []
        state.auto_crop_page = {}
        state.auto_crops = {}
        state.auto_crop_overlay_path = ""
        state.last_error = ""
        state.last_error_detail = ""
        searchable = state.searchable_units
        if not searchable:
            state.phase = A3_PHASE_COMPLETE
            self.store.save(state)
            text = (
                "这张图里仍然没有识别到可以进入搜题的完整结构题。"
                if retry
                else "我理解了这张图，但没有找到结构、支座和外荷载都清晰完整的可检索题目。"
            )
            return _response(text, state, intent="a3_page_ready")

        if self.auto_cropper is not None:
            self._ground_auto_crops(
                state,
                persisted,
                identity_key=identity_key,
                progress=progress,
            )
            if state.auto_crop_enabled:
                if len(searchable) == 1:
                    unit_id = str(searchable[0]["unit_id"])
                    self._prepare_units_locked(
                        state,
                        [unit_id],
                        identity_key=identity_key,
                        progress=progress,
                    )
                    return self._select_locked(
                        state,
                        unit_id,
                        identity_key=identity_key,
                        progress=progress,
                        request_id=request_id,
                    )
                if self.auto_prepare_all_units:
                    unit_ids = [str(unit["unit_id"]) for unit in searchable]
                    ready, manual = self._prepare_units_locked(
                        state,
                        unit_ids,
                        identity_key=identity_key,
                        progress=progress,
                    )
                    text = f"已准备 {len(unit_ids)} 道题：{ready} 道可以直接检索"
                    if manual:
                        text += f"，{manual} 道需要人工裁剪"
                    text += "。请选择一道继续。"
                    return _response(text, state, intent="a3_units_prepared")
                state.phase = A3_PHASE_WAIT_SELECTION
                self.store.save(state)
                text = (
                    f"已经识别并定位到 {len(searchable)} 道可处理题目。"
                    "请选择要查询的题目，我只校验你选中的裁图。"
                )
                return _response(text, state, intent="a3_auto_crops_ready")

        if len(searchable) == 1:
            state.selected_unit_id = str(searchable[0]["unit_id"])
            state.phase = A3_PHASE_CROP_REQUIRED
            text = (
                f"已经识别到「{searchable[0]['display_label']}」，接下来裁剪它的单个结构图。"
                if retry
                else f"我在这张图里识别到 1 道可以处理的题："
                f"「{searchable[0]['display_label']}」。已经为你选中，接下来裁剪它的单个结构图。"
            )
        else:
            state.phase = A3_PHASE_WAIT_SELECTION
            text = (
                f"这次识别到 {len(searchable)} 道可以处理的题。请先选一道。"
                if retry
                else f"我在这张图里识别到 {len(searchable)} 道可以处理的题。"
                "先选一道，我再带你裁剪对应的结构图。"
            )
        self.store.save(state)
        return _response(text, state, intent="a3_page_ready")

    def _ground_auto_crops(
        self,
        state: A3SessionState,
        persisted: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
    ) -> None:
        if self.auto_cropper is None:
            return
        state.phase = A3_PHASE_AUTO_GROUNDING
        self.store.save(state)
        if progress is not None:
            progress("a3_auto_grounding", "正在一次定位整页所有可检索结构图…")
        try:
            page = self._call_model(
                state,
                "a3_auto_crop_grounding",
                lambda: self.auto_cropper.ground(
                    persisted,
                    state.searchable_units,
                    state.page_understanding,
                ),
                identity_key=identity_key,
            )
        except Exception as exc:  # noqa: BLE001 - automatic crop always degrades to manual.
            state.auto_crop_enabled = False
            state.auto_crop_page = {
                "schema_version": "a3-page-crops-v1",
                "page_status": "manual_required",
                "unknowns": ["grounding_error"],
            }
            state.auto_crops = {
                str(unit["unit_id"]): {
                    "grounding_status": "error",
                    "validation_status": "manual_required",
                    "reason_codes": ["grounding_error"],
                    "error_type": type(exc).__name__,
                }
                for unit in state.searchable_units
            }
            self._record_page_error(state, exc, task_kind="a3_auto_crop_grounding")
            state.last_error = ""
            return

        state.auto_crop_page = {
            "schema_version": page.schema_version,
            "page_status": page.page_status,
            "unknowns": list(page.unknowns),
        }
        records: dict[str, dict[str, Any]] = {}
        for target in page.targets:
            crop_bbox = (
                expand_normalized_bbox(target.bbox)
                if target.bbox is not None
                else None
            )
            bounds = (
                normalized_bbox_to_bounds(crop_bbox)
                if crop_bbox is not None
                else {}
            )
            record: dict[str, Any] = {
                "target_id": target.target_id,
                "unit_id": target.unit_id,
                "question_label": target.question_label,
                "model_bbox": list(target.bbox) if target.bbox is not None else None,
                "bbox": list(crop_bbox) if crop_bbox is not None else None,
                "bounds": bounds,
                "grounding_status": target.status,
                "validation_status": (
                    "pending" if target.status == "auto_ready" else "manual_required"
                ),
                "reason_codes": list(target.reason_codes),
                "binding_evidence": target.binding_evidence,
                "verification_checks": {},
                "external_load_status": "not_run",
            }
            if bounds:
                try:
                    crop_path = self._crop_source_for_unit(state, target.unit_id, bounds)
                    record["path"] = str(crop_path)
                except Exception as exc:  # noqa: BLE001 - isolate one invalid crop.
                    record["validation_status"] = "manual_required"
                    record["reason_codes"] = [*record["reason_codes"], "crop_write_error"]
                    record["error_type"] = type(exc).__name__
            records[target.unit_id] = record
        state.auto_crops = records
        has_bounds = any(record.get("bounds") for record in records.values())
        if has_bounds:
            try:
                state.auto_crop_overlay_path = str(self._write_auto_crop_overlay(state))
            except Exception:  # noqa: BLE001 - overlay is optional, clean crops remain usable.
                state.auto_crop_overlay_path = ""
        if page.page_status == "manual_required":
            state.auto_crop_enabled = False
            return

    def _select_locked(
        self,
        state: A3SessionState,
        unit_id: str,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        unit = state.unit(unit_id)
        if state.page_finished:
            return _response(
                "这张图的流程已经结束，请上传新题图。",
                state,
                intent="stale_action",
                code="STALE_ACTION",
            )
        if unit_id in state.completed_unit_ids:
            return _response(
                "这道原图题目已经处理完成，没有改选其他题目。",
                state,
                intent="stale_action",
                code="STALE_ACTION",
            )
        if unit_id in state.searched_unit_ids:
            return _response(
                "这道原图题目已经停止或搜索过，没有改选其他题目。",
                state,
                intent="stale_action",
                code="STALE_ACTION",
            )
        if (
            state.phase not in {
                A3_PHASE_WAIT_SELECTION,
                A3_PHASE_CROP_REQUIRED,
                A3_PHASE_A2_ACTIVE,
            }
            or
            unit is None
            or unit.get("searchability") != "searchable_candidate"
        ):
            return _response(
                "这道题当前不能选择，请从剩余题目中选一道。",
                state,
                intent="stale_action",
                code="STALE_ACTION",
            )
        if unit_id == state.selected_unit_id:
            return _response(
                f"当前正在处理「{unit['display_label']}」，已有进度已保留。",
                state,
                intent="a3_unit_already_selected",
            )
        state.pending_intent_clarification = {}
        switching_from_a2 = state.phase == A3_PHASE_A2_ACTIVE
        if switching_from_a2:
            self.a2_runtime.clear(state.session_id, preserve_artifacts=True)
            previous_unit_id = state.selected_unit_id
            if (
                previous_unit_id
                and previous_unit_id not in state.completed_unit_ids
                and previous_unit_id not in state.searched_unit_ids
            ):
                state.searched_unit_ids.append(previous_unit_id)
        state.selected_unit_id = unit_id
        self._bind_trace_state(state, identity_key=identity_key)
        auto_record = state.auto_crops.get(unit_id) or {}
        if state.auto_crop_enabled and auto_record.get("validation_status") == "auto_ready":
            crop_path = Path(str(auto_record.get("path") or ""))
            if crop_path.is_file():
                state.crop_drafts[unit_id] = {
                    "path": str(crop_path),
                    "bounds": dict(auto_record.get("bounds") or {}),
                    "source": "auto",
                }
                state.phase = A3_PHASE_A2_ACTIVE
                state.crop_review_required = False
                state.crop_review_code = ""
                state.crop_review_feedback = ""
                state.last_error = ""
                self.store.save(state)
                if progress is not None:
                    progress("searching", "自动裁图已通过校验，正在进入题库检索…")
                response = self.a2_runtime.handle_prechecked_image(
                    state.session_id,
                    crop_path,
                    context_text=_question_context_text(unit),
                    identity_key=identity_key,
                    progress=progress,
                    request_id=request_id,
                )
                return self._after_a2_response(state, response)

        state.phase = A3_PHASE_CROP_REQUIRED
        state.crop_review_required = False
        state.crop_review_code = ""
        state.crop_review_feedback = ""
        state.last_error = ""
        auto_bounds = dict(auto_record.get("bounds") or {})
        auto_path = str(auto_record.get("path") or "")
        if auto_bounds:
            state.crop_drafts[unit_id] = {
                "path": auto_path,
                "bounds": auto_bounds,
                "source": "auto_suggestion",
            }
        self.store.save(state)
        return _response(
            (
                f"好，已停止当前题，改查「{unit['display_label']}」。"
                "请裁剪它对应的单个结构图。"
                if switching_from_a2
                else (
                    f"「{unit['display_label']}」的自动裁图需要人工确认。请调整裁剪范围后提交。"
                    if state.auto_crop_enabled
                    else f"好，先查「{unit['display_label']}」。请裁剪它对应的单个结构图。"
                )
            ),
            state,
            intent="a3_unit_selected",
        )

    def _return_to_unit_selection_locked(self, state: A3SessionState) -> AgentResponse:
        self.a2_runtime.clear(state.session_id, preserve_artifacts=True)
        previous_unit_id = state.selected_unit_id
        if (
            previous_unit_id
            and previous_unit_id not in state.completed_unit_ids
            and previous_unit_id not in state.searched_unit_ids
        ):
            state.searched_unit_ids.append(previous_unit_id)
        state.selected_unit_id = ""
        remaining = state.remaining_units
        state.phase = A3_PHASE_WAIT_SELECTION if remaining else A3_PHASE_COMPLETE
        state.crop_review_required = False
        state.crop_review_code = ""
        state.crop_review_feedback = ""
        state.last_error = ""
        self.store.save(state)
        return _response(
            (
                f"好，已停止当前题。这张图里还有 {len(remaining)} 道可以选择。"
                if remaining
                else "好，已停止当前题。这张图里没有其他待处理题目。"
            ),
            state,
            intent="a3_reselect" if remaining else "a3_complete",
        )

    def _after_a2_response(
        self,
        state: A3SessionState,
        response: AgentResponse,
    ) -> AgentResponse:
        self._bind_trace_state(state)
        child_snapshot = dict(self.a2_runtime.session_snapshot(state.session_id))
        child_snapshot["session_valid"] = True
        child_snapshot["has_active_image"] = bool(state.source_page_path)
        child_snapshot["image_route"] = "A3"
        child_snapshot["workflow_search_id"] = (
            state.workflow_search_id or state.current_search_id
        )

        def bind_response_snapshot() -> None:
            child_snapshot["phase"] = state.phase
            child_snapshot["task_revision"] = state.task_revision
            child_snapshot["a3"] = self._a3_snapshot(state)
            response.response_projection_snapshot = dict(child_snapshot)

        child_phase = str(response.state.get("phase") or "")
        response_media_kind = str(
            getattr(response, "media_kind", "") or ""
        ).strip().lower()
        if response_media_kind not in {"candidates", "answer"}:
            response_media_kind = ""
            if response.images:
                response_media_kind = (
                    "answer"
                    if response.intent in {"select_candidate", "resend_answer"}
                    else "candidates"
                )
        if response_media_kind:
            child = self.a2_runtime.session_snapshot(state.session_id)
            response.state["_a3_media_guard"] = {
                "unit_id": state.selected_unit_id,
                "task_revision": state.task_revision,
                "candidate_generation": str(child.get("candidate_generation") or ""),
            }
        if response.intent == "cancel" or child_phase == "CANCELLED":
            state.selected_unit_id = ""
            remaining = state.remaining_units
            state.phase = A3_PHASE_WAIT_SELECTION if remaining else A3_PHASE_COMPLETE
            response.state["phase"] = state.phase
            response.text = (
                f"好，已停止这道题。这张图里还有 {len(remaining)} 道可以选择。"
                if remaining
                else "好，已停止这道题。这张图里没有其他待处理题目。"
            )
            self.store.save(state)
            bind_response_snapshot()
            return response
        if child_phase != "ANSWERED":
            state.phase = A3_PHASE_A2_ACTIVE
            selected = state.unit(state.selected_unit_id) or {}
            display_label = str(selected.get("display_label") or "").strip()
            if child_phase == "WAIT_CHAPTER" and display_label:
                response.text = response.text.replace("这题", f"「{display_label}」", 1)
            candidate_count = int(response.state.get("candidate_count") or 0)
            if (
                child_phase == "WAIT_CANDIDATE_CHOICE"
                and response_media_kind == "candidates"
                and display_label
                and candidate_count
            ):
                state.last_error = ""
                notice = ""
                marker = "\n\n提示："
                if marker in response.text:
                    _base, _separator, suffix = response.text.partition(marker)
                    notice = marker + suffix
                response.text = (
                    f"我从题库里找到了与「{display_label}」最相似的一道题。你看看是不是这道。"
                    if candidate_count == 1
                    else f"我从题库里找到了与「{display_label}」相似的 {candidate_count} 道题，已按相似度排序。"
                ) + notice
            self.store.save(state)
            bind_response_snapshot()
            return response
        selected = state.unit(state.selected_unit_id) or {}
        display_label = str(selected.get("display_label") or "").strip()
        if display_label and response.intent in {"select_candidate", "resend_answer"}:
            response.text = f"「{display_label}」的题库答案找到了，已经发给你。"
        if state.selected_unit_id and state.selected_unit_id not in state.completed_unit_ids:
            state.completed_unit_ids.append(state.selected_unit_id)
        state.last_error = ""
        remaining = state.remaining_units
        if remaining:
            state.phase = A3_PHASE_WAIT_SELECTION
            response.text = (
                response.text.rstrip()
                + f"这道题处理好了，这张图里还有 {len(remaining)} 道可以继续查。"
            )
        else:
            state.phase = A3_PHASE_COMPLETE
            response.text = response.text.rstrip() + "这张图里的可处理题目已经全部完成。"
        self.store.save(state)
        bind_response_snapshot()
        return response

    def _crop_source(self, state: A3SessionState, bounds: dict[str, float]) -> Path:
        return self._crop_source_for_unit(state, state.selected_unit_id, bounds)

    def _crop_source_for_unit(
        self,
        state: A3SessionState,
        unit_id: str,
        bounds: Mapping[str, Any],
    ) -> Path:
        source = Path(state.source_page_path)
        target_dir = self.artifacts.session_dir(state.session_id) / "crops"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_id = sha256(str(unit_id).encode("utf-8")).hexdigest()[:20]
        target = target_dir / f"{safe_id}.jpg"
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            left = max(0, min(width - 1, round(float(bounds["x"]) * width)))
            top = max(0, min(height - 1, round(float(bounds["y"]) * height)))
            right = max(
                left + 1,
                min(width, round((float(bounds["x"]) + float(bounds["width"])) * width)),
            )
            bottom = max(
                top + 1,
                min(height, round((float(bounds["y"]) + float(bounds["height"])) * height)),
            )
            image.crop((left, top, right, bottom)).save(
                target,
                format="JPEG",
                quality=94,
                optimize=True,
            )
        return target.resolve()

    def _write_auto_crop_overlay(self, state: A3SessionState) -> Path:
        source = Path(state.source_page_path)
        target_dir = self.artifacts.session_dir(state.session_id) / "crops"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "a3_auto_crop_overlay.jpg"
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        draw = ImageDraw.Draw(image)
        width, height = image.size
        line_width = max(2, round(min(width, height) * 0.004))
        for unit in state.searchable_units:
            record = state.auto_crops.get(str(unit.get("unit_id") or "")) or {}
            bounds = record.get("bounds") or {}
            if not bounds:
                continue
            x1 = round(float(bounds["x"]) * width)
            y1 = round(float(bounds["y"]) * height)
            x2 = round((float(bounds["x"]) + float(bounds["width"])) * width)
            y2 = round((float(bounds["y"]) + float(bounds["height"])) * height)
            ready = record.get("grounding_status") == "auto_ready"
            color = "#159447" if ready else "#c17a00"
            draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
            label = f"Q{int(unit.get('page_index') or 0)} {unit.get('display_label') or ''}".strip()
            label_y = max(0, y1 - max(16, line_width * 5))
            draw.rectangle(
                (x1, label_y, min(width, x1 + max(54, len(label) * 9)), y1),
                fill=color,
            )
            draw.text((x1 + 4, label_y + 2), label, fill="white")
        image.save(target, format="JPEG", quality=92, optimize=True)
        return target.resolve()

    def _a3_snapshot(self, state: A3SessionState) -> dict[str, Any]:
        completed = set(state.completed_unit_ids)
        searched = set(state.searched_unit_ids)
        requested = set(state.requested_unit_ids)
        remaining = state.remaining_units
        auto_prepare_all_active = (
            self.auto_prepare_all_units
            and state.auto_crop_enabled
            and bool(requested)
            and all(str(item["unit_id"]) in requested for item in remaining)
        )
        units = []
        for item in state.units:
            if item.get("searchability") != "searchable_candidate":
                continue
            auto = state.auto_crops.get(str(item["unit_id"])) or {}
            units.append({
                "unit_id": item["unit_id"],
                "page_index": int(item.get("page_index") or 0),
                "display_label": item["display_label"],
                "title_text": item.get("title_text") or "",
                "completed": item["unit_id"] in completed,
                "searched": item["unit_id"] in searched,
                "selected": item["unit_id"] == state.selected_unit_id,
                "requested": item["unit_id"] in state.requested_unit_ids,
                "grounding_status": str(auto.get("grounding_status") or ""),
                "validation_status": str(auto.get("validation_status") or ""),
                "crop_available": bool(auto.get("path")),
                "auto_bounds": dict(auto.get("bounds") or {}),
                "reason_codes": list(auto.get("reason_codes") or []),
            })
        selected = state.unit(state.selected_unit_id) or {}
        draft = state.crop_drafts.get(state.selected_unit_id) or {}
        return {
            "enabled": True,
            "auto_crop_enabled": state.auto_crop_enabled,
            "auto_prepare_all_enabled": bool(
                self.auto_prepare_all_units and state.auto_crop_enabled
            ),
            "auto_prepare_all_units": auto_prepare_all_active,
            "phase": state.phase,
            "intent_v1_enabled": self.intent_engine is not None,
            "page_finished": state.page_finished,
            "pending_intent_clarification": dict(state.pending_intent_clarification),
            "last_intent": dict(state.last_a3_intent),
            "units": units,
            "selected_unit": {
                "unit_id": selected.get("unit_id", ""),
                "display_label": selected.get("display_label", ""),
                "context_text": _question_context_text(selected),
            },
            "completed_unit_ids": list(state.completed_unit_ids),
            "searched_unit_ids": list(state.searched_unit_ids),
            "remaining_count": len(remaining),
            "requested_unit_ids": list(state.requested_unit_ids),
            "auto_crop_page_status": str(state.auto_crop_page.get("page_status") or ""),
            "auto_crop_overlay_available": bool(state.auto_crop_overlay_path),
            "crop_review_required": state.crop_review_required,
            "crop_review_code": state.crop_review_code,
            "crop_review_feedback": state.crop_review_feedback,
            "crop_draft": {
                "bounds": dict(draft.get("bounds") or {}),
                "available": bool(draft.get("path")),
            },
            "task_revision": state.task_revision,
        }

    def _clear_locked(self, session_id: str, *, preserve_artifacts: bool = False) -> None:
        self.store.clear(session_id)
        if not preserve_artifacts:
            self.artifacts.clear_session(session_id)
        self.a2_runtime.clear(session_id, preserve_artifacts=preserve_artifacts)

    def _bind_trace_state(
        self,
        state: A3SessionState,
        *,
        identity_key: str = "",
    ) -> None:
        child_search_id = ""
        if state.entry_route == "A2" or state.phase == A3_PHASE_A2_ACTIVE:
            try:
                child = self.a2_runtime.session_snapshot(state.session_id)
                child_search_id = str(child.get("search_id") or "").strip()
            except Exception:  # noqa: BLE001 - observability must not affect A3.
                child_search_id = ""
        bind_trace_event_dimensions(
            session_key=session_key(state.session_id),
            workflow_search_id=state.workflow_search_id or state.current_search_id,
        )
        bind_trace_event_dimensions(search_id=child_search_id)
        bind_trace_event_dimensions(unit_id=state.selected_unit_id)
        if str(identity_key or "").strip():
            bind_trace_event_dimensions(identity_key=str(identity_key).strip())

    def _record_image_route(
        self,
        state: A3SessionState,
        route: str,
        *,
        identity_key: str,
    ) -> None:
        self._bind_trace_state(state, identity_key=identity_key)
        route_outcome = "rejected" if route == "A1" else "success"
        record_trace_event(
            "route_decided",
            stage="image_routing",
            outcome=route_outcome,
            safe_attributes={"route": route},
        )
        record_trace_event(
            "stage_finished",
            stage="image_routing",
            outcome="success",
            safe_attributes={"operation": "route_image", "completed": True},
        )

    def _record_image_route_error(
        self,
        state: A3SessionState,
        exc: BaseException,
        *,
        identity_key: str,
    ) -> None:
        self._bind_trace_state(state, identity_key=identity_key)
        record_trace_event(
            "stage_finished",
            stage="image_routing",
            outcome="error",
            safe_attributes={
                "operation": "route_image",
                "completed": False,
                "error_kind": type(exc).__name__,
            },
        )

    def _call_model(
        self,
        state: A3SessionState,
        task_kind: str,
        function: Callable[[], Any],
        *,
        identity_key: str = "",
    ) -> Any:
        collector = ModelCostCollector(
            run_id=new_run_id(),
            trace_id=current_trace_id(),
            session_key=session_key(state.session_id),
            identity_key=str(identity_key).strip(),
            search_key=state.current_search_id,
            task_kind=task_kind,
        )
        outcome = "error"
        try:
            with model_cost_scope(collector):
                result = function()
            outcome = "success"
            return result
        finally:
            if self.cost_ledger is not None:
                try:
                    self.cost_ledger.write_run(
                        collector,
                        finished_at=datetime.now(UTC).isoformat(),
                        outcome=outcome,
                    )
                except Exception:  # noqa: BLE001 - observability must not break A3.
                    pass

    def _ensure_budget(self, identity_key: str) -> None:
        ensure_budget = getattr(self.a2_runtime, "ensure_budget_available", None)
        if callable(ensure_budget):
            ensure_budget(identity_key)

    def _record_page_error(
        self,
        state: A3SessionState,
        exc: Exception,
        *,
        task_kind: str,
    ) -> None:
        diagnostic = _page_error_diagnostic(exc)
        state.last_error_detail = _page_error_summary(diagnostic)
        try:
            self.store.record_page_error(
                state,
                task_kind=task_kind,
                diagnostic=diagnostic,
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break A3.
            pass

    def _observe_page(
        self,
        state: A3SessionState,
        image_path: Path,
        *,
        task_kind: str,
    ):
        observe_with_diagnostics = getattr(
            self.page_observer,
            "observe_with_diagnostics",
            None,
        )
        if not callable(observe_with_diagnostics):
            return self.page_observer.observe(image_path)
        return observe_with_diagnostics(
            image_path,
            on_validation_error=lambda attempt, exc: self._record_page_validation_error(
                state,
                exc,
                task_kind=task_kind,
                attempt=attempt,
            ),
        )

    def _record_page_validation_error(
        self,
        state: A3SessionState,
        exc: Exception,
        *,
        task_kind: str,
        attempt: int,
    ) -> None:
        try:
            self.store.record_page_error(
                state,
                task_kind=f"{task_kind}_schema_attempt_{max(1, int(attempt))}",
                diagnostic=_page_error_diagnostic(exc),
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break A3.
            pass

    def _lock(self, session_id: str) -> threading.RLock:
        return self._locks[hash(session_id) % len(self._locks)]


def _page_error_diagnostic(exc: Exception) -> dict[str, str]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    specific = next(
        (item for item in chain if str(getattr(item, "code", "")).strip()),
        chain[-1],
    )
    code = str(getattr(specific, "code", "") or "").strip()
    message = re.sub(r"\s+", " ", str(specific)).strip() or "no error message"
    message = f"{type(specific).__name__}: {message}"[:500]
    return {
        "error_type": type(exc).__name__,
        "error_code": code,
        "error_message": message,
    }


def _page_error_summary(diagnostic: Mapping[str, str]) -> str:
    label = str(diagnostic.get("error_type") or "A3PageError")
    code = str(diagnostic.get("error_code") or "").strip()
    if code:
        label = f"{label}/{code}"
    return f"{label}: {diagnostic.get('error_message') or 'no error message'}"[:700]


def _flatten_units(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for group in page.get("groups") or []:
        if not isinstance(group, Mapping):
            continue
        parent_title_text = str(group.get("parent_title_text") or "").strip()
        for unit in group.get("units") or []:
            if isinstance(unit, Mapping):
                item = dict(unit)
                item["parent_title_text"] = parent_title_text
                item["a2_context_text"] = _question_context_text(item)
                units.append(item)
    _ensure_stable_page_indexes(units)
    _refresh_unlabelled_display_labels(units)
    return units


def _ensure_stable_page_indexes(units: list[dict[str, Any]]) -> None:
    """Bind each semantic page unit to its original, persistent page position."""

    for page_index, unit in enumerate(units, start=1):
        unit["page_index"] = page_index


def _refresh_unlabelled_display_labels(units: list[dict[str, Any]]) -> None:
    for ordinal, unit in enumerate(units, start=1):
        parent = str(unit.get("parent_question_label") or "").strip()
        child = str(unit.get("question_label") or "").strip()
        if not parent and not child:
            unit["display_label"] = f"未标号题{int(unit.get('page_index') or ordinal)}"


def _question_context_text(unit: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("parent_title_text", "shared_stem_text", "title_text"):
        value = str(unit.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


def _normalize_bounds(payload: Mapping[str, Any]) -> dict[str, float]:
    try:
        bounds = {name: float(payload[name]) for name in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("crop bounds are invalid") from exc
    if not all(0 <= bounds[name] <= 1 for name in bounds):
        raise ValueError("crop bounds must be normalized")
    if bounds["width"] < 0.02 or bounds["height"] < 0.02:
        raise ValueError("crop area is too small")
    if bounds["x"] + bounds["width"] > 1.000001 or bounds["y"] + bounds["height"] > 1.000001:
        raise ValueError("crop bounds exceed the source image")
    return {name: round(value, 6) for name, value in bounds.items()}


def _resolve_unit_selection(text: str, units: list[dict[str, Any]]) -> tuple[str, bool]:
    clean = re.sub(r"\s+", "", str(text or "")).strip("。！？,.!?")
    if not clean or not units:
        return "", False
    exact_matches = []
    for unit in units:
        values = {
            str(unit.get("unit_id") or "").replace(" ", ""),
            str(unit.get("display_label") or "").replace(" ", ""),
            str(unit.get("question_label") or "").replace(" ", ""),
        }
        if clean in values or clean in {f"第{value}题" for value in values if value}:
            exact_matches.append(str(unit["unit_id"]))
    if len(set(exact_matches)) == 1:
        return exact_matches[0], False
    if len(set(exact_matches)) > 1:
        return "", True

    contained_matches: set[str] = set()
    for unit in units:
        values = {
            str(unit.get("display_label") or "").replace(" ", ""),
            str(unit.get("question_label") or "").replace(" ", ""),
        }
        for value in values:
            if not value:
                continue
            pattern = rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z-])"
            if re.search(pattern, clean):
                contained_matches.add(str(unit["unit_id"]))
    if len(contained_matches) == 1:
        return next(iter(contained_matches)), False
    if len(contained_matches) > 1:
        return "", True

    numbers = [int(value) for value in re.findall(r"\d+", clean)]
    chinese = [value for char, value in _CHINESE_ORDINALS.items() if f"第{char}" in clean]
    ordinals = numbers or chinese
    if len(set(ordinals)) > 1:
        return "", True
    if len(ordinals) == 1 and re.fullmatch(r"(?:要|搜|查|选)?第?[0-9]+个?(?:题)?", clean):
        index = ordinals[0]
        if 1 <= index <= len(units):
            return str(units[index - 1]["unit_id"]), False
    if len(chinese) == 1 and re.fullmatch(r"(?:要|搜|查|选)?第[一二两三四五六七八九十]个?(?:题)?", clean):
        index = chinese[0]
        if 1 <= index <= len(units):
            return str(units[index - 1]["unit_id"]), False
    return "", False


def _is_a3_reselect_request(text: str) -> bool:
    clean = re.sub(r"[\s，,。！？!?]+", "", str(text or ""))
    if not clean:
        return False
    return bool(
        re.fullmatch(
            r"(?:算了)?(?:我)?(?:想|要)?(?:换|重选|重新选|切换)(?:一?道|个)?题(?:目)?(?:(?:重新|再)?搜)?(?:了)?(?:吧|呢|啊|呀)?",
            clean,
        )
        or re.fullmatch(
            r"(?:这|当前)(?:一?道|个)?题(?:目)?(?:不搜|不查|不要)(?:了)?(?:吧|呢|啊|呀)?",
            clean,
        )
    )


def _resolve_active_unit_selection(
    text: str,
    units: list[dict[str, Any]],
) -> tuple[str, bool]:
    """Resolve only explicit cross-question requests while an A2 child is active."""

    clean = re.sub(r"\s+", "", str(text or "")).strip("。！？,.!?")
    if not clean or not units:
        return "", False

    label_matches: set[str] = set()
    for unit in units:
        label = str(unit.get("display_label") or "").replace(" ", "")
        if not label:
            continue
        pattern = rf"(?<![0-9A-Za-z]){re.escape(label)}(?![0-9A-Za-z-])"
        if re.search(pattern, clean):
            label_matches.add(str(unit["unit_id"]))
    if len(label_matches) == 1:
        return next(iter(label_matches)), False
    if len(label_matches) > 1:
        return "", True

    original_prefix = re.fullmatch(
        r"(?:图片|原图)(?:中|里的?)?(第?[0-9]+(?:个|道)?题)",
        clean,
    )
    if original_prefix:
        return _resolve_unit_selection(original_prefix.group(1), units)

    if not re.search(r"(?:搜|查|选|换到|切换到)", clean):
        return "", False
    ordinal_matches = re.findall(r"第?([0-9]+)(?:个|道)?题", clean)
    indexes = {int(value) for value in ordinal_matches}
    if len(indexes) > 1:
        return "", True
    if len(indexes) == 1:
        index = next(iter(indexes))
        if 1 <= index <= len(units):
            return str(units[index - 1]["unit_id"]), False
    return "", False


def _active_bare_reference_rank(text: str) -> int | None:
    clean = re.sub(r"\s+", "", str(text or "")).strip("。！？,.!?")
    numeric = re.fullmatch(r"第?([0-9]+)(?:个|道)?题", clean)
    if numeric:
        return int(numeric.group(1))
    numeric_item = re.fullmatch(r"第([0-9]+)个", clean)
    if numeric_item:
        return int(numeric_item.group(1))
    chinese = re.fullmatch(r"第([一二两三四五六七八九十])(?:个|道)?(?:题)?", clean)
    if chinese:
        return _CHINESE_ORDINALS[chinese.group(1)]
    return None


def _response(
    text: str,
    state: A3SessionState,
    *,
    intent: str,
    code: str = "REQUEST_SUCCEEDED",
) -> AgentResponse:
    protocol = RequestProtocol.from_code(
        code,
        request_id=current_request_id() or new_request_id(),
        search_id=state.current_search_id,
    )
    return AgentResponse(
        text=text,
        state={"phase": state.phase},
        intent=intent,
        protocol=protocol.to_dict(),
    )


def _crop_review_code(result: CropCompareResult) -> str:
    checks = result.checks
    if checks.get("selected_diagram_match") is False:
        return "SELECTED_DIAGRAM_MISMATCH"
    if checks.get("single_target_diagram") is False:
        return "MULTIPLE_DIAGRAMS"
    if checks.get("external_loads_complete") is False:
        return "EXTERNAL_LOADS_INCOMPLETE"
    if checks.get("structure_complete") is False or checks.get("supports_complete") is False:
        return "STRUCTURE_INCOMPLETE"
    if checks.get("image_clear") is False:
        return "IMAGE_UNCLEAR"
    return "CROP_UNCONFIRMED"


def _clean_session_id(session_id: str) -> str:
    clean = str(session_id or "").strip()
    if not clean or len(clean) > 256:
        raise ValueError("session_id is required")
    return clean

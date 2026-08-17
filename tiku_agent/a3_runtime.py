"""Isolated A3 manual-crop runtime layered over the existing A2 runtime."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Mapping

from PIL import Image, ImageOps

from tiku_agent.a3_models import (
    A3CropVerifier,
    A3ModelError,
    A3PageObserver,
    A3UnitAnalyzer,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.session_artifacts import SessionArtifacts, session_key
from tiku_agent.session_runtime import AgentSessionRuntime, ProgressReporter
from tiku_shared.model_costs import ModelCostCollector, SQLiteModelCostLedger, model_cost_scope
from tiku_shared.request_protocol import RequestProtocol, new_request_id, new_search_id


A3_PHASE_IDLE = "IDLE"
A3_PHASE_UNDERSTANDING = "UNDERSTANDING_PAGE"
A3_PHASE_WAIT_SELECTION = "WAIT_UNIT_SELECTION"
A3_PHASE_CROP_REQUIRED = "CROP_REQUIRED"
A3_PHASE_VERIFYING = "VERIFYING_CROP"
A3_PHASE_A2_ACTIVE = "A2_ACTIVE"
A3_PHASE_COMPLETE = "COMPLETE"
A3_PHASE_ERROR = "ERROR"

_A3_PHASES = {
    A3_PHASE_IDLE,
    A3_PHASE_UNDERSTANDING,
    A3_PHASE_WAIT_SELECTION,
    A3_PHASE_CROP_REQUIRED,
    A3_PHASE_VERIFYING,
    A3_PHASE_A2_ACTIVE,
    A3_PHASE_COMPLETE,
    A3_PHASE_ERROR,
}
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
    phase: str = A3_PHASE_IDLE
    source_page_path: str = ""
    page_understanding: dict[str, Any] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)
    selected_unit_id: str = ""
    completed_unit_ids: list[str] = field(default_factory=list)
    crop_drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    crop_review_required: bool = False
    task_revision: int = 0
    current_search_id: str = ""
    last_error: str = ""

    def validate(self) -> None:
        if self.phase not in _A3_PHASES:
            raise ValueError(f"unknown A3 phase: {self.phase}")
        unit_ids = [str(item.get("unit_id") or "") for item in self.units]
        if any(not value for value in unit_ids) or len(unit_ids) != len(set(unit_ids)):
            raise ValueError("A3 unit ids must be present and unique")
        if self.selected_unit_id and self.selected_unit_id not in unit_ids:
            raise ValueError("selected A3 unit is unavailable")
        if any(value not in unit_ids for value in self.completed_unit_ids):
            raise ValueError("completed A3 unit is unavailable")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "A3SessionState":
        state = cls(**dict(payload))
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
        completed = set(self.completed_unit_ids)
        return [item for item in self.searchable_units if item["unit_id"] not in completed]


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
        unit_analyzer: A3UnitAnalyzer,
        cost_ledger: SQLiteModelCostLedger | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.a2_runtime = a2_runtime
        self.page_observer = page_observer
        self.crop_verifier = crop_verifier
        self.unit_analyzer = unit_analyzer
        self.cost_ledger = cost_ledger
        self._locks = tuple(threading.RLock() for _ in range(64))

    def handle_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        del identity_key
        clean = _clean_session_id(session_id)
        lock = self._lock(clean)
        with lock:
            previous = self.store.load(clean)
            next_revision = int(previous.task_revision if previous is not None else 0) + 1
            self._clear_locked(clean)
            persisted = self.artifacts.persist_image(clean, image_path)
            state = A3SessionState(
                session_id=clean,
                phase=A3_PHASE_UNDERSTANDING,
                source_page_path=str(persisted),
                task_revision=next_revision,
                current_search_id=new_search_id(),
            )
            self.store.save(state)
            if progress is not None:
                progress("a3_understanding", "正在理解整页题目和图形关系…")
            try:
                understanding = self._call_model(
                    state,
                    "a3_page_understanding",
                    lambda: self.page_observer.observe(persisted),
                )
            except Exception as exc:  # noqa: BLE001 - keep the upload available for retry.
                state.phase = A3_PHASE_ERROR
                state.last_error = type(exc).__name__
                self.store.save(state)
                return _response(
                    "这次没能完成整页理解。题图已保留，你可以直接回复“重试”。",
                    state,
                    intent="a3_page_error",
                    code="SERVICE_UNAVAILABLE",
                )

            state.page_understanding = understanding.to_dict(include_derived=True)
            state.units = _flatten_units(state.page_understanding)
            searchable = state.searchable_units
            if len(searchable) == 1:
                state.selected_unit_id = str(searchable[0]["unit_id"])
                state.phase = A3_PHASE_CROP_REQUIRED
                text = (
                    f"我在这张图里识别到 1 道可以处理的题："
                    f"「{searchable[0]['display_label']}」。已经为你选中，接下来裁剪它的单个结构图。"
                )
            elif searchable:
                state.phase = A3_PHASE_WAIT_SELECTION
                text = (
                    f"我在这张图里识别到 {len(searchable)} 道可以处理的题。"
                    "先选一道，我再带你裁剪对应的结构图。"
                )
            else:
                state.phase = A3_PHASE_COMPLETE
                text = "我理解了这张图，但没有找到结构、支座和外荷载都清晰完整的可检索题目。"
            self.store.save(state)
            return _response(text, state, intent="a3_page_ready")

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
            state = self.store.load(clean)
            if state is None:
                return AgentResponse(
                    text="先发一张题图给我吧。",
                    intent="clarification",
                    protocol=RequestProtocol.from_code("UPLOAD_REQUIRED").to_dict(),
                )
            clean_text = str(text or "").strip()
            if state.phase == A3_PHASE_ERROR and clean_text in {"重试", "再试一次", "重新识别"}:
                return self._retry_page_understanding(state, progress=progress)
            if state.phase in {A3_PHASE_WAIT_SELECTION, A3_PHASE_COMPLETE}:
                unit_id, ambiguous = _resolve_unit_selection(clean_text, state.remaining_units)
                if unit_id:
                    return self._select_locked(state, unit_id)
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
                            return self._select_locked(state, unit_id)
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
                    return self._select_locked(state, unit_id)
                if ambiguous:
                    return _response(
                        "你像是想切换题目，但我不能唯一确定是哪一道。请从题目列表中选一个。",
                        state,
                        intent="a3_unit_clarification",
                        code="CLARIFICATION_REQUIRED",
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

    def select_unit(
        self,
        session_id: str,
        unit_id: str,
        *,
        task_revision: int | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        del request_id
        clean = _clean_session_id(session_id)
        with self._lock(clean):
            state = self.store.load(clean)
            if state is None:
                return AgentResponse(
                    text="当前题目列表已失效，请重新上传题图。",
                    intent="stale_action",
                    protocol=RequestProtocol.from_code("STALE_ACTION").to_dict(),
                )
            if task_revision is not None and int(task_revision) != state.task_revision:
                return _response(
                    "这是上一张题图的选题操作，已经失效。请使用当前题目列表。",
                    state,
                    intent="stale_action",
                    code="STALE_ACTION",
                )
            return self._select_locked(state, str(unit_id or "").strip())

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
            state = self.store.load(clean)
            if state is None or state.phase != A3_PHASE_CROP_REQUIRED:
                return AgentResponse(
                    text="这次裁剪已失效，请从当前题目列表重新选择。",
                    intent="stale_action",
                    protocol=RequestProtocol.from_code("STALE_ACTION").to_dict(),
                )
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
                self.store.save(state)
                return _response(
                    f"这次裁剪还不能确认是「{selected['display_label']}」的完整结构图。"
                    "请重新框选，确保只有一个结构，并保留完整支座和全部外荷载。",
                    state,
                    intent="a3_crop_review_required",
                    code="CLARIFICATION_REQUIRED",
                )

            if progress is not None:
                progress("a3_analyzing_unit", "校验通过，正在结合题干识别章节和荷载…")
            context_text = _question_context_text(selected)
            try:
                analysis = self._call_model(
                    state,
                    "a3_unit_analysis",
                    lambda: self.unit_analyzer.analyze(crop_path, context_text),
                )
            except Exception as exc:  # noqa: BLE001 - preserve the verified draft.
                state.phase = A3_PHASE_CROP_REQUIRED
                state.last_error = type(exc).__name__
                self.store.save(state)
                return _response(
                    "裁剪图校验已通过，但单题识别暂时没有完成。图片已保留，可以直接重试。",
                    state,
                    intent="a3_unit_analysis_error",
                    code="SERVICE_UNAVAILABLE",
                )
            if not analysis.loads:
                state.phase = A3_PHASE_CROP_REQUIRED
                state.crop_review_required = True
                self.store.save(state)
                return _response(
                    "这张裁剪图里还没有识别到可检索的外荷载。请重新框选，把结构上的全部荷载和可见标注一起保留。",
                    state,
                    intent="a3_crop_review_required",
                    code="CLARIFICATION_REQUIRED",
                )

            state.phase = A3_PHASE_A2_ACTIVE
            state.last_error = ""
            self.store.save(state)
            classified = {
                "loads": list(analysis.loads),
                "chapter_hint": analysis.chapter_hint,
                "chapter_confidence": analysis.chapter_confidence,
                "chapter_evidence": analysis.chapter_evidence,
                "category": analysis.category,
                "load_details": list(analysis.load_details),
                "visible_problem_text": context_text,
            }
            response = self.a2_runtime.handle_preanalyzed_image(
                clean,
                crop_path,
                loads=list(analysis.loads),
                chapter=(analysis.chapter_hint if analysis.chapter_hint != "unknown" else ""),
                context_text=context_text,
                classified=classified,
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
        draft = state.crop_drafts.get(str(unit_id or "").strip()) or {}
        path = Path(str(draft.get("path") or "")).resolve()
        crop_dir = (self.artifacts.session_dir(state.session_id) / "crops").resolve()
        return path if path.is_file() and path.parent == crop_dir else None

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        clean = _clean_session_id(session_id)
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
                "a3": {"enabled": True, "phase": A3_PHASE_IDLE, "units": []},
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
        snapshot["a3"] = self._a3_snapshot(state)
        return snapshot

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        state = self.store.load(_clean_session_id(session_id))
        if state is None:
            return None
        path = Path(state.source_page_path).resolve()
        return path if path.is_file() and path.name == Path(str(filename)).name == str(filename) else None

    def persist_media(self, session_id: str, source: str | Path) -> Path | None:
        return self.a2_runtime.persist_media(_clean_session_id(session_id), source)

    def resolve_media(self, session_id: str, filename: str) -> Path | None:
        return self.a2_runtime.resolve_media(_clean_session_id(session_id), filename)

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

    def _retry_page_understanding(
        self,
        state: A3SessionState,
        *,
        progress: ProgressReporter | None,
    ) -> AgentResponse:
        if progress is not None:
            progress("a3_understanding", "正在重新理解整页题目…")
        try:
            understanding = self._call_model(
                state,
                "a3_page_understanding_retry",
                lambda: self.page_observer.observe(Path(state.source_page_path)),
            )
        except A3ModelError as exc:
            state.last_error = type(exc).__name__
            self.store.save(state)
            return _response(
                "这次仍然没能完成整页理解。题图还在，可以稍后继续重试。",
                state,
                intent="a3_page_error",
                code="SERVICE_UNAVAILABLE",
            )
        state.page_understanding = understanding.to_dict(include_derived=True)
        state.units = _flatten_units(state.page_understanding)
        state.last_error = ""
        searchable = state.searchable_units
        if len(searchable) == 1:
            state.selected_unit_id = str(searchable[0]["unit_id"])
            state.phase = A3_PHASE_CROP_REQUIRED
            text = f"已经识别到「{searchable[0]['display_label']}」，接下来裁剪它的单个结构图。"
        elif searchable:
            state.phase = A3_PHASE_WAIT_SELECTION
            text = f"这次识别到 {len(searchable)} 道可以处理的题。请先选一道。"
        else:
            state.phase = A3_PHASE_COMPLETE
            text = "这张图里仍然没有识别到可以进入搜题的完整结构题。"
        self.store.save(state)
        return _response(text, state, intent="a3_page_ready")

    def _select_locked(self, state: A3SessionState, unit_id: str) -> AgentResponse:
        unit = state.unit(unit_id)
        if (
            state.phase not in {
                A3_PHASE_WAIT_SELECTION,
                A3_PHASE_CROP_REQUIRED,
                A3_PHASE_A2_ACTIVE,
            }
            or
            unit is None
            or unit.get("searchability") != "searchable_candidate"
            or unit_id in state.completed_unit_ids
        ):
            return _response(
                "这道题当前不能选择，请从剩余题目中选一道。",
                state,
                intent="stale_action",
                code="STALE_ACTION",
            )
        switching_from_a2 = state.phase == A3_PHASE_A2_ACTIVE
        if switching_from_a2:
            self.a2_runtime.clear(state.session_id)
        state.selected_unit_id = unit_id
        state.phase = A3_PHASE_CROP_REQUIRED
        state.crop_review_required = False
        state.last_error = ""
        self.store.save(state)
        return _response(
            (
                f"好，已停止当前题，改查「{unit['display_label']}」。"
                "请裁剪它对应的单个结构图。"
                if switching_from_a2
                else f"好，先查「{unit['display_label']}」。请裁剪它对应的单个结构图。"
            ),
            state,
            intent="a3_unit_selected",
        )

    def _return_to_unit_selection_locked(self, state: A3SessionState) -> AgentResponse:
        self.a2_runtime.clear(state.session_id)
        state.selected_unit_id = ""
        remaining = state.remaining_units
        state.phase = A3_PHASE_WAIT_SELECTION if remaining else A3_PHASE_COMPLETE
        state.crop_review_required = False
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
        child_phase = str(response.state.get("phase") or "")
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
            return response
        if child_phase != "ANSWERED":
            state.phase = A3_PHASE_A2_ACTIVE
            self.store.save(state)
            return response
        if state.selected_unit_id and state.selected_unit_id not in state.completed_unit_ids:
            state.completed_unit_ids.append(state.selected_unit_id)
        remaining = state.remaining_units
        if remaining:
            state.phase = A3_PHASE_WAIT_SELECTION
            response.text = (
                response.text.rstrip()
                + f"\n\n这道题处理好了，这张图里还有 {len(remaining)} 道可以继续查。"
            )
        else:
            state.phase = A3_PHASE_COMPLETE
            response.text = response.text.rstrip() + "\n\n这张图里的可处理题目已经全部完成。"
        self.store.save(state)
        return response

    def _crop_source(self, state: A3SessionState, bounds: dict[str, float]) -> Path:
        source = Path(state.source_page_path)
        target_dir = self.artifacts.session_dir(state.session_id) / "crops"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_id = sha256(state.selected_unit_id.encode("utf-8")).hexdigest()[:20]
        target = target_dir / f"{safe_id}.jpg"
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            left = max(0, min(width - 1, round(bounds["x"] * width)))
            top = max(0, min(height - 1, round(bounds["y"] * height)))
            right = max(left + 1, min(width, round((bounds["x"] + bounds["width"]) * width)))
            bottom = max(top + 1, min(height, round((bounds["y"] + bounds["height"]) * height)))
            image.crop((left, top, right, bottom)).save(
                target,
                format="JPEG",
                quality=94,
                optimize=True,
            )
        return target.resolve()

    def _a3_snapshot(self, state: A3SessionState) -> dict[str, Any]:
        completed = set(state.completed_unit_ids)
        units = []
        for item in state.units:
            if item.get("searchability") != "searchable_candidate":
                continue
            units.append({
                "unit_id": item["unit_id"],
                "display_label": item["display_label"],
                "title_text": item.get("title_text") or "",
                "completed": item["unit_id"] in completed,
                "selected": item["unit_id"] == state.selected_unit_id,
            })
        selected = state.unit(state.selected_unit_id) or {}
        draft = state.crop_drafts.get(state.selected_unit_id) or {}
        return {
            "enabled": True,
            "phase": state.phase,
            "units": units,
            "selected_unit": {
                "unit_id": selected.get("unit_id", ""),
                "display_label": selected.get("display_label", ""),
                "context_text": _question_context_text(selected),
            },
            "completed_unit_ids": list(state.completed_unit_ids),
            "remaining_count": len(state.remaining_units),
            "crop_review_required": state.crop_review_required,
            "crop_draft": {
                "bounds": dict(draft.get("bounds") or {}),
                "available": bool(draft.get("path")),
            },
            "task_revision": state.task_revision,
        }

    def _clear_locked(self, session_id: str) -> None:
        self.store.clear(session_id)
        self.artifacts.clear_session(session_id)
        self.a2_runtime.clear(session_id)

    def _call_model(
        self,
        state: A3SessionState,
        task_kind: str,
        function: Callable[[], Any],
    ) -> Any:
        collector = ModelCostCollector(
            run_id=new_request_id(),
            session_key=session_key(state.session_id),
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

    def _lock(self, session_id: str) -> threading.RLock:
        return self._locks[hash(session_id) % len(self._locks)]


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
    return units


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
            r"(?:算了)?(?:我)?(?:想|要)?(?:换|重选|重新选|切换)(?:一?道|个)?题(?:目)?(?:了)?(?:吧|呢|啊|呀)?",
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


def _response(
    text: str,
    state: A3SessionState,
    *,
    intent: str,
    code: str = "REQUEST_SUCCEEDED",
) -> AgentResponse:
    protocol = RequestProtocol.from_code(
        code,
        request_id=new_request_id(),
        search_id=state.current_search_id,
    )
    return AgentResponse(
        text=text,
        state={"phase": state.phase},
        intent=intent,
        protocol=protocol.to_dict(),
    )


def _clean_session_id(session_id: str) -> str:
    clean = str(session_id or "").strip()
    if not clean or len(clean) > 256:
        raise ValueError("session_id is required")
    return clean

"""Session-aware outer layer for the isolated question-bank Agent."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
import time
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.external_load_screen import (
    ImageSearchCancelled,
    NO_EXTERNAL_LOAD_MESSAGE,
)
from tiku_agent.session_artifacts import SessionArtifacts, session_key
from tiku_agent.session_store import SessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_state_builder import READ_MISSING, READ_OK, READ_UNREADABLE
from tiku_agent.task_state_contract import TaskStateSnapshotV1, empty_task_state_snapshot
from tiku_agent.task_state_runtime import (
    TaskStateEntryCapabilities,
    build_standalone_a2_runtime_snapshot_v1,
    classify_frozen_child_state,
    read_child_state_once,
)
from tiku_agent.task_log import JsonlTaskLogger, TaskLogEntry, TaskLogger
from tiku_agent.tools import AgentToolConfig
from tiku_shared.model_costs import (
    ModelCostCollector,
    SQLiteModelCostLedger,
    model_cost_scope,
    new_run_id,
    submit_with_model_cost_context,
)
from tiku_shared.request_protocol import (
    RequestProtocol,
    new_request_id,
    new_search_id,
)
from tiku_shared.trace_context import current_trace_id, submit_with_trace_context
from tiku_shared.trace_events import (
    bind_trace_event_dimensions,
    current_trace_event_session,
    record_trace_event,
)


AgentFactory = Callable[[AgentState], TikuSearchAgent]
ProgressReporter = Callable[[str, str], None]
ExternalLoadScreen = Callable[[str | Path], str]


@dataclass(frozen=True)
class SessionResponseSnapshotV1:
    """One frozen read-set for a public session-bearing response."""

    uploaded_image_path: Path | None
    legacy_session: dict[str, object]
    task_state: TaskStateSnapshotV1
    submitted_crop_path: Path | None = None
    feedback_overlay_path: Path | None = None


class SessionResponseSnapshotError(RuntimeError):
    """Fail a session capture without discarding or rereading its frozen V1 state."""

    def __init__(self, message: str, *, task_state: TaskStateSnapshotV1) -> None:
        super().__init__(message)
        self.task_state = task_state
        self.response_task_state_snapshot: TaskStateSnapshotV1 | None = task_state
        # A non-empty safe snapshot prevents the generic HTTP handler from
        # performing a second live store read.
        self.response_snapshot: dict[str, object] = {"session_valid": False}


class _ImageRace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._winner = ""
        self.cancel_search = threading.Event()

    def claim_no_load(self) -> bool:
        with self._lock:
            if self._winner:
                return self._winner == "no_load"
            self._winner = "no_load"
            self.cancel_search.set()
            return True

    def claim_candidates(self) -> bool:
        with self._lock:
            if self._winner == "no_load":
                return False
            if self._winner in {"", "screen_expired"}:
                self._winner = "candidates"
            return self._winner == "candidates"

    def expire_screen(self) -> None:
        with self._lock:
            if not self._winner:
                self._winner = "screen_expired"

    @property
    def candidates_committed(self) -> bool:
        with self._lock:
            return self._winner == "candidates"

    @property
    def no_load_committed(self) -> bool:
        with self._lock:
            return self._winner == "no_load"


class BudgetPolicy(Protocol):
    def budget_limits_for(self, identity_key: str) -> Any: ...


class AgentProtocolError(RuntimeError):
    """A request-boundary error with stable public protocol metadata."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = ""
        self.search_id = ""
        self.response_snapshot: dict[str, object] = {}
        self.response_task_state_snapshot: TaskStateSnapshotV1 | None = None

    def bind(self, *, request_id: str, search_id: str = "") -> RequestProtocol:
        self.request_id = request_id
        self.search_id = search_id
        return RequestProtocol.from_code(
            self.code, request_id=request_id, search_id=search_id
        )


class AgentRuntimeBusyError(AgentProtocolError):
    """The bounded web runtime cannot accept another task right now."""

    def __init__(self, message: str, *, code: str = "QUEUE_FULL") -> None:
        super().__init__(message, code=code)


class AgentBudgetExceededError(AgentProtocolError):
    """The configured daily model-cost ceiling has already been reached."""

    def __init__(
        self, message: str, *, code: str = "GLOBAL_DAILY_QUOTA_EXCEEDED"
    ) -> None:
        super().__init__(message, code=code)


class _ExecutionCancelled(RuntimeError):
    """A queued request was withdrawn before business execution began."""


def _progress_cancellation_probe(
    progress: ProgressReporter | None,
) -> Callable[[], bool] | None:
    probe = getattr(progress, "cancelled", None)
    return probe if callable(probe) else None


class _ExecutionGate:
    def __init__(self, max_concurrent: int, max_queued: int, wait_seconds: float) -> None:
        self.max_concurrent = max(0, int(max_concurrent or 0))
        self.max_queued = max(0, int(max_queued or 0))
        self.wait_seconds = max(0.0, float(wait_seconds or 0.0))
        self._active = 0
        self._active_session_keys: set[object] = set()
        self._waiters: deque[tuple[object, object | None]] = deque()
        self._condition = threading.Condition()

    @staticmethod
    def _is_cancelled(cancelled: Callable[[], bool] | None) -> bool:
        return bool(cancelled is not None and cancelled())

    def _session_is_available(self, session_key: object | None) -> bool:
        return session_key is None or session_key not in self._active_session_keys

    def _first_eligible_waiter(self) -> tuple[object, object | None] | None:
        for waiter in self._waiters:
            if self._session_is_available(waiter[1]):
                return waiter
        return None

    def _admit(self, session_key: object | None) -> None:
        self._active += 1
        if session_key is not None:
            self._active_session_keys.add(session_key)

    def _withdraw(self, waiter: tuple[object, object | None]) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        self._condition.notify_all()

    @contextmanager
    def enter(
        self,
        progress: ProgressReporter | None = None,
        *,
        session_key: object | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        if self.max_concurrent <= 0:
            yield
            return

        queued = False
        admitted = False
        waiter: tuple[object, object | None] | None = None
        cancellation_probe = cancelled or _progress_cancellation_probe(progress)
        with self._condition:
            if self._is_cancelled(cancellation_probe):
                raise _ExecutionCancelled()

            earlier_eligible = self._first_eligible_waiter()
            if (
                self._active < self.max_concurrent
                and self._session_is_available(session_key)
                and earlier_eligible is None
            ):
                self._admit(session_key)
                admitted = True
            else:
                if len(self._waiters) >= self.max_queued:
                    raise AgentRuntimeBusyError(
                        "当前请求较多，请稍后再试。", code="QUEUE_FULL"
                    )
                queued = True
                waiter = (object(), session_key)
                self._waiters.append(waiter)
                try:
                    if progress is not None:
                        progress("queued", "前面有任务正在处理，已进入队列…")
                    deadline = time.monotonic() + self.wait_seconds
                    while True:
                        if self._is_cancelled(cancellation_probe):
                            self._withdraw(waiter)
                            raise _ExecutionCancelled()
                        if (
                            self._active < self.max_concurrent
                            and self._session_is_available(session_key)
                            and self._first_eligible_waiter() is waiter
                        ):
                            self._waiters.remove(waiter)
                            self._admit(session_key)
                            admitted = True
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._withdraw(waiter)
                            raise AgentRuntimeBusyError(
                                "排队等待超时，请稍后重新提交。",
                                code="QUEUE_TIMEOUT",
                            )
                        self._condition.wait(
                            min(remaining, 0.05)
                            if cancellation_probe is not None
                            else remaining
                        )
                except Exception:
                    if not admitted:
                        self._withdraw(waiter)
                    raise

        try:
            if self._is_cancelled(cancellation_probe):
                raise _ExecutionCancelled()
            if queued and progress is not None:
                progress("dequeued", "轮到你的题目了，正在开始处理…")
            if self._is_cancelled(cancellation_probe):
                raise _ExecutionCancelled()
            yield
        finally:
            if admitted:
                with self._condition:
                    self._active -= 1
                    if session_key is not None:
                        self._active_session_keys.remove(session_key)
                    self._condition.notify_all()


class AgentSessionRuntime:
    """Restore, run, and checkpoint one Agent turn by a caller-owned session ID."""

    def __init__(
        self,
        store: SessionStore,
        *,
        artifacts: SessionArtifacts | None = None,
        task_logger: TaskLogger | None = None,
        cost_ledger: SQLiteModelCostLedger | None = None,
        agent_factory: AgentFactory | None = None,
        max_concurrent_tasks: int = 0,
        max_queued_tasks: int = 0,
        queue_wait_seconds: float = 90.0,
        daily_budget_cny: float | Decimal | None = None,
        per_identity_daily_budget_cny: float | Decimal | None = None,
        budget_timezone: str = "Asia/Shanghai",
        budget_policy: BudgetPolicy | None = None,
        external_load_screen: ExternalLoadScreen | None = None,
        external_load_timeout_seconds: float = 15.0,
        image_triage_authority: object | None = None,
        preserve_artifacts_on_cancel: bool = False,
    ) -> None:
        self.store = store
        self.artifacts = artifacts or SessionArtifacts()
        self.task_logger = task_logger or JsonlTaskLogger()
        self.cost_ledger = cost_ledger
        self.agent_factory = agent_factory
        self._execution_gate = _ExecutionGate(
            max_concurrent_tasks,
            max_queued_tasks,
            queue_wait_seconds,
        )
        self._session_locks = tuple(threading.Lock() for _ in range(64))
        budget = Decimal(str(daily_budget_cny or 0))
        self._daily_budget_micros = max(0, int(budget * Decimal("1000000")))
        identity_budget = Decimal(str(per_identity_daily_budget_cny or 0))
        self._per_identity_daily_budget_micros = max(
            0, int(identity_budget * Decimal("1000000"))
        )
        self._budget_timezone = ZoneInfo(budget_timezone)
        self._budget_policy = budget_policy
        self.external_load_screen = external_load_screen
        self.external_load_timeout_seconds = max(
            0.01, float(external_load_timeout_seconds)
        )
        self.image_triage_authority = image_triage_authority
        self.preserve_artifacts_on_cancel = bool(preserve_artifacts_on_cancel)
        self._image_executor = (
            ThreadPoolExecutor(max_workers=8, thread_name_prefix="tiku-image-race")
            if external_load_screen is not None
            else None
        )
        self._observer_executor = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="tiku-image-observer")
            if external_load_screen is not None
            else None
        )
        self._background_image_lock = threading.Lock()
        self._background_image_futures: dict[str, set[Future]] = {}
        self._error_snapshot_capture_local = threading.local()

    def handle_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        clean_request_id = str(request_id or "").strip() or new_request_id()
        search_id = new_search_id()
        _bind_trace_lifecycle(clean_session_id, search_id, identity_key=identity_key)

        def execute() -> AgentResponse:
            self.purge_expired()
            try:
                persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
            except Exception as exc:  # noqa: BLE001 - normalize storage failures at the request boundary.
                raise AgentProtocolError(
                    "题图暂时无法保存，请重新上传。",
                    code="UPLOAD_PERSIST_FAILED",
                ) from exc
            if self.external_load_screen is not None:
                return self._run_screened_image(
                    clean_session_id,
                    persisted_image,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=clean_request_id,
                    search_id=search_id,
                    task_state_capabilities=task_state_capabilities,
                )
            if self.image_triage_authority is not None:
                return self._run_authoritative_image(
                    clean_session_id,
                    persisted_image,
                    identity_key=identity_key,
                    progress=progress,
                    request_id=clean_request_id,
                    search_id=search_id,
                    task_state_capabilities=task_state_capabilities,
                )
            return self._run(
                clean_session_id,
                "image",
                lambda agent: agent.handle_image(persisted_image, search_id=search_id),
                identity_key=identity_key,
                progress=progress,
                request_id=clean_request_id,
                task_state_capabilities=task_state_capabilities,
            )

        return self._admit(
            clean_session_id,
            execute,
            kind="image",
            request_id=clean_request_id,
            search_id=search_id,
            identity_key=identity_key,
            progress=progress,
            task_state_capabilities=task_state_capabilities,
        )

    def handle_preanalyzed_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        loads: list[dict[str, Any]],
        chapter: str = "",
        context_text: str = "",
        classified: dict[str, Any] | None = None,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        """Persist an A3-verified crop and enter A2 without rerunning image triage."""

        clean_session_id = self._clean_session_id(session_id)
        clean_request_id = str(request_id or "").strip() or new_request_id()
        search_id = new_search_id()
        _bind_trace_lifecycle(clean_session_id, search_id, identity_key=identity_key)

        def execute() -> AgentResponse:
            self.purge_expired()
            try:
                persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
            except Exception as exc:  # noqa: BLE001 - normalize storage failures at the request boundary.
                raise AgentProtocolError(
                    "裁剪图暂时无法保存，请重新裁剪。",
                    code="UPLOAD_PERSIST_FAILED",
                ) from exc
            return self._run(
                clean_session_id,
                "a3_verified_image",
                lambda agent: agent.handle_preanalyzed_image(
                    persisted_image,
                    loads=list(loads or []),
                    chapter=chapter,
                    context_text=context_text,
                    classified=classified,
                    search_id=search_id,
                ),
                identity_key=identity_key,
                progress=progress,
                request_id=clean_request_id,
            )

        return self._admit(
            clean_session_id,
            execute,
            kind="a3_verified_image",
            request_id=clean_request_id,
            search_id=search_id,
            identity_key=identity_key,
            progress=progress,
        )

    def handle_prechecked_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        context_text: str = "",
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
    ) -> AgentResponse:
        """Enter A2 after the authoritative A1/A2/A3 gate confirmed one complete question."""

        clean_session_id = self._clean_session_id(session_id)
        clean_request_id = str(request_id or "").strip() or new_request_id()
        search_id = new_search_id()
        _bind_trace_lifecycle(clean_session_id, search_id, identity_key=identity_key)

        def execute() -> AgentResponse:
            self.purge_expired()
            try:
                persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
            except Exception as exc:  # noqa: BLE001 - normalize storage failures at the request boundary.
                raise AgentProtocolError(
                    "题图暂时无法保存，请重新上传。",
                    code="UPLOAD_PERSIST_FAILED",
                ) from exc
            return self._run(
                clean_session_id,
                "image",
                lambda agent: agent.handle_image(
                    persisted_image,
                    search_id=search_id,
                    prechecked_single=True,
                    context_text=context_text,
                ),
                identity_key=identity_key,
                progress=progress,
                request_id=clean_request_id,
            )

        return self._admit(
            clean_session_id,
            execute,
            kind="image",
            request_id=clean_request_id,
            search_id=search_id,
            identity_key=identity_key,
            progress=progress,
        )

    def _run_authoritative_image(
        self,
        session_id: str,
        image_path: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
        search_id: str,
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        """Run 8891 triage inside the normal admission, logging and cost scope."""

        def handle(agent: TikuSearchAgent) -> AgentResponse:
            if progress is not None:
                progress("triage", "正在检查图片并决定处理路线…")
            try:
                decision = self.image_triage_authority.decide(image_path)  # type: ignore[union-attr]
            except Exception as exc:
                raise AgentProtocolError(
                    "图片检查暂时失败，请稍后重试。",
                    code="SERVICE_UNAVAILABLE",
                ) from exc
            handoff = decision.handoff
            record_trace_event(
                "route_decided",
                stage="image_routing",
                outcome="rejected" if handoff.route == "A1" else "success",
                safe_attributes={"route": handoff.route},
            )
            if handoff.route == "A2":
                if progress is not None:
                    progress("searching", "图片适合直接检索，正在识别题目信息…")
                return agent.handle_image(
                    image_path,
                    search_id=search_id,
                    prechecked_single=True,
                )

            state = agent.state
            state.start_search(str(image_path), search_id=search_id)
            state.current_route = handoff.route
            state.last_error = decision.reply
            state.set_candidates([])
            code = (
                "TRIAGE_A1_STOPPED"
                if handoff.route == "A1"
                else "TRIAGE_A3_REQUIRES_REUPLOAD"
            )
            protocol = RequestProtocol.from_code(code, search_id=search_id).to_dict()
            return AgentResponse(
                text=decision.reply,
                state=state.to_dict(),
                intent="image_triage_stop",
                reply_source=decision.reply_source,
                fallback_reason=decision.fallback_reason,
                protocol=protocol,
            )

        return self._run(
            session_id,
            "image",
            handle,
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
            task_state_capabilities=task_state_capabilities,
        )

    def _run_screened_image(
        self,
        session_id: str,
        image_path: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
        search_id: str,
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        """Run the image race and carry its in-memory state on any failure."""

        error_context: dict[str, object] = {
            "state": None,
            "read_status": READ_UNREADABLE,
            "initial_state": None,
            "initially_missing": False,
        }
        try:
            return self._run_screened_image_inner(
                session_id,
                image_path,
                identity_key=identity_key,
                progress=progress,
                request_id=request_id,
                search_id=search_id,
                error_context=error_context,
            )
        except Exception as exc:
            state, read_status = self._error_snapshot_read_set(error_context)
            self._attach_error_response_snapshot_from_read_set(
                exc,
                session_id,
                state=state,
                read_status=read_status,
                capabilities=task_state_capabilities,
            )
            raise

    def _run_screened_image_inner(
        self,
        session_id: str,
        image_path: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
        request_id: str,
        search_id: str,
        error_context: dict[str, object],
    ) -> AgentResponse:
        persisted_state = self._load_session_state(session_id)
        baseline_state = persisted_state or AgentState(session_id=session_id)
        error_context.update(
            state=baseline_state,
            read_status=(READ_MISSING if persisted_state is None else None),
            initial_state=baseline_state.to_dict(),
            initially_missing=persisted_state is None,
        )
        phase_before = baseline_state.phase
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        task_id = request_id
        collector = ModelCostCollector(
            run_id=new_run_id(),
            trace_id=current_trace_id(),
            session_key=session_key(session_id),
            identity_key=str(identity_key).strip(),
            task_kind="image",
            started_at=started_at.isoformat(),
        )
        race = _ImageRace()
        agent = self._make_agent(
            AgentState.from_dict(baseline_state.to_dict()), progress=progress
        )
        error_context["state"] = agent.state
        agent.image_search_cancelled = race.cancel_search.is_set
        agent.commit_image_candidates = race.claim_candidates
        assert self._image_executor is not None

        with model_cost_scope(collector):
            screen_future = submit_with_model_cost_context(
                self._image_executor,
                self._run_external_load_screen,
                self.external_load_screen,
                image_path,
                race,
            )
            search_future = submit_with_model_cost_context(
                self._image_executor,
                agent.handle_image,
                image_path,
                search_id=search_id,
            )

        deadline = time.monotonic() + self.external_load_timeout_seconds
        response: AgentResponse | None = None
        response_state: AgentState | None = None
        search_error: BaseException | None = None

        while response is None:
            pending = [
                future
                for future in (screen_future, search_future)
                if not future.done()
            ]
            if pending:
                remaining = max(0.0, deadline - time.monotonic())
                done, _ = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not done and remaining <= 0:
                    break

            if screen_future.done():
                verdict = self._screen_verdict(screen_future)
                if verdict == "no" and race.claim_no_load():
                    response_state, response = self._no_load_response(
                        baseline_state, image_path, search_id=search_id
                    )
                    break

            if search_future.done():
                try:
                    search_response = search_future.result()
                except ImageSearchCancelled:
                    if race.cancel_search.is_set():
                        response_state, response = self._no_load_response(
                            baseline_state, image_path, search_id=search_id
                        )
                        break
                    raise
                except BaseException as exc:  # Re-raised if A does not override it.
                    search_error = exc
                else:
                    if race.candidates_committed:
                        response_state, response = agent.state, search_response
                        break
                    if screen_future.done() or time.monotonic() >= deadline:
                        response_state, response = agent.state, search_response
                        break

            if search_error is not None and screen_future.done():
                raise search_error

            if time.monotonic() >= deadline:
                break

        if response is None:
            race.expire_screen()
            if race.no_load_committed:
                response_state, response = self._no_load_response(
                    baseline_state, image_path, search_id=search_id
                )
            elif search_future.done():
                if search_error is not None:
                    raise search_error
                response_state, response = agent.state, search_future.result()
            else:
                try:
                    response = search_future.result()
                except ImageSearchCancelled:
                    response_state, response = self._no_load_response(
                        baseline_state, image_path, search_id=search_id
                    )
                else:
                    response_state = agent.state

        assert response is not None and response_state is not None
        error_context.update(
            state=response_state,
            read_status=None,
            initially_missing=False,
        )
        record_trace_event(
            "route_decided",
            stage="image_routing",
            outcome=("rejected" if response.intent == "external_load_screen" else "success"),
            safe_attributes={
                "route": "A1" if response.intent == "external_load_screen" else "A2"
            },
        )
        self._attach_response_protocol(
            response,
            state=response_state,
            request_id=request_id,
            search_id=response_state.current_search_id or search_id,
        )
        self.store.save(response_state)
        duration_ms = round((time.perf_counter() - started) * 1000)
        futures = (screen_future, search_future)
        if all(future.done() for future in futures):
            self._finish_screened_observability(
                futures,
                task_id=task_id,
                session_id=session_id,
                started_at=started_at,
                duration_ms=duration_ms,
                phase_before=phase_before,
                state=response_state,
                response=response,
                collector=collector,
                identity_key=identity_key,
            )
        else:
            if not search_future.done():
                self._track_background_image_future(session_id, search_future)
            assert self._observer_executor is not None
            submit_with_trace_context(
                self._observer_executor,
                self._finish_screened_observability,
                futures,
                task_id=task_id,
                session_id=session_id,
                started_at=started_at,
                duration_ms=duration_ms,
                phase_before=phase_before,
                state=response_state,
                response=response,
                collector=collector,
                identity_key=identity_key,
            )
        return response

    @staticmethod
    def _run_external_load_screen(
        screen: ExternalLoadScreen, image_path: Path, race: _ImageRace
    ) -> str:
        verdict = str(screen(image_path)).strip().lower()
        if verdict == "no":
            race.claim_no_load()
        return verdict

    def _track_background_image_future(
        self, session_id: str, future: Future
    ) -> None:
        with self._background_image_lock:
            self._background_image_futures.setdefault(session_id, set()).add(future)

        def remove(completed: Future) -> None:
            with self._background_image_lock:
                futures = self._background_image_futures.get(session_id)
                if futures is None:
                    return
                futures.discard(completed)
                if not futures:
                    self._background_image_futures.pop(session_id, None)

        future.add_done_callback(remove)

    def _await_background_image_work(self, session_id: str) -> None:
        with self._background_image_lock:
            futures = tuple(self._background_image_futures.get(session_id, ()))
        if futures:
            wait(futures)

    @staticmethod
    def _screen_verdict(future: Future) -> str:
        try:
            verdict = str(future.result()).strip().lower()
        except Exception:  # A failure must preserve the existing search behavior.
            return "error"
        return verdict if verdict in {"yes", "no"} else "error"

    @staticmethod
    def _no_load_response(
        state_before: AgentState, image_path: Path, *, search_id: str
    ) -> tuple[AgentState, AgentResponse]:
        state = AgentState.from_dict(state_before.to_dict())
        state.start_search(str(image_path), search_id=search_id)
        state.set_candidates([])
        state.last_error = NO_EXTERNAL_LOAD_MESSAGE
        return state, AgentResponse(
            text=NO_EXTERNAL_LOAD_MESSAGE,
            state=state.to_dict(),
            intent="external_load_screen",
            protocol=RequestProtocol.from_code(
                "EXTERNAL_LOAD_NOT_FOUND", search_id=search_id
            ).to_dict(),
        )

    def _finish_screened_observability(
        self,
        futures: tuple[Future, Future],
        *,
        task_id: str,
        session_id: str,
        started_at: datetime,
        duration_ms: int,
        phase_before: str,
        state: AgentState,
        response: AgentResponse,
        collector: ModelCostCollector,
        identity_key: str,
    ) -> None:
        wait(futures)
        for future in futures:
            try:
                future.result()
            except BaseException:
                pass
        self._write_task_log(
            task_id=task_id,
            session_id=session_id,
            kind="image",
            started_at=started_at,
            duration_ms=duration_ms,
            phase_before=phase_before,
            state=state,
            response=response,
            error_kind="",
            identity_key=identity_key,
            trace_id=collector.trace_id,
        )
        if self.cost_ledger is not None:
            try:
                if state.task_revision > 0:
                    collector.search_key = state.current_search_id or (
                        f"{collector.session_key}:{state.task_revision}"
                    )
                self.cost_ledger.write_run(
                    collector,
                    finished_at=datetime.now(UTC).isoformat(),
                    outcome=_task_outcome(state, response, ""),
                )
            except Exception:
                pass

    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str = "",
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        clean_request_id = str(request_id or "").strip() or new_request_id()
        # The state is read after admission while the session lock is held.
        # A lock-external pre-read could both race and bypass the frozen error
        # snapshot path when the store is unreadable.
        search_id = ""
        _bind_trace_lifecycle(clean_session_id, search_id, identity_key=identity_key)
        return self._admit(
            clean_session_id,
            lambda: self._run(
                clean_session_id,
                "text",
                lambda agent: agent.handle_text(text),
                identity_key=identity_key,
                progress=progress,
                request_id=clean_request_id,
                task_state_capabilities=task_state_capabilities,
            ),
            kind="text",
            request_id=clean_request_id,
            search_id=search_id,
            identity_key=identity_key,
            progress=progress,
            task_state_capabilities=task_state_capabilities,
        )

    def clear(
        self,
        session_id: str,
        *,
        preserve_artifacts: bool = False,
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> None:
        """Clear active state, optionally retaining media until the parent session expires."""
        clean_session_id = self._clean_session_id(session_id)
        lock = self._lock(clean_session_id)
        with lock:
            try:
                self._await_background_image_work(clean_session_id)
                self.store.clear(clean_session_id)
                if not preserve_artifacts:
                    self.artifacts.clear_session(clean_session_id)
            except Exception as exc:
                # Freeze the post-attempt state before releasing the A2 lock.
                # A failed store clear keeps the child visible; a later
                # artifact failure after the delete produces canonical empty.
                if not self._error_response_snapshot_capture_deferred():
                    self._capture_error_response_snapshot_locked(
                        exc,
                        clean_session_id,
                        capabilities=task_state_capabilities,
                        reuse_carried=False,
                    )
                raise

    def current_image_path(self, session_id: str) -> Path | None:
        """Return the current persisted upload for a live session."""
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        state = self._load_session_state(clean_session_id)
        if state is None or not state.current_image_path:
            return None
        path = Path(state.current_image_path)
        return path if self.resolve_upload(clean_session_id, path.name) == path.resolve() else None

    def session_response_snapshot_v1(
        self,
        session_id: str,
        *,
        capabilities: TaskStateEntryCapabilities | None = None,
        response_frozen: bool = False,
    ) -> SessionResponseSnapshotV1:
        """Capture legacy session data and V1 state from one locked A2 read."""

        clean_session_id = self._clean_session_id(session_id)
        # Preserve the legacy endpoint's expiration cleanup before freezing
        # the read-set. SQLite ``load()`` removes an expired row without
        # returning its session id, so cleanup must happen first or its
        # artifacts would become unreachable orphans. A cleanup failure means
        # freshness was not established, so it must not be downgraded to an
        # ordinary missing/IDLE state. Carry a no-read UNREADABLE sentinel and
        # let the controlled HTTP boundary publish that exact failure state.
        try:
            self.purge_expired()
        except Exception as exc:  # noqa: BLE001 - preserve the cleanup failure.
            with self._lock(clean_session_id):
                self._attach_error_response_snapshot_from_read_set(
                    exc,
                    clean_session_id,
                    state=None,
                    read_status=READ_UNREADABLE,
                    capabilities=capabilities,
                )
            setattr(exc, "_response_snapshot_attempted", True)
            raise

        with self._lock(clean_session_id):
            try:
                captured = self._response_snapshot_v1_locked(
                    clean_session_id,
                    capabilities=capabilities,
                    response_frozen=response_frozen,
                )
            except Exception as capture_error:  # noqa: BLE001 - preserve the first failure.
                # Mark the attempted read-set so an outer HTTP handler will
                # never blindly retry a live capture.  The helper also gives
                # a controlled caller a typed unreadable sentinel when the
                # lower-level read failed before it could construct one.
                setattr(capture_error, "_response_snapshot_attempted", True)
                self._attach_error_response_snapshot_from_read_set(
                    capture_error,
                    clean_session_id,
                    state=None,
                    read_status=READ_UNREADABLE,
                    capabilities=capabilities,
                )
                raise
            return captured

    def _response_snapshot_v1_locked(
        self,
        session_id: str,
        *,
        capabilities: TaskStateEntryCapabilities | None,
        response_frozen: bool = False,
    ) -> SessionResponseSnapshotV1:
        """Capture one A2 response read-set while the caller holds its lock."""

        state, read_status = read_child_state_once(self.store, session_id)
        task_state = self._task_state_snapshot_v1_from_read_set(
            session_id,
            state,
            read_status,
            capabilities=capabilities,
            response_frozen=response_frozen,
        )
        if read_status not in {READ_OK, READ_MISSING}:
            raise SessionResponseSnapshotError(
                "legacy session state is unavailable",
                task_state=task_state,
            )
        return SessionResponseSnapshotV1(
            uploaded_image_path=self._current_image_path_from_frozen_state(
                session_id,
                state,
            ),
            legacy_session=self._legacy_session_snapshot_from_state(state),
            task_state=task_state,
        )

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        """Return the small, non-sensitive state contract needed by the web client."""
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        state = self._load_session_state(clean_session_id)
        return self._legacy_session_snapshot_from_state(state)

    @staticmethod
    def _legacy_session_snapshot_from_state(
        state: AgentState | None,
    ) -> dict[str, object]:
        """Project the existing browser session shape from one frozen state."""

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
            }
        return {
            "session_valid": True,
            "phase": state.phase,
            "has_active_image": bool(state.active_image_path),
            "task_revision": state.task_revision,
            "candidate_generation": state.candidate_generation,
            "candidate_count": state.candidate_count,
            "chapter": state.chapter,
            "search_id": state.current_search_id,
        }

    def _current_image_path_from_frozen_state(
        self,
        session_id: str,
        state: AgentState | None,
    ) -> Path | None:
        """Resolve the legacy upload from a state already read under the lock."""

        if state is None or not state.current_image_path:
            return None
        path = Path(state.current_image_path)
        resolved = self.artifacts.resolve_upload(session_id, path.name)
        return path if resolved == path.resolve() else None

    def _task_state_snapshot_v1_from_read_set(
        self,
        session_id: str,
        state: AgentState | None,
        read_status: str,
        *,
        capabilities: TaskStateEntryCapabilities | None = None,
        response_frozen: bool = False,
    ) -> TaskStateSnapshotV1:
        return build_standalone_a2_runtime_snapshot_v1(
            session_id,
            child_state=state,
            child_read_status=read_status,
            child_artifacts=self.artifacts,
            child_retry_supported=True,
            capabilities=capabilities,
            response_frozen=response_frozen,
        )

    def task_state_snapshot_v1(
        self,
        session_id: str,
        *,
        capabilities: TaskStateEntryCapabilities | None = None,
    ) -> TaskStateSnapshotV1:
        """Build one authoritative standalone-A2 snapshot under its session lock."""

        clean_session_id = self._clean_session_id(session_id)
        with self._lock(clean_session_id):
            state, read_status = read_child_state_once(
                self.store,
                clean_session_id,
            )
            return self._task_state_snapshot_v1_from_read_set(
                clean_session_id,
                state,
                read_status,
                capabilities=capabilities,
            )

    def task_state_snapshot_v1_from_frozen_state(
        self,
        session_id: str,
        state: AgentState | None,
        *,
        capabilities: TaskStateEntryCapabilities | None = None,
    ) -> TaskStateSnapshotV1:
        """Project response-time A2 state without acquiring a lock or reading a store."""

        clean_session_id = self._clean_session_id(session_id)
        state, read_status = classify_frozen_child_state(state)
        return self._task_state_snapshot_v1_from_read_set(
            clean_session_id,
            state,
            read_status,
            capabilities=capabilities,
            response_frozen=True,
        )

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        """Resolve one session-owned upload without exposing arbitrary paths."""
        return self._resolve_artifact(session_id, "uploads", filename)

    def persist_media(self, session_id: str, source: str | Path) -> Path | None:
        """Keep candidate/answer media available for the live conversation."""
        clean_session_id = self._clean_session_id(session_id)
        if self._load_session_state(clean_session_id) is None:
            return None
        return self.artifacts.persist_media(clean_session_id, source)

    def record_protocol_event(
        self,
        session_id: str,
        *,
        kind: str,
        identity_key: str = "",
        protocol: RequestProtocol,
        error_kind: str = "",
    ) -> None:
        """Record an API-boundary event that never reached the Agent runner."""

        clean_session_id = self._clean_session_id(session_id)
        state = self._load_session_state(clean_session_id) or AgentState(
            session_id=clean_session_id
        )
        now = datetime.now(UTC)
        self._write_task_log(
            task_id=protocol.request_id or new_request_id(),
            session_id=clean_session_id,
            kind=kind,
            started_at=now,
            duration_ms=0,
            phase_before=state.phase,
            state=state,
            response=None,
            error_kind=error_kind,
            identity_key=identity_key,
            protocol=protocol,
        )

    def resolve_media(
        self,
        session_id: str,
        filename: str,
        *,
        allow_preserved: bool = False,
    ) -> Path | None:
        return self._resolve_artifact(
            session_id,
            "media",
            filename,
            allow_preserved=allow_preserved,
        )

    def _resolve_artifact(
        self,
        session_id: str,
        folder: str,
        filename: str,
        *,
        allow_preserved: bool = False,
    ) -> Path | None:
        clean_session_id = self._clean_session_id(session_id)
        if not allow_preserved and self._load_session_state(clean_session_id) is None:
            return None
        if folder == "uploads":
            return self.artifacts.resolve_upload(clean_session_id, filename)
        if folder == "media":
            return self.artifacts.resolve_media(clean_session_id, filename)
        return None

    def _run(
        self,
        session_id: str,
        kind: str,
        handler: Callable[[TikuSearchAgent], AgentResponse],
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        request_id: str,
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        try:
            persisted_state = self._load_session_state(clean_session_id)
        except Exception as exc:
            self._attach_error_response_snapshot_from_read_set(
                exc,
                clean_session_id,
                state=None,
                read_status=READ_UNREADABLE,
                capabilities=task_state_capabilities,
            )
            raise
        error_context: dict[str, object] = {
            "state": None,
            "read_status": READ_UNREADABLE,
            "initial_state": None,
            "initially_missing": False,
        }
        try:
            state = persisted_state or AgentState(session_id=clean_session_id)
            error_context.update(
                state=state,
                read_status=(READ_MISSING if persisted_state is None else None),
                initial_state=state.to_dict(),
                initially_missing=persisted_state is None,
            )
            if kind == "text":
                _bind_trace_lifecycle(
                    clean_session_id,
                    state.current_search_id,
                    identity_key=identity_key,
                )
            had_active_child = (
                persisted_state is not None
                and persisted_state.phase not in {"IDLE", "CANCELLED"}
            )
            phase_before = state.phase
            started_at = datetime.now(UTC)
            started = time.perf_counter()
            task_id = request_id
            cost_collector = ModelCostCollector(
                run_id=new_run_id(),
                trace_id=current_trace_id(),
                session_key=session_key(clean_session_id),
                identity_key=str(identity_key).strip(),
                task_kind=kind,
                started_at=started_at.isoformat(),
            )
            agent = self._make_agent(state, progress=progress)
        except Exception as exc:
            frozen_state, read_status = self._error_snapshot_read_set(error_context)
            self._attach_error_response_snapshot_from_read_set(
                exc,
                clean_session_id,
                state=frozen_state,
                read_status=read_status,
                capabilities=task_state_capabilities,
            )
            raise
        error_context["state"] = agent.state
        response: AgentResponse | None = None
        error_kind = ""
        try:
            with model_cost_scope(cost_collector):
                response = handler(agent)
            self._attach_response_protocol(
                response,
                state=agent.state,
                request_id=request_id,
                search_id=agent.state.current_search_id,
            )
            if response.intent == "cancel":
                if task_state_capabilities is not None:
                    frozen_legacy = self._legacy_session_snapshot_from_state(
                        agent.state if had_active_child else None
                    )
                    response.response_snapshot = dict(frozen_legacy)
                    response.response_projection_snapshot = dict(frozen_legacy)
                    response.response_task_state_snapshot = (
                        self.task_state_snapshot_v1_from_frozen_state(
                            clean_session_id,
                            agent.state,
                            capabilities=task_state_capabilities,
                        )
                        if had_active_child
                        else empty_task_state_snapshot()
                    )
                    response.response_media_snapshot_captured = True
                self.store.clear(clean_session_id)
                # Nested A3 flows own the conversation-level artifact lifetime.
                if not self.preserve_artifacts_on_cancel:
                    self.artifacts.clear_session(clean_session_id)
            else:
                self.store.save(agent.state)
            return response
        except Exception as exc:
            error_kind = type(exc).__name__
            error_context["state"] = getattr(agent, "state", None)
            frozen_state, read_status = self._error_snapshot_read_set(error_context)
            self._attach_error_response_snapshot_from_read_set(
                exc,
                clean_session_id,
                state=frozen_state,
                read_status=read_status,
                capabilities=task_state_capabilities,
            )
            raise
        finally:
            outcome = _task_outcome(agent.state, response, error_kind)
            self._write_task_log(
                task_id=task_id,
                session_id=clean_session_id,
                kind=kind,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                phase_before=phase_before,
                state=agent.state,
                response=response,
                error_kind=error_kind,
                identity_key=identity_key,
            )
            if self.cost_ledger is not None:
                try:
                    if agent.state.task_revision > 0:
                        cost_collector.search_key = agent.state.current_search_id or (
                            f"{cost_collector.session_key}:{agent.state.task_revision}"
                        )
                    self.cost_ledger.write_run(
                        cost_collector,
                        finished_at=datetime.now(UTC).isoformat(),
                        outcome=outcome,
                    )
                except Exception:  # noqa: BLE001 - cost observability must never break a user turn.
                    pass

    def _make_agent(
        self,
        state: AgentState,
        *,
        progress: ProgressReporter | None = None,
    ) -> TikuSearchAgent:
        if self.agent_factory is not None:
            agent = self.agent_factory(state)
            agent.progress_reporter = progress
            return agent
        return TikuSearchAgent(
            state=state,
            progress_reporter=progress,
            config=AgentToolConfig(
                runtime_dir=self.artifacts.root.parent,
                session_dir=self.artifacts.session_dir(state.session_id),
            ),
        )

    def purge_expired(self) -> None:
        """Remove expired state and its session-scoped files."""
        try:
            expired = self.store.purge_expired()
        except Exception as exc:
            # The store that defines child readability just failed. An A3
            # wrapper must not probe it again while rendering the same error.
            setattr(exc, "_task_state_read_set_unavailable", True)
            raise
        self.artifacts.clear_sessions(expired)

    def _admit(
        self,
        session_id: str,
        execute: Callable[[], AgentResponse],
        *,
        kind: str,
        request_id: str,
        search_id: str,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
        task_state_capabilities: TaskStateEntryCapabilities | None = None,
    ) -> AgentResponse:
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        lock = self._lock(session_id)
        try:
            with self._execution_gate.enter(progress, session_key=lock):
                with lock:
                    try:
                        self._await_background_image_work(session_id)
                        self._check_daily_budget(identity_key)
                        response = execute()
                    except Exception as exc:
                        if not self._error_response_snapshot_capture_deferred():
                            if getattr(
                                exc,
                                "_task_state_read_set_unavailable",
                                False,
                            ):
                                # purge_expired() already established that
                                # this store cannot provide a trustworthy
                                # read-set. Attach a pure unreadable sentinel;
                                # loading here would immediately repeat the
                                # failed store access.
                                self._attach_error_response_snapshot_from_read_set(
                                    exc,
                                    session_id,
                                    state=None,
                                    read_status=READ_UNREADABLE,
                                    capabilities=task_state_capabilities,
                                )
                            else:
                                self._capture_error_response_snapshot_locked(
                                    exc,
                                    session_id,
                                    capabilities=task_state_capabilities,
                                )
                        raise
                    else:
                        captured = None
                        has_attached_v1 = (
                            task_state_capabilities is not None
                            and type(response.response_task_state_snapshot)
                            is TaskStateSnapshotV1
                            and type(response.response_snapshot) is dict
                            and bool(response.response_snapshot)
                            and type(response.response_projection_snapshot) is dict
                            and bool(response.response_projection_snapshot)
                            and response.response_media_snapshot_captured is True
                        )
                        if (
                            task_state_capabilities is not None
                            and not has_attached_v1
                        ):
                            captured = self._response_snapshot_v1_locked(
                                session_id,
                                capabilities=task_state_capabilities,
                                response_frozen=True,
                            )
                        if captured is not None:
                            response.response_snapshot = dict(
                                captured.legacy_session
                            )
                            response.response_projection_snapshot = dict(
                                response.response_snapshot
                            )
                            response.response_task_state_snapshot = captured.task_state
                        elif not has_attached_v1:
                            response.response_snapshot = self.session_snapshot(
                                session_id
                            )
                            response.response_projection_snapshot = dict(
                                response.response_snapshot
                            )
                        response.response_media_snapshot_captured = True
                        if captured is not None:
                            response.uploaded_image_path = captured.uploaded_image_path
                        elif not has_attached_v1:
                            try:
                                response.uploaded_image_path = self.current_image_path(session_id)
                            except Exception:  # noqa: BLE001 - response metadata is best effort.
                                response.uploaded_image_path = None
                        return response
        except AgentProtocolError as exc:
            if not exc.response_snapshot:
                # Queue rejection happens before the session lock. Preserve the
                # legacy shape without a lock-external store read; typed state
                # intentionally remains absent on that path.
                exc.response_snapshot = self._legacy_session_snapshot_from_state(None)
            response_search_id = str(
                exc.response_snapshot.get("search_id") or search_id
            ).strip()
            protocol = exc.bind(request_id=request_id, search_id=response_search_id)
            # Protocol logging must not turn one frozen capture (or a queue
            # rejection) into another live store read.
            carried_state = getattr(exc, "_response_frozen_agent_state", None)
            state = (
                AgentState.from_dict(carried_state.to_dict())
                if type(carried_state) is AgentState
                else AgentState(session_id=session_id)
            )
            self._write_task_log(
                task_id=request_id,
                session_id=session_id,
                kind=kind,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                phase_before=state.phase,
                state=state,
                response=None,
                error_kind=type(exc).__name__,
                identity_key=identity_key,
                protocol=protocol,
            )
            raise

    @staticmethod
    def _error_snapshot_read_set(
        context: dict[str, object],
    ) -> tuple[AgentState | None, str | None]:
        """Resolve the exact in-memory read-set to freeze after one failed turn."""

        state = context.get("state")
        read_status = context.get("read_status")
        if type(state) is not AgentState:
            return None, str(read_status or READ_UNREADABLE)
        if context.get("initially_missing") is True:
            try:
                unchanged = state.to_dict() == context.get("initial_state")
            except Exception:  # noqa: BLE001 - malformed in-memory state is unreadable.
                unchanged = False
            if unchanged:
                return None, READ_MISSING
        return state, None

    def _attach_error_response_snapshot_from_read_set(
        self,
        exc: Exception,
        session_id: str,
        *,
        state: AgentState | None,
        read_status: str | None,
        capabilities: TaskStateEntryCapabilities | None,
    ) -> None:
        """Carry one frozen legacy/typed pair without touching the live store."""

        # Even if projection itself fails, an outer HTTP boundary must know
        # that the authoritative response-time read-set was already consumed
        # and use a zero-I/O sentinel instead of attempting a live recapture.
        setattr(exc, "_response_snapshot_attempted", True)

        existing_legacy = getattr(exc, "response_snapshot", None)
        existing_task_state = getattr(exc, "response_task_state_snapshot", None)
        if type(existing_legacy) is dict and bool(existing_legacy) and (
            capabilities is None or type(existing_task_state) is TaskStateSnapshotV1
        ):
            return

        try:
            frozen_state = state
            frozen_read_status = read_status
            if frozen_read_status is None:
                frozen_state, frozen_read_status = classify_frozen_child_state(state)
            elif frozen_read_status == READ_OK:
                frozen_state, frozen_read_status = classify_frozen_child_state(state)
            elif frozen_read_status != READ_MISSING:
                frozen_state = None

            if frozen_read_status == READ_OK:
                legacy = self._legacy_session_snapshot_from_state(frozen_state)
            elif frozen_read_status == READ_MISSING:
                legacy = self._legacy_session_snapshot_from_state(None)
            else:
                # An unreadable/unknown state cannot safely populate legacy
                # fields. Keep a non-empty placeholder so no outer handler
                # attempts to repair it with another live read.
                legacy = {"session_valid": False}

            task_state = None
            if capabilities is not None:
                task_state = self._task_state_snapshot_v1_from_read_set(
                    session_id,
                    frozen_state,
                    str(frozen_read_status),
                    capabilities=capabilities,
                    response_frozen=True,
                )

            setattr(exc, "response_snapshot", dict(legacy))
            if task_state is not None:
                setattr(exc, "response_task_state_snapshot", task_state)
            if frozen_read_status == READ_OK and type(frozen_state) is AgentState:
                setattr(
                    exc,
                    "_response_frozen_agent_state",
                    AgentState.from_dict(frozen_state.to_dict()),
                )
        except Exception:  # noqa: BLE001 - never replace the business failure.
            return

    def _capture_error_response_snapshot_locked(
        self,
        exc: Exception,
        session_id: str,
        *,
        capabilities: TaskStateEntryCapabilities | None,
        reuse_carried: bool = True,
    ) -> None:
        """Attach one response-time error snapshot while the A2 lock is held."""

        # Mark before reading so every failure path, including a failed
        # projection, prevents a second live capture at the HTTP boundary.
        setattr(exc, "_response_snapshot_attempted", True)

        carried_legacy = getattr(exc, "response_snapshot", None)
        if reuse_carried and (
            capabilities is None
            and type(carried_legacy) is dict
            and bool(carried_legacy)
        ):
            return
        if capabilities is None:
            try:
                setattr(exc, "response_snapshot", self.session_snapshot(session_id))
            except Exception:  # noqa: BLE001 - never mask the original failure.
                pass
            return

        if reuse_carried and (
            type(getattr(exc, "response_task_state_snapshot", None))
            is TaskStateSnapshotV1
            and type(getattr(exc, "response_snapshot", None)) is dict
            and bool(getattr(exc, "response_snapshot", None))
        ):
            return

        try:
            captured = self._response_snapshot_v1_locked(
                session_id,
                capabilities=capabilities,
                response_frozen=True,
            )
        except SessionResponseSnapshotError as capture_error:
            try:
                setattr(
                    exc,
                    "response_task_state_snapshot",
                    capture_error.task_state,
                )
                setattr(exc, "response_snapshot", dict(capture_error.response_snapshot))
            except Exception:  # noqa: BLE001 - never mask the original failure.
                pass
            return
        except Exception:  # noqa: BLE001 - never mask the original failure.
            return

        try:
            setattr(exc, "response_snapshot", dict(captured.legacy_session))
            setattr(exc, "response_task_state_snapshot", captured.task_state)
        except Exception:  # noqa: BLE001 - never mask the original failure.
            pass

    @contextmanager
    def _defer_error_response_snapshot_capture(self):
        """Let an enclosing A3 runtime own the combined error snapshot."""

        depth = int(getattr(self._error_snapshot_capture_local, "depth", 0))
        self._error_snapshot_capture_local.depth = depth + 1
        try:
            yield
        finally:
            if depth:
                self._error_snapshot_capture_local.depth = depth
            else:
                try:
                    del self._error_snapshot_capture_local.depth
                except AttributeError:
                    pass

    def _error_response_snapshot_capture_deferred(self) -> bool:
        return bool(getattr(self._error_snapshot_capture_local, "depth", 0))

    def _check_daily_budget(self, identity_key: str = "") -> None:
        if self.cost_ledger is None:
            return
        global_budget_micros = self._daily_budget_micros
        identity_budget_micros = self._per_identity_daily_budget_micros
        if self._budget_policy is not None:
            limits = self._budget_policy.budget_limits_for(str(identity_key).strip())
            global_budget_micros = max(0, int(limits.global_daily_micros))
            identity_budget_micros = max(0, int(limits.identity_daily_micros))
        now = datetime.now(self._budget_timezone)
        local_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        started_at = local_start.astimezone(UTC).isoformat()
        if global_budget_micros > 0 and self.cost_ledger.estimated_cost_micros_since(
            started_at
        ) >= global_budget_micros:
            raise AgentBudgetExceededError(
                "今日服务额度已用完，请明天再试。",
                code="GLOBAL_DAILY_QUOTA_EXCEEDED",
            )
        clean_identity = str(identity_key).strip()
        if identity_budget_micros <= 0:
            return
        if not clean_identity:
            raise AgentBudgetExceededError(
                "当前请求缺少有效邀请码，请重新登录。",
                code="INVITE_IDENTITY_MISSING",
            )
        spent = self.cost_ledger.estimated_cost_micros_since(
            started_at, identity_key=clean_identity
        )
        if spent >= identity_budget_micros:
            raise AgentBudgetExceededError(
                "该邀请码今日额度已用完，请明天再试。",
                code="INVITE_DAILY_QUOTA_EXCEEDED",
            )

    def ensure_budget_available(self, identity_key: str = "") -> None:
        """Expose the same dynamic budget check to a parent A3 workflow."""

        self._check_daily_budget(identity_key)

    def _write_task_log(
        self,
        *,
        task_id: str,
        session_id: str,
        kind: str,
        started_at: datetime,
        duration_ms: int,
        phase_before: str,
        state: AgentState,
        response: AgentResponse | None,
        error_kind: str,
        identity_key: str = "",
        protocol: RequestProtocol | None = None,
        trace_id: str = "",
    ) -> None:
        outcome = _task_outcome(state, response, error_kind)
        resolved_protocol = protocol or _request_protocol(
            state,
            response,
            error_kind,
            request_id=task_id,
        )
        entry = TaskLogEntry(
            task_id=task_id,
            session_key=session_key(session_id),
            kind=kind,  # type: ignore[arg-type]  # Internal callers pass only image/text.
            started_at=started_at.isoformat(),
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=max(0, duration_ms),
            phase_before=phase_before,
            phase_after=state.phase,
            outcome=outcome,
            question_count=len(state.questions),
            candidate_count=len(state.candidates),
            chapter=state.current_chapter,
            route=state.current_route,
            error_kind=error_kind or ("agent_error" if state.phase == "ERROR" else ""),
            trace_id=str(trace_id or current_trace_id()),
            request_id=resolved_protocol.request_id or task_id,
            search_id=resolved_protocol.search_id or state.current_search_id,
            identity_key=str(identity_key or "").strip(),
            status=resolved_protocol.status.value,
            layer=resolved_protocol.layer.value,
            code=resolved_protocol.code,
            retryable=resolved_protocol.retryable,
            action=resolved_protocol.action.value,
        )
        try:
            self.task_logger.write(entry)
        except Exception:  # noqa: BLE001 - observability must not break the Agent.
            pass

    @staticmethod
    def _attach_response_protocol(
        response: AgentResponse,
        *,
        state: AgentState,
        request_id: str,
        search_id: str,
    ) -> None:
        response.protocol = _request_protocol(
            state,
            response,
            "",
            request_id=request_id,
            search_id=search_id,
        ).to_dict()

    @staticmethod
    def _clean_session_id(session_id: str) -> str:
        clean = str(session_id).strip()
        if not clean:
            raise ValueError("session_id is required")
        return clean

    def _load_session_state(self, session_id: str) -> AgentState | None:
        """Load authoritative child state and mark an unreadable store once."""

        try:
            return self.store.load(session_id)
        except Exception as exc:
            setattr(exc, "_task_state_read_set_unavailable", True)
            raise

    def _lock(self, session_id: str) -> threading.Lock:
        return self._session_locks[hash(session_id) % len(self._session_locks)]


def _bind_trace_lifecycle(
    session_id: str,
    search_id: str = "",
    *,
    identity_key: str = "",
) -> None:
    event_session = current_trace_event_session()
    existing = event_session.dimensions if event_session is not None else {}
    clean_search_id = str(search_id or "").strip()
    dimensions = {"session_key": session_key(session_id)}
    if clean_search_id:
        dimensions["search_id"] = clean_search_id
        dimensions["workflow_search_id"] = (
            existing.get("workflow_search_id") or clean_search_id
        )
    if str(identity_key or "").strip():
        dimensions["identity_key"] = str(identity_key).strip()
    bind_trace_event_dimensions(**dimensions)


def _task_outcome(state: AgentState, response: AgentResponse | None, error_kind: str) -> str:
    if error_kind or state.phase == "ERROR":
        return "error"
    if response is not None and response.intent == "cancel":
        return "cancelled"
    if state.phase == "ANSWERED":
        return "answered"
    if state.phase == "NO_MATCH":
        return "no_match"
    if state.candidates:
        return "candidates"
    return "waiting"


def _request_protocol(
    state: AgentState,
    response: AgentResponse | None,
    error_kind: str,
    *,
    request_id: str,
    search_id: str = "",
) -> RequestProtocol:
    if response is not None and response.protocol:
        payload = dict(response.protocol)
        payload["request_id"] = request_id
        payload["search_id"] = (
            search_id or payload.get("search_id") or state.current_search_id
        )
        return RequestProtocol.from_dict(payload)
    if error_kind or state.phase == "ERROR":
        code = "AGENT_FAILED"
    elif response is not None and response.intent == "external_load_screen":
        code = "EXTERNAL_LOAD_NOT_FOUND"
    elif state.phase == "NO_MATCH":
        code = "NO_MATCH"
    elif state.phase == "WAIT_CHAPTER":
        code = "CHAPTER_REQUIRED"
    else:
        code = "REQUEST_SUCCEEDED"
    return RequestProtocol.from_code(
        code,
        request_id=request_id,
        search_id=search_id or state.current_search_id,
    )

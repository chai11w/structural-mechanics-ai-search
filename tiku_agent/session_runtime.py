"""Session-aware outer layer for the isolated question-bank Agent."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import threading
import time
from typing import Any, Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.external_load_screen import (
    ImageSearchCancelled,
    NO_EXTERNAL_LOAD_MESSAGE,
)
from tiku_agent.session_artifacts import SessionArtifacts, session_key
from tiku_agent.session_store import SessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger, TaskLogEntry, TaskLogger
from tiku_agent.tools import AgentToolConfig
from tiku_shared.model_costs import (
    ModelCostCollector,
    SQLiteModelCostLedger,
    model_cost_scope,
    submit_with_model_cost_context,
)


AgentFactory = Callable[[AgentState], TikuSearchAgent]
ProgressReporter = Callable[[str, str], None]
ExternalLoadScreen = Callable[[str | Path], str]


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


class AgentRuntimeBusyError(RuntimeError):
    """The bounded web runtime cannot accept another task right now."""


class AgentBudgetExceededError(RuntimeError):
    """The configured daily model-cost ceiling has already been reached."""


class _ExecutionGate:
    def __init__(self, max_concurrent: int, max_queued: int, wait_seconds: float) -> None:
        self.max_concurrent = max(0, int(max_concurrent or 0))
        self.max_queued = max(0, int(max_queued or 0))
        self.wait_seconds = max(0.0, float(wait_seconds or 0.0))
        self._active = 0
        self._waiting = 0
        self._condition = threading.Condition()

    @contextmanager
    def enter(self, progress: ProgressReporter | None = None):
        if self.max_concurrent <= 0:
            yield
            return

        queued = False
        with self._condition:
            if self._active >= self.max_concurrent:
                if self._waiting >= self.max_queued:
                    raise AgentRuntimeBusyError("当前请求较多，请稍后再试。")
                queued = True
                self._waiting += 1
                if progress is not None:
                    progress("queued", "前面有任务正在处理，已进入队列…")
                deadline = time.monotonic() + self.wait_seconds
                try:
                    while self._active >= self.max_concurrent:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AgentRuntimeBusyError("排队等待超时，请稍后重新提交。")
                        self._condition.wait(remaining)
                finally:
                    self._waiting -= 1
            self._active += 1

        try:
            if queued and progress is not None:
                progress("dequeued", "轮到你的题目了，正在开始处理…")
            yield
        finally:
            with self._condition:
                self._active -= 1
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

    def handle_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)

        def execute() -> AgentResponse:
            self.purge_expired()
            persisted_image = self.artifacts.persist_image(clean_session_id, image_path)
            if self.external_load_screen is not None:
                return self._run_screened_image(
                    clean_session_id,
                    persisted_image,
                    identity_key=identity_key,
                    progress=progress,
                )
            return self._run(
                clean_session_id,
                "image",
                lambda agent: agent.handle_image(persisted_image),
                identity_key=identity_key,
                progress=progress,
            )

        return self._admit(
            clean_session_id, execute, identity_key=identity_key, progress=progress
        )

    def _run_screened_image(
        self,
        session_id: str,
        image_path: Path,
        *,
        identity_key: str,
        progress: ProgressReporter | None,
    ) -> AgentResponse:
        baseline_state = self.store.load(session_id) or AgentState(session_id=session_id)
        phase_before = baseline_state.phase
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        task_id = uuid4().hex
        collector = ModelCostCollector(
            run_id=task_id,
            session_key=session_key(session_id),
            identity_key=str(identity_key).strip(),
            task_kind="image",
            started_at=started_at.isoformat(),
        )
        race = _ImageRace()
        agent = self._make_agent(
            AgentState.from_dict(baseline_state.to_dict()), progress=progress
        )
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
                self._image_executor, agent.handle_image, image_path
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
                        baseline_state, image_path
                    )
                    break

            if search_future.done():
                try:
                    search_response = search_future.result()
                except ImageSearchCancelled:
                    if race.cancel_search.is_set():
                        response_state, response = self._no_load_response(
                            baseline_state, image_path
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
                    baseline_state, image_path
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
                        baseline_state, image_path
                    )
                else:
                    response_state = agent.state

        assert response is not None and response_state is not None
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
            )
        else:
            if not search_future.done():
                self._track_background_image_future(session_id, search_future)
            assert self._observer_executor is not None
            self._observer_executor.submit(
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
        state_before: AgentState, image_path: Path
    ) -> tuple[AgentState, AgentResponse]:
        state = AgentState.from_dict(state_before.to_dict())
        state.start_search(str(image_path))
        state.set_candidates([])
        state.last_error = NO_EXTERNAL_LOAD_MESSAGE
        return state, AgentResponse(
            text=NO_EXTERNAL_LOAD_MESSAGE,
            state=state.to_dict(),
            intent="external_load_screen",
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
        )
        if self.cost_ledger is not None:
            try:
                if state.task_revision > 0:
                    collector.search_key = (
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
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        return self._admit(
            clean_session_id,
            lambda: self._run(
                clean_session_id,
                "text",
                lambda agent: agent.handle_text(text),
                identity_key=identity_key,
                progress=progress,
            ),
            identity_key=identity_key,
            progress=progress,
        )

    def clear(self, session_id: str) -> None:
        """Explicitly start a fresh conversation and remove its temporary files."""
        clean_session_id = self._clean_session_id(session_id)
        lock = self._session_locks[hash(clean_session_id) % len(self._session_locks)]
        with lock:
            self._await_background_image_work(clean_session_id)
            self.store.clear(clean_session_id)
            self.artifacts.clear_session(clean_session_id)

    def current_image_path(self, session_id: str) -> Path | None:
        """Return the current persisted upload for a live session."""
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        state = self.store.load(clean_session_id)
        if state is None or not state.current_image_path:
            return None
        path = Path(state.current_image_path)
        return path if self.resolve_upload(clean_session_id, path.name) == path.resolve() else None

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        """Return the small, non-sensitive state contract needed by the web client."""
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        state = self.store.load(clean_session_id)
        if state is None:
            return {
                "session_valid": False,
                "phase": "IDLE",
                "has_active_image": False,
                "task_revision": 0,
                "candidate_generation": "",
                "candidate_count": 0,
                "chapter": "",
            }
        return {
            "session_valid": True,
            "phase": state.phase,
            "has_active_image": bool(state.active_image_path),
            "task_revision": state.task_revision,
            "candidate_generation": state.candidate_generation,
            "candidate_count": state.candidate_count,
            "chapter": state.chapter,
        }

    def resolve_upload(self, session_id: str, filename: str) -> Path | None:
        """Resolve one session-owned upload without exposing arbitrary paths."""
        return self._resolve_artifact(session_id, "uploads", filename)

    def persist_media(self, session_id: str, source: str | Path) -> Path | None:
        """Keep candidate/answer media available for the live conversation."""
        clean_session_id = self._clean_session_id(session_id)
        if self.store.load(clean_session_id) is None:
            return None
        return self.artifacts.persist_media(clean_session_id, source)

    def resolve_media(self, session_id: str, filename: str) -> Path | None:
        return self._resolve_artifact(session_id, "media", filename)

    def _resolve_artifact(self, session_id: str, folder: str, filename: str) -> Path | None:
        clean_session_id = self._clean_session_id(session_id)
        if self.store.load(clean_session_id) is None:
            return None
        safe_name = Path(str(filename)).name
        if not safe_name or safe_name != str(filename):
            return None
        artifact_dir = (self.artifacts.session_dir(clean_session_id) / folder).resolve()
        target = (artifact_dir / safe_name).resolve()
        if target.parent != artifact_dir or not target.is_file():
            return None
        return target

    def _run(
        self,
        session_id: str,
        kind: str,
        handler: Callable[[TikuSearchAgent], AgentResponse],
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
    ) -> AgentResponse:
        clean_session_id = self._clean_session_id(session_id)
        self.purge_expired()
        state = self.store.load(clean_session_id) or AgentState(session_id=clean_session_id)
        phase_before = state.phase
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        task_id = uuid4().hex
        cost_collector = ModelCostCollector(
            run_id=task_id,
            session_key=session_key(clean_session_id),
            identity_key=str(identity_key).strip(),
            task_kind=kind,
            started_at=started_at.isoformat(),
        )
        agent = self._make_agent(state, progress=progress)
        response: AgentResponse | None = None
        error_kind = ""
        try:
            with model_cost_scope(cost_collector):
                response = handler(agent)
            if response.intent == "cancel":
                self.store.clear(clean_session_id)
                self.artifacts.clear_session(clean_session_id)
            else:
                self.store.save(agent.state)
            return response
        except Exception as exc:
            error_kind = type(exc).__name__
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
            )
            if self.cost_ledger is not None:
                try:
                    if agent.state.task_revision > 0:
                        cost_collector.search_key = (
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
        self.artifacts.clear_sessions(self.store.purge_expired())

    def _admit(
        self,
        session_id: str,
        execute: Callable[[], AgentResponse],
        *,
        identity_key: str = "",
        progress: ProgressReporter | None = None,
    ) -> AgentResponse:
        with self._execution_gate.enter(progress):
            lock = self._session_locks[hash(session_id) % len(self._session_locks)]
            with lock:
                self._await_background_image_work(session_id)
                self._check_daily_budget(identity_key)
                return execute()

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
            raise AgentBudgetExceededError("今日服务额度已用完，请明天再试。")
        clean_identity = str(identity_key).strip()
        if identity_budget_micros <= 0:
            return
        if not clean_identity:
            raise AgentBudgetExceededError("当前请求缺少有效邀请码，请重新登录。")
        spent = self.cost_ledger.estimated_cost_micros_since(
            started_at, identity_key=clean_identity
        )
        if spent >= identity_budget_micros:
            raise AgentBudgetExceededError("该邀请码今日额度已用完，请明天再试。")

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
    ) -> None:
        outcome = _task_outcome(state, response, error_kind)
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
        )
        try:
            self.task_logger.write(entry)
        except Exception:  # noqa: BLE001 - observability must not break the Agent.
            pass

    @staticmethod
    def _clean_session_id(session_id: str) -> str:
        clean = str(session_id).strip()
        if not clean:
            raise ValueError("session_id is required")
        return clean


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

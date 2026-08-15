"""Non-authoritative image-triage observation for the isolated 8890 line."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
from threading import BoundedSemaphore, Lock
import time
from typing import Protocol
from uuid import uuid4

from .image_contracts import IMAGE_TRIAGE_SCHEMA_VERSION, ImageTriageObservation
from .image_triage import QwenImageTriageResult, build_handoff


SHADOW_LOG_SCHEMA_VERSION = 1


class ImageTriageObserver(Protocol):
    def observe(self, image_path: str | Path) -> ImageTriageObservation: ...


@dataclass(frozen=True)
class ImageTriageShadowRecord:
    """One local shadow result without session identifiers or filesystem paths."""

    request_id: str
    recorded_at: str
    status: str
    duration_ms: int
    model: str = ""
    route_candidate: str = ""
    final_route: str = ""
    question_count: int | None = None
    original_structure_count: int | None = None
    auxiliary_diagram_count: int | None = None
    has_actual_load_evidence: bool | None = None
    has_structure_content: bool | None = None
    image_recoverable: bool | None = None
    has_ambiguity: bool | None = None
    observation: str = ""
    reasons: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error_kind: str = ""
    error_message: str = ""
    triage_schema_version: str = IMAGE_TRIAGE_SCHEMA_VERSION
    schema_version: int = SHADOW_LOG_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class JsonlImageTriageShadowLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def write(self, record: ImageTriageShadowRecord) -> None:
        payload = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")


class ImageTriageShadowRunner:
    """Run bounded background observations which can never affect user results."""

    def __init__(
        self,
        observer: ImageTriageObserver,
        *,
        runtime_dir: str | Path,
        logger: JsonlImageTriageShadowLogger | None = None,
        max_workers: int = 2,
        max_pending: int = 16,
    ) -> None:
        root = Path(runtime_dir).resolve()
        self.observer = observer
        self.input_dir = root / "triage_shadow_inputs"
        self.logger = logger or JsonlImageTriageShadowLogger(
            root / "triage_shadow.jsonl"
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="tiku-8890-triage-shadow",
        )
        self._capacity = BoundedSemaphore(max(1, int(max_pending)))
        self._futures_lock = Lock()
        self._futures: set[Future[None]] = set()

    def submit(self, image_path: str | Path, *, request_id: str = "") -> bool:
        clean_request_id = str(request_id or "").strip() or f"shadow_{uuid4().hex}"
        if not self._capacity.acquire(blocking=False):
            self._write_error(
                request_id=clean_request_id,
                status="skipped",
                error_kind="queue_full",
                error_message="影子预检队列已满",
            )
            return False

        source = Path(image_path)
        suffix = source.suffix.lower() if source.suffix else ".img"
        shadow_input = self.input_dir / f"{uuid4().hex}{suffix}"
        try:
            self.input_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, shadow_input)
            future = self._executor.submit(
                self._run,
                shadow_input,
                clean_request_id,
            )
        except Exception:  # noqa: BLE001 - shadow failures cannot affect the live request.
            shadow_input.unlink(missing_ok=True)
            self._capacity.release()
            self._write_error(
                request_id=clean_request_id,
                status="error",
                error_kind="input_copy_error",
                error_message="影子预检无法保存临时图片",
            )
            return False

        with self._futures_lock:
            self._futures.add(future)
        future.add_done_callback(
            lambda completed, path=shadow_input: self._forget(completed, path)
        )
        return True

    def wait_for_idle(self, timeout_seconds: float | None = None) -> bool:
        with self._futures_lock:
            futures = tuple(self._futures)
        if not futures:
            return True
        _, pending = wait(futures, timeout=timeout_seconds)
        return not pending

    def close(self, *, wait_for_work: bool = False) -> None:
        self._executor.shutdown(wait=wait_for_work, cancel_futures=not wait_for_work)

    def _run(self, image_path: Path, request_id: str) -> None:
        started = time.perf_counter()
        try:
            observation, model, usage = self._observe(image_path)
            handoff = build_handoff("", observation)
            self.logger.write(
                ImageTriageShadowRecord(
                    request_id=request_id,
                    recorded_at=datetime.now(UTC).isoformat(),
                    status="ok",
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    model=model,
                    route_candidate=observation.route_candidate,
                    final_route=handoff.route,
                    question_count=observation.question_count,
                    original_structure_count=observation.original_structure_count,
                    auxiliary_diagram_count=observation.auxiliary_diagram_count,
                    has_actual_load_evidence=observation.has_actual_load_evidence,
                    has_structure_content=observation.has_structure_content,
                    image_recoverable=observation.image_recoverable,
                    has_ambiguity=observation.has_ambiguity,
                    observation=observation.raw_text,
                    reasons=handoff.reason,
                    unknowns=observation.unknowns,
                    prompt_tokens=usage[0],
                    completion_tokens=usage[1],
                    total_tokens=usage[2],
                    triage_schema_version=observation.schema_version,
                )
            )
        except ValueError:
            self._write_error(
                request_id=request_id,
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_kind="parse_error",
                error_message="模型回答无法解析",
            )
        except Exception:  # noqa: BLE001 - model/config errors are observations, not user errors.
            self._write_error(
                request_id=request_id,
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_kind="model_error",
                error_message="模型调用或配置异常",
            )
        finally:
            image_path.unlink(missing_ok=True)
            self._capacity.release()

    def _observe(
        self, image_path: Path
    ) -> tuple[ImageTriageObservation, str, tuple[int, int, int]]:
        observe_with_metadata = getattr(self.observer, "observe_with_metadata", None)
        if callable(observe_with_metadata):
            result = observe_with_metadata(image_path)
            if isinstance(result, QwenImageTriageResult):
                return (
                    result.observation,
                    result.model,
                    (
                        result.prompt_tokens,
                        result.completion_tokens,
                        result.total_tokens,
                    ),
                )
        observation = self.observer.observe(image_path)
        return observation, type(self.observer).__name__, (0, 0, 0)

    def _forget(self, future: Future[None], shadow_input: Path) -> None:
        with self._futures_lock:
            self._futures.discard(future)
        if future.cancelled():
            shadow_input.unlink(missing_ok=True)
            self._capacity.release()

    def _write_error(
        self,
        *,
        request_id: str,
        status: str,
        error_kind: str,
        error_message: str,
        duration_ms: int = 0,
    ) -> None:
        try:
            self.logger.write(
                ImageTriageShadowRecord(
                    request_id=request_id,
                    recorded_at=datetime.now(UTC).isoformat(),
                    status=status,
                    duration_ms=max(0, duration_ms),
                    error_kind=error_kind,
                    error_message=error_message,
                )
            )
        except Exception:  # noqa: BLE001 - observability cannot affect a request.
            pass


class ImageTriageShadowRuntime:
    """Decorate only 8890 image turns while delegating every existing behavior."""

    def __init__(self, runtime: object, shadow: ImageTriageShadowRunner) -> None:
        self._runtime = runtime
        self.triage_shadow = shadow

    def __getattr__(self, name: str) -> object:
        return getattr(self._runtime, name)

    def handle_image(
        self,
        session_id: str,
        image_path: str | Path,
        *,
        identity_key: str = "",
        progress=None,
        request_id: str = "",
    ):
        try:
            self.triage_shadow.submit(image_path, request_id=request_id)
        except Exception:  # noqa: BLE001 - preserve the existing image path under all failures.
            pass
        return self._runtime.handle_image(
            session_id,
            image_path,
            identity_key=identity_key,
            progress=progress,
            request_id=request_id,
        )

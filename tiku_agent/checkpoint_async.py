"""Nonblocking capture admission with one bounded FIFO evidence consumer."""

from collections import deque
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import math
import re
from threading import Condition, Event, Thread
from time import monotonic, perf_counter
from uuid import uuid4

from tiku_agent.a2_checkpoint_recorder import A2CaptureRecordResultV1
from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1
from tiku_agent.checkpoint_capture import A2CheckpointContextV1
from tiku_agent.checkpoint_contract import SCOPE_WORKFLOW, ProducerVersionV1, STAGE_CONTRACTS
from tiku_agent.checkpoint_resource_leases import CheckpointResourceLeases
from tiku_agent.checkpoint_stage_input import FrozenCheckpointInput, materialize_a2_stage_input
from tiku_agent.checkpoint_submission_budget import current_checkpoint_budget
from tiku_shared.trace_context import is_valid_trace_id
from tiku_shared.evidence_io_budget import evidence_io_budget, check_evidence_budget, EvidenceDeadlineExceeded
from tiku_shared.trace_events import _trace_writer_maintenance_lock, _trace_reject_linked_path, TraceEventMaintenanceError


@dataclass(frozen=True)
class QueuedCapture:
    token: str
    context: A2CheckpointContextV1
    frozen: FrozenCheckpointInput = field(repr=False)
    admission: A2CaptureAdmissionV1
    stage: str
    kind: str
    created: float
    byte_size: int


class AsyncCheckpointRecorder:
    def __init__(self, engine, *, trace_recorder, max_pending=128, max_bytes=8 * 1024 * 1024,
                 max_age_seconds=120.0, clock=monotonic, autostart=True,
                 run_budget_seconds=2.0, failure_threshold=3, cooldown_seconds=5.0):
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise ValueError("invalid capture queue capacity")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid capture queue byte limit")
        if not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 600:
            raise ValueError("invalid capture queue age")
        if (not math.isfinite(run_budget_seconds) or not 0 < run_budget_seconds <= 30
                or type(failure_threshold) is not int or not 1 <= failure_threshold <= 100
                or not math.isfinite(cooldown_seconds) or not 0 < cooldown_seconds <= 300):
            raise ValueError("invalid capture recovery budget")
        self.engine = engine
        self.engine.trace_recorder = trace_recorder
        self.producer, self.gate = engine.producer, engine.gate
        self.store, self.media_root = engine.store, engine.media_root
        self.max_pending, self.max_bytes, self.max_age_seconds = max_pending, max_bytes, max_age_seconds
        self.clock = clock
        self.run_budget_seconds = run_budget_seconds
        self.failure_threshold, self.cooldown_seconds = failure_threshold, cooldown_seconds
        self._active = None
        self._active_started = 0.0
        self._cancel = Event()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self.resource_leases = CheckpointResourceLeases(clock=clock)
        self._condition = Condition()
        self._queue = deque()
        self._pending = self._bytes = 0
        self._closed = False
        self._counts = {key: 0 for key in ("queued", "stored", "rejected", "expired", "failed",
                       "shutdown_dropped", "circuit_dropped", "partial", "resource_unavailable", "recoveries")}
        self._last_failure = ""
        self._worker = Thread(target=self._consume, name="tiku-checkpoint-consumer", daemon=True)
        self._started = False
        if autostart:
            self.start()

    def start(self):
        with self._condition:
            if not self._started and not self._closed:
                self._worker.start()
                self._started = True

    def input_unavailable(self):
        self._reject("CAPTURE_INPUT_UNAVAILABLE")

    def _reject(self, code):
        with self._condition:
            self._counts["rejected"] = min(2_147_483_647, self._counts["rejected"] + 1)
            self._last_failure = code
        return A2CaptureRecordResultV1(True, False, code)

    def submit_stage(self, context, frozen, *, stage, admission, budget=None, started=None):
        try:
            value = frozen.materialize()["payload"]
            paths = [value.get(key, "") for key in ("image_path", "capture_image_path", "source_image_path")]
            return self._enqueue(context, frozen, stage, "a2", admission, paths, budget=budget, started=started)
        except Exception:
            return self._reject("CAPTURE_INPUT_UNAVAILABLE")

    def capture_parent(self, state, *, stage, identity_key, **kwargs):
        started = perf_counter()
        admission = A2CaptureAdmissionV1("search", bool(identity_key), True, True, True, True)
        if not self.gate.decide(admission).permitted:
            return A2CaptureRecordResultV1(False, False, "CAPTURE_DISABLED")
        try:
            context, frozen = self.engine.freeze_parent(state, identity_key=identity_key, **kwargs)
            value = frozen.materialize()
            budget = current_checkpoint_budget.get()
            return self._enqueue(context, frozen, stage, "a3", admission,
                [value["source_page"], value["record"].get("path", "")], budget=budget, started=started)
        except Exception:
            return self._reject("CAPTURE_INPUT_UNAVAILABLE")

    def _enqueue(self, context, frozen, stage, kind, admission, paths, *, budget=None, started=None):
        decision = self.gate.decide(admission)
        if not decision.permitted:
            return A2CaptureRecordResultV1(False, False, decision.reason_code)
        if (type(context) is not A2CheckpointContextV1 or type(context.producer) is not ProducerVersionV1
                or not is_valid_trace_id(context.trace_id) or stage not in STAGE_CONTRACTS
                or (context.request_id and not re.fullmatch(r"req_[0-9a-f]{32}", context.request_id))):
            return self._reject("CAPTURE_INPUT_UNAVAILABLE")
        context.owner()
        metadata = FrozenCheckpointInput.capture(asdict(context))
        size = len(frozen.encoded) + len(metadata.encoded) + 2048
        job = QueuedCapture(uuid4().hex, context, frozen, admission, stage, kind, self.clock(), size)
        with self._condition:
            if self._closed or not self._started:
                code = "CAPTURE_QUEUE_CLOSED"
            elif self._stalled():
                code = "CAPTURE_CONSUMER_STALLED"
            elif self.clock() < self._open_until:
                code = "CAPTURE_CIRCUIT_OPEN"
            elif self._pending >= self.max_pending:
                code = "CAPTURE_QUEUE_FULL"
            elif self._bytes + size > self.max_bytes:
                code = "CAPTURE_QUEUE_BYTES_FULL"
            elif budget is not None and not budget.charge(perf_counter() - started):
                code = "CAPTURE_REQUEST_BUDGET_EXHAUSTED"
            elif not self.resource_leases.acquire(job.token, paths, job.created + self.max_age_seconds):
                code = "CAPTURE_RESOURCE_CLEARING"
            else:
                self._queue.append(job)
                self._pending += 1
                self._bytes += size
                self._counts["queued"] = min(2_147_483_647, self._counts["queued"] + 1)
                self._condition.notify_all()
                # Acceptance never invents a checkpoint id or a successful persistence.
                return A2CaptureRecordResultV1(True, False, "CAPTURE_QUEUED")
        return self._reject(code)

    def _predecessors(self, context):
        try:
            owner = context.owner()
            owners = [owner]
            parent = replace(owner, scope=SCOPE_WORKFLOW, search_id="", candidate_generation="",
                             task_revision=owner.workflow_task_revision)
            for candidate in (parent, replace(parent, unit_id="")):
                if candidate not in owners:
                    owners.append(candidate)
            latest = success = None
            for candidate in owners:
                latest = latest or self.store.latest_checkpoint(candidate, actor_key="checkpoint_consumer")
                success = success or self.store.latest_successful_checkpoint(candidate, actor_key="checkpoint_consumer")
                if latest and success:
                    break
            return latest.checkpoint_id if latest else "", success.checkpoint_id if success else ""
        except Exception:
            self.engine.input_unavailable()
            return "", ""

    def _execute(self, job):
        context = job.context
        if job.kind == "a2" and job.stage in {"image_accepted", "image_routed"}:
            context = replace(context, scope=SCOPE_WORKFLOW, search_id="", candidate_generation="")
        predecessor, success = self._predecessors(context)
        check_evidence_budget()
        if self.clock() - job.created >= self.max_age_seconds:
            return A2CaptureRecordResultV1(True, False, "CAPTURE_QUEUE_EXPIRED")
        if job.kind == "a3":
            return self.engine.capture_parent_frozen(context, job.frozen, stage=job.stage, admission=job.admission,
                predecessor_checkpoint_id=predecessor, last_successful_checkpoint_id=success)
        result, payload = materialize_a2_stage_input(job.frozen)
        image = payload.pop("capture_image_path", "")
        if image:
            try:
                payload["inputs"]["source_image"] = sha256(self.engine.read_image(image)).hexdigest()
            except Exception:
                self.engine.input_unavailable()
                payload["inputs"]["source_image"] = ""
        check_evidence_budget()
        return self.engine.capture_stage(context, stage=job.stage, admission=job.admission,
            tool_result=result, payload=payload, predecessor_checkpoint_id=predecessor,
            last_successful_checkpoint_id=success)

    def _consume(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closed)
                if not self._queue:
                    return
                job = self._queue.popleft()
                self._active, self._active_started = job, monotonic()
            try:
                before = self.engine.health()["counters"]["rejected"]
                if self.clock() - job.created >= self.max_age_seconds:
                    result = A2CaptureRecordResultV1(True, False, "CAPTURE_QUEUE_EXPIRED")
                elif self.clock() < self._open_until:
                    result = A2CaptureRecordResultV1(True, False, "CAPTURE_CIRCUIT_OPEN")
                else:
                    with evidence_io_budget(self.run_budget_seconds, cancel=self._cancel, capture=True):
                        _trace_reject_linked_path(self.store.path)
                        self.store.path.parent.mkdir(parents=True, exist_ok=True)
                        _trace_reject_linked_path(self.store.path)
                        # Separate fence: the ordinary Trace fence remains available to our own Trace writer.
                        with _trace_writer_maintenance_lock(self.store.path, filename=".checkpoint_capture.lock"):
                            result = self._execute(job)
                partial = result.stored and (bool(result.evidence_failure_code)
                    or self.engine.health()["counters"]["rejected"] > before)
            except EvidenceDeadlineExceeded:
                result, partial = A2CaptureRecordResultV1(True, False, "CAPTURE_IO_TIMEOUT"), False
            except TraceEventMaintenanceError:
                result, partial = A2CaptureRecordResultV1(True, False, "CAPTURE_MAINTENANCE_BUSY"), False
            except BaseException:
                result, partial = A2CaptureRecordResultV1(True, False, "CAPTURE_CONSUMER_FAILED"), False
            finally:
                # Do not put file deletion in the shutdown caller or under the queue lock.
                # Keep this job active until cleanup finishes, so a blocked unlink is visible.
                try:
                    self.resource_leases.release(job.token, cleanup=not self._cancel.is_set())
                except Exception:
                    pass
                with self._condition:
                    key = "stored" if result.stored else {
                        "CAPTURE_QUEUE_EXPIRED": "expired", "CAPTURE_CIRCUIT_OPEN": "circuit_dropped"
                    }.get(result.reason_code, "failed")
                    self._counts[key] += 1
                    if partial:
                        self._counts["partial"] += 1
                        resource_code = result.evidence_failure_code or self.engine.health()["last_failure_code"]
                        self._counts["resource_unavailable"] += int(any(word in resource_code
                            for word in ("SOURCE_UNAVAILABLE", "ARTIFACT_UNAVAILABLE", "REFERENCE_UNAVAILABLE")))
                    if (not result.stored or partial) and key not in {"expired", "circuit_dropped"}:
                        self._last_failure = "CAPTURE_EVIDENCE_PARTIAL" if partial else result.reason_code
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= self.failure_threshold:
                            self._open_until = self.clock() + self.cooldown_seconds
                    elif result.stored:
                        self._counts["recoveries"] += int(self._consecutive_failures > 0)
                        self._consecutive_failures, self._open_until, self._last_failure = 0, 0.0, ""
                    elif key == "expired":
                        self._last_failure = result.reason_code
                    self._counts = {name: min(2_147_483_647, count) for name, count in self._counts.items()}
                    self._pending -= 1
                    self._bytes -= job.byte_size
                    self._active = None
                    self._condition.notify_all()

    def flush(self, timeout=5.0):
        with self._condition:
            return self._condition.wait_for(lambda: self._pending == 0, timeout=max(0.0, timeout))

    def close(self, timeout=2.0):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._started:
            self._worker.join(timeout=max(0.0, timeout))
        with self._condition:
            if self._pending:
                self._cancel.set()
                while self._queue:
                    job = self._queue.popleft()
                    self.resource_leases.release(job.token, cleanup=False)
                    self._pending -= 1
                    self._bytes -= job.byte_size
                    self._counts["shutdown_dropped"] += 1
                self._condition.notify_all()
        return not self._worker.is_alive()

    def _stalled(self):
        return self._active is not None and monotonic() - self._active_started >= self.run_budget_seconds

    def health(self):
        with self._condition:
            stalled = self._stalled()
            circuit = self.clock() < self._open_until
            code = "CAPTURE_CONSUMER_STALLED" if stalled else "CAPTURE_CIRCUIT_OPEN" if circuit else self._last_failure
            if self._closed and self._pending:
                code = "CAPTURE_SHUTDOWN_PENDING"
            return {"status": "disabled" if not self.gate.enabled else "degraded" if code else "ok",
                    "current_reasons": [code.lower()] if code else [], "last_failure_code": code.lower(),
                    "counters": {**self._counts}, "pending": self._pending, "pending_bytes": self._bytes,
                    "queue_capacity": self.max_pending, "queue_byte_capacity": self.max_bytes,
                    "max_age_seconds": self.max_age_seconds, "run_budget_seconds": self.run_budget_seconds,
                    "running": int(self._active is not None), "backlog": len(self._queue),
                    "stalled": stalled, "circuit_open": circuit,
                    "accepting": self._started and not self._closed and not stalled and not circuit,
                    "worker_alive": self._worker.is_alive()}

"""Nonblocking capture admission with one bounded FIFO evidence consumer."""

from collections import deque
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import math
import re
from threading import Condition, Thread
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
                 max_age_seconds=120.0, clock=monotonic, autostart=True):
        if type(max_pending) is not int or not 1 <= max_pending <= 4096:
            raise ValueError("invalid capture queue capacity")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid capture queue byte limit")
        if not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 600:
            raise ValueError("invalid capture queue age")
        self.engine = engine
        self.engine.trace_recorder = trace_recorder
        self.producer, self.gate = engine.producer, engine.gate
        self.store, self.media_root = engine.store, engine.media_root
        self.max_pending, self.max_bytes, self.max_age_seconds = max_pending, max_bytes, max_age_seconds
        self.clock = clock
        self.resource_leases = CheckpointResourceLeases(clock=clock)
        self._condition = Condition()
        self._queue = deque()
        self._pending = self._bytes = 0
        self._closed = False
        self._counts = {"queued": 0, "stored": 0, "rejected": 0, "expired": 0, "failed": 0}
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
            try:
                if self.clock() - job.created >= self.max_age_seconds:
                    result = A2CaptureRecordResultV1(True, False, "CAPTURE_QUEUE_EXPIRED")
                else:
                    result = self._execute(job)
                with self._condition:
                    key = "stored" if result.stored else "expired" if result.reason_code == "CAPTURE_QUEUE_EXPIRED" else "failed"
                    self._counts[key] = min(2_147_483_647, self._counts[key] + 1)
                    if not result.stored:
                        self._last_failure = result.reason_code
            except Exception:
                with self._condition:
                    self._counts["failed"] = min(2_147_483_647, self._counts["failed"] + 1)
                    self._last_failure = "CAPTURE_CONSUMER_FAILED"
            finally:
                self.resource_leases.release(job.token)
                with self._condition:
                    self._pending -= 1
                    self._bytes -= job.byte_size
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
        return not self._worker.is_alive()

    def health(self):
        engine = self.engine.health()
        with self._condition:
            code = self._last_failure or engine["last_failure_code"]
            return {"status": "disabled" if not self.gate.enabled else "degraded" if code else "ok",
                    "current_reasons": [code] if code else [], "last_failure_code": code,
                    "counters": {**self._counts}, "pending": self._pending, "pending_bytes": self._bytes,
                    "queue_capacity": self.max_pending, "queue_byte_capacity": self.max_bytes,
                    "max_age_seconds": self.max_age_seconds, "accepting": self._started and not self._closed,
                    "worker_alive": self._worker.is_alive()}

"""Bounded background workers; ownership never belongs to a HTTP consumer."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import threading
import time

from tiku_agent.execution_dispatch import safe_error, terminal_stage
from tiku_agent.execution_runtime import execute_claimed
from tiku_agent.execution_store import ExecutionError
from tiku_agent.task_state_runtime import TaskStateEntryCapabilities


# Store only a fixed stage vocabulary, never arbitrary model/progress text.
STAGES = frozenset({"queued", "running", "dequeued", "searching", "recognizing", "reranking",
                    "thinking", "answering", "preparing", "cropping", "verifying", "completed"})


class BackgroundWorker:
    def __init__(self, dispatch, private_dir, *, capabilities=None, publication=None, trace_recorder=None):
        self.dispatch = dispatch
        self.publication = publication
        self.trace_recorder = trace_recorder
        self.root = Path(private_dir).absolute()
        # Only generated files under the authority's private runtime are used.
        authority_root = dispatch.store.path.parent.resolve()
        if not self.root.resolve().is_relative_to(authority_root) or self.root.resolve() == authority_root:
            raise ValueError("worker scratch directory must be inside the isolated authority runtime")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.resolve() != self.root or any(p.is_symlink() for p in [self.root, *self.root.parents]):
            raise ValueError("worker scratch directory cannot use links")
        self.capabilities = capabilities or TaskStateEntryCapabilities()
        self._stop = threading.Event()
        self._threads = []
        self._lifecycle = threading.Lock()
        self._maintenance = threading.Lock()
        self._next_maintenance = 0.0
        self.last_error = ""

    def start(self):
        with self._lifecycle:
            if self._threads or self._stop.is_set():
                raise RuntimeError("worker may only be started once")
            self._threads = [threading.Thread(target=self._loop, name=f"tiku-dispatch-{index}", daemon=True)
                             for index in range(self.dispatch.policy.max_concurrent)]
            for thread in self._threads:
                thread.start()
        return self

    def close(self, *, drain_seconds=5):
        """Stop new acceptance/claims, wait at most the requested drain budget.

        Live providers keep their writer until true exit; an incomplete drain is
        explicitly reported. Forced process termination is classified by leases.
        """
        if not 0 <= drain_seconds <= 60:
            raise ValueError("drain must be between zero and sixty seconds")
        self.dispatch.accepting = False
        self._stop.set()
        deadline = time.monotonic() + drain_seconds
        for thread in self._threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        return {"drained": not any(thread.is_alive() for thread in self._threads),
                "running_workers": sum(thread.is_alive() for thread in self._threads)}

    def _loop(self):
        while not self._stop.is_set():
            try:
                if time.monotonic() >= self._next_maintenance and self._maintenance.acquire(blocking=False):
                    try:
                        self.maintain()
                        self._next_maintenance = time.monotonic() + 60
                    finally:
                        self._maintenance.release()
                if self.run_once():
                    continue
            except Exception as exc:
                self.last_error = safe_error(exc)
            self._stop.wait(0.25)

    def run_once(self):
        if self._stop.is_set():
            return False
        claimed = self.dispatch.claim_next()
        if claimed is None:
            return False
        from tiku_shared.trace_context import TraceContext, trace_context_scope
        from tiku_shared.trace_events import trace_event_scope, record_trace_event, record_public_terminal
        writer, operation, private, parsed = claimed
        context = TraceContext("trace_" + writer.operation_id, "req_" + writer.operation_id)
        with trace_context_scope(context), trace_event_scope(self.trace_recorder, trace_id=context.trace_id,
                request_id=context.request_id, identity_key=private["identity_key"], session_key=writer.session):
            record_trace_event("stage_started", stage="background_execution", outcome="started", safe_attributes={"operation": operation["kind"]})
            try:
                self._run_claimed(claimed)
                if self.publication is not None:
                    self.publication.publish(writer.operation_id)
            finally:
                with self.dispatch.store.transaction() as conn:
                    status = conn.execute("SELECT status FROM execution_operations WHERE id=?", (writer.operation_id,)).fetchone()[0]
                tracer = getattr(self.publication, "trace", None)
                if tracer is not None:
                    tracer.complete(writer.operation_id)
                else:
                    record_public_terminal(stage="background_execution", outcome="success" if status == "SUCCEEDED" else "error", failed=status != "SUCCEEDED")
        return True

    def _run_claimed(self, claimed):
        writer, operation, private, parsed = claimed
        path = None
        error = ""
        try:
            params = dict(parsed["parameters"])
            if private["image"] is not None:
                path = self._materialize(writer, private, parsed["extension"])
                params["image_path"] = path
            check = lambda: self.dispatch.check_running(writer, private, operation["grant_id"])
            check()
            def progress(stage, _text):
                stage = stage if stage in STAGES else "running"
                with self.dispatch.store.transaction() as conn:
                    now = self.dispatch.store.clock(conn)
                    writer.validate(conn, self.dispatch.store, writer.session, writer.epoch, now)
                    conn.execute("UPDATE execution_dispatch SET progress_stage=?,progress_version=progress_version+1,updated=? WHERE operation_id=? AND status='CLAIMED'",
                                 (stage, now, writer.operation_id))
            execute_claimed(self.dispatch.runtime, private["session_id"], writer,
                lambda: getattr(self.dispatch.runtime, operation["kind"])(private["session_id"], **params,
                    identity_key=private["identity_key"], request_id="req_" + operation["id"], progress=progress,
                    task_state_capabilities=self.capabilities), admission_check=check,
                prepare_result=self.publication.prepare if self.publication is not None else None)
        except BaseException as exc:
            error = safe_error(exc)
            # Covers failure before execute_claimed installs its effect boundary.
            # No returned REGISTERED attempt is ever silently queued a second time.
            self.dispatch.operations.fail(writer, known_not_started=True)
        finally:
            with self.dispatch.store.transaction() as conn:
                now = self.dispatch.store.clock(conn)
                row = conn.execute("SELECT status FROM execution_operations WHERE id=?", (writer.operation_id,)).fetchone()
                if row["status"] == "REGISTERED":
                    conn.execute("UPDATE execution_operations SET status='FAILED',updated=? WHERE id=?", (now, writer.operation_id))
                conn.execute("UPDATE execution_dispatch SET status='SETTLED',error_code=?,progress_stage=?,progress_version=progress_version+1,updated=? WHERE operation_id=?",
                             (error, terminal_stage(row["status"]), now, writer.operation_id))
            if path is not None:
                path.unlink(missing_ok=True)
        return True

    def _materialize(self, writer, private, extension):
        if extension not in {".png", ".jpg", ".webp"} or self.root.resolve() != self.root:
            raise ExecutionError("EXECUTION_INPUT_UNAVAILABLE")
        self.dispatch.store.storage_capacity(extra=len(private["image"]) * 2)
        path = self.root / (writer.operation_id + "." + writer.token + extension)
        created = False
        try:
            with path.open("xb") as handle:
                created = True
                handle.write(private["image"])
                handle.flush()
                os.fsync(handle.fileno())
            if hashlib.sha256(path.read_bytes()).hexdigest() != private["image_hash"]:
                raise ExecutionError("EXECUTION_INPUT_UNAVAILABLE")
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise
        return path

    def maintain(self):
        result = self.dispatch.maintain()
        removed = 0
        with self.dispatch.store.transaction() as conn:
            now = self.dispatch.store.clock(conn)
            for index, path in enumerate(self.root.iterdir()):
                if removed >= 100 or index >= 1000:
                    break
                match = re.fullmatch(r"([0-9a-f]{32})\.([0-9a-f]{32})\.(png|jpg|webp)", path.name)
                if not match or path.is_symlink() or not path.is_file():
                    continue
                row = conn.execute("SELECT status,updated FROM execution_operations WHERE id=?", (match[1],)).fetchone()
                # Unknown/active inputs remain protected even after a long outage.
                if row and (row["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}
                            or row["updated"] + self.dispatch.policy.input_ttl >= now):
                    continue
                if path.stat().st_mtime + self.dispatch.policy.input_ttl >= now:
                    continue
                path.unlink()
                removed += 1
        return {**result, "scratch_removed": removed}

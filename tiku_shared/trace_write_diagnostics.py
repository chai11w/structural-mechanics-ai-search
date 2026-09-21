"""Bounded, payload-free timings for asynchronous Trace write attempts.

These are observations, never deadlines, retry decisions or commit receipts.
"""

from collections import deque
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter


_LIMIT = 2_147_483_647
_STAGES = frozenset({
    "write", "path_checks", "path_lock", "store_lock", "maintenance_lock",
    "connect", "schema", "schema_commit", "begin", "capacity", "insert",
    "precommit_check", "rollback", "duplicate_check", "commit", "close",
})
_TRANSACTIONS = frozenset({
    "unknown", "not_started", "begin_unknown", "active", "commit_unknown", "committed",
    "rollback_unknown", "rolled_back",
})
_ERRORS = frozenset({
    "EvidenceDeadlineExceeded", "EvidenceLockBudget", "TraceEventRetryableDeadline", "OperationalError",
    "IntegrityError", "OSError", "PermissionError", "FileNotFoundError",
    "TimeoutError", "TraceEventCapacityError", "TraceEventMaintenanceBusy",
    "TraceEventMaintenanceError", "TraceCleanupDriftError", "DuplicateTerminalEvent",
})
_current = ContextVar("trace_write_diagnostic", default=None)


def _milliseconds(seconds):
    return min(_LIMIT, max(0, round(seconds * 1000, 3)))


@contextmanager
def write_stage(name):
    sample = _current.get()
    if sample is None:
        yield
        return
    name = name if name in _STAGES else "write"
    started = perf_counter()
    try:
        yield
    except BaseException:
        # Keep the first failing operation even if rollback/close also fails.
        if not sample["failure_stage"]:
            sample["failure_stage"] = name
        raise
    finally:
        sample["stage_ms"][name] = min(
            _LIMIT, sample["stage_ms"].get(name, 0) + _milliseconds(perf_counter() - started)
        )


def write_call(name, function, *args, **kwargs):
    with write_stage(name):
        return function(*args, **kwargs)


@contextmanager
def write_context(name, manager):
    # Time acquisition only, preserving context-manager exception semantics.
    with ExitStack() as stack:
        with write_stage(name):
            value = stack.enter_context(manager)
        yield value


def write_transaction(state):
    sample = _current.get()
    if sample is not None:
        sample["transaction_state"] = state if state in _TRANSACTIONS else "unknown"


class TraceWriteDiagnostics:
    def __init__(self, event_types):
        self._event_types = frozenset(event_types)
        self._lock = Lock()
        self._attempts = 0
        self._retryable = 0
        self._slow = 0
        self._stage_max_ms = {}
        self._recent_failures = deque(maxlen=8)
        self._last_slow = None
        self._last_retryable = None

    @contextmanager
    def attempt(self, event_type, *, retryable, duplicate):
        sample = {
            "event_type": event_type if event_type in self._event_types else "unknown",
            "failure_stage": "", "transaction_state": "unknown",
            "stage_ms": {}, "outcome": "success", "error_kind": "",
        }
        started = perf_counter()
        token = _current.set(sample)
        try:
            yield
        except BaseException as exc:
            sample["outcome"] = (
                "retryable" if isinstance(exc, retryable)
                else "duplicate" if isinstance(exc, duplicate) else "error"
            )
            kind = type(exc).__name__
            sample["error_kind"] = kind if kind in _ERRORS else "unknown"
            sample["failure_stage"] = sample["failure_stage"] or "write"
            raise
        finally:
            _current.reset(token)
            elapsed = perf_counter() - started
            sample["elapsed_ms"] = _milliseconds(elapsed)
            sample["budget_ms"] = 500
            sample["finished_at"] = datetime.now(UTC).isoformat()
            sample["slowest_stage"] = max(sample["stage_ms"], key=sample["stage_ms"].get, default="write")
            with self._lock:
                self._attempts = min(_LIMIT, self._attempts + 1)
                if sample["outcome"] == "retryable":
                    self._retryable = min(_LIMIT, self._retryable + 1)
                    self._last_retryable = sample
                for stage, value in sample["stage_ms"].items():
                    self._stage_max_ms[stage] = max(self._stage_max_ms.get(stage, 0), value)
                if elapsed > .5:
                    self._slow = min(_LIMIT, self._slow + 1)
                    self._last_slow = sample
                if sample["outcome"] == "error":
                    self._recent_failures.append(sample)

    def snapshot(self):
        with self._lock:
            return deepcopy({
                "attempts": self._attempts,
                "retryable_attempts": self._retryable,
                "over_budget_attempts": self._slow,
                "stage_max_ms": self._stage_max_ms,
                "recent_failures": list(self._recent_failures),
                "last_over_budget": self._last_slow,
                "last_retryable": self._last_retryable,
            })

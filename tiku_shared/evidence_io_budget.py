"""Cooperative evidence deadlines; never claim to interrupt a blocked OS call."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from time import monotonic


class EvidenceDeadlineExceeded(TimeoutError):
    pass


class EvidenceLockBudget(EvidenceDeadlineExceeded):
    """The lock was never acquired, so nothing of ours was written.

    Distinguished from a plain deadline blowout so a caller may retry: unlike a
    statement interrupted mid-flight, losing the lock race leaves no ambiguous
    commit behind.
    """


@dataclass(frozen=True)
class EvidenceIOBudget:
    deadline: float
    cancel: object = None
    capture: bool = False

    def remaining(self):
        remaining = self.deadline - monotonic()
        if remaining <= 0 or (self.cancel is not None and self.cancel.is_set()):
            raise EvidenceDeadlineExceeded("evidence operation budget exhausted")
        return remaining


current_evidence_budget = ContextVar("evidence_io_budget", default=None)


@contextmanager
def evidence_io_budget(seconds, *, cancel=None, capture=False):
    parent = current_evidence_budget.get()
    deadline = monotonic() + seconds
    if parent is not None:
        deadline = min(deadline, parent.deadline)
        cancel = cancel if cancel is not None else parent.cancel
        capture = capture or parent.capture
    budget = EvidenceIOBudget(deadline, cancel, capture)
    token = current_evidence_budget.set(budget)
    try:
        budget.remaining()
        yield budget
    finally:
        current_evidence_budget.reset(token)


def check_evidence_budget():
    budget = current_evidence_budget.get()
    if budget is not None:
        budget.remaining()


def evidence_sqlite_timeout(configured):
    budget = current_evidence_budget.get()
    return min(configured, budget.remaining(), 0.25) if budget is not None else configured


def configure_evidence_connection(connection):
    budget = current_evidence_budget.get()
    if budget is not None:
        def interrupted():
            try:
                budget.remaining()
                return 0
            except EvidenceDeadlineExceeded:
                return 1
        connection.set_progress_handler(interrupted, 1000)


class EvidenceRLock:
    """Ordinary reentrant lock outside evidence work, deadline-aware inside it."""
    def __init__(self):
        self._lock = RLock()

    def acquire(self, blocking=True, timeout=-1):
        budget = current_evidence_budget.get()
        if budget is not None and blocking:
            remaining = min(budget.remaining(), 0.25)
            timeout = min(timeout, remaining) if timeout >= 0 else remaining
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        if not self.acquire():
            raise EvidenceLockBudget("evidence lock budget exhausted")
        return self

    def __exit__(self, *args):
        self.release()

"""Shared elapsed submission budget, including capture locks and lease I/O."""

from contextvars import ContextVar
from threading import Lock


class CheckpointSubmissionBudget:
    def __init__(self, seconds=0.1):
        self.seconds = seconds
        self.spent = 0.0
        self._lock = Lock()

    def charge(self, elapsed):
        with self._lock:
            self.spent += max(0.0, elapsed)
            return self.spent <= self.seconds

    def snapshot(self, stage):
        with self._lock:
            return {"stage": stage, "spent_ms": min(2_147_483_647, int(self.spent * 1000)),
                    "limit_ms": min(2_147_483_647, int(self.seconds * 1000))}


current_checkpoint_budget = ContextVar("checkpoint_submission_budget", default=None)

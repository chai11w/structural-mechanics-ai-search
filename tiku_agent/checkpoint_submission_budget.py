"""Shared CPU submission budget, excluding time spent in business/model work."""

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


current_checkpoint_budget = ContextVar("checkpoint_submission_budget", default=None)

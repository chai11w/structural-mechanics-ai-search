"""Request-local, server-owned parent identity for nested A2 evidence."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class A3CheckpointBindingV1:
    recorder: object
    session_key: str
    identity_key: str
    workflow_search_id: str
    workflow_task_revision: int
    unit_id: str
    source_page_path: str


current_a3_checkpoint_binding = ContextVar("a3_checkpoint_binding", default=None)


@contextmanager
def a3_checkpoint_request_scope():
    from tiku_agent.checkpoint_submission_budget import CheckpointSubmissionBudget, current_checkpoint_budget
    token = current_a3_checkpoint_binding.set(None)
    budget_token = current_checkpoint_budget.set(current_checkpoint_budget.get() or CheckpointSubmissionBudget())
    try:
        yield
    finally:
        current_a3_checkpoint_binding.reset(token)
        current_checkpoint_budget.reset(budget_token)

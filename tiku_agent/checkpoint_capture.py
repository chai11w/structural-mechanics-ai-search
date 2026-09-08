"""Pure A2 runtime to Checkpoint V1 adaptation helpers.

This module deliberately has no Store, filesystem, clock, or runtime wiring.
It only turns already available A2 context and tool results into the frozen
Checkpoint V1 value objects. Persistence and best-effort error handling belong
to later 4.3 batches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from tiku_agent.checkpoint_contract import (
    CHECKPOINT_SCHEMA_VERSION,
    BANK_REFERENCE_CHECKPOINT_SCHEMA_VERSION,
    OUTCOME_FAILED,
    OUTCOME_NEEDS_INPUT,
    OUTCOME_NO_MATCH,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    RETENTION_FAILED,
    RETENTION_NORMAL,
    SCOPE_CHILD_TASK,
    ArtifactLinkV1,
    CheckpointFailureV1,
    CheckpointOwnerV1,
    IntermediateCheckpointV1,
    ProducerVersionV1,
    compute_input_fingerprint_v1,
    has_bank_references,
    new_checkpoint_id,
)
from tiku_agent.tool_result import ToolResult, ToolOutcome


@dataclass(frozen=True)
class A2CheckpointContextV1:
    """Identity and producer data shared by all A2 stage checkpoints."""

    trace_id: str
    session_key: str
    identity_key: str
    workflow_search_id: str
    workflow_task_revision: int
    search_id: str
    task_revision: int
    producer: ProducerVersionV1
    unit_id: str = ""
    candidate_generation: str = ""
    request_id: str = ""
    scope: str = SCOPE_CHILD_TASK

    def owner(self) -> CheckpointOwnerV1:
        return CheckpointOwnerV1(
            scope=self.scope,
            session_key=self.session_key,
            identity_key=self.identity_key,
            workflow_search_id=self.workflow_search_id,
            workflow_task_revision=self.workflow_task_revision,
            search_id=self.search_id,
            unit_id=self.unit_id,
            task_revision=self.task_revision,
            candidate_generation=self.candidate_generation,
        )


def checkpoint_outcome(result: ToolResult) -> str:
    """Map the shared five-state tool result to the Checkpoint vocabulary."""

    if type(result) is not ToolResult:
        raise TypeError("checkpoint result must be ToolResult")
    return {
        ToolOutcome.SUCCESS: OUTCOME_SUCCESS,
        ToolOutcome.NO_MATCH: OUTCOME_NO_MATCH,
        ToolOutcome.NEEDS_INPUT: OUTCOME_NEEDS_INPUT,
        ToolOutcome.PARTIAL: OUTCOME_PARTIAL,
        ToolOutcome.ERROR: OUTCOME_FAILED,
    }[result.outcome]


def checkpoint_failure(
    result: ToolResult,
    *,
    last_successful_checkpoint_id: str = "",
) -> CheckpointFailureV1 | None:
    """Create a stable failure projection for failed or partial tool results."""

    outcome = checkpoint_outcome(result)
    if outcome not in {OUTCOME_FAILED, OUTCOME_PARTIAL}:
        return None
    fallback = "retry" if result.retryable else ""
    if outcome == OUTCOME_PARTIAL and result.code.startswith("RERANK_"):
        fallback = "coarse_order"
    elif outcome == OUTCOME_PARTIAL and result.code.startswith("STRUCTURE_"):
        fallback = "skip_structure"
    return CheckpointFailureV1(
        code=str(result.code or "TOOL_FAILED").upper(),
        kind=str(result.error_category or "tool"),
        retryable=bool(result.retryable),
        fallback=fallback,
        last_successful_checkpoint_id=last_successful_checkpoint_id,
    )


def build_a2_checkpoint(
    context: A2CheckpointContextV1,
    *,
    stage: str,
    result: Mapping[str, Any],
    occurred_at: str,
    expires_at: str,
    input_digests: Mapping[str, str],
    outcome: str | None = None,
    artifacts: Sequence[ArtifactLinkV1] = (),
    predecessor_checkpoint_id: str = "",
    checkpoint_id: str = "",
    retention_class: str = RETENTION_NORMAL,
    tool_result: ToolResult | None = None,
    last_successful_checkpoint_id: str = "",
) -> IntermediateCheckpointV1:
    """Build and validate one A2 Checkpoint without performing I/O.

    ``tool_result`` is optional so callers may use the adapter for already
    normalized stage data. When supplied, its outcome and failure projection
    are derived centrally and an explicit ``outcome`` must agree.
    """

    if type(context) is not A2CheckpointContextV1:
        raise TypeError("context must be A2CheckpointContextV1")
    owner = context.owner()
    derived_outcome = checkpoint_outcome(tool_result) if tool_result is not None else None
    final_outcome = outcome or derived_outcome
    if not final_outcome:
        raise ValueError("checkpoint outcome is required")
    if derived_outcome is not None and final_outcome != derived_outcome:
        raise ValueError("checkpoint outcome conflicts with tool result")
    failure = (
        checkpoint_failure(
            tool_result,
            last_successful_checkpoint_id=last_successful_checkpoint_id,
        )
        if tool_result is not None
        else None
    )
    effective_retention = RETENTION_FAILED if final_outcome == OUTCOME_FAILED else retention_class
    return IntermediateCheckpointV1(
        schema_version=BANK_REFERENCE_CHECKPOINT_SCHEMA_VERSION if has_bank_references(result) else CHECKPOINT_SCHEMA_VERSION,
        checkpoint_id=checkpoint_id or new_checkpoint_id(),
        trace_id=context.trace_id,
        request_id=context.request_id,
        stage=stage,
        outcome=final_outcome,
        occurred_at=occurred_at,
        expires_at=expires_at,
        retention_class=effective_retention,
        owner=owner,
        producer=context.producer,
        input_fingerprint=compute_input_fingerprint_v1(
            stage=stage,
            owner=owner,
            producer=context.producer,
            input_digests=input_digests,
        ),
        result=dict(result),
        artifacts=tuple(artifacts),
        predecessor_checkpoint_id=predecessor_checkpoint_id,
        failure=failure,
    )


__all__ = [
    "A2CheckpointContextV1",
    "build_a2_checkpoint",
    "checkpoint_failure",
    "checkpoint_outcome",
]

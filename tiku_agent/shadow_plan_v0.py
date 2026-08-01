"""Pure shadow-plan contract: plan data structures, permission facts, review.

Stage 5 (shadow planning) lets an injected AI Planner propose a structured
multi-step plan for requests the fixed state machine cannot resolve, but only
records the plan and its permission review.  This module is deliberately pure:
it does not import the Agent runtime, call a model, execute a tool, or mutate
conversation state.  It defines what a Planner may propose (``ShadowPlan``), the
code-only facts used to judge it (``PermissionReviewFacts``), and the review
itself (``review_shadow_plan``).  Nothing here may change the user-facing reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tiku_agent.action_decision_v2 import TASK_ACTIONS, ActionDecisionV2
from tiku_agent.action_permissions_v2 import (
    DecisionContextV2,
    authorize_action_v2,
)
from tiku_agent.intent_contract import CHAPTERS
from tiku_agent.state import PHASE_ERROR, AgentState, KNOWN_PHASES

# Actions a shadow plan may propose.  ``cancel`` clears the task through the
# fixed state machine and ``search_image`` needs a trusted image event, so both
# stay out of the planner universe; a model proposing them is a violation.
PLAN_ACTION_UNIVERSE = tuple(sorted(TASK_ACTIONS - {"cancel", "search_image"}))

# Budget constants: a single plan may contain at most four steps, and each user
# turn may be planned at most once (enforced by the agent wiring, double-checked
# here for plans that claim more steps than allowed).
MAX_PLAN_STEPS = 4
MAX_PLANS_PER_TURN = 1

# Tool-cost budget, in real tool calls.  A plan step is an *action*, but in the
# fixed state machine one action expands into several tool calls (see
# ``agent.py`` dispatch chains).  Budgeting at the step count would let a four
# step plan trigger up to 28 tool calls, so the permission review also sums the
# conservative per-action tool upper bound and rejects plans over this cap.
# Each value mirrors the longest tool chain for that action in ``agent.py``;
# changing a dispatch chain MUST update this map (guarded by a test).
MAX_TOOLS_PER_PLAN = 8

ACTION_TOOL_COST: dict[str, int] = {
    # Full image-analysis + search chain: analyze_multi_image → (multi)
    # prepare_question_units → analyze_image → route_bank → classify_structure
    # → coarse_search → rerank_candidates.  ``search_image`` is not in the
    # planner universe but retry_search re-runs the same saved-image chain.
    "retry_search": 7,
    # Route + classify + coarse + rerank.
    "set_chapter": 4,
    "select_question": 4,
    # Route + classify + global search.
    "global_search": 3,
    # Coarse + rerank (reuses the stored route).
    "continue_search": 2,
    # Single answer lookup.
    "select_candidate": 1,
    # State-only actions trigger no tools.
    "reject_candidates": 0,
    "show_candidates": 0,
    "report_answer_mismatch": 0,
    "resend_answer": 0,
    "explain_failure": 0,
}

REVIEW_ALLOW = "allow"
REVIEW_REJECT = "reject"


@dataclass(frozen=True)
class ShadowPlanStep:
    """One proposed action inside a shadow plan.

    ``action`` must belong to :data:`PLAN_ACTION_UNIVERSE`.  ``params`` uses the
    same names as ``ActionDecisionV2`` business parameters (``chapter_override``,
    ``question_index``, ``candidate_rank``, …) so the permission matrix can be
    reused verbatim.  Version/batch fields are not legitimate planner input —
    they are code-owned, so a plan carrying them is checked against the current
    facts and rejected on mismatch.
    """

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action not in PLAN_ACTION_UNIVERSE:
            raise ValueError(f"action outside the shadow-plan universe: {self.action}")
        if not isinstance(self.params, dict):
            raise ValueError("params must be a dict")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")


@dataclass(frozen=True)
class ShadowPlan:
    """A structured multi-step plan the fixed state machine could not derive.

    ``steps`` may be empty only when ``source`` is ``unplannable``: the planner
    judged that the request has no legal executable read-only action (e.g. the
    user wants to give up or the request is too vague to map).  An empty plan is
    still recorded — with a ``unplannable`` review — so the shadow log shows the
    planner reached that honest conclusion instead of being forced to invent a
    wrong action.  A non-empty plan must stay within ``MAX_PLAN_STEPS``.
    """

    goal: str
    steps: tuple[ShadowPlanStep, ...]
    stop_condition: str = ""
    source: str = "planner"

    def __post_init__(self) -> None:
        if not self.goal or not str(self.goal).strip():
            raise ValueError("goal must not be empty")
        if not isinstance(self.steps, tuple):
            raise ValueError("steps must be a tuple")
        if len(self.steps) == 0:
            if self.source != "unplannable":
                raise ValueError("an empty plan must declare source=unplannable")
            return
        if len(self.steps) > MAX_PLAN_STEPS:
            raise ValueError(f"a plan may contain at most {MAX_PLAN_STEPS} steps")
        if self.source not in {"planner", "stub"}:
            raise ValueError("source must be planner or stub")


@dataclass(frozen=True)
class ShadowPlannerResult:
    """One planner turn: the rewritten request plus the derived plan.

    The user's original long-tail utterance is often vague (ellipsis, dropped
    subjects, ambiguous pronouns).  Before choosing actions the planner rewrites
    it into a fuller request — injecting the keywords and intent the fixed rules
    cannot see — and records why, so the shadow log shows *both* the rewrite and
    the plan built on it.  This is one model call producing one result.
    """

    rewritten_text: str
    keywords: tuple[str, ...]
    reason: str
    plan: ShadowPlan

    def __post_init__(self) -> None:
        if not self.rewritten_text or not str(self.rewritten_text).strip():
            raise ValueError("rewritten_text must not be empty")
        if not isinstance(self.keywords, tuple):
            raise ValueError("keywords must be a tuple")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")


@dataclass(frozen=True)
class PermissionReviewFacts:
    """Code-only facts used to judge a plan; never rendered to a model.

    Mirrors the fields the fixed web layer checks for stale actions
    (``fastapi_demo.py:_validate_action_context``): task revision identifies the
    current question version and candidate generation identifies the current
    candidate batch.  A plan step may not silently target an older one.
    """

    phase: str
    current_chapter: str | None = None
    task_revision: int = 0
    candidate_generation: str = ""
    question_count: int = 0
    candidate_count: int = 0
    has_active_image: bool = False
    has_answer: bool = False
    has_explainable_failure: bool = False
    retryable_error: bool = False
    global_search_offered: bool = False
    continuation_available: bool = False
    read_only_actions: frozenset[str] = frozenset(PLAN_ACTION_UNIVERSE)
    max_steps: int = MAX_PLAN_STEPS
    max_tools: int = MAX_TOOLS_PER_PLAN
    max_plans_per_turn: int = MAX_PLANS_PER_TURN

    def __post_init__(self) -> None:
        if self.phase not in KNOWN_PHASES:
            raise ValueError(f"unknown permission-review phase: {self.phase}")
        if self.task_revision < 0:
            raise ValueError("task_revision must not be negative")
        if self.question_count < 0 or self.candidate_count < 0:
            raise ValueError("question_count and candidate_count must not be negative")
        if self.current_chapter is not None and self.current_chapter not in CHAPTERS:
            raise ValueError("current_chapter must be a supported chapter")
        if self.max_steps < 1 or self.max_plans_per_turn < 1:
            raise ValueError("budget constants must be positive")
        if not self.read_only_actions:
            raise ValueError("read_only_actions must not be empty")


@dataclass(frozen=True)
class PermissionReview:
    """The verdict of one shadow-plan permission review."""

    outcome: str
    code: str
    reason: str
    violations: tuple[str, ...] = ()
    plan: ShadowPlan | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == REVIEW_ALLOW


def build_permission_review_facts(state: AgentState) -> PermissionReviewFacts:
    """Derive the code-only review facts from persisted Agent state."""
    return PermissionReviewFacts(
        phase=state.phase,
        current_chapter=state.current_chapter or None,
        task_revision=state.task_revision,
        candidate_generation=state.candidate_generation,
        question_count=state.question_count,
        candidate_count=state.candidate_count,
        has_active_image=bool(state.active_image_path),
        has_answer=bool(state.last_answer_paths),
        has_explainable_failure=bool(state.last_error),
        retryable_error=state.phase == PHASE_ERROR and bool(state.active_image_path),
        global_search_offered=state.global_search_offered,
        continuation_available=state.continuation_available,
    )


def review_shadow_plan(
    plan: ShadowPlan,
    facts: PermissionReviewFacts,
) -> PermissionReview:
    """Judge one plan against the code-only permission facts.

    Checks run in order: read-only boundary, per-step version/batch/chapter/
    phase/parameter legality via the existing authorization matrix, then budget.
    The first violation rejects the whole plan so the record always carries one
    clear machine-readable ``code`` plus a human-readable ``reason``.
    """
    if not plan.steps:
        # The planner honestly concluded there is no legal read-only action.
        # Record it as a special allow so the log distinguishes "cannot plan"
        # from "planned and rejected".  Zero tools, zero state changes.
        return PermissionReview(
            outcome=REVIEW_ALLOW,
            code="unplannable",
            reason="规划器判断当前请求没有可执行的只读动作。",
            plan=plan,
        )
    if len(plan.steps) > facts.max_steps:
        return _reject(
            "plan_too_long",
            f"计划步骤数 {len(plan.steps)} 超过上限 {facts.max_steps}。",
            ("plan_too_long",),
        )

    total_tools = _plan_tool_cost(plan)
    if total_tools > facts.max_tools:
        return _reject(
            "plan_tools_budget_exceeded",
            f"计划预计触发 {total_tools} 次工具调用，超过上限 {facts.max_tools}。",
            ("plan_tools_budget_exceeded",),
        )

    for step in plan.steps:
        violation = _review_step(step, facts)
        if violation is not None:
            return _reject(violation, _step_reason(violation, step), (violation,))

    return PermissionReview(
        outcome=REVIEW_ALLOW,
        code="allow",
        reason="计划在只读、阶段、版本、批次、章节与参数范围内。",
        plan=plan,
    )


def _plan_tool_cost(plan: ShadowPlan) -> int:
    """Sum the conservative per-action tool upper bound for every step.

    Unknown actions are treated as free (cost 0) so a future action added to
    ``PLAN_ACTION_UNIVERSE`` without a map entry never accidentally over-budgets;
    the coverage test forces the map to be completed instead.
    """
    return sum(ACTION_TOOL_COST.get(step.action, 0) for step in plan.steps)


def _review_step(
    step: ShadowPlanStep,
    facts: PermissionReviewFacts,
) -> str | None:
    """Return a violation code for one step, or ``None`` when it is allowed."""
    if step.action not in facts.read_only_actions:
        return "forbidden_write"

    if "task_revision" in step.params and step.params["task_revision"] != facts.task_revision:
        return "stale_task_revision"
    if "candidate_generation" in step.params and step.params["candidate_generation"] != facts.candidate_generation:
        return "stale_candidate_batch"

    chapter = step.params.get("chapter_override") or step.params.get("chapter")
    if chapter is not None and chapter not in CHAPTERS:
        return "invalid_chapter"

    decision = _step_to_decision(step)
    if decision is None:
        return "invalid_step_parameters"
    authorization = authorize_action_v2(decision, _facts_to_decision_context(facts))
    if not authorization.allowed:
        return authorization.code or "action_not_allowed"
    return None


def _step_to_decision(step: ShadowPlanStep) -> ActionDecisionV2 | None:
    """Translate one plan step into the existing authorization contract.

    Returns ``None`` when the step carries incomplete/illegal parameters so the
    review rejects it instead of raising into the shadow-record path.
    """
    params = step.params
    try:
        if step.action == "set_chapter":
            return ActionDecisionV2(
                "set_chapter",
                chapter_override=str(params["chapter_override"]),
                chapter_target=str(params.get("chapter_target") or "current_question"),
            )
        if step.action == "select_question":
            return ActionDecisionV2(
                "select_question",
                question_index=int(params["question_index"]),
                chapter_override=str(params["chapter_override"]) if params.get("chapter_override") else None,
            )
        if step.action == "select_candidate":
            return ActionDecisionV2(
                "select_candidate",
                candidate_rank=int(params["candidate_rank"]),
            )
        return ActionDecisionV2(step.action)
    except (KeyError, TypeError, ValueError):
        return None


def _facts_to_decision_context(facts: PermissionReviewFacts) -> DecisionContextV2:
    return DecisionContextV2(
        phase=facts.phase,
        question_count=facts.question_count,
        candidate_count=facts.candidate_count,
        has_active_image=facts.has_active_image,
        has_answer=facts.has_answer,
        has_explainable_failure=facts.has_explainable_failure,
        retryable_error=facts.retryable_error,
        global_search_offered=facts.global_search_offered,
        continuation_available=facts.continuation_available,
    )


def _step_reason(code: str, step: ShadowPlanStep) -> str:
    messages = {
        "forbidden_write": f"动作 {step.action} 不在只读允许范围。",
        "stale_task_revision": "计划引用了过期题目的版本，不能作用于当前题。",
        "stale_candidate_batch": "计划引用了过期候选批次，不能作用于当前候选。",
        "invalid_chapter": "计划指定了不支持的章节。",
        "invalid_step_parameters": f"动作 {step.action} 的参数不完整或非法。",
        "action_not_allowed": f"动作 {step.action} 在当前阶段不允许。",
    }
    return messages.get(code, f"动作 {step.action} 未通过权限审核（{code}）。")


def _reject(code: str, reason: str, violations: tuple[str, ...] = ()) -> PermissionReview:
    return PermissionReview(
        outcome=REVIEW_REJECT,
        code=code,
        reason=reason,
        violations=violations,
    )

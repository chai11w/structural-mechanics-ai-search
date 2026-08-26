"""Run the isolated local Agent demo on port 8790."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.external_load_screen import ZhipuExternalLoadScreen
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess
from tiku_agent.safe_answer_generator_v0 import (
    SafeAnswerGeneratorV0,
    SafeAnswerModelRequestV0,
)
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.task_log import JsonlTaskLogger
from tiku_agent.tools import AgentToolConfig, rerank_candidates_tool
from tiku_admin.auth import SQLiteInviteAccess
from tiku_admin.control_store import SQLiteControlStore
from tiku_shared.model_costs import SQLiteModelCostLedger
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder


DEFAULT_V2_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2"


def build_runtime(
    runtime_dir: str | Path = DEFAULT_V2_RUNTIME_DIR,
    *,
    enable_safe_answer_v0: bool = True,
    enable_dimension_filter: bool = False,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
    max_concurrent_tasks: int = 0,
    max_queued_tasks: int = 0,
    queue_wait_seconds: float = 90.0,
    daily_budget_cny: float | None = None,
    per_invite_daily_budget_cny: float | None = None,
    control_store: SQLiteControlStore | None = None,
    enable_external_load_screen: bool = True,
    external_load_timeout_seconds: float = 15.0,
    external_load_screen: Callable[[str | Path], str] | None = None,
    enable_chapter_scope_fallback: bool = False,
    enable_author_contact_fallback: bool = False,
    rerank_policy: dict[str, object] | None = None,
    cost_ledger: SQLiteModelCostLedger | None = None,
    preserve_artifacts_on_cancel: bool = False,
) -> AgentSessionRuntime:
    """Build the 8790 runtime with bounded safe answers enabled by default."""
    root = Path(runtime_dir).resolve()
    artifacts = SessionArtifacts(root / "sessions")
    generator = None
    if enable_safe_answer_v0:
        generator = SafeAnswerGeneratorV0(
            safe_answer_model_client or QwenSafeAnswerClientV0()
        )

    agent_factory = None
    scoped_tools = None
    if rerank_policy:
        policy = dict(rerank_policy)

        def rerank_with_policy(query_image_path, candidates, **kwargs):
            options = dict(kwargs)
            options.update(policy)
            return rerank_candidates_tool(query_image_path, candidates, **options)

        scoped_tools = AgentToolbox(rerank_candidates=rerank_with_policy)

    if (
        enable_safe_answer_v0
        or enable_dimension_filter
        or enable_chapter_scope_fallback
        or enable_author_contact_fallback
        or scoped_tools
    ):
        def build_agent(state: AgentState) -> TikuSearchAgent:
            return TikuSearchAgent(
                state=state,
                tools=scoped_tools,
                config=AgentToolConfig(
                    runtime_dir=root,
                    session_dir=artifacts.session_dir(state.session_id),
                    dimension_filter_enabled=enable_dimension_filter,
                ),
                enable_safe_answer_v0=enable_safe_answer_v0,
                safe_answer_generator_v0=generator,
                enable_chapter_scope_fallback=enable_chapter_scope_fallback,
                enable_author_contact_fallback=enable_author_contact_fallback,
            )

        agent_factory = build_agent
    return AgentSessionRuntime(
        SQLiteSessionStore(root / "session.db"),
        artifacts=artifacts,
        task_logger=JsonlTaskLogger(root / "task_logs.jsonl"),
        cost_ledger=cost_ledger or SQLiteModelCostLedger(root / "model_costs.sqlite3"),
        agent_factory=agent_factory,
        max_concurrent_tasks=max_concurrent_tasks,
        max_queued_tasks=max_queued_tasks,
        queue_wait_seconds=queue_wait_seconds,
        daily_budget_cny=daily_budget_cny,
        per_identity_daily_budget_cny=per_invite_daily_budget_cny,
        budget_policy=control_store,
        external_load_screen=(
            external_load_screen
            or ZhipuExternalLoadScreen(
                timeout_seconds=external_load_timeout_seconds
            )
            if enable_external_load_screen
            else None
        ),
        external_load_timeout_seconds=external_load_timeout_seconds,
        preserve_artifacts_on_cancel=preserve_artifacts_on_cancel,
    )


def build_app(
    runtime_dir: str | Path = DEFAULT_V2_RUNTIME_DIR,
    *,
    runtime: AgentSessionRuntime | None = None,
    enable_safe_answer_v0: bool = True,
    enable_dimension_filter: bool = False,
    safe_answer_model_client: Callable[[SafeAnswerModelRequestV0], str] | None = None,
    max_concurrent_tasks: int = 0,
    max_queued_tasks: int = 0,
    queue_wait_seconds: float = 90.0,
    daily_budget_cny: float | None = None,
    per_invite_daily_budget_cny: float | None = None,
    invite_config: str | Path | None = None,
    control_db: str | Path | None = None,
    enable_external_load_screen: bool = True,
    external_load_timeout_seconds: float = 15.0,
    external_load_screen: Callable[[str | Path], str] | None = None,
):
    root = Path(runtime_dir).resolve()
    if control_db is not None and invite_config is not None:
        raise ValueError("use either control_db or invite_config, not both")
    if control_db is not None and (
        daily_budget_cny is not None or per_invite_daily_budget_cny is not None
    ):
        raise ValueError("control_db provides dynamic budgets; omit static budget arguments")
    control_path = Path(control_db).resolve() if control_db is not None else None
    if control_path is not None and not control_path.is_file():
        raise ValueError(f"control database not found: {control_path}")
    control_store = SQLiteControlStore(control_path) if control_path is not None else None
    return create_app(
        runtime=runtime
        or build_runtime(
            root,
            enable_safe_answer_v0=enable_safe_answer_v0,
            enable_dimension_filter=enable_dimension_filter,
            safe_answer_model_client=safe_answer_model_client,
            max_concurrent_tasks=max_concurrent_tasks,
            max_queued_tasks=max_queued_tasks,
            queue_wait_seconds=queue_wait_seconds,
            daily_budget_cny=daily_budget_cny,
            per_invite_daily_budget_cny=per_invite_daily_budget_cny,
            control_store=control_store,
            enable_external_load_screen=enable_external_load_screen,
            external_load_timeout_seconds=external_load_timeout_seconds,
            external_load_screen=external_load_screen,
        ),
        incoming_dir=root / "incoming",
        invite_access=(
            SQLiteInviteAccess(control_store)
            if control_store is not None
            else InviteAccess(invite_config) if invite_config else None
        ),
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
        feedback_retention_days_provider=(
            lambda: int(control_store.settings()["feedback_retention_days"])
            if control_store is not None
            else 30
        ),
        trace_event_recorder=TraceEventRecorder(
            SQLiteTraceEventStore(root / "trace_events.sqlite3")
        ),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated question-bank Agent FastAPI demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument(
        "--max-concurrent-tasks",
        type=int,
        default=0,
        help="Maximum active web tasks; 0 keeps the local unbounded default",
    )
    parser.add_argument(
        "--max-queued-tasks",
        type=int,
        default=0,
        help="Maximum waiting web tasks when concurrency is bounded",
    )
    parser.add_argument(
        "--queue-wait-seconds",
        type=float,
        default=90.0,
        help="Maximum time a queued task waits before returning busy",
    )
    parser.add_argument(
        "--daily-budget-cny",
        type=float,
        help="Hard global daily estimated model-cost ceiling in CNY; omit to disable",
    )
    parser.add_argument(
        "--per-invite-daily-budget-cny",
        type=float,
        help="Hard daily estimated model-cost ceiling for each invitation",
    )
    parser.add_argument(
        "--invite-config",
        type=Path,
        help="Hash-only invitation configuration; omit to disable the invitation gate",
    )
    parser.add_argument(
        "--control-db",
        type=Path,
        help="Shared invitation and budget database managed by the 8795 console",
    )
    dimension_filter_group = parser.add_mutually_exclusive_group()
    dimension_filter_group.add_argument(
        "--enable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_true",
        help="Enable V5.2 dimension filtering when symbolic candidates exceed 20 (default)",
    )
    dimension_filter_group.add_argument(
        "--disable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_false",
        help="Temporarily disable the V5.2 dimension filter",
    )
    safe_answer_group = parser.add_mutually_exclusive_group()
    safe_answer_group.add_argument(
        "--enable-safe-answer-v0",
        dest="enable_safe_answer_v0",
        action="store_true",
        help="Enable bounded Qwen answers for safe zero-tool conversations (default)",
    )
    safe_answer_group.add_argument(
        "--disable-safe-answer-v0",
        dest="enable_safe_answer_v0",
        action="store_false",
        help="Temporarily use the original fixed Intent V2 replies",
    )
    external_load_group = parser.add_mutually_exclusive_group()
    external_load_group.add_argument(
        "--enable-external-load-screen",
        dest="enable_external_load_screen",
        action="store_true",
        help="Run the parallel Zhipu external-load gate for uploaded images (default)",
    )
    external_load_group.add_argument(
        "--disable-external-load-screen",
        dest="enable_external_load_screen",
        action="store_false",
        help="Temporarily roll back to the original image search flow",
    )
    parser.add_argument(
        "--external-load-timeout-seconds",
        type=float,
        default=15.0,
        help="Maximum time an empty/non-candidate result waits for the gate (default: 15)",
    )
    parser.set_defaults(
        enable_safe_answer_v0=True,
        enable_dimension_filter=True,
        enable_external_load_screen=True,
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    runtime_dir = (args.runtime_dir or DEFAULT_V2_RUNTIME_DIR).resolve()
    uvicorn.run(
        build_app(
            runtime_dir,
            enable_safe_answer_v0=args.enable_safe_answer_v0,
            enable_dimension_filter=args.enable_dimension_filter,
            max_concurrent_tasks=args.max_concurrent_tasks,
            max_queued_tasks=args.max_queued_tasks,
            queue_wait_seconds=args.queue_wait_seconds,
            daily_budget_cny=args.daily_budget_cny,
            per_invite_daily_budget_cny=args.per_invite_daily_budget_cny,
            invite_config=args.invite_config,
            control_db=args.control_db,
            enable_external_load_screen=args.enable_external_load_screen,
            external_load_timeout_seconds=args.external_load_timeout_seconds,
        ),
        host=args.host,
        port=args.port,
    )
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

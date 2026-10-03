"""Run the A3-V1 business core with the 8790 production access shell."""

from __future__ import annotations

import argparse
import math
import site
import subprocess
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8896 import build_runtime as build_a3_runtime
from tiku_admin.auth import SQLiteInviteAccess
from tiku_admin.control_store import SQLiteControlStore
from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1, ProducerVersionV1
from tiku_agent.a2_checkpoint_recorder import A2CheckpointRecorderV1
from tiku_agent.a3_checkpoint_recorder import A3CheckpointRecorderV1
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_async import AsyncCheckpointRecorder
from tiku_agent.checkpoint_store import SQLiteCheckpointStore
from tiku_agent.fastapi_demo import SESSION_COOKIE, create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess
from tiku_agent.output_watchdog import OutputWatchdog
from tiku_agent.a3_text_orientation import RapidOcrTextPageOrienter
from tiku_diagnostics.checkpoint_retention import (
    CHECKPOINT_ARTIFACT_ROOT,
    CHECKPOINT_DATABASE,
    TRACE_DATABASE,
    CheckpointRetentionRunner,
)
from tiku_shared.trace_events import SQLiteTraceEventStore, TraceEventRecorder
from tiku_shared.qwen_transport import configure_qwen_connect_retries_from_env


DEFAULT_PORT = 8790
DEFAULT_RUNTIME_DIR = BASE / ".tmp_tiku_agent_v2_prod_8790"
DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR = Path(
    r"F:\ruanjian\tiku-a3-orientation-8790"
)
EVIDENCE_RUNTIME_NAME = "tiku_agent_8790"


def build_a3_page_orienter(
    dependency_dir: str | Path = DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR,
) -> RapidOcrTextPageOrienter:
    dependency_path = Path(dependency_dir).resolve()
    if not dependency_path.is_dir():
        raise RuntimeError(
            f"A3 orientation dependency directory not found: {dependency_path}"
        )
    site.addsitedir(str(dependency_path))
    return RapidOcrTextPageOrienter(
        worker_count=4,
        onnx_threads_per_engine=1,
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and greater than zero")
    return parsed


def _artifact_count(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 50:
        raise argparse.ArgumentTypeError("value must be between 1 and 50")
    return parsed


def _capacity_from_args(args: argparse.Namespace) -> EvidenceCapacityPolicyV1:
    return EvidenceCapacityPolicyV1(
        max_checkpoint_rows=args.max_checkpoint_rows,
        max_artifact_rows=args.max_artifact_rows,
        max_audit_rows=args.max_audit_rows,
        max_trace_rows=args.max_trace_rows,
        max_artifact_bytes=args.max_artifact_bytes,
        min_free_bytes=args.min_free_bytes,
        max_artifacts_per_checkpoint=args.max_artifacts_per_checkpoint,
    )


def _validate_evidence_configuration(
    capacity: EvidenceCapacityPolicyV1 | None,
    *,
    backup_root: str | Path | None,
    retention_interval_seconds: float | None,
    backup_keep_runs: int | None,
    repository_root: Path | None = None,
) -> Path | None:
    settings = (backup_root, retention_interval_seconds, backup_keep_runs)
    if capacity is None:
        if any(value is not None for value in settings):
            raise ValueError("evidence retention settings require an evidence capacity")
        return None
    if type(capacity) is not EvidenceCapacityPolicyV1:
        raise TypeError("evidence_capacity must be EvidenceCapacityPolicyV1")
    if backup_root is None:
        raise ValueError("checkpoint retention backup root is required")
    raw_backup = Path(backup_root).expanduser()
    if not raw_backup.is_absolute():
        raise ValueError("checkpoint retention backup root must be absolute")
    resolved_backup = raw_backup.resolve(strict=False)
    for repository in {BASE.resolve(), (repository_root or BASE).resolve()}:
        if (
            resolved_backup == repository
            or resolved_backup.is_relative_to(repository)
            or repository.is_relative_to(resolved_backup)
        ):
            raise ValueError("checkpoint retention backup root must be outside the repository")
    if (
        isinstance(retention_interval_seconds, bool)
        or not isinstance(retention_interval_seconds, (int, float))
        or not math.isfinite(float(retention_interval_seconds))
        or float(retention_interval_seconds) <= 0
    ):
        raise ValueError("checkpoint retention interval must be greater than zero")
    if type(backup_keep_runs) is not int or backup_keep_runs <= 0:
        raise ValueError("checkpoint retention backup keep runs must be positive")
    return resolved_backup


def _evidence_repository_root(runtime: Path, data_root: str | Path | None = None) -> Path:
    """A linked release can retain runtime data in its own primary checkout."""
    repository = BASE.resolve()
    if data_root is not None:
        declared = Path(data_root)
        if not declared.is_absolute() or any(
            part.is_symlink() or getattr(part, "is_junction", lambda: False)()
            for part in (declared, *declared.parents)
        ):
            raise ValueError("evidence data root must be an absolute ordinary directory")
        declared = declared.resolve()
        if (not declared.is_dir() or declared.parent == declared or runtime.parent != declared
                or declared.is_relative_to(repository) or repository.is_relative_to(declared)):
            raise ValueError("evidence runtime must be an immediate child of the separate declared data root")
        return declared
    if not runtime.is_relative_to(repository) and (repository / ".git").is_file():
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                check=True, capture_output=True, text=True, encoding="utf-8", timeout=5,
            )
            common = Path(result.stdout.strip()).resolve()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise ValueError("evidence runtime root repository cannot be verified") from None
        if common.name == ".git" and common.is_dir():
            repository = common.parent
    if runtime == repository or not runtime.is_relative_to(repository):
        raise ValueError("evidence runtime root must be inside the repository")
    return repository


def _combined_checkpoint_health(
    store: SQLiteCheckpointStore,
    runner: CheckpointRetentionRunner,
    recorder: A2CheckpointRecorderV1 | None = None,
) -> dict[str, object]:
    store_health = store.health()
    retention_health = runner.health()
    capture_health = recorder.health() if recorder is not None else {"status": "disabled"}
    reasons: list[str] = []
    counters: dict[str, int] = {}
    for prefix, health in (
        ("store", store_health),
        ("retention", retention_health),
        ("capture", capture_health),
    ):
        raw_reasons = health.get("current_reasons")
        if isinstance(raw_reasons, (list, tuple)):
            reasons.extend(f"{prefix}_{reason}" for reason in raw_reasons)
        raw_counters = health.get("counters")
        if isinstance(raw_counters, dict):
            counters.update(
                {
                    f"{prefix}_{name}": value
                    for name, value in raw_counters.items()
                    if type(value) is int and value >= 0
                }
            )
    failed = store_health.get("status") == "degraded" or retention_health.get(
        "status"
    ) == "degraded" or capture_health.get("status") == "degraded"
    failure_source = next((health for health in (retention_health, capture_health, store_health)
                           if health.get("last_failure_code")), {})
    last_failure_code = str(failure_source.get("last_failure_code") or "")
    last_failure_at = str(failure_source.get("last_failure_at") or "")
    return {
        "status": "degraded" if failed else "ok",
        "current_reasons": reasons,
        "counters": counters,
        "pending": capture_health.get("pending", 0),
        "running": capture_health.get("running", 0),
        "backlog": capture_health.get("backlog", 0),
        "pending_bytes": capture_health.get("pending_bytes", 0),
        "stalled": capture_health.get("stalled", False),
        "circuit_open": capture_health.get("circuit_open", False),
        "maintenance_running": retention_health.get("pending", 0),
        "maintenance_stalled": retention_health.get("stalled", False),
        "queue_capacity": capture_health.get("queue_capacity", 0),
        "accepting": store_health.get("accepting") is True and capture_health.get("accepting", True),
        "last_failure_code": last_failure_code,
        "last_failure_at": last_failure_at,
        "submission_budget": capture_health.get("submission_budget", {}),
    }


def _validate_queue_settings(
    max_concurrent_tasks: int,
    max_queued_tasks: int,
    queue_wait_seconds: float,
) -> None:
    if int(max_concurrent_tasks) <= 0:
        raise ValueError("max_concurrent_tasks must be greater than zero")
    if int(max_queued_tasks) < 0:
        raise ValueError("max_queued_tasks must be zero or greater")
    wait_seconds = float(queue_wait_seconds)
    if not math.isfinite(wait_seconds) or wait_seconds <= 0:
        raise ValueError("queue_wait_seconds must be finite and greater than zero")


def _capture_producer(revision: str) -> ProducerVersionV1:
    producer = ProducerVersionV1(
        code_revision=revision, component="a2_runtime", component_version="checkpoint-v1",
        policy_version="a2-capture-v1",
    )
    head = subprocess.run(["git", "-C", str(BASE), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(BASE), "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    if revision != head or dirty:
        raise ValueError("A2 capture requires the exact clean release revision")
    return producer


def build_app(
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    *,
    control_db: str | Path | None = None,
    invite_config: str | Path | None = None,
    feedback_database: str | Path | None = None,
    evidence_data_root: str | Path | None = None,
    model_timeout_seconds: float = 120.0,
    grounding_timeout_seconds: float = 180.0,
    enable_auto_crop: bool = True,
    enable_triage: bool = True,
    triage_timeout_seconds: float = 120.0,
    reply_timeout_seconds: float = 60.0,
    enable_output_watchdog: bool = True,
    enable_a3_text_orientation: bool = False,
    a3_orientation_dependency_dir: str | Path = DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR,
    max_concurrent_tasks: int = 1,
    max_queued_tasks: int = 2,
    queue_wait_seconds: float = 55.0,
    evidence_capacity: EvidenceCapacityPolicyV1 | None = None,
    checkpoint_retention_backup_root: str | Path | None = None,
    checkpoint_retention_interval_seconds: float | None = None,
    checkpoint_retention_backup_keep_runs: int | None = None,
    enable_a2_checkpoint_capture: bool = False,
    checkpoint_code_revision: str = "",
    enable_a3_checkpoint_capture: bool = False,
    enable_durable_execution: bool = False,
    enable_model_transport_recovery: bool = False,
    background_execution: bool = False,
    background_production: bool = False,
    public_origin: str = "",
):
    _validate_queue_settings(
        max_concurrent_tasks,
        max_queued_tasks,
        queue_wait_seconds,
    )
    root = Path(runtime_dir).resolve()
    if background_production and not background_execution:
        raise ValueError("production background profile requires background execution")
    shared_control_root = None
    if background_production:
        if control_db is None or feedback_database is None or evidence_data_root is None:
            raise ValueError("production background profile requires explicit control, feedback and evidence paths")
        for configured in (runtime_dir, control_db, feedback_database, evidence_data_root):
            path = Path(configured)
            if not path.is_absolute() or any(
                item.is_symlink() or getattr(item, "is_junction", lambda: False)()
                for item in (path, *path.parents)
            ):
                raise ValueError("production service paths must be absolute ordinary paths")
        shared_control_root = Path(control_db).absolute().parent
        if Path(feedback_database).absolute().parent != shared_control_root:
            raise ValueError("shared feedback must be beside the control database")
    if background_execution:
        if not float(queue_wait_seconds).is_integer():
            raise ValueError("background queue deadline must use whole seconds")
        if not enable_durable_execution or control_db is None or invite_config is not None:
            raise ValueError("background execution requires durable execution and an isolated control database")
        if not background_production and Path(control_db).absolute() != root / "control.sqlite3":
            raise ValueError("background control database must be runtime/control.sqlite3")
        if any(part in ({".tmp_feishu_tiku"} if background_production else {".tmp_tiku_agent_v2_prod_8790", ".tmp_feishu_tiku"}) for part in root.parts):
            raise ValueError("background execution requires a separate runtime")
    if type(enable_durable_execution) is not bool:
        raise TypeError("enable_durable_execution must be boolean")
    if (root / "execution.sqlite3").exists() and not enable_durable_execution:
        raise ValueError("An execution database exists; use --enable-durable-execution instead of legacy writers")
    if type(enable_a2_checkpoint_capture) is not bool:
        raise TypeError("enable_a2_checkpoint_capture must be boolean")
    if type(enable_a3_checkpoint_capture) is not bool:
        raise TypeError("enable_a3_checkpoint_capture must be boolean")
    if enable_a3_checkpoint_capture and not enable_a2_checkpoint_capture:
        raise ValueError("A3 capture requires A2 checkpoint capture")
    if enable_a2_checkpoint_capture and evidence_capacity is None:
        raise ValueError("A2 capture requires evidence capacity and retention")
    producer = _capture_producer(checkpoint_code_revision) if enable_a2_checkpoint_capture else None
    if evidence_data_root is not None and evidence_capacity is None:
        raise ValueError("evidence data root requires evidence capacity and retention")
    repository = _evidence_repository_root(root, evidence_data_root) if evidence_capacity is not None else BASE.resolve()
    backup_root = _validate_evidence_configuration(
        evidence_capacity,
        backup_root=checkpoint_retention_backup_root,
        retention_interval_seconds=checkpoint_retention_interval_seconds,
        backup_keep_runs=checkpoint_retention_backup_keep_runs,
        repository_root=repository,
    )
    if control_db is not None and invite_config is not None:
        raise ValueError("use either control_db or invite_config, not both")
    control_path = Path(control_db).resolve() if control_db is not None else None
    if control_path is not None and not control_path.is_file():
        raise ValueError(f"control database not found: {control_path}")
    control_store = SQLiteControlStore(control_path) if control_path is not None else None
    output_watchdog = OutputWatchdog(
        root / "output_watchdog",
        enabled=enable_output_watchdog,
    )
    a3_page_orienter = (
        build_a3_page_orienter(a3_orientation_dependency_dir)
        if enable_a3_text_orientation
        else None
    )
    trace_store = SQLiteTraceEventStore(
        root / TRACE_DATABASE,
        max_rows=(
            evidence_capacity.max_trace_rows
            if evidence_capacity is not None
            else None
        ),
    )
    # Migrate a complete legacy Trace database to the persistent identity
    # format during startup, while leaving an absent database untouched.
    try:
        trace_store.path.lstat()
    except FileNotFoundError:
        pass
    else:
        trace_store.ensure_store_identity()
    checkpoint_store = None
    retention_runner = None
    if evidence_capacity is not None:
        assert backup_root is not None
        assert checkpoint_retention_interval_seconds is not None
        assert checkpoint_retention_backup_keep_runs is not None
        checkpoint_store = SQLiteCheckpointStore(
            root / CHECKPOINT_DATABASE,
            artifact_root=root / CHECKPOINT_ARTIFACT_ROOT,
            capacity=evidence_capacity,
            trace_db_path=root / TRACE_DATABASE,
        )
        retention_runner = CheckpointRetentionRunner(
            runtime_root=root,
            runtime_name="tiku_agent_phase6_8898" if background_execution and not background_production else EVIDENCE_RUNTIME_NAME,
            repository_root=repository,
            backup_root=backup_root,
            capacity=evidence_capacity,
            backup_keep_runs=checkpoint_retention_backup_keep_runs,
        )
    recorder_class = A3CheckpointRecorderV1 if enable_a3_checkpoint_capture else A2CheckpointRecorderV1
    recorder_kwargs = {"a3_media_root": root / "a3_sessions"} if enable_a3_checkpoint_capture else {}
    recorder = (
        recorder_class(
            checkpoint_store, producer=producer, media_root=root / "a2",
            gate=A2CheckpointCaptureGateV1(enabled=True),
            **recorder_kwargs,
        ) if producer is not None else None
    )
    trace_recorder = TraceEventRecorder(trace_store)
    if recorder is not None:
        recorder = AsyncCheckpointRecorder(recorder, trace_recorder=trace_recorder, autostart=False)
    capture_kwargs = {"checkpoint_recorder": recorder} if recorder is not None else {}
    if enable_a3_checkpoint_capture:
        capture_kwargs["a3_checkpoint_recorder"] = recorder
    runtime = build_a3_runtime(
        root,
        model_timeout_seconds=model_timeout_seconds,
        grounding_timeout_seconds=grounding_timeout_seconds,
        enable_auto_crop=enable_auto_crop,
        auto_prepare_all_units=True,
        enable_triage=enable_triage,
        triage_timeout_seconds=triage_timeout_seconds,
        reply_timeout_seconds=reply_timeout_seconds,
        control_store=control_store,
        enable_a3_intent_v1=True,
        enable_a3_intent_model_fallback=True,
        enable_author_contact_fallback=True,
        enable_three_scope_cancel_clarification=True,
        preserve_a2_artifacts_on_cancel=True,
        a3_page_orienter=a3_page_orienter,
        orient_before_routing=True,
        max_concurrent_tasks=max_concurrent_tasks,
        max_queued_tasks=max_queued_tasks,
        queue_wait_seconds=queue_wait_seconds,
        **capture_kwargs,
    )
    if enable_durable_execution:
        from tiku_agent.execution_runtime import attach_execution
        from tiku_agent.execution_store import ExecutionStore, ExecutionPolicy
        attach_execution(runtime, ExecutionStore(root / "execution.sqlite3", policy=ExecutionPolicy(
            model_transport_recovery=enable_model_transport_recovery)))
    if background_execution:
        from tiku_agent.execution_dispatch import DispatchStore, DispatchPolicy
        DispatchStore(runtime, authorize=lambda identity, version: control_store.active_invitation(identity, version) is not None,
                      policy=DispatchPolicy(max_concurrent=max_concurrent_tasks, max_queued=max_queued_tasks,
                                            queue_seconds=int(queue_wait_seconds)))
        from tiku_agent.background_auth import BackgroundInviteAccess
        access = BackgroundInviteAccess(control_store, cookie_name="tiku_phase6_8790_invite" if background_production else "tiku_phase6_8898_invite")
    else:
        access = SQLiteInviteAccess(control_store) if control_store is not None else InviteAccess(invite_config) if invite_config else None
    from tiku_shared.response_store import SQLiteResponseStore
    app = create_app(
        runtime=runtime,
        incoming_dir=root / "incoming",
        session_cookie="tiku_phase6_8898_session" if background_execution and not background_production else SESSION_COOKIE,
        output_watchdog=output_watchdog,
        invite_access=access,
        feedback_store=SQLiteFeedbackStore(
            Path(feedback_database).resolve() if feedback_database is not None else root / "feedback.sqlite3"
        ),
        response_store=SQLiteResponseStore(
            Path(feedback_database).absolute().with_name("responses.sqlite3")
            if feedback_database is not None else root / "responses.sqlite3"
        ),
        background_execution=background_execution,
        background_shared_control_root=shared_control_root,
        background_public_origin=public_origin,
        feedback_retention_days_provider=(
            (lambda: int(control_store.settings()["feedback_retention_days"]))
            if control_store is not None
            else None
        ),
        trace_event_recorder=trace_recorder,
        checkpoint_capture_start=recorder.start if recorder is not None else None,
        checkpoint_capture_close=recorder.close if recorder is not None else None,
        checkpoint_evidence_health_provider=(
            (
                lambda: _combined_checkpoint_health(
                    checkpoint_store,
                    retention_runner,
                    recorder,
                )
            )
            if checkpoint_store is not None and retention_runner is not None
            else None
        ),
        checkpoint_retention_runner=(
            retention_runner.run_once if retention_runner is not None else None
        ),
        checkpoint_retention_interval_seconds=(
            float(checkpoint_retention_interval_seconds)
            if checkpoint_retention_interval_seconds is not None
            else 0.0
        ),
    )
    if hasattr(app, "state"):
        app.state.checkpoint_evidence_store = checkpoint_store
        app.state.checkpoint_retention_controller = retention_runner
        app.state.a2_checkpoint_recorder = recorder
        app.state.a3_checkpoint_recorder = recorder if enable_a3_checkpoint_capture else None
    return app


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the A3-V1 business core on the 8790 production route"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--control-db", type=Path)
    parser.add_argument("--invite-config", type=Path)
    parser.add_argument("--feedback-database", type=Path)
    parser.add_argument("--public-origin", default="", help="Exact HTTPS browser origin for a TLS-terminating public proxy")
    parser.add_argument("--evidence-data-root", type=Path)
    parser.add_argument("--enable-a2-checkpoint-capture", action="store_true", default=False)
    parser.add_argument("--enable-a3-checkpoint-capture", action="store_true", default=False)
    parser.add_argument("--enable-durable-execution", action="store_true", default=False,
                        help="Enable phase-five execution; existing sessions require offline migration")
    parser.add_argument("--disable-model-transport-recovery", action="store_true",
                        help="Disable the single additional attempt for registered transient vision/search model failures")
    parser.add_argument("--enable-background-execution", action="store_true", default=False,
                        help="Enable phase-six production background jobs with explicit shared service paths")
    parser.add_argument("--checkpoint-code-revision", default="")
    parser.add_argument("--max-checkpoint-rows", type=_positive_int, required=True)
    parser.add_argument("--max-artifact-rows", type=_positive_int, required=True)
    parser.add_argument("--max-audit-rows", type=_positive_int, required=True)
    parser.add_argument("--max-trace-rows", type=_positive_int, required=True)
    parser.add_argument("--max-artifact-bytes", type=_positive_int, required=True)
    parser.add_argument("--min-free-bytes", type=_positive_int, required=True)
    parser.add_argument(
        "--max-artifacts-per-checkpoint",
        type=_artifact_count,
        required=True,
    )
    parser.add_argument(
        "--checkpoint-retention-backup-root",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--checkpoint-retention-interval-seconds",
        type=_positive_float,
        required=True,
    )
    parser.add_argument(
        "--checkpoint-retention-backup-keep-runs",
        type=_positive_int,
        required=True,
    )
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--triage-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--reply-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-concurrent-tasks",
        type=_positive_int,
        default=1,
        help="Maximum active production tasks (default: 1)",
    )
    parser.add_argument(
        "--max-queued-tasks",
        type=_nonnegative_int,
        default=2,
        help="Maximum waiting production tasks (default: 2)",
    )
    parser.add_argument(
        "--queue-wait-seconds",
        type=_positive_float,
        default=55.0,
        help="Maximum queue wait before returning busy (default: 55)",
    )
    parser.add_argument(
        "--a3-orientation-dependency-dir",
        type=Path,
        default=DEFAULT_A3_ORIENTATION_DEPENDENCY_DIR,
        help="Isolated RapidOCR dependency directory",
    )
    parser.add_argument(
        "--enable-a3-text-orientation",
        dest="enable_a3_text_orientation",
        action="store_true",
        help="Enable A3 OCR text orientation correction",
    )
    parser.add_argument(
        "--disable-a3-text-orientation",
        dest="enable_a3_text_orientation",
        action="store_false",
        help="Bypass A3 OCR text orientation correction",
    )
    parser.add_argument(
        "--disable-output-watchdog",
        dest="enable_output_watchdog",
        action="store_false",
        help="Disable fail-open output observation",
    )
    parser.add_argument(
        "--disable-triage",
        dest="enable_triage",
        action="store_false",
        help="Temporarily bypass A1/A2/A3 triage",
    )
    parser.add_argument(
        "--disable-auto-crop",
        dest="enable_auto_crop",
        action="store_false",
        help="Roll A3 back to the V0 manual-crop flow",
    )
    parser.set_defaults(
        enable_triage=True,
        enable_auto_crop=True,
        enable_output_watchdog=True,
        enable_a3_text_orientation=False,
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    configure_qwen_connect_retries_from_env()
    uvicorn.run(
        build_app(
            args.runtime_dir,
            control_db=args.control_db,
            invite_config=args.invite_config,
            feedback_database=args.feedback_database,
            public_origin=args.public_origin,
            evidence_data_root=args.evidence_data_root,
            model_timeout_seconds=args.model_timeout_seconds,
            grounding_timeout_seconds=args.grounding_timeout_seconds,
            enable_auto_crop=args.enable_auto_crop,
            enable_triage=args.enable_triage,
            triage_timeout_seconds=args.triage_timeout_seconds,
            reply_timeout_seconds=args.reply_timeout_seconds,
            enable_output_watchdog=args.enable_output_watchdog,
            enable_a3_text_orientation=args.enable_a3_text_orientation,
            a3_orientation_dependency_dir=args.a3_orientation_dependency_dir,
            max_concurrent_tasks=args.max_concurrent_tasks,
            max_queued_tasks=args.max_queued_tasks,
            queue_wait_seconds=args.queue_wait_seconds,
            evidence_capacity=_capacity_from_args(args),
            enable_a2_checkpoint_capture=args.enable_a2_checkpoint_capture,
            enable_a3_checkpoint_capture=args.enable_a3_checkpoint_capture,
            enable_durable_execution=args.enable_durable_execution,
            enable_model_transport_recovery=not args.disable_model_transport_recovery,
            background_execution=args.enable_background_execution,
            background_production=args.enable_background_execution,
            checkpoint_code_revision=args.checkpoint_code_revision,
            checkpoint_retention_backup_root=(
                args.checkpoint_retention_backup_root
            ),
            checkpoint_retention_interval_seconds=(
                args.checkpoint_retention_interval_seconds
            ),
            checkpoint_retention_backup_keep_runs=(
                args.checkpoint_retention_backup_keep_runs
            ),
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

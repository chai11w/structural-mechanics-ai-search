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
    repository = BASE.resolve()
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
    last_failure_code = str(
        retention_health.get("last_failure_code")
        or capture_health.get("last_failure_code")
        or store_health.get("last_failure_code")
        or ""
    )
    last_failure_at = str(
        retention_health.get("last_failure_at")
        or store_health.get("last_failure_at")
        or ""
    )
    return {
        "status": "degraded" if failed else "ok",
        "current_reasons": reasons,
        "counters": counters,
        "pending": capture_health.get("pending", 0),
        "queue_capacity": capture_health.get("queue_capacity", 0),
        "accepting": store_health.get("accepting") is True and capture_health.get("accepting", True),
        "last_failure_code": last_failure_code,
        "last_failure_at": last_failure_at,
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
):
    _validate_queue_settings(
        max_concurrent_tasks,
        max_queued_tasks,
        queue_wait_seconds,
    )
    root = Path(runtime_dir).resolve()
    if type(enable_a2_checkpoint_capture) is not bool:
        raise TypeError("enable_a2_checkpoint_capture must be boolean")
    if type(enable_a3_checkpoint_capture) is not bool:
        raise TypeError("enable_a3_checkpoint_capture must be boolean")
    if enable_a3_checkpoint_capture and not enable_a2_checkpoint_capture:
        raise ValueError("A3 capture requires A2 checkpoint capture")
    if enable_a2_checkpoint_capture and evidence_capacity is None:
        raise ValueError("A2 capture requires evidence capacity and retention")
    producer = _capture_producer(checkpoint_code_revision) if enable_a2_checkpoint_capture else None
    backup_root = _validate_evidence_configuration(
        evidence_capacity,
        backup_root=checkpoint_retention_backup_root,
        retention_interval_seconds=checkpoint_retention_interval_seconds,
        backup_keep_runs=checkpoint_retention_backup_keep_runs,
    )
    repository = BASE.resolve()
    if evidence_capacity is not None and (
        root == repository or not root.is_relative_to(repository)
    ):
        raise ValueError("evidence runtime root must be inside the repository")
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
            runtime_name=EVIDENCE_RUNTIME_NAME,
            repository_root=BASE,
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
    app = create_app(
        runtime=build_a3_runtime(
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
        ),
        incoming_dir=root / "incoming",
        session_cookie=SESSION_COOKIE,
        output_watchdog=output_watchdog,
        invite_access=(
            SQLiteInviteAccess(control_store)
            if control_store is not None
            else InviteAccess(invite_config) if invite_config else None
        ),
        feedback_store=SQLiteFeedbackStore(root / "feedback.sqlite3"),
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
    parser.add_argument("--enable-a2-checkpoint-capture", action="store_true", default=False)
    parser.add_argument("--enable-a3-checkpoint-capture", action="store_true", default=False)
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
    uvicorn.run(
        build_app(
            args.runtime_dir,
            control_db=args.control_db,
            invite_config=args.invite_config,
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

"""Online Checkpoint/Artifact and Trace retention maintenance."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if not sys.path or sys.path[0] != str(BASE):
    sys.path.insert(0, str(BASE))

from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1
from tiku_diagnostics.checkpoint_retention import (
    CheckpointRetentionError,
    RUNTIME_NAME_8790,
    apply_checkpoint_retention_plan,
    build_checkpoint_retention_plan,
    checkpoint_retention_plan_report,
    format_checkpoint_retention_plan,
    load_checkpoint_retention_plan,
    run_checkpoint_retention_once,
    write_checkpoint_retention_plan,
)


RUNTIME_ROOTS = {
    "8790": BASE / ".tmp_tiku_agent_v2_prod_8790",
}
RUNTIME_NAMES = {"8790": RUNTIME_NAME_8790}


def _default_backup_root(base: Path) -> Path:
    for parent in (base, *base.parents):
        if parent.name == "_worktrees":
            relative = base.relative_to(parent)
            return parent.parent / "_backups" / relative.parts[0]
    return base.parent / "_backups" / base.name


DEFAULT_BACKUP_ROOT = _default_backup_root(BASE)


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Default to a read-only unified Checkpoint/Trace retention plan. "
            "Apply and online run-once are explicit modes."
        )
    )
    parser.add_argument("--runtime", choices=tuple(RUNTIME_ROOTS), required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply-plan", type=Path)
    modes.add_argument("--run-once", action="store_true")
    parser.add_argument("--confirm-plan-hash")
    parser.add_argument("--as-of", help="Aware ISO timestamp; plan mode only")
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--backup-keep-runs", type=_positive, required=True)
    parser.add_argument("--max-checkpoint-rows", type=_positive, required=True)
    parser.add_argument("--max-artifact-rows", type=_positive, required=True)
    parser.add_argument("--max-audit-rows", type=_positive, required=True)
    parser.add_argument("--max-trace-rows", type=_positive, required=True)
    parser.add_argument("--max-artifact-bytes", type=_positive, required=True)
    parser.add_argument("--min-free-bytes", type=_positive, required=True)
    parser.add_argument(
        "--max-artifacts-per-checkpoint", type=_positive, required=True
    )
    return parser


def _capacity(args: argparse.Namespace) -> EvidenceCapacityPolicyV1:
    return EvidenceCapacityPolicyV1(
        max_checkpoint_rows=args.max_checkpoint_rows,
        max_artifact_rows=args.max_artifact_rows,
        max_audit_rows=args.max_audit_rows,
        max_trace_rows=args.max_trace_rows,
        max_artifact_bytes=args.max_artifact_bytes,
        min_free_bytes=args.min_free_bytes,
        max_artifacts_per_checkpoint=args.max_artifacts_per_checkpoint,
    )


def _rejected(exc: CheckpointRetentionError) -> int:
    print(
        json.dumps(
            {
                "status": "rejected",
                "failure_code": exc.code.lower(),
                "message": str(exc),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 2


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | str | None = None,
) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        capacity = _capacity(args)
    except ValueError as exc:
        return _rejected(CheckpointRetentionError(str(exc)))
    runtime = RUNTIME_ROOTS[args.runtime]

    if args.apply_plan is not None:
        if args.as_of or args.plan_out:
            return _rejected(
                CheckpointRetentionError("--as-of/--plan-out are plan-only options")
            )
        if not str(args.confirm_plan_hash or "").strip():
            return _rejected(
                CheckpointRetentionError("--confirm-plan-hash is required for apply")
            )
        try:
            plan = load_checkpoint_retention_plan(args.apply_plan, now=now)
            if (
                plan.get("runtime_name") != RUNTIME_NAMES[args.runtime]
                or Path(str(plan.get("runtime_root"))).resolve(strict=False)
                != runtime.resolve(strict=False)
            ):
                raise CheckpointRetentionError(
                    "saved plan does not belong to the selected runtime"
                )
            result = apply_checkpoint_retention_plan(
                plan,
                expected_plan_hash=args.confirm_plan_hash,
                repository_root=BASE,
                backup_root=args.backup_root,
                allowed_runtime_roots=(runtime,),
                capacity=capacity,
                now=now,
            )
        except CheckpointRetentionError as exc:
            return _rejected(exc)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.run_once:
        if args.as_of or args.plan_out or args.confirm_plan_hash:
            return _rejected(
                CheckpointRetentionError(
                    "--as-of/--plan-out/--confirm-plan-hash cannot be used with run-once"
                )
            )
        try:
            result = run_checkpoint_retention_once(
                runtime,
                runtime_name=RUNTIME_NAMES[args.runtime],
                repository_root=BASE,
                backup_root=args.backup_root,
                allowed_runtime_roots=(runtime,),
                capacity=capacity,
                backup_keep_runs=args.backup_keep_runs,
                now=now,
            )
        except CheckpointRetentionError as exc:
            return _rejected(exc)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.confirm_plan_hash:
        return _rejected(
            CheckpointRetentionError("--confirm-plan-hash is apply-only")
        )
    try:
        plan = build_checkpoint_retention_plan(
            runtime,
            runtime_name=RUNTIME_NAMES[args.runtime],
            repository_root=BASE,
            capacity=capacity,
            backup_keep_runs=args.backup_keep_runs,
            as_of=args.as_of,
            now=now,
        )
        if args.plan_out is not None:
            write_checkpoint_retention_plan(args.plan_out, plan, now=now)
    except CheckpointRetentionError as exc:
        return _rejected(exc)
    if args.format == "json":
        print(
            json.dumps(
                checkpoint_retention_plan_report(plan, now=now),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(format_checkpoint_retention_plan(plan, now=now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

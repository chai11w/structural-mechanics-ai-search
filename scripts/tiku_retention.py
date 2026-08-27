"""Plan or explicitly apply retention maintenance for 8790/8896 evidence."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if not sys.path or sys.path[0] != str(BASE):
    sys.path.insert(0, str(BASE))

from tiku_diagnostics.retention import (
    RetentionError,
    apply_retention_plan,
    build_retention_plan,
    format_retention_plan,
    load_retention_plan,
    retention_plan_report,
    write_retention_plan,
)


RUNTIME_ROOTS = {
    "8790": BASE / ".tmp_tiku_agent_v2_prod_8790",
    "8896": BASE / ".tmp_tiku_agent_a3_mvp_8896",
}
DEFAULT_BACKUP_ROOT = BASE.parent / "_backups" / BASE.name


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Default to a read-only retention plan. Applying requires a saved plan, "
            "its exact hash, and verified repository-external backups."
        )
    )
    parser.add_argument("--runtime", choices=tuple(RUNTIME_ROOTS), required=True)
    parser.add_argument("--as-of", help="Aware ISO-8601 cutoff anchor; defaults to current UTC")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument(
        "--plan-out",
        type=Path,
        help="Explicitly save the generated dry-run plan; runtime data stays read-only",
    )
    parser.add_argument(
        "--apply-plan",
        type=Path,
        help="Apply this saved JSON plan after all safety and backup checks",
    )
    parser.add_argument(
        "--confirm-plan-hash",
        help="Required with --apply-plan and must exactly match the plan SHA-256",
    )
    parser.add_argument(
        "--confirm-runtime-stopped",
        action="store_true",
        help="Required with --apply-plan because feedback media has no cross-process lock",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help="Repository-external backup base used only by an explicitly confirmed apply",
    )
    return parser


def main(
    argv: list[str] | None = None, *, now: datetime | str | None = None
) -> int:
    args = build_argument_parser().parse_args(argv)
    runtime_root = RUNTIME_ROOTS[args.runtime]
    if args.apply_plan is not None:
        if args.as_of or args.plan_out:
            print(
                "retention apply rejected: --as-of/--plan-out are plan-only options",
                file=sys.stderr,
            )
            return 2
        if not str(args.confirm_plan_hash or "").strip():
            print(
                "retention apply rejected: --confirm-plan-hash is required",
                file=sys.stderr,
            )
            return 2
        if not args.confirm_runtime_stopped:
            print(
                "retention apply rejected: --confirm-runtime-stopped is required",
                file=sys.stderr,
            )
            return 2
        try:
            if args.apply_plan.resolve().is_relative_to(BASE.resolve()):
                raise RetentionError("plan file must stay outside the repository")
            plan = load_retention_plan(args.apply_plan, now=now)
            if str(plan.get("runtime_name") or "") != args.runtime:
                raise RetentionError("saved plan does not belong to the selected runtime")
            if str(plan.get("runtime_root") or "") != str(runtime_root.resolve()):
                raise RetentionError("saved plan does not belong to the selected runtime")
            result = apply_retention_plan(
                plan,
                expected_plan_hash=args.confirm_plan_hash,
                repository_root=BASE,
                backup_root=args.backup_root,
                allowed_runtime_roots=(runtime_root,),
                runtime_stopped_confirmed=True,
                now=now,
            )
        except RetentionError as exc:
            print(f"retention apply rejected: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.confirm_plan_hash or args.confirm_runtime_stopped:
        print(
            "retention plan rejected: apply confirmations are apply-only",
            file=sys.stderr,
        )
        return 2
    try:
        plan = build_retention_plan(
            runtime_root,
            runtime_name=args.runtime,
            repository_root=BASE,
            as_of=args.as_of,
            now=now,
            future_report_only=args.plan_out is None,
        )
        if args.plan_out is not None:
            if args.plan_out.resolve().is_relative_to(BASE.resolve()):
                raise RetentionError("plan output must stay outside the repository")
            write_retention_plan(args.plan_out, plan, now=now)
    except RetentionError as exc:
        print(f"retention plan rejected: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(
            json.dumps(
                retention_plan_report(plan),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(format_retention_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

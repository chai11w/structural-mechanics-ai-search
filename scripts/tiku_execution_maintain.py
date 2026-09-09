"""Inspect phase-five execution or apply an explicitly reviewed local plan.

This tool never discovers, stops or restarts services and never calls models.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tiku_agent.execution_maintenance import inspect_execution, plan_cost_reconciliation, apply_cost_reconciliation
from tiku_agent.execution_operations import OperationStore
from tiku_agent.execution_retention import plan_cleanup, apply_cleanup, execution_policy
from tiku_agent.execution_maintenance import read_execution
from tiku_agent.execution_store import ExecutionError, ExecutionStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "cost-plan", "cost-apply", "cleanup-plan", "cleanup-apply"))
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--run-id", action="append")
    parser.add_argument("--limit", type=int, help="1..100; plan/inspect default is 20")
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--confirm-plan-hash")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--artifact-root", action="append", type=Path,
                        help="Optional subtree of a persisted approved artifact root; narrow it if scanning exceeds the limit")
    parser.add_argument("--compact", action="store_true", help="Include database compaction in a cleanup plan")
    args = parser.parse_args(argv)
    limit = 20 if args.limit is None else args.limit
    if args.mode.endswith("-apply") and (args.limit is not None or args.run_id or args.compact):
        parser.error("apply uses the reviewed plan's exact selection; limit/run-id/compact belong to plan creation")
    if args.run_id and args.mode != "cost-plan":
        parser.error("--run-id is only valid with cost-plan")
    if args.compact and args.mode != "cleanup-plan":
        parser.error("--compact is only valid with cleanup-plan")
    if args.artifact_root and not args.mode.startswith("cleanup-"):
        parser.error("--artifact-root is only valid with cleanup modes")
    if args.mode.startswith("cost-") and args.ledger is None:
        parser.error("cost modes require --ledger")
    if args.mode.endswith("-apply") and not all((args.plan, args.confirm_plan_hash, args.backup_dir)):
        parser.error("apply requires --plan, --confirm-plan-hash and --backup-dir")
    if args.plan_out is not None and not args.mode.endswith("-plan"):
        parser.error("--plan-out is only valid in plan modes")
    try:
        if args.mode == "inspect":
            result = inspect_execution(args.database, limit=limit)
        elif args.mode == "cost-plan":
            result = plan_cost_reconciliation(args.database, args.ledger, run_ids=args.run_id, limit=limit)
        elif args.mode == "cleanup-plan":
            result = plan_cleanup(args.database, roots=args.artifact_root, limit=limit, compact=args.compact)
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
            if plan.get("plan_hash") != args.confirm_plan_hash:
                raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
            if not args.database.is_file():
                raise ExecutionError("EXECUTION_DATABASE_MISSING")
            with read_execution(args.database) as conn:
                policy = execution_policy(conn)
            operations = OperationStore(ExecutionStore(args.database, policy=policy))
            if args.mode == "cost-apply":
                result = apply_cost_reconciliation(operations, args.ledger, plan, backup_dir=args.backup_dir)
            else:
                result = apply_cleanup(operations, plan, backup_dir=args.backup_dir, roots=args.artifact_root)
        if args.plan_out is not None:
            with args.plan_out.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExecutionError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error":exc.code if isinstance(exc, ExecutionError) else "EXECUTION_MAINTENANCE_IO_ERROR"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

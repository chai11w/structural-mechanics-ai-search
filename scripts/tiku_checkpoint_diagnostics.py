"""Audited Checkpoint diagnostics for trusted local operators."""

from __future__ import annotations

import argparse
import base64
import json
from hashlib import sha256
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if not sys.path or sys.path[0] != str(BASE):
    sys.path.insert(0, str(BASE))

from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1
from tiku_agent.checkpoint_store import (
    CheckpointQueryScopeV1, EvidenceAuditError, EvidenceConflictError,
    EvidenceOwnershipError, EvidenceValidationError,
)
from tiku_diagnostics.checkpoints import CheckpointDiagnosticService
from tiku_diagnostics import checkpoint_retention as maintenance


CAPACITY_FIELDS = (
    "max_checkpoint_rows", "max_artifact_rows", "max_audit_rows", "max_trace_rows",
    "max_artifact_bytes", "min_free_bytes", "max_artifacts_per_checkpoint",
)


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Audited, scoped evidence reads and confirmed maintenance")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--actor-key", required=True)
    parser.add_argument("--identity-key", required=True)
    parser.add_argument("--session-key", default="", help="Required except for audited Trace/identity discovery")
    parser.add_argument("--workflow-search-id", default="")
    parser.add_argument("--workflow-task-revision", type=int)
    for key in CAPACITY_FIELDS:
        parser.add_argument("--" + key.replace("_", "-"), type=int, required=True)
    parser.add_argument("--format", choices=("human", "json"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query", help="List scoped checkpoints by Trace or workflow, with view audits")
    query.add_argument("--trace-id", default="")
    query.add_argument("--limit", type=int, default=50)
    query.add_argument("--after-checkpoint-id", default="")
    show = commands.add_parser("show", help="Read one complete validated checkpoint")
    show.add_argument("--checkpoint-id", required=True)
    chain = commands.add_parser("chain", help="Follow bounded predecessors within one workflow revision")
    chain.add_argument("--checkpoint-id", required=True)
    chain.add_argument("--limit", type=int, default=20)
    artifact = commands.add_parser("artifact", help="Read an authorized Artifact through its checkpoint")
    artifact.add_argument("--checkpoint-id", required=True)
    artifact.add_argument("--artifact-id", required=True)
    artifact.add_argument("--include-content", action="store_true", help="Include base64 image bytes; no preview file is created")
    bank = commands.add_parser("bank-image", help="Read the current bank image through an audited checkpoint reference")
    bank.add_argument("--checkpoint-id", required=True)
    bank.add_argument("--bank-root", type=Path, required=True, help="Trusted current root for bank id main")
    selection = bank.add_mutually_exclusive_group(required=True)
    selection.add_argument("--answer-ordinal", type=int)
    selection.add_argument("--candidate-id")
    bank.add_argument("--include-content", action="store_true")
    for operation in ("extend", "delete"):
        plan = commands.add_parser("plan-" + operation, help="Build a finite exact-target management plan")
        plan.add_argument("--checkpoint-id", required=True)
        plan.add_argument("--artifact-id", default="")
        plan.add_argument("--reason-code", required=True)
        plan.add_argument("--plan-out", type=Path)
        if operation == "extend":
            plan.add_argument("--new-expires-at", required=True)
            plan.add_argument("--retention-class", choices=("investigation", "feedback"), default="")
    apply = commands.add_parser("apply", help="Apply a confirmed plan after a verified external backup")
    apply.add_argument("--plan-file", type=Path, required=True)
    apply.add_argument("--confirm-plan-hash", required=True)
    apply.add_argument("--backup-root", type=Path, required=True)
    apply.add_argument("--max-backup-runs", type=int, required=True)
    return parser


def format_human(result):
    if "checkpoints" in result:
        lines = [f"{row['occurred_at']} {row['stage']} {row['outcome']} {row['checkpoint_id']}"
                 for row in result["checkpoints"]]
        if result.get("next_checkpoint_id"):
            lines.append("next_checkpoint_id=" + result["next_checkpoint_id"])
        if result.get("stop_reason"):
            lines.append("stop_reason=" + result["stop_reason"])
        return "\n".join(lines) or "No available checkpoints in this scope."
    return json.dumps(result, ensure_ascii=False, indent=2)


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    try:
        capacity = EvidenceCapacityPolicyV1(**{key: getattr(args, key) for key in CAPACITY_FIELDS})
        service = CheckpointDiagnosticService(args.runtime_root, capacity=capacity, actor_key=args.actor_key,
            scope=CheckpointQueryScopeV1(args.identity_key, args.session_key,
                                        args.workflow_search_id, args.workflow_task_revision))
        if args.command == "query":
            result = service.query(trace_id=args.trace_id, limit=args.limit, after_checkpoint_id=args.after_checkpoint_id)
        elif args.command == "show":
            result = service.checkpoint(args.checkpoint_id).to_dict()
        elif args.command == "chain":
            result = service.chain(args.checkpoint_id, limit=args.limit)
        elif args.command == "artifact":
            artifact = service.artifact(args.checkpoint_id, args.artifact_id)
            result = {"descriptor": artifact.descriptor.to_dict()}
            if args.include_content:
                result["content_base64"] = base64.b64encode(artifact.content).decode("ascii")
        elif args.command == "bank-image":
            from tiku_agent.checkpoint_bank_reference import CheckpointBankCatalog
            reference, content = service.bank_image(args.checkpoint_id,
                catalog=CheckpointBankCatalog({"main": args.bank_root}),
                answer_ordinal=args.answer_ordinal, candidate_id=args.candidate_id or "")
            result = {"reference": reference.to_dict(), "byte_size": len(content),
                      "current_sha256": sha256(content).hexdigest()}
            if args.include_content:
                result["content_base64"] = base64.b64encode(content).decode("ascii")
        elif args.command.startswith("plan-"):
            result = service.plan(args.command.removeprefix("plan-"), checkpoint_id=args.checkpoint_id,
                artifact_id=args.artifact_id, new_expires_at=getattr(args, "new_expires_at", ""),
                retention_class=getattr(args, "retention_class", ""), reason_code=args.reason_code)
            if args.plan_out is not None:
                path = maintenance._absolute_path(args.plan_out)
                maintenance._reject_linked_path(path)
                if path == service.runtime or path.is_relative_to(service.runtime):
                    raise EvidenceValidationError("plan output must be outside runtime data")
                maintenance._write_json_exclusive(path, result)
        else:
            maintenance._reject_linked_path(args.plan_file)
            if args.plan_file.stat().st_size > 65_536:
                raise EvidenceValidationError("management plan exceeds its size bound")
            plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
            result = service.apply(plan, expected_hash=args.confirm_plan_hash, backup_root=args.backup_root,
                                   repository_root=BASE, max_backup_runs=args.max_backup_runs)
    except Exception as exc:
        code = ("EVIDENCE_AUDIT_UNAVAILABLE" if isinstance(exc, EvidenceAuditError) else
                "EVIDENCE_CONFLICT" if isinstance(exc, EvidenceConflictError) else
                "EVIDENCE_SCOPE_REJECTED" if isinstance(exc, EvidenceOwnershipError) else
                "EVIDENCE_REQUEST_INVALID" if isinstance(exc, (EvidenceValidationError, ValueError)) else
                "EVIDENCE_UNAVAILABLE")
        print(json.dumps({"status": "rejected", "failure_code": code}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")) if args.format == "json" else format_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

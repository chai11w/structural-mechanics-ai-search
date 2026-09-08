"""Plan/apply phase 5 migration of an offline runtime clone only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tiku_agent.execution_migration import migrate_offline, plan_migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--child-db", type=Path, required=True)
    parser.add_argument("--workflow-db", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--offline-clone-root", type=Path)
    parser.add_argument("--expected-plan", type=Path)
    parser.add_argument("--backup-dir", type=Path, help="New directory outside the Git repository")
    args = parser.parse_args()
    if args.mode == "plan":
        result = plan_migration(args.child_db, args.workflow_db)
    else:
        if not all((args.destination, args.offline_clone_root, args.expected_plan, args.backup_dir)):
            parser.error("apply requires destination, offline-clone-root, expected-plan and backup-dir")
        repo = Path(__file__).resolve().parents[1]
        if args.backup_dir.resolve().is_relative_to(repo):
            parser.error("backup-dir must be outside this repository")
        root = args.offline_clone_root.resolve()
        if not all(p.resolve().is_relative_to(root) for p in (args.child_db,args.workflow_db,args.destination) if p):
            parser.error("databases must belong to the offline clone")
        result = migrate_offline(args.child_db,args.workflow_db,destination=args.destination,
            artifact_root=root,expected_plan=json.loads(args.expected_plan.read_text(encoding="utf-8-sig")),
            backup_dir=args.backup_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()

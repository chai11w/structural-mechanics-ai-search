"""Initialize the 8795 administrator and migrate hash-only invitation config."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_admin.control_store import LegacyImportReport, SQLiteControlStore


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local 8795 control database")
    parser.add_argument("--control-db", type=Path, required=True)
    parser.add_argument("--import-invites", type=Path)
    parser.add_argument(
        "--apply-import",
        action="store_true",
        help="Apply a compatible legacy invitation import after printing its preflight",
    )
    parser.add_argument(
        "--require-status-match",
        action="store_true",
        help="Reject existing invitation records whose status differs from the legacy config",
    )
    return parser


def _print_import_report(report: LegacyImportReport) -> None:
    print("legacy invitation import preflight")
    print(f"source invitations: {report.source_count}")
    print(f"existing invitations: {report.existing_count}")
    print(f"invitations to insert: {report.insert_count}")
    print(f"already present: {report.unchanged_count}")
    print(f"conflicts: {len(report.conflicts)}")
    print(f"invite cookie secret: {report.cookie_secret_action}")
    for conflict in report.conflicts:
        suffix = (
            f" (existing invitation: {conflict.existing_invite_id})"
            if conflict.existing_invite_id
            else ""
        )
        print(f"conflict: {conflict.kind} for {conflict.invite_id}{suffix}")


def main() -> int:
    args = build_argument_parser().parse_args()
    if args.apply_import and not args.import_invites:
        raise SystemExit("--apply-import requires --import-invites")
    store = SQLiteControlStore(args.control_db)
    if args.import_invites:
        report = store.preflight_legacy_config(
            args.import_invites,
            require_status_match=args.require_status_match,
        )
        _print_import_report(report)
        if not report.can_apply:
            print("no changes written because conflicts must be resolved first")
            return 2
        if not args.apply_import:
            print("no changes written; rerun with --apply-import after reviewing this report")
            return 0
        applied = store.import_legacy_config(
            args.import_invites,
            require_status_match=args.require_status_match,
        )
        print(f"imported {applied.insert_count} hash-only invitations")
        print(f"preserved {applied.unchanged_count} matching invitations")
    if not store.has_admin():
        password = getpass("Administrator password (12+ characters): ")
        confirmation = getpass("Confirm administrator password: ")
        if password != confirmation:
            raise SystemExit("password confirmation does not match")
        store.initialize_admin(password)
        print("administrator initialized")
    else:
        print("administrator already initialized")
    print(f"control database: {store.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

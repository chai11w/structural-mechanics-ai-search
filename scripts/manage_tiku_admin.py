"""Initialize the 8795 administrator and migrate hash-only invitation config."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_admin.control_store import SQLiteControlStore


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the local 8795 control database")
    parser.add_argument("--control-db", type=Path, required=True)
    parser.add_argument("--import-invites", type=Path)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    store = SQLiteControlStore(args.control_db)
    if args.import_invites:
        count = store.import_legacy_config(args.import_invites)
        print(f"imported {count} hash-only invitations")
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

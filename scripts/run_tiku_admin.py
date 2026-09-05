"""Run the isolated 8795 administration console."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_admin.app import create_admin_app
from tiku_admin.control_store import SQLiteControlStore
from tiku_admin.invite_vault import InvitationCodeVault
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_diagnostics import DiagnosticQueryService


DEFAULT_ADMIN_RUNTIME = BASE / ".tmp_tiku_admin_8795"
DEFAULT_SOURCE_RUNTIME = BASE / ".tmp_tiku_agent_v2_prod_8790"


def build_app(
    *,
    admin_runtime: str | Path = DEFAULT_ADMIN_RUNTIME,
    source_runtime: str | Path = DEFAULT_SOURCE_RUNTIME,
    allow_local_setup: bool = True,
):
    admin_root = Path(admin_runtime).resolve()
    source_root = Path(source_runtime).resolve()
    invitation_vault = InvitationCodeVault.load_or_create(
        admin_root / "invite_code_encryption.key"
    )
    control_store = SQLiteControlStore(
        admin_root / "control.sqlite3",
        invitation_vault=invitation_vault,
    )
    feedback_store = SQLiteFeedbackStore(
        source_root / "feedback.sqlite3", cases_root=source_root / "feedback_cases"
    )
    reporter = AdminReporter(
        control_store=control_store,
        cost_databases=(
            source_root / "model_costs.sqlite3",
            source_root / "a2" / "model_costs.sqlite3",
        ),
        feedback_store=feedback_store,
    )
    diagnostic_query = DiagnosticQueryService(source_root)
    return create_admin_app(
        control_store=control_store,
        reporter=reporter,
        feedback_store=feedback_store,
        diagnostic_query=diagnostic_query,
        allow_local_setup=allow_local_setup,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated 8795 administration console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--admin-runtime", type=Path, default=DEFAULT_ADMIN_RUNTIME)
    parser.add_argument("--source-runtime", type=Path, default=DEFAULT_SOURCE_RUNTIME)
    parser.add_argument("--disable-local-setup", action="store_true")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    uvicorn.run(
        build_app(
            admin_runtime=args.admin_runtime,
            source_runtime=args.source_runtime,
            allow_local_setup=not args.disable_local_setup,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

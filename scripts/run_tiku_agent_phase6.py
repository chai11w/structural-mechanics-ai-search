"""Run authenticated detached A2/A3 jobs using an isolated, migrated runtime."""
from __future__ import annotations

import inspect
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import run_tiku_agent_8790 as production


def build_app(runtime_dir, **kwargs):
    root = Path(runtime_dir).absolute()
    if root != root.resolve() or any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("phase 6 runtime must not contain links")
    kwargs.update(background_execution=True, enable_durable_execution=True,
                  control_db=root / "control.sqlite3")
    return production.build_app(root, **kwargs)


def build_argument_parser():
    parser = production.build_argument_parser()
    parser.description = __doc__
    parser.set_defaults(port=8898, runtime_dir=None)
    return parser


def main():
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.runtime_dir is None:
        parser.error("--runtime-dir is required; prepare an isolated control database and migrate offline first")
    if args.control_db is not None or args.invite_config is not None:
        parser.error("phase 6 uses only runtime-dir/control.sqlite3")
    if args.host != "127.0.0.1" or not 1024 <= args.port <= 65535 or args.port in {8788, 8790, 8795, 8888, 8896}:
        parser.error("choose a separate loopback port (default 8898)")
    parameters = inspect.signature(production.build_app).parameters
    kwargs = {key: value for key, value in vars(args).items() if key in parameters and key != "runtime_dir"}
    kwargs["evidence_capacity"] = production._capacity_from_args(args)
    import uvicorn
    uvicorn.run(build_app(args.runtime_dir, **kwargs), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

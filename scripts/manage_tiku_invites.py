"""Create local-only invitation credentials for the 8790 web route."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.invite_access import build_invitation_config


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create hash-only 8790 invitation config")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--delivery-file", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    config_path = args.config.resolve()
    delivery_path = args.delivery_file.resolve()
    if config_path.exists() or delivery_path.exists():
        raise SystemExit("refusing to overwrite an existing invitation file")
    config, codes = build_invitation_config(args.count)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    delivery_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    delivery_path.write_text(
        "结构力学搜题 8790 内测邀请码（每码每日独立额度）\n"
        "请逐个发放，不要公开或提交到 Git。\n\n"
        + "\n".join(f"{invite_id}: {code}" for invite_id, code in codes)
        + "\n",
        encoding="utf-8",
    )
    print(f"created {len(codes)} invitations")
    print(f"hash-only config: {config_path}")
    print(f"private delivery file: {delivery_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

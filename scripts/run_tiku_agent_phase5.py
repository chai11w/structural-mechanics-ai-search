"""Isolated phase 5 A2/A3 web entry. No production launcher defaults change."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from scripts.run_tiku_agent_8896 import build_runtime
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionStore
from tiku_agent.fastapi_demo import create_app


def build_app(runtime_dir: str | Path, *, runtime=None, configuration_version=None):
    root=Path(runtime_dir).resolve()
    if any(part in {".tmp_tiku_agent_v2_prod_8790", ".tmp_feishu_tiku"} for part in root.parts):
        raise ValueError("phase 5 requires a separate runtime directory")
    authority=ExecutionStore(root/"execution.sqlite3")
    target=runtime or build_runtime(root,max_concurrent_tasks=1,max_queued_tasks=2,queue_wait_seconds=55)
    attach_execution(target,authority,configuration_version=configuration_version)
    return create_app(runtime=target,incoming_dir=root/"incoming",session_cookie="tiku_phase5_session")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir",type=Path,default=BASE/".tmp_phase5_runtime")
    parser.add_argument("--port",type=int,default=8910)
    args=parser.parse_args()
    if not 1024<=args.port<=65535 or args.port in {8788,8790,8795,8888,8896,8902}:
        parser.error("choose an isolated non-production port")
    import uvicorn
    uvicorn.run(build_app(args.runtime_dir),host="127.0.0.1",port=args.port)


if __name__=="__main__":
    main()

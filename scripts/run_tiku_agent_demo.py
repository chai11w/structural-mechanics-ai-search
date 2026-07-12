"""Run the isolated local Agent demo on port 8790."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_agent.fastapi_demo import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated question-bank Agent FastAPI demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

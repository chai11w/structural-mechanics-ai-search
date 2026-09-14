"""Run the phase 6.2 kernel offline with a counted synthetic provider.

No listener, model credentials, production configuration or live bank is used.
The only permitted output tree is this checkout's .tmp_phase6_runtime directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def isolated_root(value=None):
    allowed = BASE / ".tmp_phase6_runtime"
    root = Path(value).absolute() if value else allowed / ("probe-" + uuid4().hex)
    if (not root.resolve().is_relative_to(allowed) or root.resolve() == allowed
            or root.resolve() != root or any(parent.is_symlink() for parent in [root, *root.parents])):
        raise ValueError("runtime must be a new directory inside this checkout's .tmp_phase6_runtime")
    if root.exists():
        raise ValueError("probe runtime must be new; existing state is never reused")
    root.mkdir(parents=True, exist_ok=False)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", help="New private output directory inside .tmp_phase6_runtime")
    args = parser.parse_args(argv)
    root = isolated_root(args.runtime_dir)
    from tests.test_execution_dispatch import DispatchFixture
    from tiku_agent.execution_worker import BackgroundWorker
    fixture = DispatchFixture(root)
    ack, request, grant = fixture.accept("phase6-offline-probe")
    # Reconstruct the execution runtime after acceptance. The original submitter
    # supplies neither a closure nor a body to the worker.
    _, dispatch = fixture.build()
    worker = BackgroundWorker(dispatch, root / "reconstructed-worker")
    worker.run_once()
    result = fixture.observe(request, grant, result=True)
    for _ in range(3):
        assert fixture.observe(request, grant)["operation_id"] == ack["operation_id"]
    assert result["status"] == "SUCCEEDED" and fixture.calls == ["phase6-offline-probe"]
    assert fixture.rows("execution_cost_outbox")[0]["status"] == "CONFIRMED"
    report = {"scope": "phase6.2 offline synthetic kernel", "accepted": ack,
              "final_status": result["status"], "provider_calls": len(fixture.calls),
              "attempts": len(fixture.rows("execution_attempts")), "accounting": "CONFIRMED",
              "repeated_reads": 3, "drain": worker.close(), "runtime_dir": str(root)}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

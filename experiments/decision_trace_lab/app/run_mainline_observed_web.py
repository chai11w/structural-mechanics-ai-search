from __future__ import annotations

from pathlib import Path
import os
import sys


LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from mainline_mirror.observation.web import create_observed_app  # noqa: E402


runtime_root = os.environ.get("REVIEW_TIKU_RUNTIME_ROOT")
data_root = os.environ.get("REVIEW_TIKU_DATA_ROOT")
runtime_namespace = os.environ.get("REVIEW_TIKU_RUNTIME_NAMESPACE", "decision-trace-dev")
app = create_observed_app(
    **({"runtime_root": runtime_root} if runtime_root else {}),
    **({"data_root": data_root} if data_root else {}),
    runtime_namespace=runtime_namespace,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8793, access_log=False)

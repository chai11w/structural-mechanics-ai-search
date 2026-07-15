from __future__ import annotations

from pathlib import Path
import sys


LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from mainline_mirror.observation.web import create_observed_app  # noqa: E402


app = create_observed_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8793, access_log=False)

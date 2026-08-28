"""Install the pinned A3 orientation dependencies into an isolated directory."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


DEFAULT_TARGET = Path(r"F:\ruanjian\tiku-a3-orientation-8790")
PINNED_REQUIREMENTS = ("rapidocr==3.9.2", "onnxruntime==1.29.0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    target = args.target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--target",
            str(target),
            *PINNED_REQUIREMENTS,
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

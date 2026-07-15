from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any


MIRROR_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = MIRROR_ROOT / "source"
MANIFEST_PATH = MIRROR_ROOT / "manifest.json"


class SnapshotIntegrityError(RuntimeError):
    pass


def load_manifest() -> dict[str, Any]:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - startup must fail closed.
        raise SnapshotIntegrityError("mainline mirror manifest is unreadable") from exc
    if not payload.get("source_commit") or not payload.get("files"):
        raise SnapshotIntegrityError("mainline mirror manifest is incomplete")
    return payload


def verify_snapshot() -> dict[str, Any]:
    manifest = load_manifest()
    expected_paths: set[str] = set()
    failures: list[str] = []
    for entry in manifest["files"]:
        relative = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "").lower()
        if not relative or not expected:
            failures.append("invalid manifest entry")
            continue
        expected_paths.add(relative)
        target = (SOURCE_ROOT / relative).resolve()
        try:
            target.relative_to(SOURCE_ROOT.resolve())
        except ValueError:
            failures.append(f"path escapes source root: {relative}")
            continue
        if not target.is_file():
            failures.append(f"missing: {relative}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(f"hash mismatch: {relative}")
    actual_paths = {
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    extras = sorted(actual_paths - expected_paths)
    if extras:
        failures.extend(f"unmanifested: {path}" for path in extras)
    if failures:
        raise SnapshotIntegrityError("mainline mirror verification failed: " + "; ".join(failures[:8]))
    return manifest


def activate_verified_source() -> dict[str, Any]:
    manifest = verify_snapshot()
    source = str(SOURCE_ROOT)
    if source not in sys.path:
        sys.path.insert(0, source)
    return manifest

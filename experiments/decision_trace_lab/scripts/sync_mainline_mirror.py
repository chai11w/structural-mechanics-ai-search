"""Rebuild the 8793 verified source mirror from one committed mainline tree."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


LAB_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = LAB_ROOT.parents[1]
MIRROR_ROOT = LAB_ROOT / "mainline_mirror"
SOURCE_ROOT = MIRROR_ROOT / "source"
MANIFEST_PATH = MIRROR_ROOT / "manifest.json"

ROOT_FILES = {
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "SKILL.md",
    "build_index.py",
    "config.example.json",
    "multi_agent_pipeline.py",
    "requirements.txt",
    "search.py",
}
SOURCE_PREFIXES = ("scripts/", "tests/", "tiku_agent/", "tiku_shared/")
SNAPSHOT_SCOPE = (
    "tracked mainline code, tests, fixtures and safe documentation; excludes .git, "
    "local config/secrets, xlsx question-bank assets, runtime/logs/caches, "
    "project-agent metadata, review data, and special_unindexed_questions.json"
)


def _git(*args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
    )
    return result.stdout


def _source_paths(commit: str) -> list[str]:
    raw = _git("-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "-z", commit)
    assert isinstance(raw, bytes)
    tracked = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    return sorted(
        path
        for path in tracked
        if path in ROOT_FILES or path.startswith(SOURCE_PREFIXES)
    )


def rebuild_mirror(commit_ref: str, source_branch: str) -> dict[str, object]:
    commit = str(_git("rev-parse", f"{commit_ref}^{{commit}}", text=True)).strip()
    paths = _source_paths(commit)
    if not paths:
        raise RuntimeError("mainline mirror source selection is empty")

    expected_root = (LAB_ROOT / "mainline_mirror" / "source").resolve()
    if SOURCE_ROOT.resolve() != expected_root:
        raise RuntimeError("refusing to replace an unexpected mirror source path")
    if SOURCE_ROOT.exists():
        shutil.rmtree(SOURCE_ROOT)
    SOURCE_ROOT.mkdir(parents=True)

    entries: list[dict[str, str]] = []
    for relative in paths:
        payload = _git("show", f"{commit}:{relative}")
        assert isinstance(payload, bytes)
        target = SOURCE_ROOT / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append({
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
        })

    manifest: dict[str, object] = {
        "schema_version": 1,
        "source_repository": REPOSITORY_ROOT.as_posix(),
        "source_branch": source_branch,
        "source_commit": commit,
        "created_at": datetime.now(UTC).isoformat(),
        "snapshot_scope": SNAPSHOT_SCOPE,
        "files": entries,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="Committed mainline revision to mirror")
    parser.add_argument("--source-branch", required=True)
    args = parser.parse_args()
    manifest = rebuild_mirror(args.commit, args.source_branch)
    print(
        f"mirrored {len(manifest['files'])} files from "
        f"{manifest['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Session-scoped temporary files for the isolated Agent."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
from uuid import uuid4

from tiku_agent.tools import DEFAULT_RUNTIME_DIR


class SessionArtifacts:
    """Keep uploaded images, crops, and copied answers inside one safe directory."""

    def __init__(self, root: str | Path = DEFAULT_RUNTIME_DIR / "sessions") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_key(session_id)

    def persist_image(self, session_id: str, source: str | Path) -> Path:
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"image file not found: {source_path}")
        target_dir = self.session_dir(session_id) / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix.lower() or ".bin"
        target = target_dir / f"{uuid4().hex}{suffix}"
        shutil.copy2(source_path, target)
        return target

    def persist_media(self, session_id: str, source: str | Path) -> Path:
        """Copy one user-visible candidate or answer into session storage."""
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"media file not found: {source_path}")
        target_dir = self.session_dir(session_id) / "media"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix.lower() or ".bin"
        target = target_dir / f"{uuid4().hex}{suffix}"
        shutil.copy2(source_path, target)
        return target

    def clear_session(self, session_id: str) -> None:
        target = self.session_dir(session_id)
        if target.parent != self.root:
            raise ValueError("refusing to clear a path outside the session artifact root")
        shutil.rmtree(target, ignore_errors=True)

    def clear_sessions(self, session_ids: list[str]) -> None:
        for session_id in session_ids:
            self.clear_session(session_id)


def session_key(session_id: str) -> str:
    clean = str(session_id).strip()
    if not clean:
        raise ValueError("session_id is required")
    return sha256(clean.encode("utf-8")).hexdigest()

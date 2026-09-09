"""Publish complete local artifacts atomically without replacing existing files."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
from uuid import uuid4

from tiku_shared.execution_hooks import execution_observer


@contextmanager
def atomic_output(target):
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    # Keep names shorter than the final artifact on Windows MAX_PATH hosts.
    temporary = target.with_name(f".part-{uuid4().hex[:16]}")
    observer = execution_observer()
    file_id = observer.prepare_file(target, temporary) if observer is not None else None
    try:
        temporary.touch(exist_ok=False)
        yield temporary
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if observer is not None:
            observer.file_ready(file_id, temporary)
        # link creates the final name exclusively. A pre-check plus replace
        # would overwrite another writer that publishes while we are writing.
        # Both names are in the same directory/filesystem; failure is closed.
        os.link(temporary, target)
        if observer is not None:
            observer.file_published(file_id)
    finally:
        # Only this function's freshly generated temporary path is removed.
        temporary.unlink(missing_ok=True)


def atomic_copy(source, target):
    with atomic_output(target) as temporary:
        shutil.copy2(source, temporary)
    return Path(target).resolve()

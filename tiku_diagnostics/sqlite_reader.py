"""SQLite primitives that cannot create, migrate, or mutate source databases."""

from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from time import sleep
from typing import Iterator


_SQLITE_HEADER = b"SQLite format 3\x00"
_WAL_FORMAT_VERSION = 2
_SNAPSHOT_ATTEMPTS = 5
_SNAPSHOT_RETRY_SECONDS = 0.02


class _SnapshotChanged(OSError):
    """The live database changed while one snapshot round was captured."""


def _wal_sources(path: Path) -> tuple[Path, Path]:
    return path, Path(f"{path}-wal")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_manifest(path: Path) -> tuple[tuple[bool, str], tuple[bool, str]]:
    result: list[tuple[bool, str]] = []
    for item in _wal_sources(path):
        exists = item.is_file()
        result.append((exists, _sha256_file(item) if exists else ""))
    return result[0], result[1]


def _copy_snapshot_round(source: Path, destination_root: Path) -> tuple[
    Path, tuple[tuple[bool, str], tuple[bool, str]]
]:
    destination_root.mkdir(parents=True)
    destination = destination_root / source.name
    source_database, source_wal = _wal_sources(source)
    destination_database, destination_wal = _wal_sources(destination)
    try:
        shutil.copyfile(source_database, destination_database)
        try:
            shutil.copyfile(source_wal, destination_wal)
        except FileNotFoundError:
            destination_wal.unlink(missing_ok=True)
    except OSError as exc:
        raise _SnapshotChanged from exc
    try:
        return destination, _snapshot_manifest(destination)
    except OSError as exc:
        raise _SnapshotChanged from exc


def _uses_wal(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise sqlite3.OperationalError("unable to inspect SQLite source") from exc
    return (
        header.startswith(_SQLITE_HEADER)
        and len(header) >= 20
        and header[18] == _WAL_FORMAT_VERSION
        and header[19] == _WAL_FORMAT_VERSION
    )


def _copy_stable_wal_snapshot(source: Path, destination_root: Path) -> Path:
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        first_root = destination_root / f"attempt-{attempt}-first"
        second_root = destination_root / f"attempt-{attempt}-second"
        try:
            _first, first_manifest = _copy_snapshot_round(source, first_root)
            second, second_manifest = _copy_snapshot_round(source, second_root)
            if first_manifest == second_manifest:
                _validate_readable_snapshot(second)
                return second
        except (OSError, sqlite3.Error):
            pass
        shutil.rmtree(first_root, ignore_errors=True)
        shutil.rmtree(second_root, ignore_errors=True)
        if attempt + 1 < _SNAPSHOT_ATTEMPTS:
            sleep(_SNAPSHOT_RETRY_SECONDS)

    raise sqlite3.OperationalError(
        "unable to capture a stable readable SQLite snapshot"
    )


@contextmanager
def _open_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        connection.row_factory = sqlite3.Row
        with closing(connection.execute("PRAGMA query_only=ON")):
            pass
        yield connection


def _validate_readable_snapshot(path: Path) -> None:
    try:
        with _open_readonly(path) as connection:
            with closing(connection.execute("PRAGMA quick_check(1)")) as cursor:
                rows = cursor.fetchall()
    except sqlite3.Error as exc:
        raise sqlite3.OperationalError("SQLite snapshot failed quick_check") from exc
    if len(rows) != 1 or str(rows[0][0]).strip().lower() != "ok":
        raise sqlite3.OperationalError("SQLite snapshot failed quick_check")


@contextmanager
def readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = path.resolve()
    if not _uses_wal(resolved):
        with _open_readonly(resolved) as connection:
            yield connection
        return

    # SQLite may create or update a WAL index beside a mode=ro database. Query a
    # verified temporary copy so diagnostics cannot change the live runtime root.
    with TemporaryDirectory(prefix="tiku-diagnostics-") as temporary:
        snapshot = _copy_stable_wal_snapshot(resolved, Path(temporary))
        with _open_readonly(snapshot) as connection:
            yield connection


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    with closing(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        )
    ) as cursor:
        exists = cursor.fetchone()
    if exists is None:
        return set()
    with closing(connection.execute(f"PRAGMA table_info({table})")) as cursor:
        return {str(row[1]) for row in cursor.fetchall()}

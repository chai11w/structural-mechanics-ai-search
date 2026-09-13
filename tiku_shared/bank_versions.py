"""Pin an immutable published bank for one read, independently of the management service."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re

from tiku_shared.bank_readers import bank_access, reader_gate

_CURRENT = ContextVar("published_bank", default=None)
_VERSION = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class BankVersion:
    main: Path
    symbolic: Path
    version: str | None


def store_root():
    value = os.environ.get("TIKU_BANK_STORE", "")
    return Path(value).resolve() if value else None


def runtime_file(name, fallback):
    """CLI/Feishu caches belong to their own runtime, never an immutable bank."""
    root = store_root()
    if root is None:
        return Path(fallback)
    value = os.environ.get("TIKU_SEARCH_STATE_DIR", "")
    if not value:
        raise ValueError("TIKU_SEARCH_STATE_DIR is required for search cache output")
    directory = Path(value).resolve()
    if root == directory or root in directory.parents or directory in root.parents:
        raise ValueError("search state must be outside the published bank")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def read_version(root, version):
    with reader_gate(root):
        return _read_version(root, version)


def _read_version(root, version):
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("invalid bank version")
    directory = root / "versions" / version
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise ValueError("linked bank version is not allowed")
    manifest_file = directory / "manifest.json"
    if manifest_file.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("bank manifest is too large")
    raw = manifest_file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != version:
        raise ValueError("bank manifest does not match version")
    manifest = json.loads(raw)
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("invalid bank manifest")
    for name in ("main", "symbolic"):
        path = directory / name
        if not path.is_dir() or path.resolve().parent != directory.resolve():
            raise ValueError("bank root is missing or outside the version")
    return BankVersion(directory / "main", directory / "symbolic", version)


@bank_access
def active_version():
    root = store_root()
    if root is None:
        return None
    pointer = json.loads((root / "active.json").read_bytes())
    if pointer.get("schema") != 1 or type(pointer.get("revision")) is not int or pointer["revision"] < 1:
        raise ValueError("invalid active bank pointer")
    return read_version(root, pointer.get("version"))


@contextmanager
def pin_bank(version=None):
    inherited = version or _CURRENT.get()
    with reader_gate(_pinned_store(inherited) if inherited is not None else None):
        pinned = inherited or active_version()
        token = _CURRENT.set(pinned)
        try:
            yield pinned
        finally:
            _CURRENT.reset(token)


def _pinned_store(version):
    # A trusted validation worker explicitly pins its private copy with no
    # publication version. Its environment must not route it back to production.
    if version.version is None:
        return ""
    directory = version.main.parent
    if (not isinstance(version.version, str) or not _VERSION.fullmatch(version.version)
            or directory.name != version.version or directory.parent.name != "versions"
            or version.main.name != "main" or version.symbolic != directory / "symbolic"):
        raise ValueError("invalid pinned bank version")
    return directory.parent.parent


def bank_read(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with pin_bank():
            return function(*args, **kwargs)
    return wrapped


def main_root(fallback):
    current = _CURRENT.get() or active_version()
    return current.main if current else Path(fallback)


def symbolic_root(main):
    current = _CURRENT.get() or active_version()
    return current.symbolic if current and Path(main).resolve() == current.main.resolve() else Path(main).parent / f"{Path(main).name}_字母库"


@contextmanager
def pin_question_path(path, legacy_root):
    """Old candidates retain their original version, even after deletion in a newer publication."""
    current, root, candidate = _CURRENT.get(), store_root(), Path(path)
    gate = (root if root and candidate.is_absolute() and candidate.is_relative_to(root / "versions")
            else _pinned_store(current) if current is not None else None)
    with reader_gate(gate):
        with _pin_question_path(path, legacy_root):
            yield


@contextmanager
def _pin_question_path(path, legacy_root):
    candidate = Path(path)
    root = store_root()
    selected = None
    if root and candidate.is_absolute():
        candidate = candidate.resolve()
        try:
            parts = candidate.relative_to(root / "versions").parts
        except ValueError:
            # Pre-migration candidates remain readable only inside the frozen legacy root.
            legacy = Path(legacy_root).resolve()
            if legacy not in candidate.parents:
                raise ValueError("candidate is outside its bank")
            selected = BankVersion(legacy, legacy.parent / f"{legacy.name}_字母库", None)
        else:
            if len(parts) < 4 or parts[1] != "main":
                raise ValueError("candidate path does not identify published media")
            selected = read_version(root, parts[0])
    with pin_bank(selected):
        yield


def require_legacy_writer():
    if store_root() is not None:
        raise PermissionError("Published banks are read-only here; use the owner-approved bank executor")

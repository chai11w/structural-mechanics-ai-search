"""Read-only process locks coordinating published-bank readers with retention.

This gate is independent of the writer service. Publication does not acquire it
exclusively: only a short retention commit may do so, after preparation elsewhere.
"""
from contextlib import contextmanager
from functools import wraps
import errno
import os
from pathlib import Path
import stat
import threading
import time
from uuid import uuid4


GATE_NAME = "reader-gate.lock"
GATE_BYTES = b"Lida bank readers v1\n"
_held = threading.local()


class BankReadersBusy(TimeoutError):
    pass


def _path(root):
    root = Path(root).absolute()
    path = root / GATE_NAME
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("linked bank reader gate")
    return path


def _validate(stream, path):
    opened, named = os.fstat(stream.fileno()), path.stat()
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or named.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_size != len(GATE_BYTES) or stream.read(len(GATE_BYTES) + 1) != GATE_BYTES):
        raise ValueError("invalid bank reader gate")


def initialize_reader_gate(root):
    """Writer-only installation, called while holding the existing writer lock.

    Readers never create or replace this file. Keep it outside version directories
    and preserve its identity for the life of the store, including retention.
    """
    path = _path(root)
    if not path.exists():
        temporary = path.with_name(".reader-gate-" + uuid4().hex)
        with temporary.open("xb") as stream:
            stream.write(GATE_BYTES); stream.flush(); os.fsync(stream.fileno())
        if path.exists():
            raise ValueError("bank reader gate installation conflict")
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    with reader_gate(root):
        pass


def _native_lock(stream, exclusive):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.UnlockFileEx.restype = wintypes.BOOL
        handle, overlapped = msvcrt.get_osfhandle(stream.fileno()), Overlapped()
        def acquire():
            if kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
                return True
            error = ctypes.get_last_error()
            if error == 33:  # ERROR_LOCK_VIOLATION; no wait queue that would block new readers.
                return False
            raise ctypes.WinError(error)
        def release():
            if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                raise ctypes.WinError(ctypes.get_last_error())
        return acquire, release
    import fcntl
    def acquire():
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return True
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
    return acquire, lambda: fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def _gate(root, *, exclusive, timeout):
    path = _path(root)
    key = os.path.normcase(str(path))
    # ContextVars propagate to executor workers which can outlive the caller.
    # A new thread must own its own handle; only a nested call in this thread
    # may reuse a held lock. A forked child must not reuse the parent's record.
    if getattr(_held, "pid", None) != os.getpid():
        _held.pid, _held.paths = os.getpid(), {}
    if key in _held.paths:
        if exclusive:
            raise BankReadersBusy("bank readers active")
        yield path.parent
        return
    with path.open("rb") as stream:
        acquire, release = _native_lock(stream, exclusive)
        deadline = time.monotonic() + timeout
        while not acquire():
            if time.monotonic() >= deadline:
                raise BankReadersBusy("bank readers active" if exclusive else "bank retention busy")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        try:
            _validate(stream, _path(root))
            _held.paths[key] = exclusive
            try:
                yield path.parent
            finally:
                del _held.paths[key]
        finally:
            release()


@contextmanager
def reader_gate(root=None):
    root = root if root is not None else os.environ.get("TIKU_BANK_STORE", "")
    if not root:
        yield None
        return
    with _gate(root, exclusive=False, timeout=30) as directory:
        yield directory


@contextmanager
def maintenance_gate(root):
    """Internal retention commit gate; busy readers make maintenance skip.

    Holding this alone never authorizes deletion. The caller must also hold the
    writer lock, stop management writers and revalidate all durable references.
    Do not hash, back up, recursively delete or wait for services inside this gate.
    """
    with _gate(root, exclusive=True, timeout=0) as directory:
        yield directory


def bank_access(function):
    """Protect one synchronous operation through its state/media persistence."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with reader_gate():
            return function(*args, **kwargs)
    return wrapped

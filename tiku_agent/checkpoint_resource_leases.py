"""Finite in-process leases for immutable capture files during session cleanup."""

from pathlib import Path
import stat
import shutil
from threading import Lock
from time import monotonic


class CheckpointResourceLeases:
    def __init__(self, *, clock=monotonic):
        self.clock = clock
        self._lock = Lock()
        self._leases = {}
        self._deferred = set()
        self._clearing = set()

    def acquire(self, token, paths, deadline):
        paths = tuple(Path(path) for path in paths if path)
        with self._lock:
            if any(path.is_relative_to(root) for path in paths for root in self._clearing):
                return False
            self._leases[token] = (paths, deadline)
            return True

    def _active_paths(self):
        now = self.clock()
        return {path for paths, deadline in self._leases.values() if deadline > now for path in paths}

    def release(self, token):
        from tiku_agent.checkpoint_store import _reject_linked_path, CheckpointStoreError
        with self._lock:
            self._leases.pop(token, None)
            ready = self._deferred - self._active_paths()
            self._deferred.difference_update(ready)
        for path in ready:
            try:
                _reject_linked_path(path)
                path.unlink(missing_ok=True)
            except (OSError, CheckpointStoreError):
                pass

    def clear_session(self, target):
        # Refuse new leases during cleanup without holding the lock over file I/O.
        with self._lock:
            if target in self._clearing:
                return True
            self._clearing.add(target)
            protected = {path for path in self._active_paths() if path.is_relative_to(target)}
            self._deferred.update(protected)
        def clear(directory):
            try:
                details = directory.lstat()
                if (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                    if stat.S_ISLNK(details.st_mode):
                        directory.unlink()
                    else:
                        directory.rmdir()
                    return
                entries = list(directory.iterdir())
            except OSError:
                return
            for path in entries:
                try:
                    details = path.lstat()
                    linked = (stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0)
                              & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                    if stat.S_ISDIR(details.st_mode) and not linked:
                        clear(path)
                    elif path not in protected:
                        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                            path.rmdir()
                        else:
                            path.unlink()
                    else:
                        continue
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            if protected:
                clear(target)
            else:
                shutil.rmtree(target, ignore_errors=True)
        finally:
            with self._lock:
                self._clearing.discard(target)
        return True

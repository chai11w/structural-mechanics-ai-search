"""Explicit per-service configuration outside the immutable source installation."""
import json
import math
import os
from pathlib import Path

MAX_CONFIGURATION_BYTES = 1024 * 1024


def external_configuration(source):
    """Return None for legacy loading; a selected file never falls back locally.

    The path is trusted deployment environment, never a model argument. Actual
    account/file authorization remains the installer's responsibility.
    """
    value = os.environ.get("TIKU_CONFIG_FILE", "")
    if not value:
        return None
    try:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("configuration path must be absolute")
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("linked service configuration")
        path = path.resolve(strict=True)
        source = Path(source).resolve()
        if path == source or source in path.parents:
            raise ValueError("service configuration must be outside source")
        before = path.stat()
        if not path.is_file() or before.st_nlink != 1 or before.st_size > MAX_CONFIGURATION_BYTES:
            raise ValueError("unsupported service configuration file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
        after = path.stat()
        if len(raw) > MAX_CONFIGURATION_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("service configuration changed while reading")

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate configuration key")
                result[key] = item
            return result

        def invalid_constant(_):
            raise ValueError("nonfinite configuration value")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("nonfinite configuration value")
            return number

        result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                            parse_constant=invalid_constant, parse_float=finite_float)
        if not isinstance(result, dict):
            raise ValueError("service configuration must be an object")
        return result
    except (OSError, ValueError, RecursionError):
        # Configurations can contain credentials; do not include paths or input
        # fragments in startup failures.
        raise ValueError("external service configuration is unavailable or invalid") from None

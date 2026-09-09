"""Bounded configuration fingerprints for reusable validation results.

Only hashes leave this module. Credentials and arbitrary object attributes are
never inspected. Custom clients must explicitly version their behavior.
"""
import hashlib
import inspect
import json
import math
from pathlib import Path

from tiku_agent.execution_store import ExecutionError, digest


def _json_value(value, *, depth=0, budget=None):
    budget = [2048] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or (type(value) is str and len(value) > 16384):
        raise ValueError("version exceeds limit")
    if depth > 8:
        raise ValueError("version nesting exceeds limit")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list and len(value) <= 256:
        return [_json_value(item, depth=depth + 1, budget=budget) for item in value]
    if type(value) is dict and len(value) <= 256 and all(type(key) is str for key in value):
        return {key: _json_value(item, depth=depth + 1, budget=budget) for key, item in value.items()}
    raise ValueError("version must be bounded JSON")


def component_version(client):
    """Version standard crop checks or an explicitly declared custom client.

    A custom ``execution_version`` value (or zero-argument callable) must cover
    its code, prompts, options and any nested dependencies. It is an adapter
    contract, not an assertion that arbitrary Python state can be discovered.
    Runtime counters must not be included. Re-read on each reuse decision.
    """
    if client is None:
        return None
    from tiku_agent.a3_models import QwenA3CropVerifier
    from tiku_agent.external_load_screen import QwenExternalLoadScreen, ZhipuExternalLoadScreen

    try:
        standard = type(client) in (QwenA3CropVerifier, QwenExternalLoadScreen, ZhipuExternalLoadScreen)
        declaration = getattr(client, "execution_version", None)
        if not standard and declaration is None:
            raise ValueError("custom client requires execution_version")
        if callable(declaration):
            declaration = declaration()
        if not standard and declaration in (None, "", {}, []):
            raise ValueError("custom client version must not be empty")
        identity = client if inspect.isfunction(client) or inspect.isclass(client) else type(client)
        payload = {"schema": 1, "type": identity.__module__ + "." + identity.__qualname__,
                   "declared": _json_value(declaration)}
        # Known request-affecting fields supplement a custom declaration too.
        for name in ("model", "endpoint", "timeout_seconds"):
            if hasattr(client, name):
                payload[name] = _json_value(getattr(client, name))
        prompt = getattr(client, "prompt_path", None)
        if prompt is not None:
            with Path(prompt).open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("prompt exceeds versioning limit")
            payload["prompt_sha256"] = hashlib.sha256(content).hexdigest()
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("version exceeds limit")
        return digest(payload)
    except Exception:
        # Do not expose configuration values, file paths or callback exceptions.
        raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE") from None

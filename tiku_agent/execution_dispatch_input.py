"""Private, reconstructible inputs for the bounded background kernel.

Only the five existing business methods are accepted. No client file paths,
callbacks, credentials or pickled objects enter the durable command.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import math

from PIL import Image

from tiku_agent.execution_store import ExecutionError, canonical


METHOD_FIELDS = {
    "handle_image": {"image_path"},
    "handle_text": {"text", "action_context"},
    "select_unit": {"unit_id", "task_revision", "workflow_search_id"},
    "prepare_units": {"unit_ids", "task_revision", "workflow_search_id"},
    "handle_crop": {"bounds", "unit_id", "task_revision", "workflow_search_id"},
}


def _json_value(value, depth=0):
    if depth > 8:
        raise ExecutionError("EXECUTION_INPUT_INVALID")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list and len(value) <= 100:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and len(value) <= 32 and all(type(k) is str for k in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ExecutionError("EXECUTION_INPUT_INVALID")


def freeze_input(runtime, kind, parameters, image, policy):
    if (kind not in METHOD_FIELDS or not callable(getattr(runtime, kind, None))
            or type(parameters) is not dict or set(parameters) - (METHOD_FIELDS[kind] - {"image_path"})):
        raise ExecutionError("EXECUTION_INPUT_INVALID")
    _json_value(parameters)
    try:
        encoded = canonical(parameters)
    except (ValueError, TypeError, OverflowError):
        raise ExecutionError("EXECUTION_INPUT_INVALID") from None
    if len(encoded.encode()) > policy.max_json_bytes:
        raise ExecutionError("EXECUTION_INPUT_TOO_LARGE")
    parameters = json.loads(encoded)
    extension = ""
    image_hash = ""
    if kind == "handle_image":
        if type(image) is not bytes or not image or len(image) > policy.max_image_bytes:
            raise ExecutionError("EXECUTION_INPUT_TOO_LARGE")
        try:
            with Image.open(io.BytesIO(image)) as opened:
                if opened.format not in {"PNG", "JPEG", "WEBP"} or opened.width * opened.height > policy.max_image_pixels:
                    raise ValueError("unsupported image")
                extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[opened.format]
                opened.verify()
            # JPEG verify() checks headers but does not decode the image body.
            # Prove readability before durable ACK, within the pixel bound.
            with Image.open(io.BytesIO(image)) as opened:
                opened.load()
        except Exception:
            raise ExecutionError("EXECUTION_INPUT_INVALID") from None
        image_hash = hashlib.sha256(image).hexdigest()
        parameters["image_path"] = {"content_sha256": image_hash}
    elif image is not None:
        raise ExecutionError("EXECUTION_INPUT_INVALID")
    if kind == "handle_text" and (type(parameters.get("text")) is not str or not parameters["text"].strip()):
        raise ExecutionError("EXECUTION_INPUT_INVALID")
    if "action_context" in parameters and parameters["action_context"] is not None and type(parameters["action_context"]) is not dict:
        raise ExecutionError("EXECUTION_INPUT_INVALID")
    if kind in {"select_unit", "prepare_units", "handle_crop"}:
        if (type(parameters.get("task_revision")) is not int or parameters["task_revision"] < 0
                or type(parameters.get("workflow_search_id")) is not str or not parameters["workflow_search_id"]
                or len(parameters["workflow_search_id"]) > 128):
            raise ExecutionError("EXECUTION_INPUT_INVALID")
        units = parameters.get("unit_ids") if kind == "prepare_units" else [parameters.get("unit_id")]
        if (type(units) is not list or not 1 <= len(units) <= 100 or len(set(map(str, units))) != len(units)
                or any(type(unit) is not str or not unit.strip() or len(unit) > 128 for unit in units)):
            raise ExecutionError("EXECUTION_INPUT_INVALID")
    if kind == "handle_crop":
        bounds = parameters.get("bounds")
        if (type(bounds) is not dict or set(bounds) != {"x", "y", "width", "height"}
                or any(type(v) not in {int, float} for v in bounds.values())
                or any(not 0 <= v <= 1 for v in bounds.values())
                or bounds["width"] < 0.02 or bounds["height"] < 0.02
                or bounds["x"] + bounds["width"] > 1.000001
                or bounds["y"] + bounds["height"] > 1.000001):
            raise ExecutionError("EXECUTION_INPUT_INVALID")
    try:
        bound = inspect.signature(getattr(runtime, kind)).bind("session", **parameters)
        bound.apply_defaults()
    except TypeError:
        raise ExecutionError("EXECUTION_INPUT_INVALID") from None
    # Use precisely the existing operation fingerprint, including method defaults.
    inputs = {key: value for key, value in bound.arguments.items() if key in METHOD_FIELDS[kind]}
    payload = canonical({"schema": 1, "parameters": inputs, "extension": extension})
    if len(payload.encode()) > policy.max_json_bytes:
        raise ExecutionError("EXECUTION_INPUT_TOO_LARGE")
    return payload, inputs, image_hash

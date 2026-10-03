"""Parse bounded browser reports of a previous session-binding failure.

These attributes are untrusted client observations, never execution authority.
Only authenticated POST /api/jobs/session may record them in the service log.
The shared Trace format stays unchanged for older readers and service rollback.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any


BIND_DIAGNOSTIC_HEADER = "X-Tiku-Bind-Diagnostic"
MAX_BIND_DIAGNOSTIC_BYTES = 512
_FIELDS = frozenset({"schema", "request_id", "phase", "code", "elapsed_ms", "status"})
_PHASES = frozenset({"fetch", "body", "protocol"})
_CODES = frozenset({"REQUEST_TIMEOUT", "NETWORK_UNAVAILABLE", "RESPONSE_INVALID", "HTTP_ERROR"})
_REQUEST_ID = re.compile(r"req_[0-9a-f]{32}")
_LOGGER = logging.getLogger(__name__)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate diagnostic field")
        result[key] = value
    return result


def parse_bind_diagnostic(value: Any) -> dict[str, str | int | bool]:
    """Return only validated self-reported diagnostic fields, or an empty dict."""
    if (type(value) is not str or not value or len(value) > MAX_BIND_DIAGNOSTIC_BYTES
            or any(ord(character) < 32 or ord(character) > 126 for character in value)):
        return {}
    try:
        data = json.loads(value, object_pairs_hook=_unique_object)
        if type(data) is not dict or set(data) != _FIELDS:
            return {}
        if type(data["schema"]) is not int or data["schema"] != 1:
            return {}
        if type(data["request_id"]) is not str or not _REQUEST_ID.fullmatch(data["request_id"]):
            return {}
        if type(data["phase"]) is not str or data["phase"] not in _PHASES:
            return {}
        if type(data["code"]) is not str or data["code"] not in _CODES:
            return {}
        if type(data["elapsed_ms"]) is not int or not 0 <= data["elapsed_ms"] <= 120_000:
            return {}
        if type(data["status"]) is not int or not 0 <= data["status"] <= 599:
            return {}
        return {"client_bind_reported": True,
                **{f"client_bind_{name}": data[name] for name in _FIELDS}}
    except (ValueError, TypeError, RecursionError):
        return {}


def record_bind_diagnostic(raw_header: Any, current_request_id: Any) -> None:
    """Log one bounded report; every diagnostic failure leaves binding unaffected."""
    try:
        if type(current_request_id) is not str or not _REQUEST_ID.fullmatch(current_request_id):
            return
        attributes = parse_bind_diagnostic(raw_header)
        if not attributes:
            return
        payload = json.dumps({"request_id": current_request_id, **attributes},
                             ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        _LOGGER.warning("BIND_TRANSPORT_DIAGNOSTIC %s", payload)
    except Exception:
        # Never emit the header or exception text, including on logger failure.
        return

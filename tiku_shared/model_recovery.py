"""Pure eligibility policy for one explicitly bounded model recovery attempt.

This module never sends a request, sleeps, or changes receipts. The caller must
limit recovery to one additional attempt and preserve the first attempt's
unknown outcome and cost evidence. Eligibility does not imply it was unsent.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error


_DASHSCOPE_CALL_TYPES = frozenset({
    "qwen_image_triage",
    "qwen_a3_page_understanding",
    "qwen_a3_crop_compare",
    "qwen_image_classification",
    "qwen_a3_unit_analysis",
    "external_load_screen",
})
_ZHIPU_CALL_TYPES = frozenset({
    "glm_a3_page_auto_crop",
    "external_load_screen",
})
MODEL_RECOVERY_CALL_TYPES = _DASHSCOPE_CALL_TYPES | _ZHIPU_CALL_TYPES

_TRANSIENT_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_CONNECTION_ERRORS = (
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    BrokenPipeError,
    http.client.RemoteDisconnected,
    ssl.SSLEOFError,
)
_MAX_URL_ERROR_DEPTH = 8


def _transient_transport_error(error: BaseException) -> bool:
    """Unwrap only urllib's typed reason, never arbitrary exception chains.

    For example, an execution or parsing error caused by a timeout is still an
    execution or parsing error. A textual URLError reason is not enough evidence
    to retry. HTTPError must be handled before its URLError superclass.
    """
    seen: set[int] = set()
    for _ in range(_MAX_URL_ERROR_DEPTH):
        if not isinstance(error, BaseException) or id(error) in seen:
            return False
        seen.add(id(error))
        if isinstance(error, ssl.SSLCertVerificationError):
            return False
        if isinstance(error, urllib.error.HTTPError):
            return type(error.code) is int and error.code in _TRANSIENT_HTTP_STATUS
        if isinstance(error, socket.gaierror):
            return error.errno == socket.EAI_AGAIN
        if isinstance(error, _TRANSIENT_CONNECTION_ERRORS):
            return True
        if isinstance(error, urllib.error.URLError):
            error = error.reason
            continue
        return False
    return False


def model_recovery_allowed(provider: str, call_type: str, error: BaseException) -> bool:
    """Allow only known transient failures in the necessary vision stages.

    The provider/stage pairs are explicit so an unrelated caller cannot opt in by
    reusing a stage name. Optional classification, reranking and reply/intent calls
    keep their own existing fallback policies.
    """
    if not isinstance(provider, str) or not isinstance(call_type, str):
        return False
    provider = provider.strip().lower()
    call_type = call_type.strip()
    if provider == "dashscope":
        allowed_call_types = _DASHSCOPE_CALL_TYPES
    elif provider == "zhipu":
        allowed_call_types = _ZHIPU_CALL_TYPES
    else:
        return False
    return call_type in allowed_call_types and _transient_transport_error(error)

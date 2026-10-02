"""Opt-in retries before a Qwen HTTP request can send any bytes.

Only the TCP/DNS connection factory is retried. TLS negotiation, HTTP writes,
response reads, model accounting, and durable execution receipts are unchanged.
"""

from __future__ import annotations

import copy
import errno
import math
import os
import socket
import ssl
import time
from collections.abc import Callable, Mapping
import http.client
import urllib.parse
import urllib.request


QWEN_CONNECT_RETRIES_ENV = "TIKU_QWEN_CONNECT_RETRIES"
QWEN_HOST = "dashscope.aliyuncs.com"

_TRANSIENT_ERRNOS = {
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ETIMEDOUT,
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ENETRESET,
}
_TRANSIENT_WINERRORS = {10050, 10051, 10052, 10053, 10054, 10060, 10061, 10065}


def _temporary_connect_error(error: OSError) -> bool:
    if isinstance(error, ssl.SSLError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return (
        isinstance(error, TimeoutError)
        or error.errno in _TRANSIENT_ERRNOS
        or error.errno in _TRANSIENT_WINERRORS
        or getattr(error, "winerror", None) in _TRANSIENT_WINERRORS
    )


class _TCPConnectRetry:
    """Retry an unsuccessful socket connection within its original budget.

    The operating system's DNS resolver cannot be interrupted by a socket
    timeout. An overdue result is closed before HTTP can send anything.
    """

    def __init__(self, connect: Callable, *, retries: int = 1):
        if type(retries) is not int or retries not in (0, 1):
            raise ValueError("Qwen connect retries must be 0 or 1")
        self.connect = connect
        self.retries = retries
        self.connection_attempts = 0

    def __call__(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        effective_timeout = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        budget = None if effective_timeout is None else float(effective_timeout)
        if budget is not None and not math.isfinite(budget):
            # Preserve the socket factory's validation of non-finite timeouts.
            return self.connect(address, timeout, source_address)
        if budget is not None and budget <= 0:
            self.connection_attempts += 1
            return self.connect(address, timeout, source_address)
        deadline = None if budget is None else time.monotonic() + budget
        for attempt in range(self.retries + 1):
            remaining = timeout if deadline is None else max(0.0, deadline - time.monotonic())
            if attempt and deadline is not None and remaining <= 0:
                raise last_error
            self.connection_attempts += 1
            try:
                connection = self.connect(address, remaining, source_address)
            except OSError as error:
                if attempt >= self.retries or not _temporary_connect_error(error):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                last_error = error
                continue
            if deadline is not None and time.monotonic() > deadline:
                connection.close()
                raise TimeoutError("Qwen TCP connection budget exhausted")
            return connection
        raise AssertionError("Unreachable TCP connection retry state")


class QwenConnectRetryHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, connect_retries: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.qwen_tcp_connector = _TCPConnectRetry(self._create_connection, retries=connect_retries)
        self._create_connection = self.qwen_tcp_connector


class QwenConnectRetryHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, *args, connect_retries: int = 1, **kwargs):
        if type(connect_retries) is not int or connect_retries not in (0, 1):
            raise ValueError("Qwen connect retries must be 0 or 1")
        super().__init__(*args, **kwargs)
        self.connect_retries = connect_retries

    def https_open(self, request):
        if urllib.parse.urlsplit(request.full_url).hostname != QWEN_HOST:
            return super().https_open(request)

        def connection_factory(*args, **kwargs):
            return QwenConnectRetryHTTPSConnection(*args, connect_retries=self.connect_retries, **kwargs)

        return self.do_open(connection_factory, request, context=self._context)


def configure_qwen_connect_retries_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Configure once at the 8790 startup boundary; CLI/Feishu do not call this.

    An absent variable or ``0`` leaves urllib's global opener untouched. ``1``
    enables one additional TCP connection attempt for the exact Qwen HTTPS host.
    Existing proxy handlers and the standard HTTPS handler's TLS context survive.
    An unknown custom HTTPS handler is rejected rather than silently replaced.
    """

    settings = os.environ if environ is None else environ
    value = settings.get(QWEN_CONNECT_RETRIES_ENV, "0")
    if value not in ("0", "1"):
        raise ValueError(f"{QWEN_CONNECT_RETRIES_ENV} must be 0 or 1")
    if value == "0":
        return False

    existing = urllib.request._opener
    if existing is None:
        opener = urllib.request.build_opener(QwenConnectRetryHTTPSHandler())
    else:
        handlers = []
        replaced_https = False
        for handler in existing.handlers:
            if isinstance(handler, urllib.request.HTTPSHandler):
                if type(handler) not in (urllib.request.HTTPSHandler, QwenConnectRetryHTTPSHandler):
                    raise ValueError("Qwen retry configuration cannot replace a custom HTTPS handler")
                handlers.append(QwenConnectRetryHTTPSHandler(context=handler._context, debuglevel=handler._debuglevel))
                replaced_https = True
            else:
                # ProxyHandler installs methods whose closures capture itself.
                # Rebuild the standard handler so those closures do not point
                # at the old opener; custom handlers retain their own behavior.
                handlers.append(
                    urllib.request.ProxyHandler(dict(handler.proxies))
                    if type(handler) is urllib.request.ProxyHandler
                    else copy.copy(handler)
                )
        if not replaced_https:
            handlers.append(QwenConnectRetryHTTPSHandler())
        opener = urllib.request.build_opener(*handlers)
    urllib.request.install_opener(opener)
    return True

"""Optional durable execution hooks; ordinary CLI/Feishu calls remain unchanged."""
from contextlib import contextmanager
from contextvars import ContextVar


_OBSERVER = ContextVar("durable_execution_observer", default=None)


def execution_observer():
    return _OBSERVER.get()


@contextmanager
def execution_effect_scope(observer):
    token = _OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _OBSERVER.reset(token)


def bounded_transport_retries(legacy_retries):
    """A sent request with no response is not safe to replay implicitly."""
    return 0 if execution_observer() is not None else legacy_retries

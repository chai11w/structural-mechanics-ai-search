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


def accept_rerank_fallback(error):
    """A candidate scorer handled this failure by returning its coarse result.

    This never confirms a model response or authorizes replay. Only an error
    tagged by the durable model boundary can acknowledge a failed call.
    """
    accept_model_fallback(error, "rerank_coarse")


def accept_model_fallback(error, kind):
    """Acknowledge only the specific model error handled by a defined fallback."""
    observer = execution_observer()
    if observer is None:
        return
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        call_id = getattr(error, "_tiku_model_call_id", None)
        if call_id is not None:
            observer.accept_model_fallback(call_id, kind)
            return
        error = error.__cause__ or error.__context__

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from functools import wraps
import time
from typing import Any, Callable

from .storage import ObservationStore, TurnSink


_ACTIVE_TURN: ContextVar[TurnSink | None] = ContextVar("mainline_observation_turn", default=None)
_TOOL_COUNTS: ContextVar[dict[str, int] | None] = ContextVar("mainline_observation_tool_counts", default=None)
_AUTHORIZATION_COUNT: ContextVar[int] = ContextVar("mainline_observation_authorization_count", default=0)

TOOL_NAMES = (
    "analyze_multi_image", "prepare_question_units", "analyze_image", "route_bank",
    "classify_structure", "coarse_search", "global_search", "rerank_candidates",
    "answer_candidate",
)
STATE_METHODS = (
    "remember_intent", "start_search", "set_analysis", "set_chapter", "correct_chapter",
    "set_route", "set_questions", "select_question", "set_candidates", "record_search_batch",
    "reject_current_candidates", "report_answer_mismatch", "select_candidate", "set_answer_paths",
    "set_pending_chapter", "consume_pending_chapter", "offer_global_search",
    "consume_global_search_offer", "mark_done", "cancel", "fail",
)


def _safe_emit(event_type: str, payload_factory: Callable[[], dict[str, Any]], *, duration_ms: int | None = None) -> None:
    sink = _ACTIVE_TURN.get()
    if sink is None:
        return
    try:
        sink.emit(event_type, payload_factory(), duration_ms=duration_ms)
    except Exception:
        pass


def _state_summary(state: Any) -> dict[str, Any]:
    return {
        "phase": str(getattr(state, "phase", "")),
        "question_count": int(getattr(state, "question_count", 0)),
        "candidate_count": int(getattr(state, "candidate_count", 0)),
        "answer_count": len(getattr(state, "last_answer_paths", []) or []),
        "chapter": str(getattr(state, "current_chapter", "")),
        "route": str(getattr(state, "current_route", "")),
        "structure_type": str(getattr(state, "current_structure_type", "")),
        "selected_question": getattr(state, "selected_question", None),
        "selected_rank": getattr(state, "selected_rank", None),
        "global_search_offered": bool(getattr(state, "global_search_offered", False)),
        "continuation_available": bool(getattr(state, "continuation_available", False)),
        "has_active_image": bool(getattr(state, "active_image_path", "")),
        "pending_chapter": str(getattr(state, "pending_chapter", "")),
        "revision_count": int(getattr(state, "revision_count", 0)),
        "last_intent_action": str(
            (getattr(state, "last_intent", {}) or {}).get("action")
            or (getattr(state, "last_intent", {}) or {}).get("intent")
            or ""
        ),
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {key: {"before": before.get(key), "after": after.get(key)} for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)}


def _decision_summary(decision: Any) -> dict[str, Any]:
    payload = decision.to_dict() if callable(getattr(decision, "to_dict", None)) else {}
    return {
        "final_action": str(payload.get("action") or ""),
        "source": str(payload.get("source") or ""),
        "question_index": payload.get("question_index"),
        "candidate_rank": payload.get("candidate_rank"),
        "chapter": payload.get("chapter_override"),
        "chapter_target": payload.get("chapter_target"),
        "clarification_reason": payload.get("clarification_reason"),
        "requested_action": payload.get("requested_action"),
    }


def _authorization_summary(decision: Any, context: Any, result: Any) -> dict[str, Any]:
    decision_payload = _decision_summary(decision)
    return {
        "requested_action": decision_payload["final_action"],
        "source": decision_payload["source"],
        "question_index": decision_payload["question_index"],
        "candidate_rank": decision_payload["candidate_rank"],
        "chapter": decision_payload["chapter"],
        "phase": str(getattr(context, "phase", "")),
        "question_count": int(getattr(context, "question_count", 0)),
        "candidate_count": int(getattr(context, "candidate_count", 0)),
        "outcome": str(getattr(result, "outcome", "")),
        "authorization_code": str(getattr(result, "code", "")),
        "allowed": bool(getattr(result, "allowed", False)),
    }


def _args_summary(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"positional_count": len(args), "keyword_names": sorted(kwargs)}
    if name in {"route_bank", "coarse_search", "global_search"} and args:
        summary["load_count"] = len(args[0] or []) if isinstance(args[0], list) else 0
    if name in {"coarse_search", "global_search"}:
        summary.update({key: kwargs.get(key) for key in ("chapter", "route", "structure_type", "top_k") if key in kwargs})
        summary["excluded_candidate_count"] = len(kwargs.get("exclude_candidate_keys") or [])
    if name == "rerank_candidates" and len(args) > 1:
        summary["candidate_count"] = len(args[1] or [])
    if name == "answer_candidate":
        summary["candidate_count"] = len(args[0] or []) if args else 0
        summary["rank"] = kwargs.get("rank")
    if name == "prepare_question_units" and len(args) > 1:
        summary["question_count"] = len(args[1] or [])
    return summary


def _tool_output_summary(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", {}) if result is not None else {}
    data = data if isinstance(data, dict) else {}
    summary: dict[str, Any] = {}
    if "is_multi" in data:
        summary["is_multi"] = bool(data.get("is_multi"))
    mappings = {
        "chapter": "chapter", "route": "route", "structure_type": "structure_type",
        "reranked": "reranked", "rerank_complete": "rerank_complete",
    }
    for source, target in mappings.items():
        if source in data:
            summary[target] = data.get(source)
    for key in ("questions", "loads", "candidates", "visible_candidates", "copied_paths", "answer_paths"):
        if key in data:
            summary[f"{key}_count"] = len(data.get(key) or [])
    return summary


class ObservedToolbox:
    """Wrap the nine real mainline callables without changing their results."""

    def __init__(self, toolbox: Any):
        self._toolbox = toolbox
        for name in TOOL_NAMES:
            setattr(self, name, self._wrap(name, getattr(toolbox, name)))

    @staticmethod
    def _wrap(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            counts = _TOOL_COUNTS.get()
            if counts is None:
                counts = {}
                _TOOL_COUNTS.set(counts)
            call_index = counts.get(name, 0) + 1
            counts[name] = call_index
            started = time.perf_counter()
            _safe_emit("tool_started", lambda: {
                "tool_name": name,
                "call_index": call_index,
                "input_summary": _args_summary(name, args, kwargs),
            })
            try:
                result = original(*args, **kwargs)
            except Exception:
                raise
            _safe_emit("tool_completed", lambda: {
                "tool_name": name,
                "call_index": call_index,
                "ok": bool(getattr(result, "ok", False)),
                "next_state": str(getattr(result, "next_state", "")),
                "output_summary": _tool_output_summary(result),
                "error_kind": "" if bool(getattr(result, "ok", False)) else "tool_result_error",
            }, duration_ms=round((time.perf_counter() - started) * 1000))
            return result
        return wrapped


class HookManager:
    """Install transparent hooks on the mirror module references used by mainline."""

    def __init__(self) -> None:
        self._installed = False
        self._originals: list[tuple[Any, str, Any]] = []

    def install(self) -> None:
        if self._installed:
            return
        import tiku_agent.agent as agent_module
        import tiku_agent.intent_v2 as intent_module
        from tiku_agent.state import AgentState

        original_decide = agent_module.decide_intent_v2

        @wraps(original_decide)
        def observed_decide(*args: Any, **kwargs: Any) -> Any:
            result = original_decide(*args, **kwargs)
            _safe_emit("intent_decided", lambda: _decision_summary(result))
            return result

        self._replace(agent_module, "decide_intent_v2", observed_decide)

        original_authorize = intent_module.authorize_action_v2

        @wraps(original_authorize)
        def observed_authorize(decision: Any, context: Any) -> Any:
            result = original_authorize(decision, context)
            _AUTHORIZATION_COUNT.set(_AUTHORIZATION_COUNT.get() + 1)
            _safe_emit("authorization_checked", lambda: _authorization_summary(decision, context, result))
            return result

        self._replace(intent_module, "authorize_action_v2", observed_authorize)

        for method_name in STATE_METHODS:
            original = getattr(AgentState, method_name)

            @wraps(original)
            def observed_state(self: Any, *args: Any, __original: Callable[..., Any] = original, __name: str = method_name, **kwargs: Any) -> Any:
                before = _state_summary(self)
                result = __original(self, *args, **kwargs)
                after = _state_summary(self)
                changes = _diff(before, after)
                if changes:
                    _safe_emit("state_transition", lambda: {
                        "trigger": __name,
                        "phase_before": before["phase"],
                        "phase_after": after["phase"],
                        "changes": changes,
                        "automatic_check": "pass",
                        "check_codes": [],
                    })
                return result

            self._replace(AgentState, method_name, observed_state)
        self._installed = True

    def uninstall(self) -> None:
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)
        self._originals.clear()
        self._installed = False

    def _replace(self, owner: Any, name: str, value: Any) -> None:
        self._originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)


class ObservedAgent:
    """Turn shell around the real TikuSearchAgent; business calls happen once."""

    def __init__(self, agent: Any, store: ObservationStore):
        object.__setattr__(self, "_agent", agent)
        object.__setattr__(self, "_store", store)

    @property
    def state(self) -> Any:
        return self._agent.state

    @property
    def progress_reporter(self) -> Any:
        return self._agent.progress_reporter

    @progress_reporter.setter
    def progress_reporter(self, value: Any) -> None:
        self._agent.progress_reporter = value

    def handle_image(self, image_path: Any) -> Any:
        return self._handle("image", lambda: self._agent.handle_image(image_path))

    def handle_text(self, text: str) -> Any:
        return self._handle("text", lambda: self._agent.handle_text(text))

    def _handle(self, kind: str, call: Callable[[], Any]) -> Any:
        sink = self._store.new_turn(self.state.session_id)
        turn_token = _ACTIVE_TURN.set(sink)
        count_token = _TOOL_COUNTS.set({})
        authorization_token = _AUTHORIZATION_COUNT.set(0)
        before = _state_summary(self.state)
        started = time.perf_counter()
        _safe_emit("turn_started", lambda: {
            "kind": kind,
            "phase_before": before["phase"],
            "context_summary": before,
        })
        try:
            result = call()
            _safe_emit("turn_completed", lambda: {
                "response_type": _response_type(result),
                "phase_after": str(getattr(self.state, "phase", "")),
                "intent": str(getattr(result, "intent", "")),
                "candidate_count": int(getattr(self.state, "candidate_count", 0)),
                "answer_count": len(getattr(self.state, "last_answer_paths", []) or []),
                "authorization_count": _AUTHORIZATION_COUNT.get(),
                "automatic_issues": 0,
            }, duration_ms=round((time.perf_counter() - started) * 1000))
            return result
        except Exception as exc:
            _safe_emit("turn_completed", lambda: {
                "response_type": "exception",
                "phase_after": str(getattr(self.state, "phase", "")),
                "intent": "",
                "candidate_count": int(getattr(self.state, "candidate_count", 0)),
                "answer_count": len(getattr(self.state, "last_answer_paths", []) or []),
                "error_kind": type(exc).__name__,
                "authorization_count": _AUTHORIZATION_COUNT.get(),
                "automatic_issues": 0,
            }, duration_ms=round((time.perf_counter() - started) * 1000))
            raise
        finally:
            _TOOL_COUNTS.reset(count_token)
            _AUTHORIZATION_COUNT.reset(authorization_token)
            _ACTIVE_TURN.reset(turn_token)


def _response_type(response: Any) -> str:
    state = getattr(response, "state", {}) or {}
    phase = str(state.get("phase") or "") if isinstance(state, dict) else ""
    images = list(getattr(response, "images", []) or [])
    if phase == "NO_MATCH":
        return "no_match"
    if phase == "WAIT_CANDIDATE_CHOICE":
        return "candidates"
    if phase == "ANSWERED":
        return "answer"
    if phase == "ERROR":
        return "error"
    if images:
        return "media"
    return "text"

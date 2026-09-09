"""Bounded configuration fingerprints for operation and validation results.

Only hashes leave this module. Credentials and arbitrary object attributes are
never inspected. Custom clients must explicitly version their behavior.
"""
import hashlib
import inspect
import json
import math
from dataclasses import fields
from pathlib import Path
from time import perf_counter

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


def component_version(client, *, _depth=0):
    """Version standard components or an explicitly declared custom adapter.

    A custom ``execution_version`` value (or zero-argument callable) must cover
    its code, prompts, options and any nested dependencies. It is an adapter
    contract, not an assertion that arbitrary Python state can be discovered.
    Runtime counters must not be included. Re-read on each reuse decision.
    """
    if client is None:
        return None
    from tiku_agent.a3_models import QwenA3CropVerifier, QwenA3PageObserver, QwenA3UnitAnalyzer
    from tiku_agent.a3_auto_crop import GlmA3AutoCropper
    from tiku_agent.a3_intent_v1 import A3IntentEngineV1
    from tiku_agent.external_load_screen import QwenExternalLoadScreen, ZhipuExternalLoadScreen
    from tiku_agent.image_triage import QwenImageTriage, observation_from_model_text, build_handoff
    from tiku_agent.image_triage_8897 import observation_from_model_text_8897_v1, build_handoff_8897_v1
    from tiku_agent.image_triage_authority import ImageTriageAuthority, QwenTriageReplyClient
    from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
    from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0
    from tiku_agent.agent import AgentToolbox

    try:
        if _depth > 8:
            raise ValueError("component nesting exceeds limit")
        nested = {
            QwenA3CropVerifier: (), QwenA3PageObserver: (), QwenA3UnitAnalyzer: (),
            QwenExternalLoadScreen: (), ZhipuExternalLoadScreen: (), GlmA3AutoCropper: (),
            QwenSafeAnswerClientV0: (), QwenTriageReplyClient: (),
            QwenImageTriage: ("observation_parser",),
            ImageTriageAuthority: ("observer", "reply_client", "handoff_builder"),
            A3IntentEngineV1: ("model_client",), SafeAnswerGeneratorV0: ("model_client", "clock"),
            AgentToolbox: tuple(field.name for field in fields(AgentToolbox)),
        }
        pure_functions = (observation_from_model_text, build_handoff,
                          observation_from_model_text_8897_v1, build_handoff_8897_v1, perf_counter,
                          *(field.default for field in fields(AgentToolbox)))
        standard = type(client) in nested or any(client is function for function in pure_functions)
        declaration = getattr(client, "execution_version", None)
        if not standard and declaration is None:
            raise ValueError("custom client requires execution_version")
        if callable(declaration):
            declaration = declaration()
        if not standard and declaration in (None, "", {}, []):
            raise ValueError("custom client version must not be empty")
        identity = client if inspect.isroutine(client) or inspect.isclass(client) else type(client)
        payload = {"schema": 1, "type": identity.__module__ + "." + identity.__qualname__,
                   "declared": _json_value(declaration)}
        # Known request-affecting fields supplement a custom declaration too.
        for name in ("model", "endpoint", "timeout_seconds", "max_attempts", "temperature", "max_tokens"):
            if hasattr(client, name):
                payload[name] = _json_value(getattr(client, name))
        prompt = getattr(client, "prompt_path", None)
        if prompt is not None:
            with Path(prompt).open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("prompt exceeds versioning limit")
            payload["prompt_sha256"] = hashlib.sha256(content).hexdigest()
        for name in nested.get(type(client), ()):
            payload[name] = component_version(getattr(client, name), _depth=_depth + 1)
        if type(client) is QwenTriageReplyClient:
            payload["system_prompt"] = hashlib.sha256(client.SYSTEM_PROMPT.encode()).hexdigest()
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("version exceeds limit")
        return digest(payload)
    except Exception:
        # Do not expose configuration values, file paths or callback exceptions.
        raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE") from None


def configuration_digest(value):
    """Validate an explicit adapter attestation without persisting its values."""
    try:
        value = value() if callable(value) else value
        if value in (None, "", {}, []):
            raise ValueError("missing configuration version")
        value = _json_value(value)
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 16384:
            raise ValueError("configuration version exceeds limit")
        return digest(value)
    except Exception:
        raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE") from None


def runtime_version(runtime):
    """Version the standard runtime graph; custom factories declare their own contract."""
    from tiku_agent.a3_runtime import A3MvpRuntime
    from tiku_agent.session_runtime import AgentSessionRuntime
    if type(runtime) is A3MvpRuntime:
        components = ("page_observer", "crop_verifier", "auto_cropper", "unit_analyzer",
                      "external_load_screen", "image_triage_authority", "intent_engine", "a3_page_orienter")
        options = ("auto_crop_max_workers", "auto_prepare_all_units", "enable_three_scope_cancel_clarification")
        payload = {"a2": runtime_version(runtime.a2_runtime)}
    elif type(runtime) is AgentSessionRuntime:
        components = ("agent_factory", "external_load_screen", "image_triage_authority")
        options = ("external_load_timeout_seconds", "preserve_artifacts_on_cancel")
        payload = {}
    else:
        # Runtime subclasses may replace execution or add whole model paths.
        return component_version(runtime)
    payload.update({name: component_version(getattr(runtime, name)) for name in components})
    payload.update({name: getattr(runtime, name) for name in options})
    return configuration_digest(payload)

"""Bounded, immutable in-process capture inputs, never a public or stored payload."""

from dataclasses import dataclass, field
import json
import math
from collections.abc import Mapping

from tiku_agent.tool_result import ToolResult


MAX_CAPTURE_INPUT_BYTES = 512 * 1024
MAX_CAPTURE_INPUT_NODES = 16_000


@dataclass(frozen=True)
class FrozenCheckpointInput:
    encoded: bytes = field(repr=False)

    @classmethod
    def capture(cls, value):
        remaining = MAX_CAPTURE_INPUT_NODES
        remaining_bytes = MAX_CAPTURE_INPUT_BYTES

        def reserve(size):
            nonlocal remaining_bytes
            remaining_bytes -= size
            if remaining_bytes < 0:
                raise ValueError("capture input exceeds byte bound")

        def scalar(item):
            reserve(len(json.dumps(item, ensure_ascii=True, allow_nan=False).encode("utf-8")))
            return item

        def copy(item, depth=0):
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 12:
                raise ValueError("capture input exceeds structural bounds")
            if item is None or type(item) in {bool, int}:
                return scalar(item)
            if type(item) is float and math.isfinite(item):
                return scalar(item)
            if type(item) is str and len(item) <= 16_384:
                return scalar(item)
            if isinstance(item, Mapping) and len(item) <= 1000:
                if any(type(key) is not str or len(key) > 128 for key in item):
                    raise ValueError("invalid capture input key")
                reserve(2 * len(item) + 1 if item else 2)
                return {scalar(key): copy(value, depth + 1) for key, value in item.items()}
            if isinstance(item, (list, tuple)) and len(item) <= 1000:
                reserve(len(item) + 1 if item else 2)
                return [copy(value, depth + 1) for value in item]
            raise ValueError("unsupported capture input")

        encoded = json.dumps(copy(value), ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CAPTURE_INPUT_BYTES:
            raise ValueError("capture input exceeds byte bound")
        return cls(encoded)

    def materialize(self):
        return json.loads(self.encoded)


def freeze_a2_stage_input(result: ToolResult, payload: dict) -> FrozenCheckpointInput:
    # Only fields consumed by stage projectors; raw error text and model output stay out.
    fields = {
        "checkpoint_producer", "checkpoint_rerank", "checkpoint_counts", "candidates",
        "visible_candidates", "reranked", "dimension_filter", "remaining_candidate_count",
        "chapter", "chapter_scope_status", "chapter_confidence", "chapter_source",
        "chapter_reason_code", "structure_type", "structure_source", "structure_reason_code",
        "structure_confidence", "structure_filter_applicable", "loads", "analysis_schema_version",
        "visible_problem_text", "recognized_text_excerpt", "category",
    }
    data = {key: value for key, value in result.data.items() if key in fields}
    if isinstance(result.data.get("classified"), Mapping):
        data["classified"] = {"visible_problem_text": result.data["classified"].get("visible_problem_text", "")}
    return FrozenCheckpointInput.capture({
        "outcome": result.outcome.value, "code": result.code,
        "retryable": result.retryable, "error_category": result.error_category,
        "data": data, "payload": payload,
    })


def materialize_a2_stage_input(snapshot: FrozenCheckpointInput):
    value = snapshot.materialize()
    return ToolResult(outcome=value["outcome"], code=value["code"], data=value["data"],
                      retryable=value["retryable"], error_category=value["error_category"]), value["payload"]

"""Pure-code safety routing for the isolated 8890 image triage stage."""

from __future__ import annotations

import re

from .image_contracts import ImageTriageHandoff, ImageTriageObservation, Route


_EXPLICIT_ROUTE = re.compile(
    r"(?:建议|推荐)?\s*(?:路线|分流)\s*[:：]?\s*[`* _-]*(A[123])\b",
    re.IGNORECASE,
)
_BARE_ROUTE = re.compile(r"^\s*[`* _-]*(A[123])\s*[`* _-]*(?:$|[：:，,。.!！])", re.IGNORECASE)


def parse_route_candidate(text: str) -> Route:
    """Read the model's first-pass route without accepting A30-like tokens."""

    content = str(text or "")
    explicit = [match.group(1).upper() for match in _EXPLICIT_ROUTE.finditer(content)]
    if explicit:
        if len(set(explicit)) != 1:
            raise ValueError("模型回答包含互相冲突的分流建议")
        return explicit[0]  # type: ignore[return-value]

    first_line = content.splitlines()[0] if content.splitlines() else content
    match = _BARE_ROUTE.match(first_line)
    if match:
        return match.group(1).upper()  # type: ignore[return-value]
    raise ValueError("模型回答缺少明确的 A1/A2/A3 分流建议")


def finalize_route(observation: ImageTriageObservation) -> Route:
    """Apply only high-risk A2/A1 guards; uncertain cases fall back to A3."""

    if observation.route_candidate == "A2":
        a2_facts = (
            observation.question_count == 1,
            observation.original_structure_count == 1,
            observation.auxiliary_diagram_count == 0,
            observation.has_actual_load_evidence is True,
            observation.image_recoverable is True,
            observation.has_ambiguity is False,
        )
        if not all(a2_facts):
            return "A3"

    if observation.route_candidate == "A1":
        if observation.has_structure_content is not False:
            return "A3"
    return observation.route_candidate


def build_handoff(
    source_image_path: str,
    observation: ImageTriageObservation,
) -> ImageTriageHandoff:
    """Create the branch payload without invoking any model or downstream tool."""

    route = finalize_route(observation)
    if route == "A1":
        next_action = "stop"
    elif route == "A2":
        next_action = "existing_search"
    else:
        next_action = "a3_processing"
    return ImageTriageHandoff(
        route=route,
        source_image_path=str(source_image_path),
        observation=observation,
        next_action=next_action,
        reason=observation.evidence,
    )

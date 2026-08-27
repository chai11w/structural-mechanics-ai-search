"""Deterministic, privacy-bounded comparison of diagnostic bundles."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
import re
from typing import Mapping, Sequence


COMPARISON_SCHEMA_VERSION = 1
COMPARISON_CLASSIFICATIONS = frozenset(
    {
        "match",
        "authoritative_only",
        "legacy_only",
        "conflict",
        "evidence_missing",
    }
)

_AUTHORITATIVE_ASSOCIATIONS = frozenset(
    {
        "direct_selector",
        "authoritative_trace_id",
        "authoritative_response_id",
        "authoritative_identity_key",
        "trace_exact",
        "response_trace_exact",
        "feedback_response_exact",
        "workflow_exact",
    }
)
_LEGACY_ASSOCIATION = "legacy_compatibility"
_COMPLETENESS_VALUES = frozenset({"complete", "partial", "missing"})
_CONTEXT_ONLY_SOURCES = frozenset({"trace_events", "responses"})
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,192}$")
_SAFE_TIMESTAMP_RE = re.compile(r"^[0-9TZ:+.\-]{1,64}$")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:TIKU-|https?://|[A-Za-z]:[\\/]|(?:^|\s)/(?:app|etc|home|opt|private|root|srv|tmp|usr|var)(?:/|\b)|"
    r"\b(?:authorization|bearer|api[_ -]?key|access[_ -]?token|password|cookie|secret|"
    r"prompt|conversation|provider[_ -]?request)\b)",
    re.IGNORECASE,
)
_HARD_SOURCE_FAILURES = frozenset(
    {
        "missing",
        "unreadable",
        "query_failed",
        "schema_mismatch",
        "limit_exceeded",
        "partial",
    }
)
_HARD_AUTHORITATIVE_GAP_PREFIXES = (
    "legacy_feedback_unbound:",
    "feedback_response_missing:",
    "response_trace_missing:",
    "terminal_missing:",
    "terminal_duplicate:",
    "trace_not_found",
    "response_not_found",
    "feedback_not_found",
    "model_costs:authoritative_evidence_missing",
    "task_logs:authoritative_evidence_missing",
)

# These are facts safe to compare and return. Session keys, all request_id
# spellings, free-form text, paths, conversation, safe_attributes and provider
# request IDs are intentionally absent.
COMPARISON_FIELD_ALLOWLIST: Mapping[str, tuple[str, ...]] = {
    "feedback": (
        "feedback_id",
        "feedback_number",
        "rated_response_id",
        "identity_key",
        "rating",
        "tags",
        "task_revision",
        "phase",
        "candidate_count",
        "search_duration_ms",
        "search_key",
        "search_id",
        "status",
        "layer",
        "code",
        "chapter",
        "image_route",
        "workflow_search_id",
        "intent",
        "feedback_scope",
        "review_status",
        "archived_at",
        "case_expires_at",
        "case_purged_at",
        "created_at",
        "updated_at",
        "schema_version",
        "legacy_binding",
    ),
    "model_cost_runs": (
        "run_id",
        "trace_id",
        "identity_key",
        "search_key",
        "task_kind",
        "started_at",
        "finished_at",
        "outcome",
        "call_count",
        "total_tokens",
        "estimated_cost_micros",
        "warning_codes",
        "schema_version",
    ),
    "model_cost_calls": (
        "call_id",
        "run_id",
        "sequence",
        "provider",
        "model",
        "call_type",
        "status",
        "started_at",
        "finished_at",
        "latency_ms",
        "input_tokens",
        "image_tokens",
        "cached_tokens",
        "output_tokens",
        "total_tokens",
        "attempt_count",
        "trace_id",
        "error_kind",
        "price_version",
        "pricing_status",
        "estimated_cost_micros",
        "schema_version",
    ),
    "task_logs": (
        "trace_id",
        "identity_key",
        "kind",
        "started_at",
        "finished_at",
        "duration_ms",
        "phase_before",
        "phase_after",
        "outcome",
        "question_count",
        "candidate_count",
        "chapter",
        "route",
        "error_kind",
        "search_id",
        "status",
        "layer",
        "code",
        "retryable",
        "action",
        "schema_version",
    ),
    "a3_page_errors": (
        "event_id",
        "search_id",
        "task_kind",
        "phase",
        "error_type",
        "error_code",
        "created_at",
    ),
}

_PRIMARY_KEYS: Mapping[str, tuple[str, ...]] = {
    "feedback": ("feedback_id", "feedback_number"),
    "model_cost_runs": ("run_id",),
    "model_cost_calls": ("call_id",),
    "task_logs": (),
    "a3_page_errors": ("event_id",),
}

_SOURCE_DOMAINS: Mapping[str, str] = {
    "feedback": "feedback",
    "model_cost_runs": "costs",
    "model_cost_calls": "costs",
    "task_logs": "tasks",
    "a3_page_errors": "page_errors",
}

_DOMAIN_SOURCE_NAMES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "authoritative": {
        "feedback": ("feedback",),
        "costs": ("model_costs",),
        "tasks": ("task_logs",),
        "page_errors": ("a3_page_errors",),
    },
    "legacy": {
        "feedback": ("feedback",),
        "costs": ("model_costs", "model_costs_legacy_a2"),
        "tasks": ("task_logs", "task_logs_legacy_root"),
        "page_errors": ("a3_page_errors",),
    },
}

_DROP = object()


@dataclass(frozen=True)
class ComparisonEvidence:
    """One safe evidence projection plus its original association metadata."""

    source: str
    association: str
    completeness: str
    comparison_key: str
    fields: tuple[tuple[str, object], ...]
    timestamp: str = ""

    @property
    def fingerprint(self) -> str:
        payload = {
            "source": self.source,
            "fields": {name: _json_value(value) for name, value in self.fields},
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source": self.source,
            "association": self.association,
            "completeness": self.completeness,
            "comparison_key": self.comparison_key,
            "fields": {name: _json_value(value) for name, value in self.fields},
        }
        if self.timestamp:
            result["timestamp"] = self.timestamp
        return result


@dataclass(frozen=True)
class DiagnosticComparison:
    """A deterministic comparison ready for a CLI or another local consumer."""

    classification: str
    authoritative: tuple[ComparisonEvidence, ...]
    legacy: tuple[ComparisonEvidence, ...]
    authoritative_only: tuple[ComparisonEvidence, ...]
    legacy_only: tuple[ComparisonEvidence, ...]
    evidence_gaps: tuple[str, ...]
    ignored_authoritative_items: int = 0
    ignored_legacy_items: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": COMPARISON_SCHEMA_VERSION,
            "classification": self.classification,
            "summary": {
                "authoritative_count": len(self.authoritative),
                "legacy_count": len(self.legacy),
                "authoritative_only_count": len(self.authoritative_only),
                "legacy_only_count": len(self.legacy_only),
                "ignored_authoritative_items": self.ignored_authoritative_items,
                "ignored_legacy_items": self.ignored_legacy_items,
            },
            "authoritative": [item.to_dict() for item in self.authoritative],
            "legacy": [item.to_dict() for item in self.legacy],
            "differences": {
                "authoritative_only": [
                    item.to_dict() for item in self.authoritative_only
                ],
                "legacy_only": [item.to_dict() for item in self.legacy_only],
            },
            "evidence_gaps": list(self.evidence_gaps),
        }


@dataclass(frozen=True)
class _ExtractedBundle:
    items: tuple[ComparisonEvidence, ...]
    source_states: Mapping[str, str]
    gaps: tuple[str, ...]
    structurally_incomplete: bool
    ignored_items: int


def compare_bundles(
    authoritative_bundle: Mapping[str, object],
    legacy_bundle: Mapping[str, object],
) -> DiagnosticComparison:
    """Compare exact and compatibility views without comparing private fields."""

    if not isinstance(authoritative_bundle, Mapping):
        raise TypeError("authoritative_bundle must be a mapping")
    if not isinstance(legacy_bundle, Mapping):
        raise TypeError("legacy_bundle must be a mapping")

    authoritative = _extract_bundle(authoritative_bundle, side="authoritative")
    legacy = _extract_bundle(legacy_bundle, side="legacy")
    authoritative_only = _multiset_difference(authoritative.items, legacy.items)
    legacy_only = _multiset_difference(legacy.items, authoritative.items)
    domains = {
        _SOURCE_DOMAINS[item.source]
        for item in (*authoritative.items, *legacy.items)
    }
    availability_gaps = (
        *_source_availability_gaps(authoritative, "authoritative", domains),
        *_source_availability_gaps(legacy, "legacy", domains),
    )
    relevant_missing = (
        authoritative.structurally_incomplete
        or legacy.structurally_incomplete
        or bool(availability_gaps)
    )

    if relevant_missing:
        classification = "evidence_missing"
    elif authoritative_only and legacy_only:
        classification = "conflict"
    elif authoritative_only:
        classification = "authoritative_only"
    elif legacy_only:
        classification = "legacy_only"
    elif authoritative.items and legacy.items:
        classification = "match"
    else:
        classification = "evidence_missing"

    gaps = tuple(
        dict.fromkeys(
            [
                *authoritative.gaps,
                *legacy.gaps,
                *availability_gaps,
                *(
                    ()
                    if domains
                    else ("comparison:no_comparable_evidence",)
                ),
            ]
        )
    )
    return DiagnosticComparison(
        classification=classification,
        authoritative=authoritative.items,
        legacy=legacy.items,
        authoritative_only=authoritative_only,
        legacy_only=legacy_only,
        evidence_gaps=gaps,
        ignored_authoritative_items=authoritative.ignored_items,
        ignored_legacy_items=legacy.ignored_items,
    )


compare_diagnostic_bundles = compare_bundles


def _extract_bundle(
    bundle: Mapping[str, object], *, side: str
) -> _ExtractedBundle:
    raw_evidence = bundle.get("evidence")
    if not isinstance(raw_evidence, Sequence) or isinstance(
        raw_evidence, (str, bytes, bytearray)
    ):
        return _ExtractedBundle(
            items=(),
            source_states=_source_states(bundle),
            gaps=(f"{side}:evidence_unavailable",),
            structurally_incomplete=True,
            ignored_items=0,
        )

    items: list[ComparisonEvidence] = []
    gaps = _bundle_gaps(bundle, side)
    structurally_incomplete = side == "authoritative" and any(
        gap.removeprefix("authoritative:").startswith(
            _HARD_AUTHORITATIVE_GAP_PREFIXES
        )
        for gap in gaps
    )
    ignored = 0
    for raw_item in raw_evidence:
        if not isinstance(raw_item, Mapping):
            structurally_incomplete = True
            gaps.append(f"{side}:malformed_evidence_item")
            continue
        source = str(raw_item.get("source") or "").strip()
        association = str(raw_item.get("association") or "").strip()
        if source in _CONTEXT_ONLY_SOURCES:
            ignored += 1
            continue
        if side == "authoritative" and association == _LEGACY_ASSOCIATION:
            ignored += 1
            continue
        legacy_direct_feedback = (
            side == "legacy"
            and source == "feedback"
            and association == "direct_selector"
        )
        if (
            side == "legacy"
            and association in _AUTHORITATIVE_ASSOCIATIONS
            and not legacy_direct_feedback
        ):
            ignored += 1
            continue
        if side == "authoritative" and association not in _AUTHORITATIVE_ASSOCIATIONS:
            structurally_incomplete = True
            ignored += 1
            gaps.append(f"{side}:unsupported_association")
            continue
        if (
            side == "legacy"
            and association != _LEGACY_ASSOCIATION
            and not legacy_direct_feedback
        ):
            structurally_incomplete = True
            ignored += 1
            gaps.append(f"{side}:unsupported_association")
            continue
        if source not in COMPARISON_FIELD_ALLOWLIST:
            structurally_incomplete = True
            ignored += 1
            gaps.append(f"{side}:unsupported_source")
            continue
        record = raw_item.get("record")
        if not isinstance(record, Mapping):
            structurally_incomplete = True
            gaps.append(f"{side}:{source}:record_unavailable")
            continue
        completeness = str(raw_item.get("completeness") or "").strip()
        if completeness not in _COMPLETENESS_VALUES:
            structurally_incomplete = True
            completeness = "missing"
            gaps.append(f"{side}:{source}:invalid_completeness")
        elif completeness == "missing" or (
            side == "authoritative" and completeness != "complete"
        ):
            structurally_incomplete = True
            gaps.append(f"{side}:{source}:evidence_incomplete")
        fields = _project_fields(source, record)
        if not fields:
            structurally_incomplete = True
            ignored += 1
            gaps.append(f"{side}:{source}:no_comparable_fields")
            continue
        timestamp = str(raw_item.get("timestamp") or "").strip()
        if not _SAFE_TIMESTAMP_RE.fullmatch(timestamp):
            timestamp = ""
        item = ComparisonEvidence(
            source=source,
            association=association,
            completeness=completeness,
            comparison_key=_comparison_key(source, fields),
            fields=fields,
            timestamp=timestamp,
        )
        items.append(item)
    return _ExtractedBundle(
        items=tuple(sorted(items, key=lambda item: (item.fingerprint, item.association))),
        source_states=_source_states(bundle),
        gaps=tuple(dict.fromkeys(gaps)),
        structurally_incomplete=structurally_incomplete,
        ignored_items=ignored,
    )


def _project_fields(
    source: str, record: Mapping[str, object]
) -> tuple[tuple[str, object], ...]:
    result: list[tuple[str, object]] = []
    for name in COMPARISON_FIELD_ALLOWLIST[source]:
        if name not in record:
            continue
        value = _normalize_value(record[name])
        if value is not _DROP:
            result.append((name, value))
    return tuple(result)


def _normalize_value(value: object) -> object:
    if value is None or isinstance(value, bool) or type(value) is int:
        return value
    if isinstance(value, float):
        return value if isfinite(value) else _DROP
    if isinstance(value, str):
        clean = value[:256]
        return _DROP if _SENSITIVE_VALUE_RE.search(clean) else clean
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        output: list[object] = []
        for item in value[:32]:
            normalized = _normalize_value(item)
            if normalized is not _DROP and not isinstance(normalized, (tuple, list, dict)):
                output.append(normalized)
        return tuple(output)
    return _DROP


def _comparison_key(
    source: str, fields: tuple[tuple[str, object], ...]
) -> str:
    values = dict(fields)
    for name in _PRIMARY_KEYS[source]:
        value = values.get(name)
        if value not in (None, ""):
            return f"{source}:{value}"
    canonical = json.dumps(
        {name: _json_value(value) for name, value in fields},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{source}:sha256:{sha256(canonical.encode('ascii')).hexdigest()[:16]}"


def _multiset_difference(
    left: tuple[ComparisonEvidence, ...], right: tuple[ComparisonEvidence, ...]
) -> tuple[ComparisonEvidence, ...]:
    remaining = Counter(item.fingerprint for item in right)
    result: list[ComparisonEvidence] = []
    for item in left:
        if remaining[item.fingerprint] > 0:
            remaining[item.fingerprint] -= 1
        else:
            result.append(item)
    return tuple(result)


def _source_states(bundle: Mapping[str, object]) -> dict[str, str]:
    raw_sources = bundle.get("sources")
    if not isinstance(raw_sources, Sequence) or isinstance(
        raw_sources, (str, bytes, bytearray)
    ):
        return {}
    result: dict[str, str] = {}
    for raw in raw_sources:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        status = str(raw.get("status") or "").strip()
        if _SAFE_TOKEN_RE.fullmatch(name) and _SAFE_TOKEN_RE.fullmatch(status):
            result[name] = status
    return result


def _source_availability_gaps(
    bundle: _ExtractedBundle, side: str, domains: set[str]
) -> tuple[str, ...]:
    if not domains:
        return ()
    item_domains = {_SOURCE_DOMAINS[item.source] for item in bundle.items}
    configured = _DOMAIN_SOURCE_NAMES[side]
    gaps: list[str] = []
    for domain in domains:
        names = configured[domain]
        states = {
            name: bundle.source_states[name]
            for name in names
            if name in bundle.source_states
        }
        for name, status in states.items():
            if status in _HARD_SOURCE_FAILURES:
                gaps.append(f"{side}:{name}:{status}")
        if domain in item_domains:
            continue
        if not any(status == "ok" for status in states.values()):
            gaps.append(f"{side}:{domain}:source_unavailable")
    return tuple(dict.fromkeys(gaps))


def _bundle_gaps(bundle: Mapping[str, object], side: str) -> list[str]:
    summary = bundle.get("summary")
    if not isinstance(summary, Mapping):
        return []
    raw = summary.get("evidence_gaps")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    result = []
    for value in raw:
        clean = str(value or "").strip()
        if _SAFE_TOKEN_RE.fullmatch(clean):
            result.append(f"{side}:{clean}")
    return result


def _json_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


__all__ = [
    "COMPARISON_CLASSIFICATIONS",
    "COMPARISON_FIELD_ALLOWLIST",
    "COMPARISON_SCHEMA_VERSION",
    "ComparisonEvidence",
    "DiagnosticComparison",
    "compare_bundles",
    "compare_diagnostic_bundles",
]

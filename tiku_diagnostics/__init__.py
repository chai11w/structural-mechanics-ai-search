"""Read-only, privacy-bounded diagnostics for Agent runtime roots."""

from .compare import (
    COMPARISON_CLASSIFICATIONS,
    DiagnosticComparison,
    compare_diagnostic_bundles,
)
from .query import (
    ASSOCIATION_MODES,
    DIAGNOSTIC_SCHEMA_VERSION,
    MAX_DIAGNOSTIC_LIMIT,
    MAX_DIAGNOSTIC_OUTPUT_BYTES,
    MAX_IDENTITY_WINDOW_DAYS,
    DiagnosticQueryError,
    DiagnosticQueryService,
    QuerySpec,
)

__all__ = [
    "DIAGNOSTIC_SCHEMA_VERSION",
    "ASSOCIATION_MODES",
    "MAX_DIAGNOSTIC_LIMIT",
    "MAX_DIAGNOSTIC_OUTPUT_BYTES",
    "MAX_IDENTITY_WINDOW_DAYS",
    "COMPARISON_CLASSIFICATIONS",
    "DiagnosticQueryError",
    "DiagnosticComparison",
    "DiagnosticQueryService",
    "QuerySpec",
    "compare_diagnostic_bundles",
]

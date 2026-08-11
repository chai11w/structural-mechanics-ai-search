"""Provider-neutral model usage collection and local cost persistence.

The module is deliberately independent from the Agent and retrieval layers so
Qwen and Zhipu adapters can emit usage without importing product state.  An
active request scope collects records in memory; one SQLite transaction writes
the completed run so observability cannot add lock contention to concurrent
visual reranking.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4


CATALOG_PATH = Path(__file__).with_name("model_price_catalog.json")
COST_SCHEMA_VERSION = 1
MICRO_CNY = Decimal("1000000")
TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ModelCallRecord:
    call_id: str
    sequence: int
    provider: str
    model: str
    call_type: str
    status: str
    started_at: str
    finished_at: str
    latency_ms: int
    input_tokens: int = 0
    image_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    attempt_count: int = 1
    request_id: str = ""
    error_kind: str = ""
    price_version: str = ""
    pricing_status: str = "unpriced"
    estimated_cost_micros: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelCostCollector:
    run_id: str
    session_key: str = ""
    search_key: str = ""
    task_kind: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    _records: list[ModelCallRecord] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def record(
        self,
        *,
        provider: str,
        model: str,
        call_type: str,
        status: str,
        started_at: str,
        finished_at: str,
        latency_ms: int,
        usage: Any = None,
        attempt_count: int = 1,
        request_id: str = "",
        error_kind: str = "",
    ) -> ModelCallRecord:
        tokens = normalize_usage(usage)
        pricing = estimate_cost(provider, model, tokens)
        with self._lock:
            sequence = len(self._records) + 1
            record = ModelCallRecord(
                call_id=uuid4().hex,
                sequence=sequence,
                provider=str(provider).strip().lower(),
                model=str(model).strip(),
                call_type=str(call_type).strip(),
                status=str(status).strip().lower(),
                started_at=started_at,
                finished_at=finished_at,
                latency_ms=max(0, int(latency_ms or 0)),
                input_tokens=tokens["input_tokens"],
                image_tokens=tokens["image_tokens"],
                cached_tokens=tokens["cached_tokens"],
                output_tokens=tokens["output_tokens"],
                total_tokens=tokens["total_tokens"],
                attempt_count=max(1, int(attempt_count or 1)),
                request_id=str(request_id or ""),
                error_kind=str(error_kind or ""),
                price_version=pricing["price_version"],
                pricing_status=pricing["pricing_status"],
                estimated_cost_micros=pricing["estimated_cost_micros"],
            )
            self._records.append(record)
            return record

    def records(self) -> list[ModelCallRecord]:
        with self._lock:
            return list(self._records)


_ACTIVE_COLLECTOR: ContextVar[ModelCostCollector | None] = ContextVar(
    "active_model_cost_collector", default=None
)


@contextmanager
def model_cost_scope(collector: ModelCostCollector) -> Iterator[ModelCostCollector]:
    token = _ACTIVE_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _ACTIVE_COLLECTOR.reset(token)


def record_model_call(
    *,
    provider: str,
    model: str,
    call_type: str,
    status: str,
    started_at: str,
    finished_at: str,
    latency_ms: int,
    usage: Any = None,
    attempt_count: int = 1,
    request_id: str = "",
    error_kind: str = "",
) -> ModelCallRecord | None:
    collector = _ACTIVE_COLLECTOR.get()
    if collector is None:
        return None
    return collector.record(
        provider=provider,
        model=model,
        call_type=call_type,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        latency_ms=latency_ms,
        usage=usage,
        attempt_count=attempt_count,
        request_id=request_id,
        error_kind=error_kind,
    )


def submit_with_model_cost_context(executor: Any, function: Callable, /, *args: Any, **kwargs: Any) -> Any:
    """Submit work with a fresh copy of the caller's cost context."""

    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def timed_model_call(
    function: Callable[[], Any],
    *,
    provider: str,
    model: str,
    call_type: str,
    usage_getter: Callable[[Any], Any],
    request_id_getter: Callable[[Any], str] | None = None,
    attempt_count_getter: Callable[[Any], int] | None = None,
    attempt_count: int = 1,
) -> Any:
    """Run one provider request and emit usage without changing its return value."""

    started_at = utc_now()
    started = time.perf_counter()
    try:
        result = function()
    except Exception as exc:
        failed_attempt_count = max(1, int(getattr(exc, "model_attempt_count", attempt_count) or 1))
        record_model_call(
            provider=provider,
            model=model,
            call_type=call_type,
            status="error",
            started_at=started_at,
            finished_at=utc_now(),
            latency_ms=round((time.perf_counter() - started) * 1000),
            attempt_count=failed_attempt_count,
            error_kind=type(exc).__name__,
        )
        raise
    record_model_call(
        provider=provider,
        model=model,
        call_type=call_type,
        status="success",
        started_at=started_at,
        finished_at=utc_now(),
        latency_ms=round((time.perf_counter() - started) * 1000),
        usage=usage_getter(result),
        attempt_count=(attempt_count_getter(result) if attempt_count_getter else attempt_count),
        request_id=request_id_getter(result) if request_id_getter else "",
    )
    return result


def normalize_usage(usage: Any) -> dict[str, int]:
    data = _object_mapping(usage)
    details = _object_mapping(
        data.get("input_tokens_details") or data.get("prompt_tokens_details")
    )
    input_tokens = _non_negative_int(data.get("input_tokens", data.get("prompt_tokens", 0)))
    output_tokens = _non_negative_int(data.get("output_tokens", data.get("completion_tokens", 0)))
    image_tokens = _non_negative_int(data.get("image_tokens", details.get("image_tokens", 0)))
    cached_tokens = _non_negative_int(details.get("cached_tokens", 0))
    total_tokens = _non_negative_int(data.get("total_tokens", input_tokens + output_tokens))
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "image_tokens": image_tokens,
        "cached_tokens": min(cached_tokens, input_tokens),
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def estimate_cost(provider: str, model: str, tokens: Mapping[str, int]) -> dict[str, Any]:
    entry = _price_entry(provider, model)
    if entry is None:
        return {"price_version": "", "pricing_status": "missing_price", "estimated_cost_micros": 0}
    input_tokens = int(tokens.get("input_tokens", 0))
    tier = next(
        (item for item in entry["tiers"] if input_tokens <= int(item["max_input_tokens"])),
        None,
    )
    if tier is None:
        return {
            "price_version": str(entry["price_version"]),
            "pricing_status": "outside_price_tier",
            "estimated_cost_micros": 0,
        }
    cached_tokens = min(input_tokens, int(tokens.get("cached_tokens", 0)))
    regular_tokens = input_tokens - cached_tokens
    input_rate = Decimal(str(tier["input_cny_per_million"]))
    cached_rate = Decimal(str(tier.get("cached_input_cny_per_million", input_rate)))
    output_rate = Decimal(str(tier["output_cny_per_million"]))
    cost_cny = (
        Decimal(regular_tokens) * input_rate
        + Decimal(cached_tokens) * cached_rate
        + Decimal(int(tokens.get("output_tokens", 0))) * output_rate
    ) / TOKENS_PER_MILLION
    cost_micros = int((cost_cny * MICRO_CNY).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "price_version": str(entry["price_version"]),
        "pricing_status": "priced",
        "estimated_cost_micros": max(0, cost_micros),
    }


class SQLiteModelCostLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def write_run(
        self,
        collector: ModelCostCollector,
        *,
        finished_at: str,
        outcome: str,
    ) -> None:
        records = collector.records()
        call_count = sum(item.attempt_count for item in records)
        total_tokens = sum(item.total_tokens for item in records)
        total_cost = sum(item.estimated_cost_micros for item in records)
        warnings = _warning_codes(records)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                _create_schema(connection)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO model_cost_runs (
                        run_id, session_key, search_key, task_kind, started_at, finished_at,
                        outcome, call_count, total_tokens, estimated_cost_micros,
                        warning_codes_json, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        collector.run_id,
                        collector.session_key,
                        collector.search_key,
                        collector.task_kind,
                        collector.started_at,
                        finished_at,
                        outcome,
                        call_count,
                        total_tokens,
                        total_cost,
                        json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                        COST_SCHEMA_VERSION,
                    ),
                )
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO model_cost_calls (
                        call_id, run_id, sequence, provider, model, call_type,
                        status, started_at, finished_at, latency_ms, input_tokens,
                        image_tokens, cached_tokens, output_tokens, total_tokens,
                        attempt_count, request_id, error_kind, price_version,
                        pricing_status, estimated_cost_micros, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.call_id, collector.run_id, item.sequence, item.provider,
                            item.model, item.call_type, item.status, item.started_at,
                            item.finished_at, item.latency_ms, item.input_tokens,
                            item.image_tokens, item.cached_tokens, item.output_tokens,
                            item.total_tokens, item.attempt_count, item.request_id,
                            item.error_kind, item.price_version, item.pricing_status,
                            item.estimated_cost_micros, COST_SCHEMA_VERSION,
                        )
                        for item in records
                    ],
                )

    def estimated_cost_micros_since(self, started_at: str) -> int:
        """Return the recorded estimated cost at or after an ISO-8601 timestamp."""
        if not self.path.is_file():
            return 0
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                try:
                    row = connection.execute(
                        """
                        SELECT COALESCE(SUM(estimated_cost_micros), 0)
                        FROM model_cost_runs
                        WHERE started_at >= ?
                        """,
                        (str(started_at),),
                    ).fetchone()
                except sqlite3.OperationalError:
                    return 0
        return max(0, int(row[0] if row else 0))


def _warning_codes(records: list[ModelCallRecord]) -> list[str]:
    warnings = []
    if sum(item.attempt_count for item in records) > 10:
        warnings.append("MODEL_CALLS_OVER_10")
    if any(item.status == "success" and item.total_tokens <= 0 for item in records):
        warnings.append("USAGE_MISSING")
    if any(item.pricing_status != "priced" for item in records):
        warnings.append("PRICE_MISSING_OR_OUTSIDE_TIER")
    if any(item.status != "success" for item in records):
        warnings.append("MODEL_CALL_FAILED")
    return warnings


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_cost_runs (
            run_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            search_key TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            call_count INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            estimated_cost_micros INTEGER NOT NULL,
            warning_codes_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS model_cost_calls (
            call_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            call_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL,
            image_tokens INTEGER NOT NULL,
            cached_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            error_kind TEXT NOT NULL,
            price_version TEXT NOT NULL,
            pricing_status TEXT NOT NULL,
            estimated_cost_micros INTEGER NOT NULL,
            schema_version INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES model_cost_runs(run_id)
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_runs_started ON model_cost_runs(started_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_calls_run ON model_cost_calls(run_id, sequence)")


def _price_entry(provider: str, model: str) -> dict[str, Any] | None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    clean_provider = str(provider).strip().lower()
    clean_model = str(model).strip().lower()
    for entry in catalog.get("prices", []):
        if str(entry.get("provider", "")).lower() != clean_provider:
            continue
        if clean_model in {str(item).lower() for item in entry.get("models", [])}:
            return entry
    return None


def _object_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    result = {}
    for key in (
        "input_tokens", "prompt_tokens", "output_tokens", "completion_tokens",
        "total_tokens", "image_tokens", "input_tokens_details", "prompt_tokens_details",
    ):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

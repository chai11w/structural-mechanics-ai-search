"""Isolated synthetic benchmark: python -m tests.trace_capacity_benchmark.

No live databases, models, or services. Wall-clock values are evidence for this
machine and sample, not a timing assertion or a production latency guarantee.
"""

from contextlib import closing
import json
from math import ceil
from pathlib import Path
import sqlite3
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from tiku_shared.trace_capacity import trace_row_count
from tiku_shared.trace_context import TraceContext
from tiku_shared.trace_events import (
    SQLiteTraceEventStore, TraceEvent, TraceEventRecorder, _create_schema,
    _event_row, _new_trace_store_identity,
)


def summarize(values):
    return {"median": round(median(values), 3),
            "p95": round(sorted(values)[ceil(len(values) * .95) - 1], 3),
            "max": round(max(values), 3)}


def run(rows):
    root = Path(__file__).resolve().parents[1] / ".tmp_tests"
    root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="trace-capacity-", dir=root) as directory:
        path = Path(directory) / "trace.sqlite3"
        event = TraceEvent.create(trace_id=TraceContext.create().trace_id,
            event_type="stage_started", stage="benchmark", outcome="started")
        template = list(_event_row(event))

        def generated():
            for i in range(rows):
                row = template.copy()
                row[0], row[2], row[7] = f"evt_{i:032x}", f"trace_{i:032x}", f"req_{i:032x}"
                row[9], row[10] = "session_benchmark", "identity_benchmark"
                yield row

        with closing(sqlite3.connect(path)) as connection:
            _create_schema(connection)
            _new_trace_store_identity(connection)
            connection.executemany(
                "INSERT INTO trace_events VALUES (" + ",".join("?" for _ in template) + ")",
                generated(),
            )
            connection.commit()
        store = SQLiteTraceEventStore(path, max_rows=rows + 100)
        start = perf_counter()
        store.ensure_store_identity()
        migration_ms = (perf_counter() - start) * 1000
        scans, counts = [], []
        for i in range(30):
            # Alternate order; both use a fresh SQLite connection.
            methods = [(scans, lambda c: c.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]),
                       (counts, trace_row_count)]
            for samples, query in (methods if i % 2 else reversed(methods)):
                with closing(sqlite3.connect(path)) as connection:
                    start = perf_counter()
                    count = query(connection)
                    samples.append((perf_counter() - start) * 1000)
                    assert count == rows
        recorder = TraceEventRecorder(store)
        try:
            for _ in range(30):
                recorder.record(TraceEvent.create(trace_id=TraceContext.create().trace_id,
                    event_type="stage_started", stage="benchmark", outcome="started"))
            assert recorder.flush(30)
            health = recorder.health()
            assert health["written"] == 30 and health["write_failures"] == 0
            assert health["capacity"]["current_rows"] == rows + 30
        finally:
            assert recorder.close(timeout=5)
        return {"rows": rows, "migration_ms": round(migration_ms, 3),
                "full_count_ms": summarize(scans), "maintained_count_ms": summarize(counts),
                "written": health["written"], "write_failures": health["write_failures"],
                "write_diagnostics": health["write_diagnostics"]}


if __name__ == "__main__":
    for size in (100_000, 1_000_000):
        print(json.dumps(run(size)), flush=True)

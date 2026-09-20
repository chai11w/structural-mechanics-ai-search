"""Exact Trace row counts maintained in the event transaction, including old writers."""

import sqlite3


_TABLE = "trace_event_counts"
_INSERT = "trace_event_count_insert"
_DELETE = "trace_event_count_delete"
_SCHEMA = {
    _TABLE: ("table", """
        CREATE TABLE trace_event_counts (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            row_count INTEGER NOT NULL CHECK (typeof(row_count) = 'integer' AND row_count >= 0)
        )
    """),
    _INSERT: ("trigger", """
        CREATE TRIGGER trace_event_count_insert AFTER INSERT ON trace_events
        BEGIN
            UPDATE trace_event_counts SET row_count = row_count + 1 WHERE singleton = 1;
            SELECT CASE WHEN changes() != 1 THEN RAISE(ABORT, 'trace row count unavailable') END;
        END
    """),
    _DELETE: ("trigger", """
        CREATE TRIGGER trace_event_count_delete AFTER DELETE ON trace_events
        BEGIN
            UPDATE trace_event_counts SET row_count = row_count - 1 WHERE singleton = 1;
            SELECT CASE WHEN changes() != 1 THEN RAISE(ABORT, 'trace row count unavailable') END;
        END
    """),
}


def _objects(connection: sqlite3.Connection) -> dict:
    return {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name IN (?, ?, ?)",
            (_TABLE, _INSERT, _DELETE),
        )
    }


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _validate(objects: dict) -> None:
    if objects.keys() != _SCHEMA.keys() or any(
        objects[name][0] != kind or _normalize(objects[name][1] or "") != _normalize(sql)
        for name, (kind, sql) in _SCHEMA.items()
    ):
        raise sqlite3.DatabaseError("trace row count schema is incomplete or changed")


def _count(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT singleton, row_count FROM trace_event_counts LIMIT 2"
    ).fetchall()
    if (len(rows) != 1 or rows[0][0] != 1
            or type(rows[0][1]) is not int or rows[0][1] < 0):
        raise sqlite3.DatabaseError("trace row count is unavailable")
    return rows[0][1]


def ensure_trace_row_count(connection: sqlite3.Connection) -> None:
    """Migrate once under SQLite's writer lock; the caller commits schema setup.

    A partial extension is corruption, not permission to recount or silently
    recreate triggers. Production performs this during store initialization,
    before starting the recorder and its per-event deadline.
    """
    objects = _objects(connection)
    if not objects:
        connection.execute("BEGIN IMMEDIATE")
        # Another process may have migrated while we waited for the writer lock.
        objects = _objects(connection)
        if not objects:
            connection.execute(_SCHEMA[_TABLE][1])
            connection.execute(
                "INSERT INTO trace_event_counts SELECT 1, COUNT(*) FROM trace_events"
            )
            for name in (_INSERT, _DELETE):
                connection.execute(_SCHEMA[name][1])
            objects = _objects(connection)
    _validate(objects)
    _count(connection)


def trace_row_count(connection: sqlite3.Connection) -> int:
    """Read without migration; legacy stores remain readable by diagnostics."""
    objects = _objects(connection)
    if not objects:
        return int(connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0])
    _validate(objects)
    return _count(connection)

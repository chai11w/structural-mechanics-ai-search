"""A child response is committed with child state before parent finalization."""
from functools import wraps
import json
from uuid import uuid4

from tiku_agent.execution_store import ExecutionError, _WRITER, canonical, session_key


def create_handoff_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_handoffs (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES execution_operations(id),
        attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
        session TEXT NOT NULL, epoch TEXT NOT NULL, parent_record_id TEXT REFERENCES execution_tasks(id),
        child_record_id TEXT REFERENCES execution_tasks(id), unit_id TEXT NOT NULL,
        child_version INTEGER NOT NULL, result TEXT NOT NULL, status TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL)""")


def save_child_result(runtime, sid, response):
    operations = getattr(runtime, "execution_operations", None)
    writer = _WRITER.get()
    if operations is None or writer is None:
        return
    from tiku_agent.execution_runtime import encode_response
    store = operations.authority
    encoded = canonical(encode_response(response))
    if len(encoded.encode()) > store.policy.max_result_bytes:
        raise ExecutionError("EXECUTION_CAPACITY")
    with store.transaction() as conn:
        now = store.clock(conn)
        writer.validate(conn, store, session_key(sid), writer.epoch, now)
        slot = conn.execute("SELECT payload,version FROM execution_states WHERE session=? AND kind='child' AND epoch=?",
                            (writer.session, writer.epoch)).fetchone()
        if slot is None or slot["payload"] is None:
            raise ExecutionError("EXECUTION_STALE")
        state = json.loads(slot["payload"])
        task = conn.execute("SELECT * FROM execution_tasks WHERE session=? AND epoch=? AND kind='child' AND task_id=? AND task_revision=?",
                            (writer.session, writer.epoch, state.get("current_search_id", ""), state.get("task_revision", 0))).fetchone()
        handoff_id = uuid4().hex
        store.storage_capacity(extra=2*len(encoded.encode()))
        conn.execute("INSERT INTO execution_handoffs VALUES (?,?,?,?,?,?,?,?,?,?,'CHILD_READY',?,?)",
                     (handoff_id, writer.operation_id, writer.attempt_id, writer.session, writer.epoch,
                      task["parent_id"] if task else None, task["id"] if task else None,
                      task["unit_id"] if task else "", slot["version"], encoded, now, now))
        response._execution_handoff_id = handoff_id


def parent_finalize(method):
    @wraps(method)
    def wrapped(runtime, state, response):
        operations = getattr(runtime, "execution_operations", None)
        if operations is None:
            return method(runtime, state, response)
        store = operations.authority
        with runtime.a2_runtime._lock(state.session_id), store.transaction() as conn:
            result = method(runtime, state, response)
            handoff_id = getattr(response, "_execution_handoff_id", None)
            if handoff_id:
                writer = _WRITER.get()
                writer.validate(conn, store, session_key(state.session_id), writer.epoch, store.clock(conn))
                row = conn.execute("SELECT * FROM execution_handoffs WHERE id=? AND attempt_id=?", (handoff_id, writer.attempt_id)).fetchone()
                child = conn.execute("SELECT version FROM execution_states WHERE session=? AND kind='child'", (writer.session,)).fetchone()
                if row is None or child is None or row["child_version"] != child[0]:
                    raise ExecutionError("EXECUTION_STALE")
                conn.execute("UPDATE execution_handoffs SET status='PARENT_APPLIED',updated=? WHERE id=?", (store.clock(conn), handoff_id))
            return result
    return wrapped

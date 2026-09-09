"""Per-unit validation receipts, independent of the parent batch's final save."""
import json
from uuid import uuid4

from tiku_agent.execution_store import ExecutionError, _WRITER, canonical, digest


def create_unit_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_unit_batches (
        operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id),
        parent_record_id TEXT NOT NULL REFERENCES execution_tasks(id),
        requested TEXT NOT NULL, status TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_unit_checks (
        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES execution_operations(id),
        attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
        parent_record_id TEXT NOT NULL REFERENCES execution_tasks(id), unit_id TEXT NOT NULL,
        input_version TEXT NOT NULL, status TEXT NOT NULL, result TEXT, updated REAL NOT NULL,
        UNIQUE(operation_id,unit_id))""")


def start_batch(runtime, state, requested):
    operations = getattr(runtime, "execution_operations", None)
    if operations is None:
        return
    writer = _WRITER.get()
    with operations.authority.transaction() as conn:
        writer.validate(conn, operations.authority, writer.session, writer.epoch, operations.authority.clock(conn))
        parent = conn.execute("SELECT id FROM execution_tasks WHERE session=? AND epoch=? AND kind='workflow' AND task_id=? AND task_revision=?",
            (writer.session, writer.epoch, state.workflow_search_id or state.current_search_id, state.task_revision)).fetchone()
        if parent is None:
            raise ExecutionError("EXECUTION_STALE")
        conn.execute("INSERT INTO execution_unit_batches VALUES (?,?,?,'RUNNING')",
                     (writer.operation_id, parent["id"], canonical(list(requested))))


def finish_batch(runtime):
    operations = getattr(runtime, "execution_operations", None)
    if operations is None:
        return
    writer = _WRITER.get()
    with operations.authority.transaction() as conn:
        writer.validate(conn, operations.authority, writer.session, writer.epoch, operations.authority.clock(conn))
        if conn.execute("SELECT 1 FROM execution_unit_checks WHERE operation_id=? AND status<>'CONFIRMED'", (writer.operation_id,)).fetchone():
            raise ExecutionError("EXECUTION_UNKNOWN")
        conn.execute("UPDATE execution_unit_batches SET status='CONFIRMED' WHERE operation_id=?", (writer.operation_id,))


def input_version(runtime, state, unit_id):
    from tiku_agent.execution_runtime import file_digest
    from tiku_agent.execution_versions import component_version
    record = state.auto_crops.get(unit_id) or {}
    return digest({"producer":runtime.execution_operations.current_producer,
        "verifier":component_version(runtime.crop_verifier),
        "load_screen":component_version(runtime.external_load_screen),
        "page":file_digest(state.source_page_path), "crop":file_digest(record["path"]),
        "bounds":record.get("bounds"), "unit":state.unit(unit_id),
        "understanding":state.page_understanding})


def reusable(runtime, state, unit_id):
    if getattr(runtime, "execution_operations", None) is None:
        return True
    record = state.auto_crops.get(unit_id) or {}
    try:
        return record.get("execution_validation_version") == input_version(runtime, state, unit_id)
    except (OSError, KeyError, TypeError, ExecutionError):
        return False


def validate_unit(runtime, state, unit_id, function):
    operations = getattr(runtime, "execution_operations", None)
    if operations is None:
        return function()
    writer = _WRITER.get()
    store = operations.authority
    version = input_version(runtime, state, unit_id)
    identifier = uuid4().hex
    with store.transaction() as conn:
        now = store.clock(conn)
        writer.validate(conn, store, writer.session, writer.epoch, now)
        parent = conn.execute("SELECT id FROM execution_tasks WHERE session=? AND epoch=? AND kind='workflow' AND task_id=? AND task_revision=?",
            (writer.session, writer.epoch, state.workflow_search_id or state.current_search_id, state.task_revision)).fetchone()
        if parent is None:
            raise ExecutionError("EXECUTION_STALE")
        store.capacity(conn, "execution_unit_checks", store.policy.max_operations * 10)
        conn.execute("INSERT INTO execution_unit_checks VALUES (?,?,?,?,?,?, 'RUNNING',NULL,?)",
            (identifier, writer.operation_id, writer.attempt_id, parent["id"], unit_id, version, now))
    try:
        result = function()
    except Exception as exc:
        # This is a completed local outcome, not evidence that an external
        # timeout was free. The operation-wide effect gate still owns that.
        result = {"validation_status":"manual_required", "external_load_status":"error",
                  "verification_checks":{}, "error_type":type(exc).__name__}
    if version != input_version(runtime, state, unit_id):
        raise ExecutionError("EXECUTION_STALE")
    result = {**result, "execution_validation_version":version}
    encoded = canonical(result)
    if len(encoded.encode()) > store.policy.max_result_bytes:
        raise ExecutionError("EXECUTION_CAPACITY")
    with store.transaction() as conn:
        now = store.clock(conn)
        writer.validate(conn, store, writer.session, writer.epoch, now)
        store.storage_capacity(extra=2*len(encoded.encode()))
        conn.execute("UPDATE execution_unit_checks SET result=?,status='CONFIRMED',updated=? WHERE id=? AND status='RUNNING'",
                     (encoded, now, identifier))
    return result


def recover_preparation(runtime, sid, source, conn, writer):
    """Apply known per-unit outcomes; missing never-started units stay manual."""
    from tiku_agent.a3_runtime import _response, A3_PHASE_WAIT_SELECTION, A3_PHASE_AUTO_VALIDATING
    state = runtime.store.load(sid)
    batch = conn.execute("SELECT b.*,t.task_id,t.task_revision FROM execution_unit_batches b JOIN execution_tasks t ON t.id=b.parent_record_id WHERE b.operation_id=?", (source["id"],)).fetchone()
    if batch is None:
        raise ExecutionError("EXECUTION_RECOVERY_INVALID")
    requested = json.loads(batch["requested"])
    if (state is None or state.page_finished or state.selected_unit_id
            or state.phase not in {A3_PHASE_AUTO_VALIDATING, A3_PHASE_WAIT_SELECTION}
            or state.task_revision != batch["task_revision"]
            or (state.workflow_search_id or state.current_search_id) != batch["task_id"]
            or type(requested) is not list or not requested or state.requested_unit_ids != requested):
        raise ExecutionError("EXECUTION_RECOVERY_INVALID")
    rows = conn.execute("SELECT u.*, t.task_id,t.task_revision FROM execution_unit_checks u JOIN execution_tasks t ON t.id=u.parent_record_id WHERE u.operation_id=?", (source["id"],)).fetchall()
    if any(row["status"] != "CONFIRMED" for row in rows):
        raise ExecutionError("EXECUTION_UNKNOWN")
    for row in rows:
        if (row["unit_id"] not in requested or row["task_revision"] != state.task_revision
                or row["task_id"] != (state.workflow_search_id or state.current_search_id)
                or row["input_version"] != input_version(runtime, state, row["unit_id"])):
            raise ExecutionError("EXECUTION_STALE")
        state.auto_crops[row["unit_id"]].update(json.loads(row["result"]))
    for unit_id in requested:
        record = state.auto_crops.setdefault(unit_id, {})
        if record.get("validation_status") == "auto_ready" and reusable(runtime, state, unit_id):
            continue
        record["validation_status"] = "manual_required"
    state.phase = A3_PHASE_WAIT_SELECTION
    runtime.store.save(state)
    conn.execute("UPDATE execution_unit_batches SET status='CONFIRMED' WHERE operation_id=?", (source["id"],))
    ready = sum(state.auto_crops.get(unit_id, {}).get("validation_status") == "auto_ready" for unit_id in requested)
    return _response(f"已恢复本页准备结果：{ready} 道可以直接检索，其余题目可重新准备或人工裁剪。", state,
                     intent="a3_units_prepared")

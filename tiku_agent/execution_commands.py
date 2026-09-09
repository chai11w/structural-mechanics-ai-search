"""Explicit local control/reconciliation: one SQLite transaction, no model calls.

These commands use the unified database authority rather than waiting for a
Python session lock held by an in-flight provider call. Old writers are fenced
before the command commits. No mutable in-memory agent state is reused.
"""
import json

from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_store import ExecutionError, _WRITER, canonical


COMMANDS = frozenset({"clear", "control_execution", "recover_operation"})


def command_snapshot(runtime, sid, *, capabilities=None, response_frozen=False):
    """Read persisted parent/child/context atomically without a provider lock."""
    operations = getattr(runtime, "execution_operations", None)
    if operations is None:
        raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
    with operations.authority.transaction():
        return _snapshot(runtime, sid, capabilities, response_frozen)


def _snapshot(runtime, sid, capabilities, response_frozen):
    if hasattr(runtime, "a2_runtime"):
        parent = runtime.store.load(sid)
        child = runtime.a2_runtime.store.load(sid)
        from tiku_agent.task_state_builder import READ_OK, READ_MISSING
        snapshot = runtime._response_snapshot_v1_from_read_set(
            sid, workflow_state=parent, workflow_read_status=READ_OK if parent else READ_MISSING,
            child_state=child, child_read_status=READ_OK if child else READ_MISSING,
            capabilities=capabilities, response_frozen=response_frozen)
    else:
        snapshot = runtime._response_snapshot_v1_locked(sid, capabilities=capabilities, response_frozen=response_frozen)
    from tiku_agent.execution_runtime import bind_snapshot_context
    return bind_snapshot_context(runtime, sid, snapshot)


def _capture(runtime, sid, response, capabilities):
    snapshot = command_snapshot(runtime, sid, capabilities=capabilities, response_frozen=True)
    response.response_snapshot = dict(snapshot.legacy_session)
    response.response_projection_snapshot = dict(snapshot.legacy_session)
    response.response_task_state_snapshot = snapshot.task_state
    response.response_media_snapshot_captured = True
    response.uploaded_image_path = snapshot.uploaded_image_path
    response.submitted_crop_path = snapshot.submitted_crop_path
    response.feedback_overlay_path = snapshot.feedback_overlay_path
    return response


def _control(runtime, sid, scope, target):
    from tiku_agent.agent import AgentResponse
    from tiku_shared.request_protocol import RequestProtocol
    child_runtime = getattr(runtime, "a2_runtime", runtime)
    child = child_runtime.store.load(sid)
    parent = runtime.store.load(sid) if hasattr(runtime, "a2_runtime") else None
    if type(scope) is not str or scope not in {"child", "workflow"} or type(target) is not dict:
        raise ExecutionError("EXECUTION_CONTROL_INVALID")
    if type(target.get("task_revision")) is not int or any(
            type(value) is not str for key, value in target.items() if key != "task_revision"):
        raise ExecutionError("EXECUTION_CONTROL_INVALID")
    if parent is not None and parent.entry_route == "A3":
        expected = {"workflow_id": parent.workflow_search_id or parent.current_search_id,
                    "task_revision": parent.task_revision}
        if scope == "child":
            expected["unit_id"] = parent.selected_unit_id
            if not parent.selected_unit_id or parent.page_finished:
                raise ExecutionError("EXECUTION_STALE")
    elif scope == "child" and child is not None:
        expected = {"task_id": child.current_search_id, "task_revision": child.task_revision}
    else:
        raise ExecutionError("EXECUTION_STALE")
    if target != expected:
        raise ExecutionError("EXECUTION_STALE")
    child_runtime.store.clear(sid)
    if parent is not None:
        parent.selected_unit_id = ""
        parent.crop_review_required = False
        parent.crop_review_code = ""
        parent.crop_review_feedback = ""
        if scope == "workflow":
            parent.page_finished = True
            parent.phase = "COMPLETE"
        elif parent.entry_route == "A3":
            parent.phase = "WAIT_UNIT_SELECTION" if parent.remaining_units else "COMPLETE"
        else:
            runtime.store.clear(sid)
            parent = None
        if parent is not None:
            runtime.store.save(parent)
    return AgentResponse(text="已停止当前题，其余题目保留。" if scope == "child" else "已结束本页任务。",
                         intent="a3_current_unit_cancelled" if scope == "child" else "a3_page_finished",
                         protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict())


def _recover(runtime, sid, source_id, conn, operations, writer):
    from tiku_agent.execution_runtime import decode_response
    source = conn.execute("SELECT * FROM execution_operations WHERE id=? AND session=? AND epoch=?",
                          (source_id, writer.session, writer.epoch)).fetchone()
    current = conn.execute("SELECT identity FROM execution_operations WHERE id=?", (writer.operation_id,)).fetchone()
    if (source is None or source["status"] != "UNKNOWN" or source["producer"] != operations.producer
            or current is None or source["identity"] != current["identity"]):
        raise ExecutionError("EXECUTION_RECOVERY_INVALID")
    if conn.execute("SELECT 1 FROM execution_effects WHERE operation_id=? AND status<>'CONFIRMED' LIMIT 1", (source_id,)).fetchone():
        raise ExecutionError("EXECUTION_UNKNOWN")
    if conn.execute("SELECT 1 FROM execution_files WHERE operation_id=? AND status<>'PUBLISHED' LIMIT 1", (source_id,)).fetchone():
        raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
    handoff = conn.execute("SELECT * FROM execution_handoffs WHERE operation_id=? ORDER BY rowid DESC LIMIT 1", (source_id,)).fetchone()
    child = conn.execute("SELECT version,payload FROM execution_states WHERE session=? AND epoch=? AND kind='child'", (writer.session, writer.epoch)).fetchone()
    if handoff is None or child is None or child["payload"] is None or handoff["child_version"] != child["version"]:
        raise ExecutionError("EXECUTION_RECOVERY_INVALID")
    response = decode_response(json.loads(handoff["result"]))
    if response is None:
        raise ExecutionError("EXECUTION_RECOVERY_INVALID")
    if handoff["parent_record_id"]:
        if not hasattr(runtime, "a2_runtime"):
            raise ExecutionError("EXECUTION_RECOVERY_INVALID")
        parent = runtime.store.load(sid)
        record = conn.execute("SELECT * FROM execution_tasks WHERE id=?", (handoff["parent_record_id"],)).fetchone()
        if (parent is None or record is None or parent.task_revision != record["task_revision"]
                or (parent.workflow_search_id or parent.current_search_id) != record["task_id"]
                or parent.selected_unit_id != handoff["unit_id"] or parent.page_finished):
            raise ExecutionError("EXECUTION_STALE")
        if parent.entry_route == "A3":
            # The ordinary adapter takes Python locks; the body only projects
            # the child and writes parent state. Here the whole read/write set
            # is already protected by this command's exclusive transaction.
            from tiku_agent.a3_runtime import A3MvpRuntime
            response = A3MvpRuntime._after_a2_response.__wrapped__(runtime, parent, response)
    return response


def run_command(runtime, sid, kind, inputs, *, operation_request=None, identity_key="", capabilities=None):
    from tiku_agent.execution_runtime import _REQUEST, decode_response, encode_response, execution_message
    from tiku_agent.session_runtime import AgentProtocolError
    operations = getattr(runtime, "execution_operations", None)
    context = _REQUEST.get()
    identity = context[1] if context else identity_key or "local"
    try:
        if operations is None or kind not in COMMANDS:
            raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
        if kind == "recover_operation" and (type(inputs.get("source_operation_id")) is not str
                or not 1 <= len(inputs["source_operation_id"]) <= 128):
            raise ExecutionError("EXECUTION_RECOVERY_INVALID")
        request = OperationRequest.parse(operation_request if operation_request is not None else context[0] if context else None)
        with operations.authority.transaction() as conn:
            row = operations.register(sid, identity, request, kind, inputs)
            if row["status"] == "SUCCEEDED":
                response = decode_response(json.loads(row["result"]))
                if response is not None:
                    response.execution_receipt = {"operation_id":row["id"], "status":"SUCCEEDED", "replayed":True}
                return response
            writer = operations.claim(row["id"])
            token = _WRITER.set(writer)
            try:
                if kind == "clear":
                    response = None  # finish rotates epoch and clears both slots atomically
                elif kind == "control_execution":
                    response = _control(runtime, sid, inputs.get("scope"), inputs.get("target"))
                else:
                    response = _recover(runtime, sid, inputs.get("source_operation_id"), conn, operations, writer)
                if response is not None:
                    _capture(runtime, sid, response, capabilities)
                encoded = encode_response(response)
                final_context = operations.finish(writer, encoded, reset=kind == "clear")
                if kind == "recover_operation":
                    # Reconciliation completes the original logical operation;
                    # it does not fabricate another original execution attempt.
                    source_id = inputs["source_operation_id"]
                    result = canonical({**encoded, "context":final_context})
                    conn.execute("UPDATE execution_operations SET status='SUCCEEDED',result=?,result_bytes=?,updated=? WHERE id=?",
                                 (result, len(result.encode()), operations.authority.clock(conn), source_id))
                    conn.execute("UPDATE execution_attempts SET status='RECOVERED',updated=? WHERE operation_id=? AND status='UNKNOWN'",
                                 (operations.authority.clock(conn), source_id))
                    conn.execute("UPDATE execution_handoffs SET status='COMMITTED',updated=? WHERE operation_id=?",
                                 (operations.authority.clock(conn), source_id))
                if response is not None:
                    response.execution_context = final_context
                    response.execution_receipt = {"operation_id":row["id"], "attempt_id":writer.attempt_id, "status":"SUCCEEDED", "replayed":False}
                return response
            finally:
                _WRITER.reset(token)
    except ExecutionError as exc:
        raise AgentProtocolError(execution_message(exc.code), code=exc.code) from exc

"""Shared phase 5 boundary for HTTP, A3 and direct A2 callers."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import dataclasses
from functools import wraps
import hashlib
import inspect
import json
from pathlib import Path
import threading

from tiku_agent.execution_operations import OperationRequest, OperationStore
from tiku_agent.execution_store import ExecutionError, ExecutionSessionStore, _WRITER, canonical, session_key


OPERATION_HEADER = "X-Tiku-Operation"
MUTATION_PATHS = frozenset({"/api/image", "/api/image/stream", "/api/message", "/api/message/stream",
    "/api/a3/select", "/api/a3/select/stream", "/api/a3/prepare/stream", "/api/a3/crop/stream", "/api/reset",
    "/api/execution/control", "/api/execution/recover"})
_REQUEST: ContextVar[tuple[object, str] | None] = ContextVar("execution_request", default=None)
_DELIVERY_CONTEXT: ContextVar[object] = ContextVar("execution_delivery_context", default=None)


@contextmanager
def operation_request_scope(request, identity):
    token = _REQUEST.set((request, identity or "local"))
    delivery = _DELIVERY_CONTEXT.set(None)
    try:
        yield
    finally:
        _REQUEST.reset(token)
        _DELIVERY_CONTEXT.reset(delivery)


def file_digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def execution_snapshot_scope(runtime):
    operations = getattr(runtime,"execution_operations",None)
    return operations.authority.transaction() if operations is not None else nullcontext()


def bind_snapshot_context(runtime, sid, snapshot):
    operations = getattr(runtime,"execution_operations",None)
    if operations is None:
        return snapshot
    return dataclasses.replace(snapshot,execution_context=operations.authority.context(sid))


def execution_snapshot_locked(method):
    @wraps(method)
    def wrapped(runtime,sid,*args,**kwargs):
        # Caller already owns all required runtime locks before taking SQLite.
        with execution_snapshot_scope(runtime):
            return bind_snapshot_context(runtime,sid,method(runtime,sid,*args,**kwargs))
    return wrapped


def _input(value):
    if isinstance(value, Path):
        return {"content_sha256":file_digest(value)}
    if isinstance(value, dict):
        return {key:_input(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [_input(item) for item in value]
    return value


def _pack(value):
    if dataclasses.is_dataclass(value):
        return {"type":type(value).__name__, "fields":{f.name:_pack(getattr(value,f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value,tuple):
        return {"tuple":[_pack(v) for v in value]}
    if isinstance(value,dict):
        return {k:_pack(v) for k,v in value.items()}
    if isinstance(value,list):
        return [_pack(v) for v in value]
    return value


def _unpack(value):
    from tiku_agent import task_state_contract as contract
    allowed = {cls.__name__:cls for cls in (contract.TaskStateSnapshotV1,contract.WorkflowStateView,
        contract.ChildTaskStateView,contract.UnitStateView,contract.ConsistencyView)}
    if isinstance(value,list):
        return [_unpack(v) for v in value]
    if isinstance(value,dict):
        if set(value)=={"tuple"}:
            return tuple(_unpack(v) for v in value["tuple"])
        if set(value)=={"type","fields"}:
            if value["type"] not in allowed:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            return allowed[value["type"]](**{k:_unpack(v) for k,v in value["fields"].items()})
        return {k:_unpack(v) for k,v in value.items()}
    return value


def encode_response(response):
    if response is None:
        return {"schema":1,"response":None}
    if type(response) is bool:
        return {"schema":1,"primitive":response}
    from tiku_agent.fastapi_demo import _public_session_snapshot
    from tiku_agent.session_runtime import SessionResponseSnapshotV1
    if type(response) is SessionResponseSnapshotV1:
        snapshot={"legacy_session":_public_session_snapshot(response.legacy_session),"task_state":_pack(response.task_state)}
        for name in ("uploaded_image_path","submitted_crop_path","feedback_overlay_path"):
            value=getattr(response,name)
            snapshot[name]=str(value) if value else None
        return {"schema":1,"snapshot":snapshot}
    # Do not duplicate AgentState (intent/model raw/analysis) in receipts.
    state_fields = {"phase","current_search_id","task_revision","candidate_revision","candidate_generation",
                    "selected_rank","current_chapter","_a3_media_guard"}
    state = {k:v for k,v in response.state.items() if k in state_fields}
    for key in ("candidates","last_answer_paths"):
        if isinstance(response.state.get(key),list):
            state[key] = [None] * len(response.state[key])
    body = {"text":response.text,"images":list(response.images),"state":state,"intent":response.intent,
            "protocol":dict(response.protocol),"media_kind":response.media_kind,
            "author_contact":dict(response.author_contact),
            "response_snapshot":_public_session_snapshot(response.response_snapshot) if response.response_snapshot else {},
            "response_projection_snapshot":_public_session_snapshot(response.response_projection_snapshot) if response.response_projection_snapshot else {},
            "response_task_state_snapshot":_pack(response.response_task_state_snapshot),
            "response_media_snapshot_captured":response.response_media_snapshot_captured}
    refs = {}
    for field in ("uploaded_image_path","submitted_crop_path","feedback_overlay_path"):
        value = getattr(response,field,None)
        body[field] = str(value) if value else None
    for value in [*response.images,body["uploaded_image_path"],body["submitted_crop_path"],body["feedback_overlay_path"]]:
        if value:
            path = Path(value)
            refs[str(path)] = file_digest(path) if path.is_file() else None
    return {"schema":1,"response":body,"files":refs}


def decode_response(payload):
    from tiku_agent.agent import AgentResponse
    if payload.get("schema") != 1:
        raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
    if "primitive" in payload:
        if type(payload["primitive"]) is not bool:
            raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
        return payload["primitive"]
    if "snapshot" in payload:
        from tiku_agent.session_runtime import SessionResponseSnapshotV1
        fields=dict(payload["snapshot"])
        fields["task_state"]=_unpack(fields["task_state"])
        fields["execution_context"]=payload.get("context")
        for name in ("uploaded_image_path","submitted_crop_path","feedback_overlay_path"):
            fields[name]=Path(fields[name]) if fields[name] else None
        return SessionResponseSnapshotV1(**fields)
    if payload["response"] is None:
        return None
    for path, expected in payload.get("files",{}).items():
        if expected is None or not Path(path).is_file() or file_digest(path)!=expected:
            raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
    fields = dict(payload["response"])
    fields["response_task_state_snapshot"] = _unpack(fields["response_task_state_snapshot"])
    for name in ("uploaded_image_path","submitted_crop_path","feedback_overlay_path"):
        fields[name] = Path(fields[name]) if fields[name] else None
    response = AgentResponse(**fields)
    response.execution_context = payload.get("context")
    return response


def execution_entry(method):
    """Opt-in boundary. Internal A3→A2 calls inherit the same fenced attempt."""
    signature = inspect.signature(method)
    @wraps(method)
    def wrapped(runtime, session_id, *args, **kwargs):
        operations = getattr(runtime,"execution_operations",None)
        explicit = kwargs.pop("operation_request",None)
        if operations is None:
            if isinstance(getattr(runtime,"store",None),ExecutionSessionStore):
                raise ExecutionError("EXECUTION_CONTEXT_REQUIRED")
            if explicit is not None:
                raise ExecutionError("EXECUTION_DISABLED")
            return method(runtime,session_id,*args,**kwargs)
        current = _WRITER.get()
        store = operations.authority
        if current is not None:
            if current.authority != store.authority or current.session != session_key(session_id):
                raise ExecutionError("EXECUTION_STALE")
            operations.renew(current)
            return method(runtime,session_id,*args,**kwargs)
        request_context = _REQUEST.get()
        raw_request = explicit if explicit is not None else (request_context[0] if request_context else None)
        identity = request_context[1] if request_context else (kwargs.get("identity_key") or "local")
        if method.__name__ == "clear":
            from tiku_agent.execution_commands import run_command
            # Internal A3→A2 clear returned through the inherited-writer branch
            # above; only explicit outer resets rotate the conversation epoch.
            return run_command(runtime, session_id, "clear", {}, operation_request=raw_request,
                               identity_key=identity, capabilities=kwargs.get("task_state_capabilities"))
        try:
            request = OperationRequest.parse(raw_request)
            bound = signature.bind(runtime,session_id,*args,**kwargs)
            bound.apply_defaults()
            inputs = {k:v for k,v in bound.arguments.items() if k not in {"self","runtime","session_id","identity_key","request_id","progress","task_state_capabilities","capabilities"}}
            if "image_path" in inputs:
                inputs["image_path"] = Path(inputs["image_path"])
            row = operations.register(session_id,identity,request,method.__name__,_input(inputs))
            if row["status"] == "SUCCEEDED":
                response = decode_response(json.loads(row["result"]))
                if hasattr(response,"text"):
                    response.execution_receipt = {"operation_id":row["id"],"status":"SUCCEEDED","replayed":True}
                return response
            writer = operations.claim(row["id"])
            stop = threading.Event()
            def heartbeat():
                while not stop.wait(max(0.05,store.policy.lease_seconds / 3)):
                    try:
                        operations.renew(writer)
                    except Exception:
                        return
            worker = threading.Thread(target=heartbeat,daemon=True,name="tiku-execution-lease")
            token = _WRITER.set(writer)
            worker.start()
            try:
                from tiku_shared.model_costs import model_run_binding
                from tiku_shared.execution_hooks import execution_effect_scope
                from tiku_agent.execution_effects import ExecutionEffects
                ledgers = [getattr(target, "cost_ledger", None) for target in (runtime, getattr(runtime, "a2_runtime", None))]
                ledger_paths = [ledger.path for ledger in ledgers if ledger is not None and getattr(ledger, "path", None) is not None]
                artifact_roots = [target.artifacts.root for target in (runtime, getattr(runtime, "a2_runtime", None)) if target is not None and getattr(target, "artifacts", None) is not None]
                with model_run_binding(lambda run_id:operations.bind_cost_run(writer,run_id)), execution_effect_scope(ExecutionEffects(operations, writer, ledger_paths=ledger_paths, artifact_roots=artifact_roots)):
                    response = method(runtime,session_id,*args,**kwargs)
                    child_runtime=getattr(runtime,"a2_runtime",runtime)
                    await_background=getattr(child_runtime,"_await_background_image_work",None)
                    if callable(await_background):
                        await_background(session_id)
                encoded = encode_response(response)
                context = operations.finish(writer,encoded,reset=method.__name__=="clear")
                if hasattr(response,"text"):
                    response.execution_context = context
                    response.execution_receipt = {"operation_id":row["id"],"attempt_id":writer.attempt_id,"status":"SUCCEEDED","replayed":False}
                return response
            except BaseException as exc:
                try:
                    operations.fail(writer,known_not_started=type(exc).__name__ in {"_ExecutionCancelled","AgentRuntimeBusyError","AgentBudgetExceededError"})
                except Exception:
                    pass  # a persisted RUNNING record will become UNKNOWN; never replay it
                raise
            finally:
                stop.set()
                worker.join(timeout=1)
                _WRITER.reset(token)
        except ExecutionError as exc:
            from tiku_agent.session_runtime import AgentProtocolError
            raise AgentProtocolError(execution_message(exc.code),code=exc.code) from exc
    return wrapped


def execution_message(code):
    return {
        "EXECUTION_CONTEXT_REQUIRED":"请重新连接会话后再操作。",
        "EXECUTION_STALE":"任务已经变化，请重新连接后使用当前操作。",
        "EXECUTION_INPUT_CONFLICT":"同一次操作的内容发生变化，请重新确认后提交。",
        "EXECUTION_BUSY":"该会话已有操作正在处理，请重新连接查看进度。",
        "EXECUTION_UNKNOWN":"上次操作结果尚未确认，请先核对进度，暂不重复执行。",
        "EXECUTION_RESULT_UNAVAILABLE":"该操作已有记录，但保存的结果已不可用，请先核对进度。",
        "EXECUTION_CAPACITY":"执行记录暂时无法接收新操作，请稍后重新连接。",
        "EXECUTION_COST_PENDING":"已有调用的费用尚待核对，当前结果保留，暂不启动新的操作。",
        "EXECUTION_COST_CONFLICT":"费用记录存在不一致，需要核对后再继续。",
        "EXECUTION_CONTROL_INVALID":"停止范围无效，请从当前任务重新选择。",
        "EXECUTION_RECOVERY_INVALID":"现有记录不足以安全恢复，请先核对当前任务。",
    }.get(code,"执行状态暂时无法确认，请重新连接后核对进度。")


def delivery_execution_entry(method):
    guarded = execution_entry(method)
    @wraps(method)
    def wrapped(runtime,sid,*args,**kwargs):
        operations=getattr(runtime,"execution_operations",None)
        context=_REQUEST.get()
        if operations is None or _WRITER.get() is not None:
            return guarded(runtime,sid,*args,**kwargs)
        if context is None:
            # Direct callers must supply an explicit operation envelope.
            return guarded(runtime,sid,*args,**kwargs)
        original=OperationRequest.parse(context[0])
        current=operations.authority.context(sid)
        if current["epoch"] != original.epoch:
            raise ExecutionError("EXECUTION_STALE")
        key=hashlib.sha256((original.key+":"+method.__name__).encode()).hexdigest()
        request=OperationRequest(key,original.epoch,current["state_version"])
        old=operations.lookup(sid,context[1],request)
        if old is not None:
            request=OperationRequest(key,original.epoch,old["expected_version"])
        result=guarded(runtime,sid,*args,operation_request=request,**kwargs)
        row=operations.lookup(sid,context[1],request)
        _DELIVERY_CONTEXT.set(json.loads(row["result"])["context"])
        return result
    return wrapped


def delivery_context():
    return _DELIVERY_CONTEXT.get()


def attach_execution(runtime, authority):
    """Attach only to a new/migrated isolated runtime, never silently import."""
    from tiku_agent.a3_runtime import A3MvpRuntime
    operations = OperationStore(authority)
    runtimes = [(runtime,"workflow" if isinstance(runtime,A3MvpRuntime) else "child")]
    if isinstance(runtime,A3MvpRuntime):
        runtimes.append((runtime.a2_runtime,"child"))
    for target,kind in runtimes:
        old = target.store
        if not isinstance(old,ExecutionSessionStore):
            path = getattr(old,"database_path",None)
            if path and Path(path).is_file():
                import sqlite3
                from contextlib import closing
                table = "a3_sessions" if kind=="workflow" else "agent_sessions"
                with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)) as conn:
                    count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                with authority.transaction() as conn:
                    migrated = conn.execute("SELECT value FROM execution_meta WHERE key='migration'").fetchone()
                if count and (migrated is None or migrated[0]!="complete"):
                    raise ExecutionError("EXECUTION_MIGRATION_REQUIRED")
        elif old.authority.authority != authority.authority:
            raise ExecutionError("EXECUTION_AUTHORITY_MISMATCH")
    for target,kind in runtimes:
        target.store = ExecutionSessionStore(authority,kind)
        target.execution_operations = operations
    authority.require_writer = True
    with authority.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO execution_meta VALUES ('execution_enabled','1')")
    return runtime

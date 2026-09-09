"""Reviewed cleanup of retired execution records and scoped artifact orphans."""
from contextlib import closing
from collections import defaultdict
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3

from tiku_agent.execution_maintenance import (
    PLAN_TTL, backup_databases, bounded_limit, path_key, read_execution, seal_plan, trusted_now, verify_plan,
    ensure_maintenance_idle,
)
from tiku_agent.execution_store import ExecutionError, ExecutionPolicy, canonical


TABLES = ("execution_sessions", "execution_states", "execution_tasks", "execution_operations",
          "execution_attempts", "execution_owners", "execution_cost_runs", "execution_task_attempts",
          "execution_effects", "execution_collectors", "execution_cost_outbox", "execution_files",
          "execution_handoffs", "execution_unit_batches", "execution_unit_checks")
SCAN_LIMIT = 10000
HASH_LIMIT = 256 * 1024 * 1024


def execution_policy(conn):
    row = conn.execute("SELECT value FROM execution_meta WHERE key='execution_policy'").fetchone()
    return ExecutionPolicy(**json.loads(row[0])) if row else ExecutionPolicy()


def source_digest(conn):
    hasher = hashlib.sha256()
    for table in TABLES:
        hasher.update(table.encode())
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            hasher.update(canonical(list(row)).encode())
            hasher.update(b"\n")
    for row in conn.execute("SELECT key,value FROM execution_meta WHERE key<>'clock' ORDER BY key"):
        hasher.update(canonical(list(row)).encode())
    return hasher.hexdigest()


def _roots(conn, database, selected=None):
    row = conn.execute("SELECT value FROM execution_meta WHERE key='artifact_roots'").fetchone()
    approved = [Path(value) for value in json.loads(row[0])] if row else []
    roots = sorted({Path(value).absolute() for value in (approved if selected is None else selected)})
    runtime = Path(database).resolve().parent
    for root in roots:
        if (root == runtime or not root.is_relative_to(runtime) or root.resolve() != root or (root.exists() and not root.is_dir())
                or not any(root.is_relative_to(parent) for parent in approved)):
            raise ExecutionError("EXECUTION_MAINTENANCE_ROOT_INVALID")
        cursor = root
        while cursor != runtime:
            if cursor.is_symlink() or cursor.is_junction():
                raise ExecutionError("EXECUTION_MAINTENANCE_ROOT_INVALID")
            cursor = cursor.parent
    return [root for root in roots if not any(root != other and root.is_relative_to(other) for other in roots)]


def _location(value, roots):
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    path = Path(value).resolve()
    for root in roots:
        if path.is_relative_to(root):
            return (path_key(root), path.relative_to(root).as_posix())
    return None


def _references(value, roots, result):
    if isinstance(value, str):
        location = _location(value, roots)
        if location:
            result.add(location)
    elif isinstance(value, dict):
        for item in value.values():
            _references(item, roots, result)
    elif isinstance(value, list):
        for item in value:
            _references(item, roots, result)


def _files(roots):
    scanned = 0
    for root in roots:
        if not root.exists():
            continue
        for directory, directories, files in os.walk(root, followlinks=False):
            scanned += len(directories) + len(files)
            if scanned > SCAN_LIMIT:
                raise ExecutionError("EXECUTION_MAINTENANCE_SCAN_LIMIT")
            directories[:] = sorted(name for name in directories if not
                (Path(directory, name).is_symlink() or Path(directory, name).is_junction()))
            for name in sorted(files):
                path = Path(directory, name)
                if path.is_symlink() or path.is_junction() or not path.is_file():
                    continue
                yield root, path


def _file_record(root, path):
    from tiku_agent.execution_runtime import file_digest
    before = path.stat()
    checksum = file_digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
    return {"root":path_key(root), "path":path.relative_to(root).as_posix(), "bytes":after.st_size,
            "mtime_ns":after.st_mtime_ns, "ctime_ns":after.st_ctime_ns, "sha256":checksum}


def _cleanup_plan(conn, database, roots, limit, created, compact):
    policy = execution_policy(conn)
    cutoff = created - policy.history_ttl
    sessions = {row["session"]:dict(row) for row in conn.execute("SELECT * FROM execution_sessions")}
    operations = {row["id"]:dict(row) for row in conn.execute("SELECT * FROM execution_operations ORDER BY updated,id")}
    pending_costs = {row[0] for row in conn.execute("SELECT DISTINCT e.operation_id FROM execution_effects e LEFT JOIN execution_cost_outbox c ON c.run_id=e.run_id WHERE e.status NOT IN ('CONFIRMED','NOT_SENT') OR (e.status='CONFIRMED' AND e.usage_known=0) OR c.status IS NULL OR c.status<>'CONFIRMED'")}
    pending_costs.update(row[0] for row in conn.execute("SELECT r.operation_id FROM execution_cost_runs r JOIN execution_cost_outbox c ON c.run_id=r.run_id WHERE c.status<>'CONFIRMED'"))
    eligible = {key for key,op in operations.items() if op["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
        and op["updated"] < cutoff and key not in pending_costs and
        (op["session"] not in sessions or sessions[op["session"]]["epoch"] != op["epoch"] or sessions[op["session"]]["expires"] <= created)}
    protected = set()
    unresolved_epochs = {(op["session"], op["epoch"]) for key, op in operations.items()
                         if key in pending_costs or op["status"] in {"REGISTERED", "RUNNING", "UNKNOWN"}}
    for row in conn.execute("SELECT * FROM execution_states WHERE payload IS NOT NULL"):
        session = sessions.get(row["session"])
        if ((session and session["epoch"] == row["epoch"] and session["expires"] > created)
                or (row["session"], row["epoch"]) in unresolved_epochs):
            _references(json.loads(row["payload"]), roots, protected)
    for key, op in operations.items():
        if key not in eligible and op["result"]:
            _references(json.loads(op["result"]), roots, protected)
    for row in conn.execute("SELECT operation_id,result FROM execution_handoffs"):
        if row["operation_id"] not in eligible:
            _references(json.loads(row["result"]), roots, protected)
    records = [dict(row) for row in conn.execute("SELECT * FROM execution_files")]
    records_by_operation = defaultdict(list)
    for row in records:
        records_by_operation[row["operation_id"]].append(row)
        if row["operation_id"] not in eligible:
            for field in ("path", "temporary_path"):
                location = _location(row[field], roots)
                if location:
                    protected.add(location)
    files, hashed = [], 0
    tracked_locations = {_location(row[field], roots) for row in records for field in ("path", "temporary_path")}
    for root, path in _files(roots):
        location = (path_key(root), path.relative_to(root).as_posix())
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".json", ".jsonl", ".log", ".key", ".toml"}:
            continue
        if (location not in tracked_locations and path.suffix.lower() not in
                {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".pdf"}
                and not re.fullmatch(r"\.part-[0-9a-f]{16}", path.name)):
            continue
        stat = path.stat()
        if location in protected or max(stat.st_mtime, stat.st_ctime) >= cutoff:
            continue
        if hashed + stat.st_size > HASH_LIMIT or len(files) >= limit:
            break
        files.append(_file_record(root, path))
        hashed += stat.st_size
    selected_files = {(item["root"], item["path"]) for item in files}
    record_ids = []
    for row in records:
        if row["operation_id"] not in eligible:
            continue
        locations = [_location(row[field], roots) for field in ("path", "temporary_path")]
        if all(location is not None and location not in protected and
               (not Path(row[field]).exists() or location in selected_files)
               for field, location in zip(("path", "temporary_path"), locations)):
            record_ids.append(row["id"])
    selected_records = set(record_ids)
    removable_ops = [key for key in operations if key in eligible and all(
        row["id"] in selected_records for row in records_by_operation[key])][:limit]
    removed_ops = set(removable_ops)
    # Metadata rows can only disappear with their exact reviewed operation.
    record_ids = [row["id"] for row in records if row["id"] in selected_records and row["operation_id"] in removed_ops]
    # Build references after the reviewed operation deletions, including the
    # new unit receipts. No inference from a parent/child display label.
    task_refs = set()
    for table, fields in (("execution_handoffs", ("parent_record_id", "child_record_id")),
                          ("execution_unit_batches", ("parent_record_id",)), ("execution_unit_checks", ("parent_record_id",))):
        for row in conn.execute(f"SELECT * FROM {table}"):
            if row["operation_id"] not in removed_ops:
                task_refs.update(row[field] for field in fields if row[field])
    tasks = [dict(row) for row in conn.execute("SELECT * FROM execution_tasks ORDER BY kind,updated,id")]
    task_children = defaultdict(set)
    for task in tasks:
        if task["parent_id"]:
            task_children[task["parent_id"]].add(task["id"])
    remaining_operation_sessions = {op["session"] for op in operations.values() if op["id"] not in removed_ops}
    removed_tasks = []
    removed_task_ids = set()
    for task in tasks:
        session = sessions.get(task["session"])
        if (len(removed_tasks) >= limit or task["id"] in task_refs or task["updated"] >= cutoff
                or (session and session["epoch"] == task["epoch"] and session["expires"] > created)
                or task["session"] in remaining_operation_sessions
                or task_children[task["id"]] - removed_task_ids):
            continue
        removed_tasks.append(task["id"])
        removed_task_ids.add(task["id"])
    remaining_task_sessions = {task["session"] for task in tasks if task["id"] not in removed_task_ids}
    removed_sessions = [key for key, session in sessions.items() if session["expires"] < cutoff and
        key not in remaining_operation_sessions and key not in remaining_task_sessions][:limit]
    return seal_plan({"schema":1, "action":"cleanup", "database":path_key(database), "created_at":created,
        "expires_at":created + PLAN_TTL, "limit":limit, "roots":[path_key(root) for root in roots],
        "source_digest":source_digest(conn), "policy":vars(policy), "compact":bool(compact),
        "mark_unknown":[key for key, op in operations.items() if op["status"] == "RUNNING" and op["lease_until"] <= created][:limit],
        "files":files, "file_records":record_ids, "operations":removable_ops,
        "tasks":removed_tasks, "sessions":removed_sessions, "protected_file_references":len(protected)})


def plan_cleanup(database, *, roots=None, limit=100, now=None, compact=False):
    bounded_limit(limit)
    with read_execution(database) as conn:
        return _cleanup_plan(conn, database, _roots(conn, database, roots), limit, trusted_now(conn, now), compact)


def _planned_path(item, root_map):
    root = root_map.get(item["root"])
    if root is None:
        raise ExecutionError("EXECUTION_MAINTENANCE_ROOT_INVALID")
    path = root / item["path"]
    if path.resolve() != path or not path.is_relative_to(root) or path == root:
        raise ExecutionError("EXECUTION_MAINTENANCE_ROOT_INVALID")
    cursor = path
    while cursor != root:
        if cursor.is_symlink() or cursor.is_junction():
            raise ExecutionError("EXECUTION_MAINTENANCE_ROOT_INVALID")
        cursor = cursor.parent
    return root, path


def apply_cleanup(operations, plan, *, backup_dir, roots=None):
    from tiku_agent.execution_runtime import file_digest
    store = operations.authority
    with store.transaction() as conn:
        now = trusted_now(conn, store.now())
        verify_plan(plan, "cleanup", store.path, now)
        ensure_maintenance_idle(conn, now)
        selected_roots = _roots(conn, store.path, roots)
        actual = _cleanup_plan(conn, store.path, selected_roots, plan["limit"], plan["created_at"], plan["compact"])
        if actual != plan:
            raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
        backup = backup_databases(store.path, backup_dir, plan, extra_bytes=sum(item["bytes"] for item in plan["files"]))
        (backup / "result.json").write_text(canonical({"status":"PREPARED", "plan_hash":plan["plan_hash"],
            "requires_reinspection_after_interruption":True}), encoding="utf-8")
        root_map = {path_key(root):root for root in selected_roots}
        (backup / "files").mkdir()
        selected = []
        for index, item in enumerate(plan["files"]):
            root, path = _planned_path(item, root_map)
            if _file_record(root, path) != item:
                raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
            copy = backup / "files" / f"{index:04d}.bin"
            shutil.copy2(path, copy)
            if file_digest(copy) != item["sha256"]:
                raise ExecutionError("EXECUTION_MAINTENANCE_BACKUP_INVALID")
            selected.append((root, path, item))
        # All copies are verified before the first deletion. A crash leaves
        # the reviewed manifest and exact bytes for a new plan or restoration.
        for root, path, item in selected:
            if _file_record(root, path) != item:
                raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
            path.unlink()
        for file_id in plan["file_records"]:
            conn.execute("DELETE FROM execution_files WHERE id=?", (file_id,))
        for operation_id in plan["operations"]:
            operations._delete_operation_rows(conn, operation_id)
        for task_id in plan["tasks"]:
            conn.execute("DELETE FROM execution_tasks WHERE id=?", (task_id,))
        for session in plan["sessions"]:
            conn.execute("DELETE FROM execution_states WHERE session=?", (session,))
            conn.execute("DELETE FROM execution_owners WHERE session=?", (session,))
            conn.execute("DELETE FROM execution_sessions WHERE session=?", (session,))
        now = store.clock(conn)
        for operation_id in plan["mark_unknown"]:
            operations._unknown(conn, operation_id, now)
    result = {"plan_hash":plan["plan_hash"], "operations_marked_unknown":len(plan["mark_unknown"]),
              **{key + "_removed":len(plan[key]) for key in ("files", "operations", "tasks", "sessions")}}
    if plan["compact"]:
        try:
            with closing(sqlite3.connect(store.path, timeout=1)) as conn:
                conn.execute("VACUUM")
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["compaction"] = "RETRY_REQUIRED" if checkpoint[0] else "COMPLETE"
        except sqlite3.Error:
            result["compaction"] = "RETRY_REQUIRED"
    (backup / "result.json").write_text(canonical(result), encoding="utf-8")
    return result

"""Bounded, read-only diagnostics and reviewed local cost reconciliation."""
from contextlib import contextmanager, closing
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time

from tiku_agent.execution_store import ExecutionError, canonical, digest


PLAN_TTL = 900
REPOSITORY = Path(__file__).resolve().parents[1]


def path_key(path):
    return digest(str(Path(path).resolve()).casefold())


@contextmanager
def read_execution(database):
    path = Path(database).resolve()
    if not path.is_file():
        raise ExecutionError("EXECUTION_DATABASE_MISSING")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn


def trusted_now(conn, now=None):
    current = time.time() if now is None else now
    if isinstance(current, bool) or not isinstance(current, (float, int)) or not math.isfinite(current):
        raise ExecutionError("EXECUTION_CLOCK_INVALID")
    row = conn.execute("SELECT value FROM execution_meta WHERE key='clock'").fetchone()
    previous = float(row[0]) if row else current
    if not math.isfinite(previous) or current < previous - 300:
        raise ExecutionError("EXECUTION_CLOCK_INVALID")
    return max(current, previous)


def bounded_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ExecutionError("EXECUTION_MAINTENANCE_LIMIT")
    return limit


def ensure_maintenance_idle(conn, now):
    # Do not hold the authority across backups while a known live provider
    # call needs to confirm its result. Admission and this check share SQLite.
    if conn.execute("SELECT 1 FROM execution_operations WHERE status='RUNNING' AND lease_until>? LIMIT 1", (now,)).fetchone():
        raise ExecutionError("EXECUTION_BUSY")


def inspect_execution(database, *, limit=20):
    bounded_limit(limit)
    with read_execution(database) as conn:
        now = trusted_now(conn)
        statuses = {row[0]: row[1] for row in conn.execute(
            "SELECT CASE WHEN status='RUNNING' AND lease_until<=? THEN 'UNKNOWN' ELSE status END,count(*) FROM execution_operations GROUP BY 1", (now,))}
        pending = conn.execute("SELECT count(*) FROM execution_cost_runs r LEFT JOIN execution_cost_outbox c ON c.run_id=r.run_id WHERE c.status<>'CONFIRMED' OR (c.run_id IS NULL AND EXISTS (SELECT 1 FROM execution_effects e WHERE e.run_id=r.run_id))").fetchone()[0]
        recent = [{"operation_id":row["id"], "status":"UNKNOWN" if row["status"] == "RUNNING" and row["lease_until"] <= now else row["status"]}
                  for row in conn.execute("SELECT id,status,lease_until FROM execution_operations ORDER BY updated DESC,id LIMIT ?", (limit,))]
        result = {"schema":1, "operations":statuses, "pending_cost_runs":pending, "recent":recent,
            "sessions":conn.execute("SELECT count(*) FROM execution_sessions").fetchone()[0],
            "tasks":conn.execute("SELECT count(*) FROM execution_tasks").fetchone()[0],
            "files":conn.execute("SELECT count(*) FROM execution_files").fetchone()[0]}
    result["database_bytes"] = sum(path.stat().st_size for path in (Path(database), Path(str(database) + "-wal")) if path.is_file())
    return result


def _cost_item(conn, ledger, run_id, now):
    operation = conn.execute("SELECT o.* FROM execution_operations o JOIN execution_cost_runs r ON r.operation_id=o.id WHERE r.run_id=?", (run_id,)).fetchone()
    collector = conn.execute("SELECT * FROM execution_collectors WHERE run_id=?", (run_id,)).fetchone()
    outbox = conn.execute("SELECT * FROM execution_cost_outbox WHERE run_id=?", (run_id,)).fetchone()
    effects = [dict(row) for row in conn.execute("SELECT * FROM execution_effects WHERE run_id=? ORDER BY call_id", (run_id,))]
    source = {"operation":dict(operation) if operation else None,
              "collector":dict(collector) if collector else None, "outbox":dict(outbox) if outbox else None,
              "effects":effects}
    reason = "READY"
    if operation is None or collector is None:
        reason = "MISSING_EVIDENCE"
    elif operation["status"] == "RUNNING" and operation["lease_until"] > now:
        reason = "RUNNING"
    elif any(row["status"] != "CONFIRMED" or not row["usage_known"] or not row["record"] for row in effects):
        reason = "UNKNOWN_USAGE"
    elif outbox and outbox["status"] == "CONFIRMED":
        reason = "ALREADY_CONFIRMED"
    elif outbox and outbox["status"] == "CONFLICT":
        reason = "CONFLICT"
    else:
        metadata = json.loads(collector["metadata"])
        if path_key(ledger) not in metadata["ledger_keys"]:
            reason = "WRONG_LEDGER"
        elif outbox and (outbox["ledger_key"] != path_key(ledger) or digest(json.loads(outbox["payload"])) != outbox["fingerprint"]):
            reason = "CONFLICT"
    return {"run_id":run_id, "reason":reason, "calls":len(effects), "source_digest":digest(source)}


def seal_plan(plan):
    return {**plan, "plan_hash":digest(plan)}


def verify_plan(plan, action, database, now):
    if (type(plan) is not dict or plan.get("schema") != 1 or plan.get("action") != action
            or plan.get("database") != path_key(database)
            or type(plan.get("created_at")) not in (float, int)
            or not math.isfinite(plan["created_at"]) or plan["created_at"] > now
            or plan.get("expires_at") != plan["created_at"] + PLAN_TTL or plan["expires_at"] < now
            or plan.get("plan_hash") != digest({key:value for key,value in plan.items() if key != "plan_hash"})):
        raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")


def _cost_plan(conn, database, ledger, run_ids, limit, created):
    bounded_limit(limit)
    if run_ids is None:
        run_ids = [row[0] for row in conn.execute("SELECT r.run_id FROM execution_cost_runs r LEFT JOIN execution_cost_outbox c ON c.run_id=r.run_id WHERE c.status<>'CONFIRMED' OR (c.run_id IS NULL AND EXISTS (SELECT 1 FROM execution_effects e WHERE e.run_id=r.run_id)) ORDER BY r.run_id LIMIT ?", (limit,))]
    if (type(run_ids) is not list or len(run_ids) > limit
            or any(type(value) is not str or not 1 <= len(value) <= 128 for value in run_ids)
            or len(set(run_ids)) != len(run_ids)):
        raise ExecutionError("EXECUTION_MAINTENANCE_SELECTION")
    return seal_plan({"schema":1, "action":"reconcile_cost", "database":path_key(database), "ledger":path_key(ledger),
        "ledger_present":Path(ledger).is_file(),
        "created_at":created, "expires_at":created + PLAN_TTL, "limit":limit,
        "items":[_cost_item(conn, ledger, run_id, created) for run_id in run_ids]})


def plan_cost_reconciliation(database, ledger, *, run_ids=None, limit=20, now=None):
    with read_execution(database) as conn:
        return _cost_plan(conn, database, ledger, run_ids, limit, trusted_now(conn, now))


def backup_databases(database, backup_dir, plan, *, ledger=None, extra_bytes=0):
    backup = Path(backup_dir).resolve()
    runtime = Path(database).resolve().parent
    if (backup.is_relative_to(REPOSITORY) or backup.is_relative_to(runtime) or backup.exists()
            or any((parent / ".git").exists() for parent in backup.parents)):
        raise ExecutionError("EXECUTION_MAINTENANCE_BACKUP_INVALID")
    sources = {"execution":Path(database).resolve()}
    if ledger is not None:
        sources["costs"] = Path(ledger).resolve()
    available_parent = next(parent for parent in backup.parents if parent.exists())
    needed = sum(path.stat().st_size for path in sources.values() if path.is_file())
    if shutil.disk_usage(available_parent).free < 2*needed + extra_bytes + 256*1024*1024:
        raise ExecutionError("EXECUTION_CAPACITY")
    backup.mkdir(parents=True)
    for name, source in sources.items():
        if not source.is_file():
            continue
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as original:
            with closing(sqlite3.connect(backup / (name + ".sqlite3"))) as target:
                original.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ExecutionError("EXECUTION_MAINTENANCE_BACKUP_INVALID")
    (backup / "plan.json").write_text(canonical(plan), encoding="utf-8")
    return backup


def apply_cost_reconciliation(operations, ledger_path, plan, *, backup_dir):
    from tiku_agent.execution_effects import reconcile_cost, stage_unwritten_cost
    from tiku_shared.model_costs import SQLiteModelCostLedger
    store = operations.authority
    backup = None
    active = None
    try:
        with store.transaction() as conn:
            now = trusted_now(conn, store.now())
            verify_plan(plan, "reconcile_cost", store.path, now)
            ensure_maintenance_idle(conn, now)
            actual = _cost_plan(conn, store.path, ledger_path, [item["run_id"] for item in plan["items"]], plan["limit"], plan["created_at"])
            if actual != plan:
                raise ExecutionError("EXECUTION_MAINTENANCE_PLAN_CHANGED")
            ready = [item for item in plan["items"] if item["reason"] == "READY"]
            if not ready:
                raise ExecutionError("EXECUTION_MAINTENANCE_NOTHING_READY")
            backup = backup_databases(store.path, backup_dir, plan, ledger=ledger_path)
            ledger = SQLiteModelCostLedger(ledger_path)
            for active in ready:
                stage_unwritten_cost(operations, ledger, active["run_id"])
                reconcile_cost(operations, ledger, active["run_id"])
    except BaseException as exc:
        if backup is not None:
            (backup / "result.json").write_text(canonical({"plan_hash":plan["plan_hash"], "status":"FAILED",
                "code":exc.code if isinstance(exc, ExecutionError) else "EXECUTION_MAINTENANCE_INTERRUPTED",
                "ledger_effects_may_be_committed":True}), encoding="utf-8")
        if isinstance(exc, ExecutionError) and exc.code == "EXECUTION_COST_CONFLICT" and active is not None:
            # Preserve the conflict after the enclosing authority transaction
            # rolled back; never overwrite evidence that changed in between.
            with store.transaction() as conn:
                current = _cost_item(conn, ledger_path, active["run_id"], trusted_now(conn, store.now()))
                if current["source_digest"] == active["source_digest"]:
                    stage_unwritten_cost(operations, ledger, active["run_id"])
                    conn.execute("UPDATE execution_cost_outbox SET status='CONFLICT' WHERE run_id=?", (active["run_id"],))
        raise
    result = {"plan_hash":plan["plan_hash"], "confirmed_runs":len(ready), "skipped_runs":len(plan["items"]) - len(ready)}
    (backup / "result.json").write_text(canonical(result), encoding="utf-8")
    return result

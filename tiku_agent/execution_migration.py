"""Explicit offline migration; legacy databases remain immutable rollback inputs."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from tiku_agent.execution_store import ExecutionError, ExecutionStore, canonical, digest


def _rows(path: Path | None, table: str):
    if path is None:
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT session_id,state_json,expires_at FROM {table} ORDER BY session_id")]


def plan_migration(child_db: str | Path, workflow_db: str | Path | None = None):
    child = _rows(Path(child_db), "agent_sessions")
    workflow = _rows(Path(workflow_db) if workflow_db else None, "a3_sessions")
    return {"schema": 1, "source_digest": digest({"child": child, "workflow": workflow}),
            "child_count": len(child), "workflow_count": len(workflow)}


def migrate_offline(child_db: str | Path, workflow_db: str | Path | None, *,
                    destination: str | Path, artifact_root: str | Path,
                    expected_plan: dict, backup_dir: str | Path):
    child_path = Path(child_db).resolve()
    workflow_path = Path(workflow_db).resolve() if workflow_db else None
    target = Path(destination).resolve()
    backup = Path(backup_dir).resolve()
    root = Path(artifact_root).resolve()
    if target.exists() or target in {child_path, workflow_path}:
        raise ExecutionError("EXECUTION_MIGRATION_TARGET_EXISTS")
    sources = {"child": _rows(child_path, "agent_sessions"),
               "workflow": _rows(workflow_path, "a3_sessions")}
    actual = {"schema": 1, "source_digest": digest(sources),
              "child_count": len(sources["child"]), "workflow_count": len(sources["workflow"])}
    if actual != expected_plan:
        raise ExecutionError("EXECUTION_MIGRATION_PLAN_CHANGED")
    # This is an offline runtime clone. Never retain a pointer into a live root.
    for rows in sources.values():
        for row in rows:
            state = json.loads(row["state_json"])
            paths = [state.get("current_image_path"), state.get("source_page_path"), state.get("current_question_image_path")]
            paths += [item.get("path") for item in state.get("crop_drafts", {}).values()]
            paths += [item.get("path") for item in state.get("auto_crops", {}).values()]
            paths += state.get("last_answer_paths", [])
            for value in filter(None, paths):
                path = Path(value).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ExecutionError("EXECUTION_MIGRATION_ARTIFACT_OUTSIDE_CLONE")
    if backup.exists():
        raise ExecutionError("EXECUTION_MIGRATION_BACKUP_EXISTS")
    backup.mkdir(parents=True)
    for kind, source in (("child", child_path), ("workflow", workflow_path)):
        if source is not None:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(backup / (kind + ".sqlite3"))) as dst:
                    src.backup(dst)
    # Revalidate after backups. Source changes cannot silently enter migration.
    if (plan_migration(child_path, workflow_path) != expected_plan
            or plan_migration(backup / "child.sqlite3", backup / "workflow.sqlite3" if workflow_path else None) != expected_plan):
        raise ExecutionError("EXECUTION_MIGRATION_PLAN_CHANGED")
    (backup / "plan.json").write_text(canonical(actual), encoding="utf-8")
    authority = ExecutionStore(target)
    from tiku_agent.state import AgentState
    from tiku_agent.a3_runtime import A3SessionState
    imported = {"child": 0, "workflow": 0}
    # Validate all rows before committing any imported state. save uses the same
    # CAS/parent validation as normal writes; incomplete imports stay disabled.
    validated = []
    for kind in ("workflow", "child"):
        cls = A3SessionState if kind == "workflow" else AgentState
        for row in sources[kind]:
            expires = datetime.fromisoformat(row["expires_at"]).timestamp()
            if expires <= authority.now():
                continue
            payload = cls.from_dict(json.loads(row["state_json"])).to_dict()
            validated.append((kind, row["session_id"], payload, expires))
    with authority.transaction() as conn:
        conn.execute("INSERT INTO execution_meta VALUES ('migration','incomplete')")
    for kind, sid, payload, expires in validated:
        authority.save(sid, kind, payload, None, origin="legacy_snapshot")
        imported[kind] += 1
    with authority.transaction() as conn:
        from tiku_agent.execution_store import session_key
        for _kind, sid, _payload, expires in validated:
            conn.execute("UPDATE execution_sessions SET expires=min(expires,?) WHERE session=?", (expires, session_key(sid)))
        conn.execute("UPDATE execution_meta SET value='complete' WHERE key='migration'")
    return {**actual, "imported": imported}

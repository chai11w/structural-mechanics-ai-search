"""Internal file-retirement core; no CLI, model tool or authorization endpoint.

The trusted maintenance controller must own all management/service/write locks,
verify deployment, owner approval, backup and live references, and supply its
freshness checks. This module does not establish those deployment privileges.
"""
from datetime import datetime, UTC
import json
import os
from pathlib import Path

from tiku_shared.bank_file_retention import FileRetentionView, canonical, digest
from tiku_shared.bank_publication import atomic_json, file_digest, reject_links, sync_directory, verify_bundle
from tiku_shared.bank_readers import maintenance_gate


def _locations(store, plan, item):
    roots = {"published": store.root, "candidate": store.private, "bundle": store.private, "receipt-bank": store.backups}
    root = reject_links(roots[item["kind"]])
    if item["kind"] == "published":
        original = root / "versions" / item["key"]
    elif item["kind"] == "receipt-bank":
        original = root / item["key"] / "bank"
    else:
        original = root / {"candidate": "candidates", "bundle": "bundles"}[item["kind"]] / item["key"]
    retired = root / ".retention-trash" / plan["id"] / item["kind"] / item["key"]
    original, retired = reject_links(original), reject_links(retired)
    if root not in original.parents or root not in retired.parents:
        raise ValueError("retention path escaped its owned root")
    return original, retired


def _inventory(directory):
    rows = []
    for path in sorted(directory.rglob("*")):
        reject_links(path)
        row = {"path": path.relative_to(directory).as_posix()}
        if path.is_dir():
            row["directory"] = True
        elif path.is_file() and path.stat().st_nlink == 1:
            row.update(sha256=file_digest(path), bytes=path.stat().st_size)
        else:
            raise ValueError("retention file is linked or unsupported")
        rows.append(row)
    return rows


def _check_tree(directory, expected, *, partial=False):
    observed = _inventory(directory)
    known = {row["path"]: row for row in expected}
    if (any(known.get(row["path"]) != row for row in observed)
            or not partial and observed != expected):
        raise ValueError("retirement directory changed")


def _validate_copy(copy):
    if set(copy) != {"item", "manifest", "entries"} or not isinstance(copy["entries"], list):
        raise ValueError("invalid retirement copy")
    manifest = copy["manifest"]
    if digest(manifest) != copy["item"]["version"] or set(manifest) != {"schema", "files"} or manifest["schema"] != 1:
        raise ValueError("retirement manifest does not bind its version")
    files = [{"path": row["path"], "bytes": row["size"], "sha256": row["sha256"]} for row in manifest["files"]]
    files.append({"path": "manifest.json", "bytes": len(canonical(manifest)), "sha256": copy["item"]["version"]})
    observed, seen = [], set()
    for row in copy["entries"]:
        path = row.get("path")
        if (not isinstance(path, str) or not path or any(part in {"", ".", ".."} or ":" in part
                or "\\" in part or part.rstrip(" .") != part for part in path.split("/")) or path.casefold() in seen):
            raise ValueError("invalid retirement inventory path")
        seen.add(path.casefold())
        if row.get("directory") is True:
            if set(row) != {"path", "directory"}:
                raise ValueError("invalid retirement directory entry")
        else:
            observed.append(row)
    if sorted(observed, key=lambda row: row["path"]) != sorted(files, key=lambda row: row["path"]):
        raise ValueError("retirement inventory is not bound to the original files")


def _read(path):
    path = reject_links(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("invalid retirement journal")
    raw = path.read_bytes()
    value = json.loads(raw)
    if canonical(value) != raw:
        raise ValueError("retirement journal is not canonical")
    return value


def _record_expiry(store, plan, expected_digest):
    with store.connection() as db:
        view = FileRetentionView(db)
        view.validate_all()
        existing = view.plans.get(plan["id"])
        if existing:
            if existing["payload"] != canonical(plan).decode() or existing["digest"] != expected_digest:
                raise ValueError("retention plan identity changed")
            for item in plan["items"]:
                record = view.records.get((item["kind"], item["key"]))
                if not record or record["plan_id"] != plan["id"] or not view.expired(item["kind"], item["key"], item["version"]):
                    raise ValueError("retirement ledger is incomplete")
            return
        if any((item["kind"], item["key"]) in view.records for item in plan["items"]):
            raise ValueError("another retirement already owns these files")
        # One SQLite FULL transaction precedes any file move. A record with
        # files still present is an intent; history continues to show available.
        db.execute("INSERT INTO bank_retention_plans VALUES (?,?,?)", (plan["id"], canonical(plan).decode(), expected_digest))
        for item in plan["items"]:
            detail = {"retention_id": plan["id"], "plan_digest": expected_digest,
                      **{key: item[key] for key in ("kind", "key", "version")}}
            store._event(db, item["operation_id"], "files-expired", detail)
            sequence = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO bank_file_expirations VALUES (?,?,?,?,?)",
                       (item["kind"], item["key"], item["version"], plan["id"], sequence))
        FileRetentionView(db).validate_all()


def _execute_locked(store, plan, *, expected_digest, approval, check_freshness, checkpoint=None):
    """Called only inside the trusted controller's management and writer locks.

    check_freshness('before-record'/'after-record'/'before-move') must return True
    only for valid authority, backup and references. after-record must verify our exact
    ledger additions before adopting the new writer fingerprint. before-move
    runs inside the reader gate and must only check captured guards, not scan,
    hash bulk files, stop services or wait. The controller is not yet installed.
    """
    if not callable(check_freshness) or digest(plan) != expected_digest:
        raise ValueError("retention controller and exact plan are required")
    if (not isinstance(approval, dict) or set(approval) != {"plan_digest", "principal", "approved_at"}
            or approval["plan_digest"] != expected_digest or not isinstance(approval["principal"], str)
            or not 1 <= len(approval["principal"]) <= 200):
        raise ValueError("trusted owner approval receipt is required")
    approved_at = datetime.fromisoformat(approval["approved_at"])
    if approved_at.tzinfo is None or approved_at > datetime.now(UTC) or approved_at < datetime.fromisoformat(plan["as_of"]):
        raise ValueError("invalid retirement approval time")
    with store.connection() as db:
        view = FileRetentionView(db)
        view.validate_all()
        value = {"id": plan["id"], "payload": canonical(plan).decode(), "digest": expected_digest}
        if plan["id"] in view.plans and view.plans[plan["id"]] != value:
            raise ValueError("retention plan identity changed")
        view.plans[plan["id"]] = value
        view._plan(plan["id"])
    if store.current() != plan["publication"]:
        raise ValueError("retirement publication changed")
    checkpoint = checkpoint or (lambda phase: None)
    def check(phase):
        if check_freshness(phase) is not True:
            raise ValueError("retirement freshness check did not approve")
    check("before-record")
    journal = reject_links(store.private / "retention" / plan["id"])
    request_path, progress_path = journal / "request.json", journal / "progress.json"
    if request_path.exists():
        request = _read(request_path)
        if (set(request) != {"schema", "plan", "plan_digest", "approval", "copies"} or request["schema"] != 1
                or request["plan"] != plan or request["plan_digest"] != expected_digest or request["approval"] != approval):
            raise ValueError("retirement request changed")
    else:
        if journal.exists():
            raise ValueError("unowned retirement journal directory")
        copies = []
        for item in plan["items"]:
            original, retired = _locations(store, plan, item)
            if retired.exists():
                raise ValueError("unowned retirement destination")
            manifest = verify_bundle(original, item["version"])
            copies.append({"item": item, "manifest": manifest, "entries": _inventory(original)})
        request = {"schema": 1, "plan": plan, "plan_digest": expected_digest, "approval": approval, "copies": copies}
        journal.mkdir(parents=True)
        atomic_json(request_path, request)
    if ([copy["item"] for copy in request["copies"]] != plan["items"]
            or any(not isinstance(copy.get("entries"), list) for copy in request["copies"])):
        raise ValueError("retirement copy inventory is incomplete")
    for copy in request["copies"]:
        _validate_copy(copy)
    state = _read(progress_path) if progress_path.exists() else {"schema": 1, "plan_digest": expected_digest, "phase": "staging"}
    if set(state) != {"schema", "plan_digest", "phase"} or state["schema"] != 1 or state["plan_digest"] != expected_digest or state["phase"] not in {"staging", "purging", "done"}:
        raise ValueError("invalid retirement progress")
    checkpoint("request-recorded")
    moves = []
    for copy in request["copies"]:
        original, retired = _locations(store, plan, copy["item"])
        if original.exists() and retired.exists():
            raise ValueError("retirement source and destination both exist")
        if original.exists():
            if state["phase"] != "staging":
                raise ValueError("retired source unexpectedly returned")
            _check_tree(original, copy["entries"])
            retired.parent.mkdir(parents=True, exist_ok=True)
            details = original.stat()
            moves.append((original, retired, (details.st_dev, details.st_ino)))
        elif retired.exists():
            if state["phase"] == "done":
                raise ValueError("completed retirement destination unexpectedly returned")
            _check_tree(retired, copy["entries"], partial=state["phase"] != "staging")
        elif state["phase"] == "staging":
            raise ValueError("retirement lost both file locations")
    with store.connection() as db:
        recorded = db.execute("SELECT id FROM bank_retention_plans WHERE id=?", (plan["id"],)).fetchone() is not None
    if not recorded and (len(moves) != len(plan["items"]) or state["phase"] != "staging"):
        raise ValueError("retirement files changed before their ledger record")
    _record_expiry(store, plan, expected_digest)
    checkpoint("ledger-committed")
    check("after-record")
    # All hashing and backup/reference scans happen before this short gate.
    if state["phase"] == "staging":
        with maintenance_gate(store.root):
            if store.current() != plan["publication"]:
                raise ValueError("retirement publication changed")
            check("before-move")
            for original, retired, identity in moves:
                reject_links(original); reject_links(retired)
                details = original.stat()
                if (details.st_dev, details.st_ino) != identity or retired.exists():
                    raise ValueError("retirement file location changed")
                os.rename(original, retired)
                sync_directory(original.parent); sync_directory(retired.parent)
                checkpoint("copy-moved")
        state["phase"] = "purging"
        atomic_json(progress_path, state)
        checkpoint("purge-recorded")
    # Only known files under the plan's own retired directories are removed.
    # No recursive delete; unexpected content causes refusal and stays intact.
    removed = 0
    for copy in request["copies"]:
        _, retired = _locations(store, plan, copy["item"])
        if not retired.exists():
            continue
        _check_tree(retired, copy["entries"], partial=True)
        for item in sorted(copy["entries"], key=lambda item: (len(Path(item["path"]).parts), item["path"]), reverse=True):
            target = reject_links(retired / item["path"])
            if retired not in target.parents:
                raise ValueError("retirement cleanup escaped its owned directory")
            if not target.exists():
                continue
            if item.get("directory"):
                target.rmdir()
            else:
                if not target.is_file() or target.stat().st_nlink != 1 or file_digest(target) != item["sha256"]:
                    raise ValueError("retirement file changed before removal")
                target.unlink(); removed += item["bytes"]
                checkpoint("file-removed")
        retired.rmdir(); sync_directory(retired.parent)
    state["phase"] = "done"
    atomic_json(progress_path, state)
    return {"retention_id": plan["id"], "phase": "done", "removed_file_bytes_this_run": removed}

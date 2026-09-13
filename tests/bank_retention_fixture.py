"""Synthetic ledger author for isolated tests, never a maintenance entry point."""
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from tiku_shared.bank_file_retention import canonical, digest
from tiku_shared.bank_publication import write_lock


def record_expiry(store, owned_root, items, *, as_of, protected, backup=None, references=None):
    owned_root = Path(owned_root).resolve()
    assert all(owned_root in path.resolve().parents for path in (store.root, store.private, store.backups))
    value = {"schema": 1, "id": "retention_" + uuid4().hex,
             "as_of": as_of.isoformat(), "cutoff": (as_of - timedelta(days=30)).isoformat(),
             "publication": store.current(), "protected_versions": sorted(set(protected)), "items": items,
             "references": references or {"management": "a" * 64, "search": "b" * 64, "deployment": "c" * 64},
             "backup": backup or {"checkpoint_id": "checkpoint_" + "d" * 32,
                 "manifest_sha256": "e" * 64, "restore_evidence_sha256": "f" * 64}}
    identity = digest(value)
    with write_lock(store.lock), store.connection() as db:
        db.execute("INSERT INTO bank_retention_plans VALUES (?,?,?)", (value["id"], canonical(value).decode(), identity))
        for item in items:
            detail = {"retention_id": value["id"], "plan_digest": identity,
                      **{key: item[key] for key in ("kind", "key", "version")}}
            store._event(db, item["operation_id"], "files-expired", detail)
            sequence = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute("INSERT INTO bank_file_expirations VALUES (?,?,?,?,?)",
                       (item["kind"], item["key"], item["version"], value["id"], sequence))
    return value


def move_owned(source, destination, owned_root):
    source, destination, owned_root = (Path(path).resolve() for path in (source, destination, owned_root))
    assert owned_root in source.parents and owned_root in destination.parents
    assert not destination.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)

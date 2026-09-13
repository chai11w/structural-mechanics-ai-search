"""Read and validate explicit file-expiry evidence, independently of bank history.

No expiry authoring, cleanup, approval or filesystem mutation entry point. The
future maintenance writer must bind live references, deployment and a verified
backup before recording these rows under the existing maintenance/write locks.
"""
from datetime import datetime, timedelta, UTC
import hashlib
import json
import math
import re


TABLES = {"bank_retention_plans", "bank_file_expirations"}
KINDS = {"published", "receipt-bank", "candidate", "bundle"}
HASH = re.compile(r"[a-f0-9]{64}")
OPERATION = re.compile(r"op_[a-f0-9]{32}")
PLAN = re.compile(r"retention_[a-f0-9]{32}")


class FileRetentionError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise FileRetentionError("invalid-file-retention-record")


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise FileRetentionError("invalid-file-retention-hash")
    return value


def _parse(raw, *, limit=8 * 1024 * 1024 + 16384):
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        raise FileRetentionError("file-retention-record-too-large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FileRetentionError("duplicate-file-retention-field")
            result[key] = value
        return result
    def invalid(_):
        raise FileRetentionError("invalid-file-retention-number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _time(value):
    if not isinstance(value, str):
        raise FileRetentionError("invalid-file-retention-time")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise FileRetentionError("invalid-file-retention-time")
    return result.astimezone(UTC)


def _pointer(value, *, parent=False):
    keys = {"revision", "version", "operation_id"} if parent else {"schema", "revision", "version", "operation_id", "parent"}
    _exact(value, keys)
    _hash(value["version"])
    if (type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["operation_id"], str) or not OPERATION.fullmatch(value["operation_id"])
            or not parent and (type(value["schema"]) is not int or value["schema"] != 1)):
        raise FileRetentionError("invalid-file-retention-publication")
    if not parent and value["parent"] is not None:
        _pointer(value["parent"], parent=True)
    return value


def initialize_schema(db):
    """Only called by the trusted writer inside its original startup write lock."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bank_retention_plans (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_file_expirations (
            kind TEXT NOT NULL, key TEXT NOT NULL, version TEXT NOT NULL,
            plan_id TEXT NOT NULL, audit_sequence INTEGER NOT NULL,
            PRIMARY KEY(kind, key), FOREIGN KEY(plan_id) REFERENCES bank_retention_plans(id)
        );
    """)


class FileRetentionView:
    """Missing files require an exact, audited expiry; absence alone proves none."""

    def __init__(self, db):
        self.db = db
        self.plans, self.records = {}, {}
        self._verified, self._operations, self._validated_records = {}, {}, set()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if not TABLES & tables:
            return  # Original installations and archives have no expiry evidence.
        if not TABLES <= tables:
            raise FileRetentionError("incomplete-file-retention-schema")
        for table, columns in (("bank_retention_plans", {"id", "payload", "digest"}),
                               ("bank_file_expirations", {"kind", "key", "version", "plan_id", "audit_sequence"})):
            if {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')} != columns:
                raise FileRetentionError("unsupported-file-retention-schema")
        size = 0
        for raw in db.execute("SELECT id,payload,digest FROM bank_retention_plans ORDER BY id"):
            row = dict(raw)
            if not isinstance(row["payload"], str):
                raise FileRetentionError("invalid-file-retention-record")
            size += len(row["payload"].encode())
            if size > 128 * 1024 * 1024 or len(self.plans) >= 100000:
                raise FileRetentionError("file-retention-ledger-too-large")
            self.plans[row["id"]] = row
        for raw in db.execute("SELECT * FROM bank_file_expirations ORDER BY kind,key"):
            row = dict(raw)
            self._item({key: row[key] for key in ("kind", "key", "version")})
            if (row["plan_id"] not in self.plans or type(row["audit_sequence"]) is not int
                    or row["audit_sequence"] < 1 or len(self.records) >= 100000):
                raise FileRetentionError("invalid-file-expiration-reference")
            self.records[row["kind"], row["key"]] = row

    @staticmethod
    def _item(item):
        if item.get("kind") not in KINDS:
            raise FileRetentionError("unsupported-file-retention-kind")
        _hash(item["version"])
        matcher = HASH if item["kind"] in {"published", "bundle"} else OPERATION
        if not isinstance(item.get("key"), str) or not matcher.fullmatch(item["key"]):
            raise FileRetentionError("invalid-file-retention-key")
        if item["kind"] in {"published", "bundle"} and item["key"] != item["version"]:
            raise FileRetentionError("file-retention-version-mismatch")

    def _operation(self, identity):
        if identity not in self._operations:
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (identity,)).fetchone()
            if row is None or hashlib.sha256(row["plan"].encode()).hexdigest() != row["digest"]:
                raise FileRetentionError("file-retention-operation-invalid")
            plan = _parse(row["plan"])
            if plan.get("operation_id") != identity:
                raise FileRetentionError("file-retention-operation-invalid")
            result = _parse(row["result"]) if row["result"] else None
            self._operations[identity] = (row, plan, result)
        return self._operations[identity]

    def _window(self, value):
        cutoff, as_of = _time(value["cutoff"]), _time(value["as_of"])
        if as_of - cutoff != timedelta(days=30):
            raise FileRetentionError("unsupported-file-retention-window")
        cursor = _pointer(value["publication"])
        keep, seen, publications = {cursor["version"]}, set(), {}
        boundary, previous_at = False, as_of.timestamp()
        while cursor:
            identity = cursor["operation_id"]
            if identity in seen or len(seen) >= 100000:
                raise FileRetentionError("file-retention-history-invalid")
            seen.add(identity)
            row, plan, result = self._operation(identity)
            base = _pointer(plan["base"]) if plan["base"] else None
            expected = {"schema": 1, "operation_id": identity, "version": plan["candidate_version"],
                        "revision": (base["revision"] if base else 0) + 1,
                        "parent": {key: base[key] for key in ("revision", "version", "operation_id")} if base else None}
            if row["state"] != "published" or result != cursor or result != expected:
                raise FileRetentionError("file-retention-history-invalid")
            at = self.db.execute("SELECT MIN(at) FROM audit WHERE operation_id=? AND event='published'", (identity,)).fetchone()[0]
            if type(at) not in (int, float) or not math.isfinite(at) or at > previous_at:
                raise FileRetentionError("file-retention-publication-time-invalid")
            if at >= cutoff.timestamp() or not boundary:
                keep.add(cursor["version"])
            if at < cutoff.timestamp():
                boundary = True
            publications[identity] = cursor
            previous_at, cursor = at, base
        return keep, publications

    def _plan(self, identity):
        if identity not in self._verified:
            row = self.plans.get(identity)
            if row is None or not isinstance(identity, str) or not PLAN.fullmatch(identity):
                raise FileRetentionError("invalid-file-retention-plan")
            value = _parse(row["payload"], limit=8 * 1024 * 1024)
            _exact(value, {"schema", "id", "as_of", "cutoff", "publication", "references", "backup", "protected_versions", "items"})
            if type(value["schema"]) is not int or value["schema"] != 1 or value["id"] != identity or digest(value) != row["digest"]:
                raise FileRetentionError("file-retention-plan-integrity-failed")
            _exact(value["references"], {"management", "search", "deployment"})
            for item in value["references"].values():
                _hash(item)
            backup = value["backup"]
            _exact(backup, {"checkpoint_id", "manifest_sha256", "restore_evidence_sha256"})
            if not isinstance(backup["checkpoint_id"], str) or not re.fullmatch(r"checkpoint_[a-f0-9]{32}", backup["checkpoint_id"]):
                raise FileRetentionError("invalid-file-retention-backup")
            _hash(backup["manifest_sha256"]); _hash(backup["restore_evidence_sha256"])
            protected = value["protected_versions"]
            if (not isinstance(protected, list) or len(protected) > 100000
                    or any(not isinstance(item, str) or not HASH.fullmatch(item) for item in protected)
                    or sorted(set(protected)) != protected):
                raise FileRetentionError("invalid-file-retention-protected-set")
            keep, publications = self._window(value)
            if not keep <= set(protected):
                raise FileRetentionError("file-retention-window-not-protected")
            if not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= 100000:
                raise FileRetentionError("invalid-file-retention-items")
            keys = set()
            for item in value["items"]:
                _exact(item, {"kind", "key", "version", "operation_id"})
                self._item(item)
                key = (item["kind"], item["key"])
                if key in keys:
                    raise FileRetentionError("duplicate-file-retention-item")
                keys.add(key)
                operation, original, result = self._operation(item["operation_id"])
                if operation["state"] not in {"published", "cancelled", "conflict"}:
                    raise FileRetentionError("file-retention-operation-unfinished")
                if item["kind"] == "published":
                    if (item["operation_id"] not in publications or item["version"] != original["candidate_version"]
                            or item["version"] in protected):
                        raise FileRetentionError("file-retention-version-protected")
                elif item["kind"] == "receipt-bank":
                    if (operation["state"] != "published" or not original["base"] or item["key"] != item["operation_id"]
                            or item["version"] != original["base"]["version"]):
                        raise FileRetentionError("file-retention-receipt-mismatch")
                elif (item["version"] != original["candidate_version"]
                        or item["kind"] == "candidate" and item["key"] != item["operation_id"]):
                    raise FileRetentionError("file-retention-candidate-mismatch")
            self._verified[identity] = value
        return self._verified[identity]

    def expired(self, kind, key, version):
        row = self.records.get((kind, key))
        if row is None:
            return False
        if row["version"] != version:
            raise FileRetentionError("file-expiration-version-changed")
        record_key = (kind, key)
        if record_key not in self._validated_records:
            plan = self._plan(row["plan_id"])
            item = next((item for item in plan["items"] if (item["kind"], item["key"]) == record_key), None)
            if not item or item["version"] != version:
                raise FileRetentionError("file-expiration-not-in-plan")
            audit = self.db.execute("SELECT * FROM audit WHERE sequence=?", (row["audit_sequence"],)).fetchone()
            detail = {"retention_id": row["plan_id"], "plan_digest": self.plans[row["plan_id"]]["digest"],
                      "kind": kind, "key": key, "version": version}
            if (audit is None or audit["operation_id"] != item["operation_id"] or audit["event"] != "files-expired"
                    or _parse(audit["detail"]) != detail or type(audit["at"]) not in (int, float)
                    or not math.isfinite(audit["at"]) or audit["at"] < _time(plan["as_of"]).timestamp()):
                raise FileRetentionError("file-expiration-audit-mismatch")
            self._validated_records.add(record_key)
        return True

    def validate_all(self):
        for identity in self.plans:
            self._plan(identity)
        for row in self.records.values():
            self.expired(row["kind"], row["key"], row["version"])

    def published_status(self, root, version):
        from tiku_shared.bank_publication import reject_links
        from tiku_shared.bank_versions import read_version
        directory = reject_links(root / "versions" / _hash(version))
        if not directory.exists():
            return "expired" if self.expired("published", version, version) else "unavailable"
        try:
            # Cheap publication metadata check. Prepare/backup still validates
            # every file; an expiry marker never excuses corrupt present data.
            read_version(root, version)
        except (ValueError, OSError):
            return "unavailable"
        return "available"

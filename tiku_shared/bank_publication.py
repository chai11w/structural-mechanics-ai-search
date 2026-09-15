"""Private, approval-bound publication of complete immutable bank versions.

This module is called by the privileged writer, never by a model tool. The caller
authenticates channel identities; directory ACLs keep other services from calling
it through arbitrary Python or changing its database. A bank-specific validator
must check the registry, Excel and media relationship before a bundle is sealed.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import time
from uuid import uuid4

from tiku_shared.bank_readers import initialize_reader_gate
from tiku_shared.bank_file_retention import FileRetentionView, initialize_schema as initialize_retention_schema


_HASH = re.compile(r"[a-f0-9]{64}")
_OPERATION = re.compile(r"op_[a-f0-9]{32}")
MINIMUM_BANK_FREE_BYTES = 20 * 1024 ** 3
BANK_WRITE_MARGIN_BYTES = 64 * 1024 ** 2


class PublicationError(RuntimeError):
    """Stable error codes; filesystem paths and private exception text stay private."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def pointer_identity(pointer):
    return {key: pointer[key] for key in ("revision", "version", "operation_id")} if pointer else None


def reject_links(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise PublicationError("linked-path")
    return path.resolve()


def sync_directory(directory):
    # Windows has no portable directory fsync. File fsync and SQLite FULL cover
    # process interruption; storage-device/power-loss guarantees require deployment validation.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path, value):
    temporary = path.parent / ("." + path.name + "-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def write_lock(path, timeout=30):
    """The kernel releases this lock on a crash; never guess whether a PID is stale."""
    reject_links(path)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0"); stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise PublicationError("writer-busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def inventory(directory):
    directory = reject_links(directory)
    from .bank_objects import object_store, shared_file
    objects = object_store(directory)
    files, folded, entries = [], set(), []
    # Walk each directory once. Re-resolving every file's entire ancestor chain
    # performs tens of thousands of redundant filesystem queries on Windows.
    def walk(parent):
        with os.scandir(parent) as children:
            for child in children:
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise PublicationError("linked-path")
                path = Path(child.path)
                if stat.S_ISDIR(info.st_mode):
                    walk(path)
                else:
                    # Windows DirEntry.stat omits the link count; use a fresh
                    # lstat for files so hard links still fail closed.
                    entries.append((path, path.lstat()))
    walk(directory)
    for path, info in sorted(entries):
        relative = path.relative_to(directory).as_posix()
        if relative == "manifest.json":
            continue
        if not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and objects is None) or relative.casefold() in folded:
            raise PublicationError("ambiguous-bundle-file")
        # No alternate data streams, traversal, or Windows normalized-name aliases.
        parts = PurePosixPath(relative).parts
        if any(":" in part or "\\" in part or part.rstrip(" .") != part for part in parts):
            raise PublicationError("invalid-bundle-path")
        if parts[0] not in ("main", "symbolic") and relative != "registry.json":
            raise PublicationError("unexpected-bundle-file")
        folded.add(relative.casefold())
        files.append({"path": relative, "size": info.st_size})
    if "registry.json" not in folded or not all((directory / bank).is_dir() for bank in ("main", "symbolic")):
        raise PublicationError("incomplete-bundle")
    # Bound outstanding I/O; each pass still hashes every byte and builds the
    # same deterministic manifest. No cache survives a verification boundary.
    def inspect_file(item):
        path = directory / item['path']
        sha256 = file_digest(path)
        if path.stat().st_nlink != 1 and not shared_file(path, sha256, objects):
            raise PublicationError('ambiguous-bundle-file')
        return sha256
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bank-hash") as pool:
        for item, sha256 in zip(files, pool.map(inspect_file, files)):
            item['sha256'] = sha256
    return files


def seal_bundle(directory, validator):
    if not callable(validator):
        raise PublicationError("bank-validator-required")
    before = inventory(directory)
    validator(directory)
    after = inventory(directory)
    if before != after:
        raise PublicationError("bundle-changed-during-validation")
    manifest = {"schema": 1, "files": after}
    atomic_json(directory / "manifest.json", manifest)
    return digest(canonical(manifest))


def verify_bundle(directory, version):
    directory = reject_links(directory)
    if not isinstance(version, str) or not _HASH.fullmatch(version):
        raise PublicationError("invalid-version")
    path = reject_links(directory / "manifest.json")
    if path.stat().st_size > 32 * 1024 * 1024 or file_digest(path) != version:
        raise PublicationError("manifest-mismatch")
    manifest = json.loads(path.read_bytes())
    if manifest.get("schema") != 1 or manifest.get("files") != inventory(directory):
        raise PublicationError("bundle-integrity-failed")
    return manifest


def require_storage_space(requirements):
    """Check combined additional bytes per volume without deleting or reserving files.

    This is a preflight, not a reservation against unrelated disk writers. Keep
    the transaction's existing I/O failure/recovery handling as the final guard.
    """
    volumes = {}
    try:
        for destination, size in requirements:
            if type(size) is not int or size < 0:
                raise PublicationError("invalid-storage-requirement")
            directory = reject_links(destination)
            while not directory.exists():
                directory = directory.parent
            device = directory.stat().st_dev
            free = shutil.disk_usage(directory).free
            previous = volumes.get(device, (0, free))
            volumes[device] = (previous[0] + size, min(previous[1], free))
    except OSError:
        raise PublicationError("bank-space-check-failed") from None
    for required, free in volumes.values():
        if free < required + MINIMUM_BANK_FREE_BYTES + BANK_WRITE_MARGIN_BYTES:
            raise PublicationError("insufficient-bank-space")


def bundle_size(manifest):
    return sum(item["size"] for item in manifest["files"]) + len(canonical(manifest))


def independent_preimage_required(plan):
    """Existing frozen plans retain their original backup contract on retry."""
    policy = plan.get("recovery_policy")
    if policy is None and "recovery_policy" not in plan:
        return True
    if policy != "retained-versions":
        raise PublicationError("unsupported-recovery-policy")
    return False


def copy_bundle(source, destination, version):
    """Freeze a version, sharing only writer-owned immutable content objects."""
    manifest = verify_bundle(source, version)
    from .bank_objects import object_store, ensure_object, missing_bytes
    objects = object_store(destination)
    required = missing_bytes(manifest, objects) + len(canonical(manifest)) if objects else bundle_size(manifest)
    require_storage_space([(destination, required)])
    destination.mkdir()
    for name in ("main", "symbolic"):
        (destination / name).mkdir()
    for parent in sorted({(destination / item["path"]).parent for item in manifest["files"]}):
        parent.mkdir(parents=True, exist_ok=True)
    def copy_file(item):
        target = destination / item["path"]
        if objects is not None:
            os.link(ensure_object(source / item['path'], item, objects), target)
            return
        with (source / item["path"]).open("rb") as input_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush(); os.fsync(output_stream.fileno())
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bank-copy") as pool:
        # Wait for all workers before publishing metadata or propagating failure.
        list(pool.map(copy_file, manifest["files"]))
    atomic_json(destination / "manifest.json", manifest)
    verify_bundle(destination, version)
    verify_bundle(source, version)


class PublicationStore:
    """Trusted writer API. HTTP/UI authorization is an additional, mandatory boundary."""

    def __init__(self, store, private, backups, *, validator, checkpoint=None, incremental=False):
        self.root, self.private, self.backups = [reject_links(Path(path)) for path in (store, private, backups)]
        paths = [self.root, self.private, self.backups]
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i + 1:]):
            raise PublicationError("storage-roots-must-be-separate")
        if any((parent / ".git").exists() for parent in (self.backups, *self.backups.parents)):
            raise PublicationError("backup-must-be-outside-repository")
        if not callable(validator):
            raise PublicationError("bank-validator-required")
        self.validator = validator
        self.checkpoint = checkpoint or (lambda name: None)
        for directory in paths + [self.root / "versions", self.private / "candidates", self.private / "bundles"]:
            reject_links(directory)
            directory.mkdir(parents=True, exist_ok=True)
        if type(incremental) is not bool:
            raise PublicationError('invalid-incremental-storage-setting')
        self.incremental = incremental
        if incremental:
            from .bank_objects import initialize_objects
            initialize_objects(self.root, self.private)
        self.lock = self.private / "writer.lock"
        with write_lock(self.lock), self.connection() as db:
            initialize_reader_gate(self.root)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, plan TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, challenge TEXT, reviewer TEXT, approval TEXT,
                    result TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                    at REAL NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    question_id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft_id TEXT NOT NULL,
                    UNIQUE(owner, draft_id)
                );
                CREATE TABLE IF NOT EXISTS identity_imports (
                    digest TEXT PRIMARY KEY, payload TEXT NOT NULL, at REAL NOT NULL
                );
            """)
            FileRetentionView(db).validate_all()
            initialize_retention_schema(db)

    @contextmanager
    def connection(self):
        path = reject_links(self.private / "writer.sqlite3")
        db = sqlite3.connect(path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def operation_id(value):
        if not isinstance(value, str) or not _OPERATION.fullmatch(value):
            raise PublicationError("invalid-operation")
        return value

    @staticmethod
    def identity(principal, channel):
        if not isinstance(principal, str) or not 1 <= len(principal) <= 256 or channel not in ("lida", "feishu", "installation"):
            raise PublicationError("invalid-identity")
        return {"principal": principal, "channel": channel}

    def reserve(self, *, owner, draft_id):
        if not all(isinstance(value, str) and 1 <= len(value) <= 256 for value in (owner, draft_id)):
            raise PublicationError("invalid-reservation")
        with write_lock(self.lock), self.connection() as db:
            row = db.execute("SELECT question_id FROM reservations WHERE owner=? AND draft_id=?", (owner, draft_id)).fetchone()
            if row:
                return row[0]
            question_id = "Q_" + str(uuid4())
            db.execute("INSERT INTO reservations VALUES (?, ?, ?)", (question_id, owner, draft_id))
            return question_id

    def import_reservations(self, *, migration_digest, reservations):
        """Offline provisioning only; no HTTP/model operation and no bank publication.

        Import a frozen legacy identity manifest into an unused writer. A retry
        verifies the original bindings even after installation has published;
        it never repairs or overwrites a competing reservation.
        """
        if (not isinstance(migration_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", migration_digest)
                or not isinstance(reservations, list) or not 1 <= len(reservations) <= 100000):
            raise PublicationError("invalid-identity-import")
        ids, bindings = set(), set()
        for item in reservations:
            if (not isinstance(item, dict) or set(item) != {"question_id", "owner", "draft_id"}
                    or not isinstance(item["question_id"], str)
                    or not re.fullmatch(r"Q_[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", item["question_id"])
                    or not all(isinstance(item[key], str) and 1 <= len(item[key]) <= 256
                               and not any(ord(char) < 32 for char in item[key]) for key in ("owner", "draft_id"))):
                raise PublicationError("invalid-identity-import")
            binding = (item["owner"], item["draft_id"])
            if item["question_id"] in ids or binding in bindings:
                raise PublicationError("duplicate-import-identity")
            ids.add(item["question_id"]); bindings.add(binding)
        payload = canonical(sorted(reservations, key=lambda item: item["question_id"])).decode()
        with write_lock(self.lock), self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT payload FROM identity_imports WHERE digest=?", (migration_digest,)).fetchone()
            if previous:
                if previous[0] != payload:
                    raise PublicationError("identity-import-changed")
                for item in reservations:
                    row = db.execute("SELECT owner, draft_id FROM reservations WHERE question_id=?", (item["question_id"],)).fetchone()
                    if row is None or tuple(row) != (item["owner"], item["draft_id"]):
                        raise PublicationError("imported-reservation-changed")
                return {"migration_digest": migration_digest, "count": len(reservations), "replayed": True}
            if (self.current() is not None or db.execute("SELECT 1 FROM operations LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM reservations LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM identity_imports LIMIT 1").fetchone()
                    or any((self.root / "versions").iterdir())):
                raise PublicationError("identity-import-requires-unused-writer")
            db.executemany("INSERT INTO reservations VALUES (?, ?, ?)",
                           [(item["question_id"], item["owner"], item["draft_id"]) for item in reservations])
            db.execute("INSERT INTO identity_imports VALUES (?, ?, ?)", (migration_digest, payload, time.time()))
            return {"migration_digest": migration_digest, "count": len(reservations), "replayed": False}

    def candidate_directory(self, operation_id):
        return reject_links(self.private / "candidates" / self.operation_id(operation_id))

    def current(self):
        path = reject_links(self.root / "active.json")
        if not path.exists():
            return None
        if path.stat().st_size > 8192:
            raise PublicationError("invalid-active-pointer")
        pointer = json.loads(path.read_bytes())
        if (pointer.get("schema") != 1 or type(pointer.get("revision")) is not int or pointer["revision"] < 1
                or not _HASH.fullmatch(str(pointer.get("version", "")))
                or not _OPERATION.fullmatch(str(pointer.get("operation_id", "")))):
            raise PublicationError("invalid-active-pointer")
        return pointer

    def _event(self, db, operation_id, event, detail):
        db.execute("INSERT INTO audit(operation_id, at, event, detail) VALUES (?, ?, ?, ?)",
                   (operation_id, time.time(), event, canonical(detail).decode()))

    def _row(self, db, operation_id):
        row = db.execute("SELECT * FROM operations WHERE id=?", (self.operation_id(operation_id),)).fetchone()
        if not row:
            raise PublicationError("operation-not-found")
        if digest(row["plan"].encode()) != row["digest"]:
            raise PublicationError("plan-integrity-failed")
        return row

    @staticmethod
    def _public(row):
        # Does not include the approval challenge or private filesystem locations.
        return {"operation_id": row["id"], "plan_digest": row["digest"], "state": row["state"],
                "plan": json.loads(row["plan"]), "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"]}

    def status(self, operation_id):
        with self.connection() as db:
            return self._public(self._row(db, operation_id))

    def _history_locked(self, principal):
        """Only publications on the actual current ancestry are rollback targets."""
        current = self.current()
        cursor, seen = pointer_identity(current), set()
        with self.connection() as db:
            retention = FileRetentionView(db)
            while cursor:
                if cursor["operation_id"] in seen or len(seen) >= 100000:
                    raise PublicationError("publication-history-invalid")
                seen.add(cursor["operation_id"])
                row = self._row(db, cursor["operation_id"])
                plan = json.loads(row["plan"])
                expected = {"schema": 1, "operation_id": row["id"], "version": plan["candidate_version"],
                    "revision": (plan["base"]["revision"] if plan["base"] else 0) + 1,
                    "parent": pointer_identity(plan["base"])}
                result = json.loads(row["result"]) if row["result"] else None
                if row["state"] != "published" or result != expected or cursor != pointer_identity(result):
                    raise PublicationError("publication-history-invalid")
                if len(seen) == 1 and (result != current or plan["requested_by"]["principal"] != principal):
                    raise PublicationError("publication-not-owned")
                if plan["requested_by"]["principal"] == principal:
                    at = db.execute("SELECT MIN(at) FROM audit WHERE operation_id=? AND event='published'", (row["id"],)).fetchone()[0]
                    yield {**result, "published_at": at, "kind": plan["summary"].get("kind", "changes" if plan["base"] else "installation"),
                           "changed_records": len(plan["summary"].get("changes", [])),
                           "files_status": retention.published_status(self.root, result["version"])}
                cursor = result["parent"]

    def history(self, *, principal, before=None, limit=20):
        self.identity(principal, "installation")
        if (before is not None and (type(before) is not int or before < 1)
                or type(limit) is not int or not 1 <= limit <= 50):
            raise PublicationError("invalid-history-page")
        with write_lock(self.lock):
            self._recover_locked()
            items = []
            for item in self._history_locked(principal):
                if before is None or item["revision"] < before:
                    items.append(item)
                    if len(items) > limit:
                        break
            return {"current": self.current(), "items": items[:limit],
                    "next_before": items[limit - 1]["revision"] if len(items) > limit else None}

    def historical(self, operation_id, *, principal):
        self.operation_id(operation_id)
        self.identity(principal, "installation")
        with write_lock(self.lock):
            self._recover_locked()
            for item in self._history_locked(principal):
                if item["operation_id"] == operation_id:
                    if item["files_status"] == "expired":
                        raise PublicationError("rollback-target-expired")
                    if item["files_status"] != "available":
                        raise PublicationError("rollback-target-unavailable")
                    return {key: item[key] for key in ("schema", "revision", "version", "operation_id", "parent")}
        raise PublicationError("rollback-target-not-published")

    def prepare(self, operation_id, *, expected, summary, principal, channel):
        """Seal a writer-owned candidate built by the typed bank mutation service."""
        identity = self.identity(principal, channel)
        directory = self.candidate_directory(operation_id)
        if len(canonical(summary)) > 8 * 1024 * 1024:
            raise PublicationError("plan-too-large")
        with write_lock(self.lock):
            self._recover_locked()
            version = seal_bundle(directory, self.validator)
            with self.connection() as db:
                previous = db.execute("SELECT id FROM operations WHERE id=?", (operation_id,)).fetchone()
                row = self._row(db, operation_id) if previous else None
            plan = {"schema": 1, "operation_id": operation_id, "base": expected,
                    "candidate_version": version, "summary": summary, "requested_by": identity}
            if row is None or "recovery_policy" in json.loads(row["plan"]):
                plan["recovery_policy"] = "retained-versions"
            if self.incremental and (row is None or 'storage_policy' in json.loads(row['plan'])):
                plan['storage_policy'] = 'content-objects-v1'
            plan_json = canonical(plan).decode(); plan_digest = digest(plan_json.encode())
            if row:
                if row["digest"] != plan_digest:
                    raise PublicationError("idempotency-conflict")
                return self._public(row)
            if self.current() != expected:
                raise PublicationError("bank-version-changed")
            destination = self.private / "bundles" / version
            if destination.exists():
                verify_bundle(destination, version)
            else:
                temporary = self.private / "bundles" / ("pending-" + uuid4().hex)
                copy_bundle(directory, temporary, version)
                os.replace(temporary, destination); sync_directory(destination.parent)
            with self.connection() as db:
                db.execute("INSERT INTO operations(id, plan, digest, state) VALUES (?, ?, ?, 'prepared')",
                           (operation_id, plan_json, plan_digest))
                self._event(db, operation_id, "prepared", {"plan_digest": plan_digest, **identity})
                return self._public(self._row(db, operation_id))

    def review(self, operation_id, *, principal, channel):
        """Owner-only route: bind a review challenge to this identity and exact plan."""
        reviewer = self.identity(principal, channel)
        with write_lock(self.lock):
            self._recover_locked()
            with self.connection() as db:
                row = self._row(db, operation_id)
                if row["state"] not in ("prepared", "approved"):
                    raise PublicationError("operation-not-reviewable")
                if self.current() != json.loads(row["plan"])["base"]:
                    raise PublicationError("bank-version-changed")
                challenge = secrets.token_urlsafe(32)
                db.execute("UPDATE operations SET challenge=?, reviewer=? WHERE id=?",
                           (digest(challenge.encode()), canonical(reviewer).decode(), operation_id))
                self._event(db, operation_id, "reviewed", {"plan_digest": row["digest"], **reviewer})
                return {**self._public(row), "approval_challenge": challenge}

    def approve(self, operation_id, *, plan_digest, challenge, principal, channel):
        """Private owner/verified-Feishu adapter only; no model-supplied owner flag."""
        identity = self.identity(principal, channel)
        with write_lock(self.lock):
            self._recover_locked()
            with self.connection() as db:
                row = self._row(db, operation_id)
                if (not isinstance(challenge, str) or len(challenge) > 128 or row["digest"] != plan_digest
                        or not hmac.compare_digest(row["challenge"] or "", digest(challenge.encode()))
                        or row["reviewer"] != canonical(identity).decode()):
                    raise PublicationError("approval-does-not-match-review")
                if row["state"] == "published":
                    return self._public(row)
                if row["state"] not in ("prepared", "approved"):
                    raise PublicationError("operation-not-approvable")
                if self.current() != json.loads(row["plan"])["base"]:
                    raise PublicationError("bank-version-changed")
                if row["state"] != "approved":
                    approval = {**identity, "plan_digest": plan_digest, "approved_at": time.time()}
                    db.execute("UPDATE operations SET state='approved', approval=?, error=NULL WHERE id=?",
                               (canonical(approval).decode(), operation_id))
                    self._event(db, operation_id, "approved", approval)
                return self._public(self._row(db, operation_id))

    def cancel(self, operation_id, *, plan_digest, principal, channel):
        """Revoke an unpublished plan, including its approval, before editing it."""
        identity = self.identity(principal, channel)
        with write_lock(self.lock):
            self._recover_locked()
            with self.connection() as db:
                row = self._row(db, operation_id)
                if row["digest"] != plan_digest:
                    raise PublicationError("plan-version-changed")
                if row["state"] == "published":
                    raise PublicationError("published-operation-cannot-be-cancelled")
                if row["state"] != "cancelled":
                    db.execute("UPDATE operations SET state='cancelled', challenge=NULL, approval=NULL WHERE id=?", (operation_id,))
                    self._event(db, operation_id, "cancelled", {"plan_digest": plan_digest, **identity})
                return self._public(self._row(db, operation_id))

    def _record_receipt(self, row, plan):
        # New plans retain the immutable old version, not a second full copy.
        # The approved policy is inside the plan digest; old plans stay intact.
        independent = independent_preimage_required(plan)
        destination = self.backups / row["id"]
        receipt = {"schema": 1, "plan_digest": row["digest"], "plan": plan, "approval": json.loads(row["approval"])}
        if plan["base"]:
            verify_bundle(self.root / "versions" / plan["base"]["version"], plan["base"]["version"])
        if not destination.exists():
            temporary = self.backups / ("pending-" + uuid4().hex)
            temporary.mkdir()
            if plan["base"] and independent:
                base = plan["base"]["version"]
                copy_bundle(self.root / "versions" / base, temporary / "bank", base)
            atomic_json(temporary / "receipt.json", receipt)
            os.replace(temporary, destination); sync_directory(destination.parent)
        if json.loads(reject_links(destination / "receipt.json").read_bytes()) != receipt:
            raise PublicationError("backup-receipt-mismatch")
        if plan["base"] and independent:
            verify_bundle(destination / "bank", plan["base"]["version"])

    def _recover_locked(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM operations WHERE state='publishing' ORDER BY rowid").fetchall()
            for row in rows:
                row = self._row(db, row["id"])
                plan = json.loads(row["plan"]); current = self.current()
                expected_revision = (plan["base"]["revision"] if plan["base"] else 0) + 1
                if (current and current["operation_id"] == row["id"] and current["version"] == plan["candidate_version"]
                        and current["revision"] == expected_revision and current.get("parent") == pointer_identity(plan["base"])):
                    verify_bundle(self.root / "versions" / current["version"], current["version"])
                    self._record_receipt(row, plan)
                    db.execute("UPDATE operations SET state='published', result=?, error=NULL WHERE id=?",
                               (canonical(current).decode(), row["id"]))
                    self._event(db, row["id"], "published", {"pointer": current, "recovered": True})
                elif current == plan["base"]:
                    db.execute("UPDATE operations SET state='approved', error='interrupted-before-publication' WHERE id=?", (row["id"],))
                    self._event(db, row["id"], "retryable", {"reason": "interrupted-before-publication"})
                else:
                    db.execute("UPDATE operations SET state='conflict', error='bank-version-changed' WHERE id=?", (row["id"],))
                    self._event(db, row["id"], "conflict", {"reason": "bank-version-changed"})
        if self.incremental and (current := self.current()):
            from .bank_objects import sync_current
            sync_current(self.root, current['version'])

    def recover(self):
        with write_lock(self.lock):
            self._recover_locked()

    def execute(self, operation_id, *, plan_digest):
        """Only the writer's dispatcher calls this after an owner-approved operation."""
        with write_lock(self.lock):
            self._recover_locked()
            with self.connection() as db:
                row = self._row(db, operation_id)
                if row["digest"] != plan_digest:
                    raise PublicationError("plan-version-changed")
                if row["state"] == "published":
                    return self._public(row)
                if row["state"] != "approved" or not row["approval"]:
                    raise PublicationError("owner-approval-required")
                plan = json.loads(row["plan"])
                if self.current() != plan["base"]:
                    raise PublicationError("bank-version-changed")
                db.execute("UPDATE operations SET state='publishing', error=NULL WHERE id=?", (operation_id,))
                self._event(db, operation_id, "publishing", {"plan_digest": plan_digest})
            try:
                self.checkpoint("before-backup")
                version = plan["candidate_version"]
                source = self.private / "bundles" / version
                manifest = verify_bundle(source, version)
                requirements = [(self.private, 0), (self.backups, 0), (self.root, 0)]
                if not (self.root / "versions" / version).exists():
                    if self.incremental:
                        from .bank_objects import missing_bytes
                        requirements.append((self.root, missing_bytes(manifest, self.root / 'objects') + len(canonical(manifest))))
                    else:
                        requirements.append((self.root, bundle_size(manifest)))
                if (plan["base"] and independent_preimage_required(plan)
                        and not (self.backups / row["id"]).exists()):
                    base_version = plan["base"]["version"]
                    requirements.append((self.backups, bundle_size(
                        verify_bundle(self.root / "versions" / base_version, base_version))))
                require_storage_space(requirements)
                self._record_receipt(row, plan)
                self.checkpoint("after-backup")
                if plan.get('storage_policy') != 'content-objects-v1':
                    self.validator(source)
                    verify_bundle(source, version)
                # Incremental plans were semantically validated before freezing.
                # The full byte verification above proves this exact approved
                # manifest is unchanged; reparsing every Excel adds no evidence.
                destination = self.root / "versions" / version
                if destination.exists():
                    verify_bundle(destination, version)
                else:
                    temporary = self.root / "versions" / ("pending-" + uuid4().hex)
                    copy_bundle(source, temporary, version)
                    os.replace(temporary, destination); sync_directory(destination.parent)
                self.checkpoint("after-version")
                # Same lock covers revalidation, backup, publication and journal reconciliation.
                if self.current() != plan["base"]:
                    raise PublicationError("bank-version-changed")
                pointer = {"schema": 1, "revision": (plan["base"]["revision"] if plan["base"] else 0) + 1,
                           "version": version, "operation_id": operation_id, "parent": pointer_identity(plan["base"])}
                self.checkpoint("before-pointer")
                atomic_json(self.root / "active.json", pointer)
                self.checkpoint("after-pointer")
                self._recover_locked()
                self.checkpoint("after-journal")
            except Exception as exc:
                # An error after replace does not mean the bank was not published.
                self._recover_locked()
                with self.connection() as db:
                    observed = self._row(db, operation_id)
                    if observed["state"] != "published":
                        reason = str(exc) if isinstance(exc, PublicationError) else "publication-io-failed"
                        db.execute("UPDATE operations SET error=? WHERE id=?", (reason, operation_id))
                        self._event(db, operation_id, "attempt-failed", {"reason": reason})
                if observed["state"] != "published":
                    raise PublicationError(reason) from None
            return self.status(operation_id)

    def audit(self, operation_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise PublicationError("invalid-audit-page")
        with self.connection() as db:
            self._row(db, operation_id)
            return [{"sequence": row[0], "at": row[1], "event": row[2], "detail": json.loads(row[3])}
                    for row in db.execute("SELECT sequence, at, event, detail FROM audit WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?", (operation_id, after, limit))]

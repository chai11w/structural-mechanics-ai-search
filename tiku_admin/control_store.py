"""Persistent control plane for administrator, invitations, budgets, and audit."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import scrypt, sha256
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from typing import Any, Iterator, Protocol

from tiku_admin.invite_vault import mask_invitation_code


CONTROL_SCHEMA_VERSION = 2
DEFAULT_GLOBAL_DAILY_BUDGET_MICROS = 30_000_000
DEFAULT_INVITE_DAILY_BUDGET_MICROS = 3_000_000
DEFAULT_FEEDBACK_RETENTION_DAYS = 30
INVITE_CODE_PREFIX = "TIKU-"
_UNSET = object()


@dataclass(frozen=True)
class InvitationRecord:
    invite_id: str
    label: str
    status: str
    daily_budget_micros: int | None
    expires_at: str
    auth_version: int
    created_at: str
    updated_at: str
    last_used_at: str
    code_preview: str
    code_recoverable: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetLimits:
    global_daily_micros: int
    identity_daily_micros: int


@dataclass(frozen=True)
class LegacyImportConflict:
    kind: str
    invite_id: str
    existing_invite_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyImportReport:
    source_count: int
    existing_count: int
    insert_count: int
    unchanged_count: int
    cookie_secret_action: str
    conflicts: tuple[LegacyImportConflict, ...] = ()

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    def to_dict(self) -> dict[str, object]:
        return {
            "source_count": self.source_count,
            "existing_count": self.existing_count,
            "insert_count": self.insert_count,
            "unchanged_count": self.unchanged_count,
            "cookie_secret_action": self.cookie_secret_action,
            "conflict_count": len(self.conflicts),
            "can_apply": self.can_apply,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass(frozen=True)
class _LegacyInvitationEntry:
    invite_id: str
    code_hash: str
    status: str


class InvitationCodeVault(Protocol):
    def seal(self, invite_id: str, code: str) -> str: ...

    def open(self, invite_id: str, token: str) -> str: ...


class SQLiteControlStore:
    """Small shared SQLite control plane read by 8790 and written by 8795."""

    def __init__(
        self,
        path: str | Path,
        *,
        invitation_vault: InvitationCodeVault | None = None,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._invitation_vault = invitation_vault
        self._initialize()

    @property
    def invite_cookie_secret(self) -> bytes:
        return _decode_secret(self._meta("invite_cookie_secret"))

    @property
    def admin_cookie_secret(self) -> bytes:
        return _decode_secret(self._meta("admin_cookie_secret"))

    def has_admin(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM admin_account WHERE admin_id = 'owner'").fetchone()
        return row is not None

    def initialize_admin(self, password: str, *, actor: str = "local-setup") -> None:
        clean = _validate_password(password)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM admin_account WHERE admin_id = 'owner'"
            ).fetchone()
            if existing is not None:
                raise ValueError("administrator is already initialized")
            connection.execute(
                """
                INSERT INTO admin_account (
                    admin_id, password_hash, credential_version, created_at, updated_at
                ) VALUES ('owner', ?, 1, ?, ?)
                """,
                (_hash_password(clean), now, now),
            )
            self._write_audit(
                connection, actor, "admin.initialize", "admin", "owner", {}, {"created": True}
            )

    def verify_admin_password(self, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM admin_account WHERE admin_id = 'owner'"
            ).fetchone()
        return bool(row and _verify_password(str(password or ""), str(row["password_hash"])))

    def admin_credential_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT credential_version FROM admin_account WHERE admin_id = 'owner'"
            ).fetchone()
        return max(0, int(row[0])) if row else 0

    def change_admin_password(self, password: str, *, actor: str = "owner") -> None:
        clean = _validate_password(password)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE admin_account
                SET password_hash = ?, credential_version = credential_version + 1, updated_at = ?
                WHERE admin_id = 'owner'
                """,
                (_hash_password(clean), now),
            )
            if cursor.rowcount != 1:
                raise ValueError("administrator is not initialized")
            self._write_audit(
                connection, actor, "admin.password_change", "admin", "owner", {}, {"changed": True}
            )

    def create_invitation(
        self,
        *,
        label: str,
        daily_budget_micros: int | None = None,
        expires_at: str = "",
        actor: str = "owner",
    ) -> tuple[InvitationRecord, str]:
        clean_label = _clean_label(label)
        clean_budget = _validate_optional_budget(daily_budget_micros)
        clean_expiry = _normalize_expiry(expires_at)
        now = _utc_now()
        code = f"{INVITE_CODE_PREFIX}{secrets.token_urlsafe(12)}"
        code_hash = sha256(code.encode("utf-8")).hexdigest()
        invite_id = f"invite-{datetime.now(UTC).strftime('%Y%m%d')}-{secrets.token_hex(3)}"
        encrypted_code = (
            self._invitation_vault.seal(invite_id, code)
            if self._invitation_vault is not None
            else ""
        )
        code_preview = mask_invitation_code(code) if encrypted_code else ""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO invitations (
                    invite_id, code_hash, label, status, daily_budget_micros, expires_at,
                    auth_version, created_at, updated_at, last_used_at,
                    encrypted_code, code_preview
                ) VALUES (?, ?, ?, 'enabled', ?, ?, 1, ?, ?, '', ?, ?)
                """,
                (
                    invite_id,
                    code_hash,
                    clean_label,
                    clean_budget,
                    clean_expiry,
                    now,
                    now,
                    encrypted_code,
                    code_preview,
                ),
            )
            after = self._invitation_row(connection, invite_id)
            self._write_audit(
                connection,
                actor,
                "invitation.create",
                "invitation",
                invite_id,
                {},
                _public_invitation(after),
            )
        return _record(after), code

    def list_invitations(self, *, include_archived: bool = False) -> list[InvitationRecord]:
        clause = "" if include_archived else "WHERE status != 'archived'"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM invitations {clause} ORDER BY created_at DESC"
            ).fetchall()
        return [_record(row) for row in rows]

    def get_invitation(self, invite_id: str) -> InvitationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM invitations WHERE invite_id = ?", (str(invite_id),)
            ).fetchone()
        return _record(row) if row else None

    def update_invitation(
        self,
        invite_id: str,
        *,
        label: str | object = _UNSET,
        daily_budget_micros: int | None | object = _UNSET,
        expires_at: str | object = _UNSET,
        actor: str = "owner",
    ) -> InvitationRecord:
        updates: list[str] = []
        values: list[object] = []
        if label is not _UNSET:
            updates.append("label = ?")
            values.append(_clean_label(str(label)))
        if daily_budget_micros is not _UNSET:
            updates.append("daily_budget_micros = ?")
            values.append(_validate_optional_budget(daily_budget_micros))
        if expires_at is not _UNSET:
            updates.append("expires_at = ?")
            values.append(_normalize_expiry(str(expires_at)))
        if not updates:
            current = self.get_invitation(invite_id)
            if current is None:
                raise KeyError("invitation not found")
            return current
        updates.append("updated_at = ?")
        values.append(_utc_now())
        values.append(str(invite_id))
        with self._lock, self._connect() as connection:
            before = self._invitation_row(connection, invite_id)
            connection.execute(
                f"UPDATE invitations SET {', '.join(updates)} WHERE invite_id = ?", values
            )
            after = self._invitation_row(connection, invite_id)
            self._write_audit(
                connection,
                actor,
                "invitation.update",
                "invitation",
                str(invite_id),
                _public_invitation(before),
                _public_invitation(after),
            )
        return _record(after)

    def set_invitation_status(
        self, invite_id: str, status: str, *, actor: str = "owner"
    ) -> InvitationRecord:
        clean_status = str(status).strip().lower()
        if clean_status not in {"enabled", "disabled", "archived"}:
            raise ValueError("invalid invitation status")
        now = _utc_now()
        with self._lock, self._connect() as connection:
            before = self._invitation_row(connection, invite_id)
            version_delta = 1 if clean_status in {"disabled", "archived"} and before["status"] != clean_status else 0
            connection.execute(
                """
                UPDATE invitations
                SET status = ?, auth_version = auth_version + ?, updated_at = ?
                WHERE invite_id = ?
                """,
                (clean_status, version_delta, now, str(invite_id)),
            )
            after = self._invitation_row(connection, invite_id)
            self._write_audit(
                connection,
                actor,
                f"invitation.{clean_status}",
                "invitation",
                str(invite_id),
                _public_invitation(before),
                _public_invitation(after),
            )
        return _record(after)

    def reset_invitation_code(
        self, invite_id: str, *, actor: str = "owner"
    ) -> tuple[InvitationRecord, str]:
        code = f"{INVITE_CODE_PREFIX}{secrets.token_urlsafe(12)}"
        now = _utc_now()
        encrypted_code = (
            self._invitation_vault.seal(str(invite_id), code)
            if self._invitation_vault is not None
            else ""
        )
        code_preview = mask_invitation_code(code) if encrypted_code else ""
        with self._lock, self._connect() as connection:
            before = self._invitation_row(connection, invite_id)
            if before["status"] == "archived":
                raise ValueError("archived invitation cannot be reset")
            connection.execute(
                """
                UPDATE invitations
                SET code_hash = ?, encrypted_code = ?, code_preview = ?,
                    auth_version = auth_version + 1, updated_at = ?
                WHERE invite_id = ?
                """,
                (
                    sha256(code.encode("utf-8")).hexdigest(),
                    encrypted_code,
                    code_preview,
                    now,
                    str(invite_id),
                ),
            )
            after = self._invitation_row(connection, invite_id)
            self._write_audit(
                connection,
                actor,
                "invitation.reset",
                "invitation",
                str(invite_id),
                _public_invitation(before),
                _public_invitation(after),
            )
        return _record(after), code

    def reveal_invitation_code(
        self, invite_id: str, *, actor: str = "owner"
    ) -> str | None:
        if self._invitation_vault is None:
            return None
        with self._lock, self._connect() as connection:
            row = self._invitation_row(connection, invite_id)
            encrypted = str(row["encrypted_code"] or "")
            if not encrypted:
                return None
            code = self._invitation_vault.open(str(invite_id), encrypted)
            candidate = sha256(code.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(candidate, str(row["code_hash"])):
                raise RuntimeError("stored invitation code failed integrity verification")
            self._write_audit(
                connection,
                actor,
                "invitation.reveal",
                "invitation",
                str(invite_id),
                {},
                {"revealed": True},
            )
        return code

    def authenticate_invitation(self, code: str) -> InvitationRecord | None:
        candidate = sha256(str(code or "").strip().encode("utf-8")).hexdigest()
        matched: sqlite3.Row | None = None
        now = _utc_now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invitations WHERE status = 'enabled'"
            ).fetchall()
            for row in rows:
                if _is_expired(str(row["expires_at"])):
                    continue
                if hmac.compare_digest(candidate, str(row["code_hash"])):
                    matched = row
            if matched is not None:
                connection.execute(
                    "UPDATE invitations SET last_used_at = ?, updated_at = ? WHERE invite_id = ?",
                    (now, now, str(matched["invite_id"])),
                )
                matched = self._invitation_row(connection, str(matched["invite_id"]))
        return _record(matched) if matched else None

    def active_invitation(self, invite_id: str, auth_version: int) -> InvitationRecord | None:
        record = self.get_invitation(invite_id)
        if record is None or record.status != "enabled" or record.auth_version != int(auth_version):
            return None
        return None if _is_expired(record.expires_at) else record

    def settings(self) -> dict[str, object]:
        with self._connect() as connection:
            return self._settings_from_connection(connection)

    def update_settings(
        self,
        *,
        global_daily_budget_micros: int,
        default_invite_daily_budget_micros: int,
        feedback_retention_days: int,
        actor: str = "owner",
    ) -> dict[str, object]:
        global_budget = _validate_budget(global_daily_budget_micros)
        invite_budget = _validate_budget(default_invite_daily_budget_micros)
        retention = int(feedback_retention_days)
        if not 1 <= retention <= 365:
            raise ValueError("feedback retention must be between 1 and 365 days")
        before = self.settings()
        now = _utc_now()
        updates = {
            "global_daily_budget_micros": str(global_budget),
            "default_invite_daily_budget_micros": str(invite_budget),
            "feedback_retention_days": str(retention),
        }
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO admin_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value, updated_at = excluded.updated_at
                """,
                [(key, value, now) for key, value in updates.items()],
            )
            after = self._settings_from_connection(connection)
            self._write_audit(
                connection, actor, "settings.update", "settings", "budget", before, after
            )
        return after

    def budget_limits_for(self, identity_key: str) -> BudgetLimits:
        settings = self.settings()
        identity_budget = int(settings["default_invite_daily_budget_micros"])
        record = self.get_invitation(identity_key)
        if record is not None and record.daily_budget_micros is not None:
            identity_budget = record.daily_budget_micros
        return BudgetLimits(
            global_daily_micros=int(settings["global_daily_budget_micros"]),
            identity_daily_micros=identity_budget,
        )

    def list_audit(self, *, limit: int = 50) -> list[dict[str, object]]:
        safe_limit = min(200, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_audit ORDER BY created_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [
            {
                "audit_id": str(row["audit_id"]),
                "actor": str(row["actor"]),
                "action": str(row["action"]),
                "target_type": str(row["target_type"]),
                "target_id": str(row["target_id"]),
                "before": json.loads(str(row["before_json"])),
                "after": json.loads(str(row["after_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def preflight_legacy_config(
        self,
        config_path: str | Path,
        *,
        require_status_match: bool = False,
    ) -> LegacyImportReport:
        entries, secret = _load_legacy_config(config_path)
        with self._connect() as connection:
            return self._legacy_import_report(
                connection,
                entries,
                secret,
                require_status_match=require_status_match,
            )

    def import_legacy_config(
        self,
        config_path: str | Path,
        *,
        actor: str = "migration",
        require_status_match: bool = False,
    ) -> LegacyImportReport:
        entries, secret = _load_legacy_config(config_path)
        now = _utc_now()
        with self._lock, self._connect() as connection:
            report = self._legacy_import_report(
                connection,
                entries,
                secret,
                require_status_match=require_status_match,
            )
            if not report.can_apply:
                raise ValueError("legacy invitation import has conflicts")
            if report.cookie_secret_action == "replace_with_legacy":
                connection.execute(
                    "UPDATE admin_meta SET meta_value = ? WHERE meta_key = 'invite_cookie_secret'",
                    (_encode_secret(secret),),
                )
            existing_ids = {
                str(row["invite_id"])
                for row in connection.execute("SELECT invite_id FROM invitations")
            }
            for entry in entries:
                if entry.invite_id in existing_ids:
                    continue
                connection.execute(
                    """
                    INSERT INTO invitations (
                        invite_id, code_hash, label, status, daily_budget_micros,
                        expires_at, auth_version, created_at, updated_at, last_used_at
                    ) VALUES (?, ?, ?, ?, NULL, '', 1, ?, ?, '')
                    """,
                    (entry.invite_id, entry.code_hash, entry.invite_id, entry.status, now, now),
                )
            if report.insert_count or report.cookie_secret_action == "replace_with_legacy":
                self._write_audit(
                    connection,
                    actor,
                    "invitation.import",
                    "invitation",
                    "legacy-config",
                    {},
                    {
                        "inserted": report.insert_count,
                        "unchanged": report.unchanged_count,
                        "cookie_secret_action": report.cookie_secret_action,
                    },
                )
        return report

    def _legacy_import_report(
        self,
        connection: sqlite3.Connection,
        entries: tuple[_LegacyInvitationEntry, ...],
        secret: bytes,
        *,
        require_status_match: bool = False,
    ) -> LegacyImportReport:
        rows = connection.execute(
            "SELECT invite_id, code_hash, status, auth_version FROM invitations"
        ).fetchall()
        by_id = {str(row["invite_id"]): str(row["code_hash"]) for row in rows}
        status_by_id = {str(row["invite_id"]): str(row["status"]) for row in rows}
        auth_version_by_id = {
            str(row["invite_id"]): int(row["auth_version"]) for row in rows
        }
        by_hash = {str(row["code_hash"]): str(row["invite_id"]) for row in rows}
        insert_count = 0
        unchanged_count = 0
        conflicts: list[LegacyImportConflict] = []
        for entry in entries:
            existing_hash = by_id.get(entry.invite_id)
            existing_owner = by_hash.get(entry.code_hash)
            if existing_hash is not None:
                if hmac.compare_digest(existing_hash, entry.code_hash):
                    if (
                        require_status_match
                        and status_by_id[entry.invite_id] != entry.status
                    ):
                        conflicts.append(
                            LegacyImportConflict("invitation_status_mismatch", entry.invite_id)
                        )
                    elif (
                        require_status_match
                        and auth_version_by_id[entry.invite_id] != 1
                    ):
                        conflicts.append(
                            LegacyImportConflict(
                                "invitation_auth_version_mismatch", entry.invite_id
                            )
                        )
                    else:
                        unchanged_count += 1
                else:
                    conflicts.append(
                        LegacyImportConflict("invite_id_hash_mismatch", entry.invite_id)
                    )
                continue
            if existing_owner is not None:
                conflicts.append(
                    LegacyImportConflict(
                        "code_hash_owned_by_other_invitation",
                        entry.invite_id,
                        existing_owner,
                    )
                )
                continue
            insert_count += 1
        current_secret = _decode_secret(self._meta_from_connection(connection, "invite_cookie_secret"))
        secret_action = (
            "unchanged"
            if hmac.compare_digest(current_secret, secret)
            else "replace_with_legacy"
        )
        return LegacyImportReport(
            source_count=len(entries),
            existing_count=len(rows),
            insert_count=insert_count,
            unchanged_count=unchanged_count,
            cookie_secret_action=secret_action,
            conflicts=tuple(conflicts),
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_account (
                    admin_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    credential_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    invite_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    daily_budget_micros INTEGER,
                    expires_at TEXT NOT NULL,
                    auth_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    encrypted_code TEXT NOT NULL DEFAULT '',
                    code_preview TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_invitations_status ON invitations(status);
                CREATE TABLE IF NOT EXISTS admin_audit (
                    audit_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at);
                """
            )
            invitation_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(invitations)").fetchall()
            }
            if "encrypted_code" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE invitations ADD COLUMN encrypted_code TEXT NOT NULL DEFAULT ''"
                )
            if "code_preview" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE invitations ADD COLUMN code_preview TEXT NOT NULL DEFAULT ''"
                )
            now = _utc_now()
            defaults = {
                "schema_version": str(CONTROL_SCHEMA_VERSION),
                "invite_cookie_secret": _encode_secret(secrets.token_bytes(32)),
                "admin_cookie_secret": _encode_secret(secrets.token_bytes(32)),
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO admin_meta (meta_key, meta_value) VALUES (?, ?)",
                    (key, value),
                )
            connection.execute(
                "UPDATE admin_meta SET meta_value = ? WHERE meta_key = 'schema_version'",
                (str(CONTROL_SCHEMA_VERSION),),
            )
            settings = {
                "global_daily_budget_micros": str(DEFAULT_GLOBAL_DAILY_BUDGET_MICROS),
                "default_invite_daily_budget_micros": str(DEFAULT_INVITE_DAILY_BUDGET_MICROS),
                "feedback_retention_days": str(DEFAULT_FEEDBACK_RETENTION_DAYS),
                "budget_timezone": "Asia/Shanghai",
            }
            for key, value in settings.items():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO admin_settings (setting_key, setting_value, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, value, now),
                )

    def _meta(self, key: str) -> str:
        with self._connect() as connection:
            return self._meta_from_connection(connection, key)

    @staticmethod
    def _meta_from_connection(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT meta_value FROM admin_meta WHERE meta_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError(f"missing control metadata: {key}")
        return str(row[0])

    def _settings_from_connection(self, connection: sqlite3.Connection) -> dict[str, object]:
        rows = connection.execute(
            "SELECT setting_key, setting_value FROM admin_settings"
        ).fetchall()
        raw = {str(row["setting_key"]): str(row["setting_value"]) for row in rows}
        return {
            "global_daily_budget_micros": int(raw["global_daily_budget_micros"]),
            "default_invite_daily_budget_micros": int(raw["default_invite_daily_budget_micros"]),
            "feedback_retention_days": int(raw["feedback_retention_days"]),
            "budget_timezone": raw.get("budget_timezone", "Asia/Shanghai"),
        }

    def _invitation_row(self, connection: sqlite3.Connection, invite_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM invitations WHERE invite_id = ?", (str(invite_id),)
        ).fetchone()
        if row is None:
            raise KeyError("invitation not found")
        return row

    def _write_audit(
        self,
        connection: sqlite3.Connection,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO admin_audit (
                audit_id, actor, action, target_type, target_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                str(actor),
                str(action),
                str(target_type),
                str(target_id),
                json.dumps(before, ensure_ascii=False, separators=(",", ":")),
                json.dumps(after, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def cny_to_micros(value: object) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("invalid CNY amount") from exc
    micros = int(amount * Decimal("1000000"))
    return _validate_budget(micros)


def micros_to_cny(value: int) -> str:
    return format(Decimal(int(value)) / Decimal("1000000"), ".2f")


def _record(row: sqlite3.Row) -> InvitationRecord:
    return InvitationRecord(
        invite_id=str(row["invite_id"]),
        label=str(row["label"]),
        status=str(row["status"]),
        daily_budget_micros=(
            int(row["daily_budget_micros"]) if row["daily_budget_micros"] is not None else None
        ),
        expires_at=str(row["expires_at"]),
        auth_version=max(1, int(row["auth_version"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_used_at=str(row["last_used_at"]),
        code_preview=str(row["code_preview"] or ""),
        code_recoverable=bool(row["encrypted_code"]),
    )


def _public_invitation(row: sqlite3.Row) -> dict[str, object]:
    return _record(row).to_dict()


def _load_legacy_config(
    config_path: str | Path,
) -> tuple[tuple[_LegacyInvitationEntry, ...], bytes]:
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid legacy invitation configuration") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid legacy invitation configuration")
    invitations = data.get("invitations")
    if int(data.get("version", 0)) != 1 or not isinstance(invitations, list) or not invitations:
        raise ValueError("invalid legacy invitation configuration")
    secret = _decode_secret(str(data.get("cookie_secret") or ""))
    if len(secret) < 32:
        raise ValueError("legacy invitation cookie secret is too short")
    entries: list[_LegacyInvitationEntry] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for item in invitations:
        if not isinstance(item, dict):
            raise ValueError("invalid legacy invitation entry")
        invite_id = str(item.get("id") or "").strip()
        code_hash = str(item.get("code_hash") or "").strip().lower()
        if (
            not invite_id
            or len(code_hash) != 64
            or any(character not in "0123456789abcdef" for character in code_hash)
        ):
            raise ValueError("invalid legacy invitation entry")
        if invite_id in seen_ids:
            raise ValueError("duplicate legacy invitation id")
        if code_hash in seen_hashes:
            raise ValueError("duplicate legacy invitation code hash")
        seen_ids.add(invite_id)
        seen_hashes.add(code_hash)
        entries.append(
            _LegacyInvitationEntry(
                invite_id=invite_id,
                code_hash=code_hash,
                status="enabled" if item.get("enabled", True) is True else "disabled",
            )
        )
    return tuple(entries), secret


def _clean_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 80:
        raise ValueError("invitation label must contain 1 to 80 characters")
    return clean


def _validate_password(value: str) -> str:
    clean = str(value or "")
    if len(clean) < 12 or len(clean) > 256:
        raise ValueError("administrator password must contain 12 to 256 characters")
    return clean


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$%s$%s" % (_encode_secret(salt), _encode_secret(digest))


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        expected = _decode_secret(digest_text)
        actual = scrypt(
            password.encode("utf-8"),
            salt=_decode_secret(salt_text),
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _validate_budget(value: object) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid budget") from exc
    if result <= 0 or result > 10_000 * 1_000_000:
        raise ValueError("budget must be greater than 0 and no more than 10000 CNY")
    return result


def _validate_optional_budget(value: object) -> int | None:
    return None if value is None else _validate_budget(value)


def _normalize_expiry(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid invitation expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _is_expired(expires_at: str) -> bool:
    return bool(expires_at and datetime.fromisoformat(expires_at) <= datetime.now(UTC))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _encode_secret(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_secret(value: str) -> bytes:
    clean = str(value).strip()
    decoded = urlsafe_b64decode(clean + "=" * (-len(clean) % 4))
    if len(decoded) < 16:
        raise ValueError("secret is too short")
    return decoded

"""Stateless signed sessions for the isolated administrator service."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import secrets
import time

from tiku_admin.control_store import SQLiteControlStore
from tiku_agent.invite_access import InviteIdentity


ADMIN_COOKIE = "tiku_admin_session"
DEFAULT_ADMIN_MAX_AGE_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class AdminSession:
    credential_version: int
    csrf_token: str
    expires_at: int


class AdminSessionAuth:
    def __init__(
        self,
        store: SQLiteControlStore,
        *,
        cookie_name: str = ADMIN_COOKIE,
        max_age_seconds: int = DEFAULT_ADMIN_MAX_AGE_SECONDS,
    ) -> None:
        self.store = store
        self.cookie_name = str(cookie_name).strip()
        self.max_age_seconds = max(300, int(max_age_seconds))

    def issue_cookie(self, *, now: int | None = None) -> str:
        payload = {
            "v": self.store.admin_credential_version(),
            "csrf": secrets.token_urlsafe(24),
            "exp": int(now if now is not None else time.time()) + self.max_age_seconds,
        }
        return self._sign(payload)

    def verify_cookie(self, value: str, *, now: int | None = None) -> AdminSession | None:
        try:
            encoded, supplied_signature = str(value or "").split(".", 1)
            expected = hmac.new(
                self.store.admin_cookie_secret, encoded.encode("ascii"), sha256
            ).digest()
            if not hmac.compare_digest(expected, _decode(supplied_signature)):
                return None
            payload = json.loads(_decode(encoded).decode("utf-8"))
            version = int(payload["v"])
            expires_at = int(payload["exp"])
            csrf = str(payload["csrf"])
            if expires_at < int(now if now is not None else time.time()):
                return None
            if version <= 0 or version != self.store.admin_credential_version() or len(csrf) < 20:
                return None
            return AdminSession(version, csrf, expires_at)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def verify_csrf(self, session: AdminSession | None, supplied: str) -> bool:
        return bool(
            session
            and supplied
            and hmac.compare_digest(session.csrf_token, str(supplied).strip())
        )

    def _sign(self, payload: dict[str, object]) -> str:
        encoded = urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = hmac.new(
            self.store.admin_cookie_secret, encoded.encode("ascii"), sha256
        ).digest()
        return f"{encoded}.{urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


class SQLiteInviteAccess:
    """Dynamic invitation authority backed by the shared control store."""

    def __init__(
        self,
        store: SQLiteControlStore,
        *,
        cookie_name: str = "tiku_agent_invite",
        auth_max_age_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        self.store = store
        self.cookie_name = str(cookie_name).strip()
        self.auth_max_age_seconds = max(60, int(auth_max_age_seconds))

    def authenticate_code(self, code: str) -> InviteIdentity | None:
        record = self.store.authenticate_invitation(code)
        return InviteIdentity(record.invite_id, record.auth_version) if record else None

    def issue_cookie(self, identity: InviteIdentity, *, now: int | None = None) -> str:
        record = self.store.active_invitation(identity.invite_id, identity.auth_version)
        if record is None:
            raise ValueError("invitation is disabled, expired, or stale")
        expires_at = int(now if now is not None else time.time()) + self.auth_max_age_seconds
        payload = f"{record.invite_id}:{record.auth_version}:{expires_at}".encode("utf-8")
        encoded = urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(
            self.store.invite_cookie_secret, encoded.encode("ascii"), sha256
        ).digest()
        return f"{encoded}.{urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"

    def verify_cookie(self, value: str, *, now: int | None = None) -> InviteIdentity | None:
        try:
            encoded, supplied_signature = str(value or "").split(".", 1)
            expected = hmac.new(
                self.store.invite_cookie_secret, encoded.encode("ascii"), sha256
            ).digest()
            if not hmac.compare_digest(expected, _decode(supplied_signature)):
                return None
            payload = _decode(encoded).decode("utf-8")
            parts = payload.rsplit(":", 2)
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                invite_id, version_text, expires_text = parts
            else:
                invite_id, expires_text = payload.rsplit(":", 1)
                version_text = "1"
            if int(expires_text) < int(now if now is not None else time.time()):
                return None
            record = self.store.active_invitation(invite_id, int(version_text))
            return InviteIdentity(record.invite_id, record.auth_version) if record else None
        except (ValueError, UnicodeDecodeError):
            return None


def _decode(value: str) -> bytes:
    clean = str(value).strip()
    return urlsafe_b64decode(clean + "=" * (-len(clean) % 4))

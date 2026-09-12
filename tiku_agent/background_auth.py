"""Isolated background HTTP login sessions, with server-side logout binding."""
from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import time

from tiku_admin.auth import SQLiteInviteAccess
from tiku_agent.invite_access import InviteIdentity


@dataclass(frozen=True)
class BackgroundLogin:
    identity: InviteIdentity
    login_id: str
    expires_at: int


class BackgroundInviteAccess(SQLiteInviteAccess):
    """A separate cookie protocol; legacy authentication behavior is unchanged."""
    def __init__(self, store, *, cookie_name="tiku_phase6_invite", auth_max_age_seconds=30 * 86400):
        if cookie_name in {"tiku_agent_invite", "tiku_agent_session", "tiku_admin_session"}:
            raise ValueError("background HTTP requires a separate invitation cookie")
        super().__init__(store, cookie_name=cookie_name, auth_max_age_seconds=auth_max_age_seconds)
        self.register_session = None

    def issue_cookie(self, identity, *, now=None):
        record = self.store.active_invitation(identity.invite_id, identity.auth_version)
        if record is None:
            raise ValueError("invitation is unavailable")
        payload = {"v": 1, "id": record.invite_id, "version": record.auth_version,
                   "exp": int(time.time() if now is None else now) + self.auth_max_age_seconds,
                   "nonce": secrets.token_hex(16)}
        encoded = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(self.store.invite_cookie_secret, encoded.encode(), hashlib.sha256).hexdigest()
        cookie = encoded + "." + signature
        if self.register_session is not None:
            self.register_session(hashlib.sha256(cookie.encode()).hexdigest(), payload["exp"])
        return cookie

    def verify_session(self, value, *, now=None):
        try:
            if type(value) is not str or len(value) > 1024:
                return None
            encoded, signature = value.split(".")
            expected = hmac.new(self.store.invite_cookie_secret, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if (set(data) != {"v", "id", "version", "exp", "nonce"} or data["v"] != 1
                    or type(data["exp"]) is not int or data["exp"] <= (time.time() if now is None else now)
                    or type(data["version"]) is not int or type(data["nonce"]) is not str or len(data["nonce"]) != 32):
                return None
            record = self.store.active_invitation(data["id"], data["version"])
            if record is None:
                return None
            return BackgroundLogin(InviteIdentity(record.invite_id, record.auth_version),
                                   hashlib.sha256(value.encode()).hexdigest(), data["exp"])
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return None

    def verify_cookie(self, value, *, now=None):
        session = self.verify_session(value, now=now)
        return session.identity if session else None

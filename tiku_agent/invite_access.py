"""Application-level invitation access for the public 8790 web route."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any


INVITE_COOKIE = "tiku_agent_invite"
INVITE_CONFIG_VERSION = 1
DEFAULT_AUTH_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class InviteIdentity:
    invite_id: str
    auth_version: int = 1


class InviteAccess:
    """Validate invitation codes and issue signed, expiring browser cookies."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        cookie_name: str = INVITE_COOKIE,
        auth_max_age_seconds: int = DEFAULT_AUTH_MAX_AGE_SECONDS,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.cookie_name = str(cookie_name).strip()
        self.auth_max_age_seconds = max(60, int(auth_max_age_seconds))
        if not self.cookie_name:
            raise ValueError("invite cookie name is required")
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != INVITE_CONFIG_VERSION:
            raise ValueError("unsupported invitation config version")
        self._secret = _decode_secret(str(data.get("cookie_secret") or ""))
        invitations = data.get("invitations")
        if not isinstance(invitations, list) or not invitations:
            raise ValueError("at least one invitation is required")
        self._code_hashes: dict[str, str] = {}
        self._enabled_ids: set[str] = set()
        for item in invitations:
            if not isinstance(item, dict):
                raise ValueError("invalid invitation entry")
            invite_id = str(item.get("id") or "").strip()
            code_hash = str(item.get("code_hash") or "").strip().lower()
            if not invite_id or len(code_hash) != 64:
                raise ValueError("invalid invitation id or code hash")
            if invite_id in self._code_hashes:
                raise ValueError("duplicate invitation id")
            self._code_hashes[invite_id] = code_hash
            if item.get("enabled", True) is True:
                self._enabled_ids.add(invite_id)

    def authenticate_code(self, code: str) -> InviteIdentity | None:
        candidate = sha256(str(code or "").strip().encode("utf-8")).hexdigest()
        matched_id = ""
        for invite_id, stored_hash in self._code_hashes.items():
            if hmac.compare_digest(candidate, stored_hash) and invite_id in self._enabled_ids:
                matched_id = invite_id
        return InviteIdentity(matched_id) if matched_id else None

    def issue_cookie(self, identity: InviteIdentity, *, now: int | None = None) -> str:
        if identity.invite_id not in self._enabled_ids:
            raise ValueError("invitation is disabled")
        expires_at = int(now if now is not None else time.time()) + self.auth_max_age_seconds
        payload = f"{identity.invite_id}:{expires_at}".encode("utf-8")
        encoded = urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(self._secret, encoded.encode("ascii"), sha256).digest()
        return f"{encoded}.{urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"

    def verify_cookie(self, value: str, *, now: int | None = None) -> InviteIdentity | None:
        try:
            encoded, supplied_signature = str(value or "").split(".", 1)
            expected = hmac.new(self._secret, encoded.encode("ascii"), sha256).digest()
            supplied = _decode_urlsafe(supplied_signature)
            if not hmac.compare_digest(expected, supplied):
                return None
            payload = _decode_urlsafe(encoded).decode("utf-8")
            invite_id, expires_text = payload.rsplit(":", 1)
            if invite_id not in self._enabled_ids:
                return None
            if int(expires_text) < int(now if now is not None else time.time()):
                return None
            return InviteIdentity(invite_id)
        except (ValueError, UnicodeDecodeError):
            return None


def build_invitation_config(count: int) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Create a hash-only config and one-time plaintext delivery list."""
    count = int(count)
    if count <= 0 or count > 1000:
        raise ValueError("invitation count must be between 1 and 1000")
    codes: list[tuple[str, str]] = []
    invitations = []
    for number in range(1, count + 1):
        invite_id = f"invite-{number:03d}"
        code = f"TIKU-{secrets.token_urlsafe(12)}"
        codes.append((invite_id, code))
        invitations.append({
            "id": invite_id,
            "code_hash": sha256(code.encode("utf-8")).hexdigest(),
            "enabled": True,
        })
    return ({
        "version": INVITE_CONFIG_VERSION,
        "cookie_secret": urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="),
        "invitations": invitations,
    }, codes)


def _decode_secret(value: str) -> bytes:
    secret = _decode_urlsafe(value)
    if len(secret) < 32:
        raise ValueError("invitation cookie secret must contain at least 32 bytes")
    return secret


def _decode_urlsafe(value: str) -> bytes:
    clean = str(value).strip()
    return urlsafe_b64decode(clean + "=" * (-len(clean) % 4))

"""Recoverable invitation-code encryption for the isolated admin service."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
import os
from pathlib import Path
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEY_BYTES = 32
NONCE_BYTES = 12
TOKEN_VERSION = "v1"


class InvitationCodeVault:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("invitation encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def load_or_create(cls, path: str | Path) -> "InvitationCodeVault":
        key_path = Path(path).resolve()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            encoded = key_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            encoded = _encode(secrets.token_bytes(KEY_BYTES))
            try:
                descriptor = os.open(
                    key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                encoded = key_path.read_text(encoding="ascii").strip()
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(encoded)
                    handle.write("\n")
        try:
            key = _decode(encoded)
        except ValueError as exc:
            raise ValueError("invalid invitation encryption key file") from exc
        return cls(key)

    def seal(self, invite_id: str, code: str) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            str(code).encode("utf-8"),
            _associated_data(invite_id),
        )
        return f"{TOKEN_VERSION}.{_encode(nonce + ciphertext)}"

    def open(self, invite_id: str, token: str) -> str:
        version, separator, encoded = str(token or "").partition(".")
        if version != TOKEN_VERSION or separator != ".":
            raise ValueError("unsupported invitation ciphertext")
        payload = _decode(encoded)
        if len(payload) <= NONCE_BYTES:
            raise ValueError("invalid invitation ciphertext")
        try:
            plaintext = self._cipher.decrypt(
                payload[:NONCE_BYTES],
                payload[NONCE_BYTES:],
                _associated_data(invite_id),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise ValueError("invalid invitation ciphertext") from exc


def mask_invitation_code(code: str) -> str:
    clean = str(code or "").strip()
    prefix = "TIKU-" if clean.startswith("TIKU-") else ""
    body = clean[len(prefix):]
    if len(body) < 9:
        return f"{prefix}{body[:2]}******{body[-2:]}"
    return f"{prefix}{body[:4]}******{body[-4:]}"


def _associated_data(invite_id: str) -> bytes:
    return f"tiku-admin-invitation:{str(invite_id)}".encode("utf-8")


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    clean = str(value or "").strip()
    return urlsafe_b64decode(clean + "=" * (-len(clean) % 4))

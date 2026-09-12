"""Verify the raw Feishu callback before trusting any sender or business command.

Protocol: official larksuite/node-sdk dispatcher/request-handle.ts and
utils/aes-cipher.ts. The signature covers timestamp + nonce + Encrypt Key +
the ORIGINAL request body. Re-serializing parsed JSON changes those bytes.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
import time


MAX_EVENT_BYTES = 1024 * 1024
_current_sender = ContextVar("verified_feishu_sender", default=None)


class EventSecurityError(ValueError):
    """Stable public failure; do not include payloads, credentials or decode errors."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EventSecurityError("invalid-event")
        result[key] = value
    return result


def _object(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise EventSecurityError("invalid-event") from None


@dataclass(frozen=True)
class VerifiedCallback:
    payload: dict
    app_id: str
    sender: str | None


class FeishuEventVerifier:
    def __init__(self, *, encrypt_key, verification_token, app_id, now=time.time, max_age_seconds=300):
        if not all(isinstance(value, str) and value.strip() for value in (encrypt_key, verification_token, app_id)):
            raise EventSecurityError("event-security-configuration-required")
        if not isinstance(max_age_seconds, int) or not 1 <= max_age_seconds <= 900:
            raise EventSecurityError("invalid-event-age-limit")
        self.encrypt_key, self.verification_token, self.app_id = encrypt_key, verification_token, app_id
        self.now, self.max_age_seconds = now, max_age_seconds

    def verify(self, raw, headers):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_EVENT_BYTES:
            raise EventSecurityError("invalid-event-size")
        values = {}
        for key, value in headers.items():
            name = str(key).lower()
            if name in ("x-lark-request-timestamp", "x-lark-request-nonce", "x-lark-signature"):
                if name in values or not isinstance(value, str):
                    raise EventSecurityError("invalid-event-signature")
                values[name] = value
        stamp, nonce, signature = (values.get(key, "") for key in
            ("x-lark-request-timestamp", "x-lark-request-nonce", "x-lark-signature"))
        if (not re.fullmatch(r"[0-9]{10}", stamp) or abs(self.now() - int(stamp)) > self.max_age_seconds
                or not 1 <= len(nonce) <= 256 or any(ord(char) < 32 for char in nonce)
                or not re.fullmatch(r"[a-fA-F0-9]{64}", signature)):
            raise EventSecurityError("invalid-event-signature")
        expected = hashlib.sha256((stamp + nonce + self.encrypt_key).encode() + raw).hexdigest()
        if not hmac.compare_digest(expected, signature.lower()):
            raise EventSecurityError("invalid-event-signature")
        payload = _object(raw)
        if "encrypt" in payload:
            if set(payload) != {"encrypt"} or not isinstance(payload["encrypt"], str):
                raise EventSecurityError("invalid-encrypted-event")
            try:
                # Already present in the bank Python environment; required only
                # for the explicitly configured encrypted callback mode.
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.primitives import padding
                data = base64.b64decode(payload["encrypt"], validate=True)
                if len(data) < 32 or (len(data) - 16) % 16:
                    raise ValueError()
                key = hashlib.sha256(self.encrypt_key.encode()).digest()
                decryptor = Cipher(algorithms.AES(key), modes.CBC(data[:16])).decryptor()
                padded = decryptor.update(data[16:]) + decryptor.finalize()
                unpadder = padding.PKCS7(128).unpadder()
                payload = _object(unpadder.update(padded) + unpadder.finalize())
            except Exception:
                raise EventSecurityError("invalid-encrypted-event") from None
        header = payload.get("header", {})
        if not isinstance(header, dict):
            raise EventSecurityError("invalid-event")
        token = header.get("token", payload.get("token"))
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.verification_token.encode()):
            raise EventSecurityError("invalid-verification-token")
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not 1 <= len(challenge) <= 512:
                raise EventSecurityError("invalid-event")
            return VerifiedCallback({"type": "url_verification", "challenge": challenge}, self.app_id, None)
        if payload.get("schema") != "2.0" or header.get("app_id") != self.app_id:
            raise EventSecurityError("invalid-event-application")
        event = payload.get("event", {})
        if not isinstance(event, dict):
            raise EventSecurityError("invalid-event")
        sender = None
        if header.get("event_type") == "im.message.receive_v1":
            message = event.get("message")
            if not isinstance(message, dict):
                raise EventSecurityError("invalid-event-message")
            if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
                       for value in (header.get("event_id"), message.get("message_id"), message.get("chat_id"))):
                raise EventSecurityError("invalid-event-message")
            created = message.get("create_time")
            if not isinstance(created, str) or not re.fullmatch(r"[0-9]{13}", created):
                raise EventSecurityError("invalid-event-message")
            identity = event.get("sender", {})
            if not isinstance(identity, dict) or identity.get("sender_type") != "user":
                raise EventSecurityError("invalid-event-sender")
            ids = identity.get("sender_id", {})
            sender = ids.get("open_id") if isinstance(ids, dict) else None
            if not isinstance(sender, str) or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,128}", sender):
                raise EventSecurityError("invalid-event-sender")
        # Do not retain the verification token in downstream state/logs/queues.
        payload["header"] = {key: value for key, value in header.items() if key != "token"}
        payload.pop("token", None)
        return VerifiedCallback(payload, self.app_id, sender)


@contextmanager
def verified_sender(callback):
    if not isinstance(callback, VerifiedCallback):
        raise EventSecurityError("verified-event-required")
    token = _current_sender.set(callback.sender)
    try:
        yield
    finally:
        _current_sender.reset(token)


def require_maintainer(sender, maintainers):
    if not maintainers or sender not in maintainers or _current_sender.get() != sender:
        raise EventSecurityError("题库维护需要已核验的维护者身份")

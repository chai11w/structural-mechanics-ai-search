import base64
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from scripts.feishu_event_security import (
    EventSecurityError, FeishuEventVerifier, MAX_EVENT_BYTES, require_maintainer, verified_sender,
)
from scripts.feishu_tiku_bot import FeishuTikuBridge, FeishuTikuOptions, TikuBot, TikuSession, TikuSessionStore
from scripts.feishu_store_flow import FeishuStoreService
from scripts.feishu_delete_flow import FeishuDeleteService


KEY = "fixture-encrypt-key-for-tests-only"
TOKEN = "fixture-verification-token"
APP = "cli_fixture"
OWNER = "ou_fixture_owner"


def event():
    return {"schema": "2.0", "header": {"event_id": "event_123", "event_type": "im.message.receive_v1", "token": TOKEN, "app_id": APP},
            "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": OWNER}},
                      "message": {"message_id": "om_fixture", "chat_id": "oc_fixture", "create_time": str(int(time.time() * 1000)),
                                  "message_type": "text", "content": json.dumps({"text": "+"})}}}


def signed(payload, *, encrypted=False, stamp=None):
    if encrypted:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(raw) + padder.finalize()
        iv = b"fixture-test-iv!"
        assert len(iv) == 16
        cipher = Cipher(algorithms.AES(hashlib.sha256(KEY.encode()).digest()), modes.CBC(iv)).encryptor()
        payload = {"encrypt": base64.b64encode(iv + cipher.update(padded) + cipher.finalize()).decode()}
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    timestamp = str(int(time.time())) if stamp is None else str(stamp)
    nonce = "fixture-nonce"
    headers = {"X-Lark-Request-Timestamp": timestamp, "X-Lark-Request-Nonce": nonce,
               "X-Lark-Signature": hashlib.sha256((timestamp + nonce + KEY).encode() + raw).hexdigest()}
    return raw, headers


class EventSecurityTests(unittest.TestCase):
    def setUp(self):
        self.verifier = FeishuEventVerifier(encrypt_key=KEY, verification_token=TOKEN, app_id=APP)

    def test_plain_signed_and_encrypted_callbacks_have_same_identity_without_persistable_token(self):
        for encrypted in (False, True):
            callback = self.verifier.verify(*signed(event(), encrypted=encrypted))
            self.assertEqual(callback.sender, OWNER)
            self.assertNotIn(TOKEN, json.dumps(callback.payload))
            with verified_sender(callback):
                require_maintainer(OWNER, (OWNER,))
                with self.assertRaises(EventSecurityError):
                    require_maintainer("ou_someone_else", ("ou_someone_else",))
            with self.assertRaises(EventSecurityError):
                require_maintainer(OWNER, (OWNER,))

    def test_raw_bytes_signature_fails_for_mutation_reserialization_duplicate_headers_and_expired_requests(self):
        raw, headers = signed(event())
        for body, supplied in (
            (raw.replace(b"om_fixture", b"om_forgery"), headers),
            (json.dumps(json.loads(raw)).encode(), headers),
            (raw, {**headers, "x-lark-signature": headers["X-Lark-Signature"]}),
            (raw, {}), signed(event(), stamp=int(time.time()) - 901), signed(event(), stamp=int(time.time()) + 901),
        ):
            with self.assertRaises(EventSecurityError):
                self.verifier.verify(body, supplied)

    def test_wrong_application_token_sender_or_message_identity_cannot_reach_business_context(self):
        cases = []
        wrong = event(); wrong["header"]["app_id"] = "cli_other"; cases.append(wrong)
        wrong = event(); wrong["header"]["token"] = "other-token"; cases.append(wrong)
        wrong = event(); wrong["event"]["sender"]["sender_type"] = "bot"; cases.append(wrong)
        wrong = event(); wrong["event"]["sender"]["sender_id"] = {"user_id": OWNER}; cases.append(wrong)
        wrong = event(); wrong["event"]["message"]["message_id"] = "../../other"; cases.append(wrong)
        wrong = event(); del wrong["event"]["message"]["create_time"]; cases.append(wrong)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(EventSecurityError):
                    self.verifier.verify(*signed(payload))

    def test_bad_encryption_and_duplicate_json_are_rejected_without_sensitive_diagnostics(self):
        for payload in ({"encrypt": "not-base64"}, {"encrypt": "eA==", "header": {}}, []):
            with self.assertRaises(EventSecurityError) as caught:
                self.verifier.verify(*signed(payload))
            self.assertNotIn(KEY, str(caught.exception))
        raw, headers = signed(event())
        raw = raw.replace(b'"schema": "2.0"', b'"schema": "2.0", "schema": "2.0"')
        headers["X-Lark-Signature"] = hashlib.sha256((headers["X-Lark-Request-Timestamp"] + headers["X-Lark-Request-Nonce"] + KEY).encode() + raw).hexdigest()
        with self.assertRaises(EventSecurityError):
            self.verifier.verify(raw, headers)
        with self.assertRaises(EventSecurityError):
            self.verifier.verify(b"x" * (MAX_EVENT_BYTES + 1), headers)

    def test_encrypted_url_verification_and_missing_security_configuration(self):
        result = self.verifier.verify(*signed({"type": "url_verification", "token": TOKEN, "challenge": "fixture-challenge"}, encrypted=True))
        self.assertEqual(result.payload, {"type": "url_verification", "challenge": "fixture-challenge"})
        self.assertIsNone(result.sender)
        for key in ("encrypt_key", "verification_token", "app_id"):
            config = {"encrypt_key": KEY, "verification_token": TOKEN, "app_id": APP}; config[key] = ""
            with self.assertRaises(EventSecurityError):
                FeishuEventVerifier(**config)

    def test_real_bridge_propagates_verified_owner_and_unsigned_or_foreign_owner_cannot_enter_store_mode(self):
        options = FeishuTikuOptions(app_id=APP, verification_token=TOKEN, encrypt_key=KEY,
                                   maintenance_sender_ids=(OWNER,), working_reaction=None)
        bot = TikuBot.__new__(TikuBot)
        bot.options = options; bot.sessions = TikuSessionStore(); bot.enroll_admin_sender_once = lambda sender: False
        replied = threading.Event()
        class Client:
            def reply_text(self, *_args):
                replied.set()
        bridge = FeishuTikuBridge(bot, Client(), options)
        self.assertFalse(bridge.handle_payload(event())["ok"])
        for text in ("+", "新增"):
            with self.assertRaises(EventSecurityError):
                bot.receive_text(OWNER, text)
        self.assertTrue(bridge.handle_request(*signed(event(), encrypted=True))["ok"])
        self.assertTrue(replied.wait(5))
        self.assertEqual(bot.sessions.get(OWNER).state, "store_waiting_question")
        with self.assertRaises(EventSecurityError):
            bot.receive_image(OWNER, Path("unused.jpg"))
        foreign = event(); foreign["header"]["event_id"] = "event_456"
        foreign["event"]["sender"]["sender_id"]["open_id"] = "ou_another_owner"
        replied.clear()
        self.assertTrue(bridge.handle_request(*signed(foreign))["ok"])
        self.assertTrue(replied.wait(5))
        self.assertEqual(bot.sessions.get("ou_another_owner").state, "idle")
        bot.sessions.save(OWNER, TikuSession(state="confirm_delete"))
        with self.assertRaises(EventSecurityError):
            bot.receive_text(OWNER, "1")

    def test_managed_bank_rejects_legacy_feishu_direct_writers_before_any_asset_access(self):
        with patch.dict(os.environ, {"TIKU_BANK_STORE": "fixture-published-bank"}):
            for service in (FeishuStoreService, FeishuDeleteService):
                instance = service.__new__(service)
                with self.assertRaises(PermissionError):
                    instance.apply_plan(None)
            with self.assertRaises(EventSecurityError):
                FeishuTikuBridge(object(), object(), FeishuTikuOptions())


if __name__ == "__main__":
    unittest.main()

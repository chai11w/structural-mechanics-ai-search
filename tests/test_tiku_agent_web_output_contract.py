from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WebOutputContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (
            ROOT / "tiku_agent" / "demo_web" / "demo.js"
        ).read_text(encoding="utf-8")

    def test_stream_accepts_only_canonical_data_messages(self) -> None:
        body = self.script[
            self.script.index("async function requestStream("):
            self.script.index("function responseItem(")
        ]

        self.assertIn("canonicalPublicMessage(event?.data, { progress: true })", body)
        self.assertIn("const message = canonicalPublicMessage(event?.data);", body)
        self.assertIn("throw publicMessageError(message, requestId);", body)
        self.assertIn("message.request_id !== requestId", body)
        self.assertIn("message.sequence !== progressSequence + 1", body)
        self.assertIn("message.search_id !== streamSearchId", body)
        self.assertNotIn("event.message", body)
        self.assertNotIn("event?.message", body)
        self.assertNotIn("event.message", self.script)

    def test_http_and_protocol_failures_do_not_render_server_details(self) -> None:
        error_body = self.script[
            self.script.index("function safeHttpError"):
            self.script.index("async function request(")
        ]

        self.assertIn("CLIENT_ERROR_CATALOG", self.script)
        self.assertIn("canonicalPublicMessage(data)", error_body)
        self.assertNotIn("rawDetail", error_body)
        self.assertNotIn("data.detail", error_body)
        self.assertNotIn("detail.includes", error_body)
        self.assertNotIn("HTTP ${status}", error_body)

    def test_response_text_and_actions_come_from_public_message(self) -> None:
        body = self.script[
            self.script.index("function responseItem"):
            self.script.index("function setResponseStatus")
        ]

        self.assertIn("const publicMessage = canonicalPublicMessage(data);", body)
        self.assertIn("message: publicMessage.text", body)
        self.assertIn("allowedActions: publicMessage.allowed_actions", body)
        self.assertIn("recoveryActionsFromAllowedActions(publicMessage.allowed_actions)", body)
        self.assertNotIn("data.text ||", body)

    def test_contact_requires_structured_contact_action(self) -> None:
        self.assertNotIn("AUTHOR_CONTACT_FALLBACK", self.script)
        self.assertNotIn("includes('联系作者手搓')", self.script)
        self.assertIn("value.allowed_actions.includes('contact_author')", self.script)
        self.assertIn("item.allowedActions?.includes('contact_author')", self.script)

    def test_session_restore_checks_the_minimum_snapshot_shape(self) -> None:
        restore_body = self.script[
            self.script.index("async function repairUploadedImageHistory"):
            self.script.index("function clearHistory")
        ]

        self.assertIn("const snapshot = normalizeSessionSnapshot(data?.session);", restore_body)
        self.assertIn("if (!snapshot) throw clientProtocolError('RESPONSE_INVALID'", restore_body)
        self.assertIn("function normalizeSessionSnapshot", self.script)

    def test_local_upload_validation_has_a_stable_code_without_text_inference(self) -> None:
        body = self.script[
            self.script.index("function validateImage"):
            self.script.index("function debugUploadMetadata")
        ]

        self.assertIn("code: 'UPLOAD_TOO_LARGE'", body)
        self.assertIn("code: 'UPLOAD_UNSUPPORTED_FORMAT'", body)
        self.assertNotIn("includes('太大')", self.script)
        self.assertNotIn("includes('格式')", self.script)


if __name__ == "__main__":
    unittest.main()

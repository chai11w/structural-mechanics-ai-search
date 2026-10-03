from __future__ import annotations

import errno
import http.client
import json
import socket
import ssl
import unittest
import urllib.error

from tiku_shared.model_recovery import MODEL_RECOVERY_CALL_TYPES, model_recovery_allowed


class ModelRecoveryPolicyTests(unittest.TestCase):
    def allowed(self, error, *, provider="dashscope", call_type="qwen_a3_crop_compare"):
        return model_recovery_allowed(provider, call_type, error)

    def test_only_registered_provider_stage_pairs_are_eligible(self):
        expected = {
            "qwen_image_triage", "qwen_a3_page_understanding", "qwen_a3_crop_compare",
            "qwen_image_classification", "qwen_a3_unit_analysis",
            "qwen_structure_type", "qwen_shape_rerank", "qwen_length_tie_break",
            "glm_a3_page_auto_crop", "external_load_screen",
        }
        self.assertEqual(MODEL_RECOVERY_CALL_TYPES, expected)
        for call_type in expected:
            providers = ("dashscope", "zhipu") if call_type == "external_load_screen" else (
                "zhipu" if call_type == "glm_a3_page_auto_crop" else "dashscope",
            )
            for provider in providers:
                with self.subTest(provider=provider, call_type=call_type):
                    self.assertTrue(self.allowed(TimeoutError(), provider=provider, call_type=call_type))
        self.assertFalse(self.allowed(TimeoutError(), provider="zhipu"))
        self.assertFalse(self.allowed(TimeoutError(), call_type="glm_a3_page_auto_crop"))

    def test_unregistered_calls_and_providers_are_denied(self):
        for call_type in ("zhipu_shape_rerank", "zhipu_length_tie_break", "qwen_length_tie",
                          "reply", "intent", "qwen_layout_analysis", "", "QWEN_A3_CROP_COMPARE"):
            with self.subTest(call_type=call_type):
                self.assertFalse(self.allowed(TimeoutError(), call_type=call_type))
        for provider in ("openai", "glm", "qwen", "dashscope.example", "", None, 1):
            with self.subTest(provider=provider):
                self.assertFalse(self.allowed(TimeoutError(), provider=provider))
        self.assertFalse(self.allowed(TimeoutError(), call_type=None))
        for call_type in ("qwen_structure_type", "qwen_shape_rerank", "qwen_length_tie_break"):
            self.assertFalse(self.allowed(TimeoutError(), provider="zhipu", call_type=call_type))
        self.assertTrue(self.allowed(TimeoutError(), provider=" DashScope ", call_type=" qwen_a3_crop_compare "))

    def test_known_transient_connection_errors_direct_and_url_wrapped(self):
        errors = [
            TimeoutError(), ConnectionResetError(), ConnectionAbortedError(),
            ConnectionRefusedError(), BrokenPipeError(), http.client.RemoteDisconnected(),
            ssl.SSLEOFError(), socket.gaierror(socket.EAI_AGAIN, "synthetic"),
        ]
        for error in errors:
            for wrapped in (error, urllib.error.URLError(error),
                            urllib.error.URLError(urllib.error.URLError(error))):
                with self.subTest(kind=type(error).__name__, wrapper=type(wrapped).__name__):
                    self.assertTrue(self.allowed(wrapped))

    def test_only_selected_http_statuses_are_transient_without_reading_body(self):
        class UnreadableBody:
            def read(self, *_args):
                raise AssertionError("policy must not read provider content")
            def close(self):
                pass

        for status in (200, 301, 400, 401, 403, 404, 408, 409, 413, 422, 429, 500, 501, 502, 503, 504, 505):
            error = urllib.error.HTTPError("https://example.invalid", status, "synthetic", {}, UnreadableBody())
            with self.subTest(status=status):
                self.assertEqual(self.allowed(error), status in {429, 500, 502, 503, 504})
        for malformed_status in ("503", 503.0, None):
            error = urllib.error.HTTPError("https://example.invalid", malformed_status, "synthetic", {}, None)
            self.assertFalse(self.allowed(error))

    def test_certificate_generic_ssl_and_permanent_dns_failures_are_denied(self):
        for error in (ssl.SSLCertVerificationError(), ssl.SSLError(),
                      socket.gaierror(socket.EAI_NONAME, "synthetic"),
                      socket.gaierror(socket.EAI_FAIL, "synthetic")):
            for wrapped in (error, urllib.error.URLError(error)):
                with self.subTest(kind=type(error).__name__, wrapper=type(wrapped).__name__):
                    self.assertFalse(self.allowed(wrapped))

    def test_generic_os_errors_and_non_transport_failures_are_denied(self):
        errors = (
            OSError(), OSError(errno.ENETUNREACH, "synthetic"), ConnectionError(),
            PermissionError(), FileNotFoundError(), IsADirectoryError(),
            json.JSONDecodeError("synthetic", "", 0), ValueError(), TypeError(),
            RuntimeError(), http.client.BadStatusLine("synthetic"),
            http.client.IncompleteRead(b"synthetic", 10), KeyboardInterrupt(), SystemExit(), None,
        )
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                self.assertFalse(self.allowed(error))
                self.assertFalse(self.allowed(urllib.error.URLError(error)))

    def test_error_messages_never_establish_retry_eligibility(self):
        for message in ("timed out", "ConnectionResetError", "SSL EOF", "HTTP 503", "429"):
            for error in (OSError(message), RuntimeError(message), urllib.error.URLError(message)):
                with self.subTest(message=message, kind=type(error).__name__):
                    self.assertFalse(self.allowed(error))

    def test_execution_or_parsing_error_with_transient_cause_is_not_unwrapped(self):
        for outer in (RuntimeError(), ValueError(), PermissionError()):
            outer.__cause__ = TimeoutError()
            outer.__context__ = urllib.error.HTTPError("https://example.invalid", 503, "synthetic", {}, None)
            with self.subTest(kind=type(outer).__name__):
                self.assertFalse(self.allowed(outer))
        # GLM's legacy RuntimeError wrapper must be handled at its source, not
        # by treating arbitrary application exception chains as transport errors.
        outer = RuntimeError()
        outer.__cause__ = urllib.error.HTTPError("https://example.invalid", 503, "synthetic", {}, None)
        self.assertFalse(self.allowed(outer, provider="zhipu", call_type="glm_a3_page_auto_crop"))

    def test_nested_url_error_depth_and_cycles_are_bounded(self):
        cycle = urllib.error.URLError("synthetic")
        cycle.reason = cycle
        self.assertFalse(self.allowed(cycle))
        nested = TimeoutError()
        for _ in range(20):
            nested = urllib.error.URLError(nested)
        self.assertFalse(self.allowed(nested))

    def test_policy_does_not_modify_the_exception_or_its_reason(self):
        reason = ssl.SSLEOFError()
        error = urllib.error.URLError(reason)
        error._tiku_model_call_id = "original-call"
        original_attributes = dict(vars(error))
        self.assertTrue(self.allowed(error))
        self.assertEqual(vars(error), original_attributes)
        self.assertIs(error.reason, reason)


if __name__ == "__main__":
    unittest.main()

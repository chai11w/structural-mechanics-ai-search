"""Untrusted browser binding reports remain bounded, optional log evidence."""

import json
import unittest
from unittest.mock import patch

from tiku_agent.bind_diagnostics import (
    MAX_BIND_DIAGNOSTIC_BYTES, parse_bind_diagnostic, record_bind_diagnostic,
)


class BindDiagnosticTests(unittest.TestCase):
    def report(self, **changes):
        value = {"schema": 1, "request_id": "req_" + "a" * 32, "phase": "fetch",
                 "code": "REQUEST_TIMEOUT", "elapsed_ms": 15001, "status": 0}
        value.update(changes)
        return value

    def header(self, **changes):
        return json.dumps(self.report(**changes), separators=(",", ":"), ensure_ascii=True)

    def test_valid_enumerations_keep_the_report_separate_from_authoritative_fields(self):
        for phase in ("fetch", "body", "protocol"):
            for code in ("REQUEST_TIMEOUT", "NETWORK_UNAVAILABLE", "RESPONSE_INVALID", "HTTP_ERROR"):
                with self.subTest(phase=phase, code=code):
                    attrs = parse_bind_diagnostic(self.header(phase=phase, code=code))
                    self.assertEqual(set(attrs), {"client_bind_reported", "client_bind_schema",
                        "client_bind_request_id", "client_bind_phase", "client_bind_code",
                        "client_bind_elapsed_ms", "client_bind_status"})
                    self.assertIs(attrs["client_bind_reported"], True)
                    self.assertEqual(attrs["client_bind_request_id"], "req_" + "a" * 32)
                    self.assertEqual(attrs["client_bind_phase"], phase)
                    self.assertEqual(attrs["client_bind_code"], code)

    def test_ascii_header_length_limit_is_checked_before_json_parsing(self):
        raw = self.header()
        padded = raw + " " * (MAX_BIND_DIAGNOSTIC_BYTES - len(raw))
        self.assertTrue(parse_bind_diagnostic(padded))
        for value in (padded + " ", raw + "\n", raw + "\t", raw + "\x00", raw + "\x7f", raw + "题"):
            with self.subTest(length=len(value)):
                self.assertEqual(parse_bind_diagnostic(value), {})

    def test_absent_malformed_and_non_object_headers_are_ignored(self):
        for raw in (None, b"{}", {}, 1, True, "", "null", "[]", "1", "{}", "{", "[" * 510):
            with self.subTest(kind=type(raw).__name__):
                self.assertEqual(parse_bind_diagnostic(raw), {})

    def test_unknown_missing_and_duplicate_fields_are_ignored(self):
        for name in ("url", "image", "error", "identity", "cookie", "prompt", "clienttimestamp"):
            self.assertEqual(parse_bind_diagnostic(self.header(**{name: "not-to-be-stored"})), {})
        for name in self.report():
            value = self.report()
            value.pop(name)
            self.assertEqual(parse_bind_diagnostic(json.dumps(value)), {})
        duplicate = self.header()[:-1] + ',"schema":1}'
        self.assertEqual(parse_bind_diagnostic(duplicate), {})

    def test_values_cannot_carry_arbitrary_text_urls_or_identifiers(self):
        cases = {
            "request_id": ["req_" + "a" * 31, "req_" + "A" * 32, "req_" + "a" * 32 + "\n",
                           "https://example.invalid/?secret=x", "../private", None, {}],
            "phase": ["fetch ", "FETCH", "tls", "fetch\r\nInjected: x", {}, None],
            "code": ["TimeoutError: arbitrary details", "request_timeout", "EXECUTION_UNKNOWN", [], None],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(field=name):
                    self.assertEqual(parse_bind_diagnostic(self.header(**{name: value})), {})

    def test_integer_types_and_ranges_are_strict(self):
        cases = {"schema": [True, False, 1.0, "1", 0, 2, None],
                 "elapsed_ms": [True, False, 1.0, "1", -1, 120001, None, float("nan")],
                 "status": [True, False, 200.0, "200", -1, 600, None]}
        for name, values in cases.items():
            for value in values:
                with self.subTest(field=name):
                    self.assertEqual(parse_bind_diagnostic(self.header(**{name: value})), {})
        for elapsed in (0, 120000):
            for status in (0, 599):
                self.assertTrue(parse_bind_diagnostic(self.header(elapsed_ms=elapsed, status=status)))

    def test_logger_records_one_bounded_line_with_current_and_previous_request_ids(self):
        current = "req_" + "b" * 32
        raw = self.header(elapsed_ms=120000, status=599)
        with self.assertLogs("tiku_agent.bind_diagnostics", level="WARNING") as logs:
            self.assertIsNone(record_bind_diagnostic(raw, current))
        self.assertEqual(len(logs.records), 1)
        line = logs.records[0].getMessage()
        prefix = "BIND_TRANSPORT_DIAGNOSTIC "
        self.assertTrue(line.startswith(prefix))
        self.assertEqual(line.splitlines(), [line])
        self.assertTrue(line.isascii())
        self.assertLess(len(line.encode("ascii")), 512)
        data = json.loads(line[len(prefix):])
        self.assertEqual(data, {"request_id": current, **parse_bind_diagnostic(raw)})
        self.assertNotEqual(data["request_id"], data["client_bind_request_id"])
        self.assertIs(data["client_bind_reported"], True)

    def test_invalid_header_does_not_reach_the_logger(self):
        with patch("tiku_agent.bind_diagnostics._LOGGER.warning") as warning:
            for raw in (None, "{", "x" * 513, self.header(error="private exception text"),
                        self.header(request_id="https://private.invalid/?token=secret")):
                self.assertIsNone(record_bind_diagnostic(raw, "req_" + "b" * 32))
            warning.assert_not_called()

    def test_current_authoritative_id_must_have_the_exact_request_id_format(self):
        with patch("tiku_agent.bind_diagnostics._LOGGER.warning") as warning:
            for current in (None, 1, True, "", "req_" + "b" * 31, "req_" + "B" * 32,
                            "req_" + "b" * 32 + "\n", "req_x\nINJECTED", "raw secret"):
                self.assertIsNone(record_bind_diagnostic(self.header(), current))
            warning.assert_not_called()

    def test_logger_exceptions_are_ignored_without_logging_exception_text(self):
        with patch("tiku_agent.bind_diagnostics._LOGGER.warning", side_effect=OSError("private log failure")) as warning:
            self.assertIsNone(record_bind_diagnostic(self.header(), "req_" + "b" * 32))
        self.assertEqual(warning.call_count, 1)
        template, payload = warning.call_args.args
        self.assertEqual(template, "BIND_TRANSPORT_DIAGNOSTIC %s")
        self.assertNotIn("private", payload)
        self.assertEqual(set(json.loads(payload)), {"request_id", *parse_bind_diagnostic(self.header())})

    def test_unexpected_diagnostic_parser_failure_cannot_escape_into_the_binding(self):
        with patch("tiku_agent.bind_diagnostics.parse_bind_diagnostic", side_effect=RuntimeError("private parser failure")), \
                patch("tiku_agent.bind_diagnostics._LOGGER.warning") as warning:
            self.assertIsNone(record_bind_diagnostic(self.header(), "req_" + "b" * 32))
            warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()

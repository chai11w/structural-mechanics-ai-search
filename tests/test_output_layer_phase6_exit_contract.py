"""Static release gates for removing legacy public-output exits.

These tests deliberately inspect the small API boundary functions rather than
unit-testing private wording.  A change that reintroduces a raw Agent response,
exception detail, or A3 internal state into the 8790 web protocol must fail the
release gate even when the browser happens not to render that field.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _source_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


class OutputLayerPhase6ExitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fastapi_source = (ROOT / "tiku_agent" / "fastapi_demo.py").read_text(
            encoding="utf-8"
        )

    def test_agent_payload_never_falls_back_to_legacy_response_text(self) -> None:
        body = _source_between(
            self.fastapi_source,
            "def _agent_payload(",
            "\ndef _public_contact(",
        )

        self.assertNotIn("response.text", body)
        self.assertIn("output = response.output", body)
        self.assertIn("if output is None:", body)
        self.assertIn("_structured_output_failure(", body)

    def test_stream_events_are_canonical_public_messages(self) -> None:
        body = _source_between(
            self.fastapi_source,
            "async def _stream_agent_events(",
            "\n\nasync def _periodic_session_cleanup(",
        )

        self.assertIn("render_progress_output(", body)
        self.assertNotIn('"message"', body)
        self.assertNotIn('"detail"', body)
        self.assertIn('"data"', body)

    def test_http_error_boundary_never_serializes_detail_or_exception_text(self) -> None:
        handlers = _source_between(
            self.fastapi_source,
            "    @app.exception_handler(HTTPException)",
            "    @app.middleware(\"http\")",
        )
        serializer = _source_between(
            self.fastapi_source,
            "def _protocol_json_response(",
            "\ndef _http_error_protocol(",
        )

        self.assertNotIn("str(exc.detail)", handlers)
        self.assertNotIn("str(exc)", handlers)
        self.assertNotIn('"detail"', serializer)
        self.assertIn("PublicMessage", serializer)

    def test_a3_public_session_snapshot_excludes_internal_reasoning_fields(self) -> None:
        public_snapshot = _source_between(
            self.fastapi_source,
            "def _public_session_snapshot(",
            "\ndef _public_crop_bounds(",
        )

        for field in (
            "pending_intent_clarification",
            "last_intent",
            "reason_codes",
            "crop_review_feedback",
        ):
            with self.subTest(field=field):
                self.assertNotIn(field, public_snapshot)

    def test_a3_display_fields_use_the_public_text_sanitizer(self) -> None:
        """Question snippets may be shown, but never as raw runtime state."""
        snapshot_body = _source_between(
            self.fastapi_source,
            "def _public_a3_snapshot(",
            "\ndef _public_crop_bounds(",
        )
        sanitizer = _source_between(
            self.fastapi_source,
            "def _public_state_text(",
            "\ndef _public_state_id(",
        )

        self.assertIn('_public_state_text(raw_unit.get("title_text"), 160)', snapshot_body)
        self.assertIn('_public_state_text(selected.get("context_text"), 480)', snapshot_body)
        self.assertIn("_PUBLIC_STATE_CONTROL_RE.sub", sanitizer)
        self.assertIn("_PUBLIC_STATE_SENSITIVE_PATTERNS", sanitizer)
        self.assertIn("clean[:max_chars]", sanitizer)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

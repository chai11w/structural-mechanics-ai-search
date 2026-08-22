import unittest

from tiku_shared.request_protocol import (
    PROTOCOL_REASONS,
    REQUEST_PROTOCOL_SCHEMA_VERSION,
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
    new_request_id,
    new_search_id,
)


REQUEST_ID = "req_12345678"
SEARCH_ID = "search_12345678"


APPROVED_PUBLIC_TOOL_REASONS = {
    "UNKNOWN_CHAPTER": (
        RequestStatus.NEEDS_INPUT,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "PAGE_NO_SEARCHABLE_UNITS": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "QUESTION_UNITS_PREPARED": (
        RequestStatus.SUCCESS,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "COARSE_CANDIDATES_FOUND": (
        RequestStatus.SUCCESS,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "GLOBAL_CANDIDATES_FOUND": (
        RequestStatus.SUCCESS,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "RERANK_COMPLETED": (
        RequestStatus.SUCCESS,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "RERANK_EMPTY_COARSE_FALLBACK": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        True,
        RequestAction.NONE,
    ),
    "RERANK_INCOMPLETE_COARSE_FALLBACK": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        True,
        RequestAction.NONE,
    ),
    "RERANK_SKIPPED_NO_IMAGE": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "MULTI_DETECTION_FALLBACK": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "MULTI_CROPS_UNAVAILABLE": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        True,
        RequestAction.NONE,
    ),
    "STRUCTURE_CLASSIFICATION_FALLBACK": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        True,
        RequestAction.NONE,
    ),
    "STRUCTURE_FILTER_SKIPPED_NO_IMAGE": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "STRUCTURE_TYPE_UNCERTAIN": (
        RequestStatus.PARTIAL,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "NO_COARSE_CANDIDATES": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "NO_RELIABLE_RERANK_CANDIDATES": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "NO_GLOBAL_COARSE_CANDIDATES": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "NO_GLOBAL_RELIABLE_CANDIDATES": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "ANSWER_FILES_FOUND": (
        RequestStatus.SUCCESS,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "ANSWER_FILES_NOT_FOUND": (
        RequestStatus.NO_MATCH,
        RequestLayer.TOOL,
        False,
        RequestAction.NONE,
    ),
    "IMAGE_ANALYSIS_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "MULTI_DETAIL_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "COARSE_SEARCH_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "GLOBAL_SEARCH_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "RERANK_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
    "ANSWER_LOOKUP_FAILED": (
        RequestStatus.ERROR,
        RequestLayer.TOOL,
        True,
        RequestAction.RETRY_SEARCH,
    ),
}


class RequestProtocolContractTest(unittest.TestCase):
    def canonical_payload(self, code="QUEUE_TIMEOUT"):
        return RequestProtocol.from_code(
            code,
            request_id=REQUEST_ID,
            search_id=SEARCH_ID,
        ).to_dict()

    def test_exactly_five_public_statuses(self):
        self.assertEqual(
            [item.value for item in RequestStatus],
            ["SUCCESS", "NO_MATCH", "NEEDS_INPUT", "PARTIAL", "ERROR"],
        )
        self.assertIs(RequestStatus.TOOL_ERROR, RequestStatus.ERROR)

    def test_registered_reason_builds_stable_metadata(self):
        request_id = new_request_id()
        search_id = new_search_id()

        result = RequestProtocol.from_code(
            "queue_timeout", request_id=request_id, search_id=search_id
        )

        self.assertEqual(result.status, RequestStatus.ERROR)
        self.assertEqual(result.layer, RequestLayer.QUEUE)
        self.assertTrue(result.retryable)
        self.assertEqual(result.action, RequestAction.RETRY_REQUEST)
        self.assertEqual(result.request_id, request_id)
        self.assertEqual(result.search_id, search_id)
        self.assertEqual(result.to_dict()["code"], "QUEUE_TIMEOUT")

    def test_load_route_review_is_registered_and_round_trips_through_v1(self):
        result = RequestProtocol.from_code(
            "LOAD_ROUTE_NEEDS_REVIEW",
            request_id=REQUEST_ID,
            search_id=SEARCH_ID,
        )

        self.assertEqual(result.status, RequestStatus.NEEDS_INPUT)
        self.assertEqual(result.layer, RequestLayer.SESSION)
        self.assertFalse(result.retryable)
        self.assertEqual(result.action, RequestAction.NONE)
        self.assertEqual(RequestProtocol.from_dict(result.to_dict()), result)

    def test_every_reason_code_matches_its_registry_key(self):
        for code in PROTOCOL_REASONS:
            self.assertEqual(RequestProtocol.from_code(code).code, code)

    def test_approved_public_tool_codes_have_exact_shared_shapes(self):
        self.assertEqual(len(APPROVED_PUBLIC_TOOL_REASONS), 26)
        for code, expected in APPROVED_PUBLIC_TOOL_REASONS.items():
            with self.subTest(code=code):
                reason = PROTOCOL_REASONS[code]
                self.assertEqual(
                    (reason.status, reason.layer, reason.retryable, reason.action),
                    expected,
                )

    def test_every_registered_reason_round_trips_through_strict_v1(self):
        for code in PROTOCOL_REASONS:
            with self.subTest(code=code):
                original = RequestProtocol.from_code(
                    code,
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                )
                self.assertEqual(RequestProtocol.from_dict(original.to_dict()), original)

    def test_strict_v1_rejects_every_registered_tuple_tampering(self):
        for code, reason in PROTOCOL_REASONS.items():
            payload = self.canonical_payload(code)
            alternate_status = next(
                item for item in RequestStatus if item is not reason.status
            )
            alternate_layer = next(
                item for item in RequestLayer if item is not reason.layer
            )
            alternate_action = (
                RequestAction.NONE
                if reason.action is not RequestAction.NONE
                else RequestAction.RETRY_REQUEST
            )
            variants = {
                "status": alternate_status.value,
                "layer": alternate_layer.value,
                "retryable": not reason.retryable,
                "action": alternate_action.value,
            }
            for field, value in variants.items():
                with self.subTest(code=code, field=field):
                    poisoned = dict(payload)
                    poisoned[field] = value
                    with self.assertRaises(ValueError):
                        RequestProtocol.from_dict(poisoned)

    def test_strict_v1_rejects_unknown_code_missing_and_extra_fields(self):
        payload = self.canonical_payload()
        unknown = dict(payload, code="UNKNOWN_STABLE_CODE")
        with self.assertRaises(ValueError):
            RequestProtocol.from_dict(unknown)

        for field in tuple(payload):
            with self.subTest(missing=field):
                incomplete = dict(payload)
                incomplete.pop(field)
                with self.assertRaises(ValueError):
                    RequestProtocol.from_dict(incomplete)

        for field in ("detail", "message", "outcome", "recovery_action", "search_key"):
            with self.subTest(extra=field):
                polluted = dict(payload)
                polluted[field] = "private"
                with self.assertRaises(ValueError):
                    RequestProtocol.from_dict(polluted)

    def test_strict_v1_rejects_coercible_schema_and_retryable_values(self):
        payload = self.canonical_payload()
        for version in (True, 1.0, "1", 0, 2, None):
            with self.subTest(schema_version=version):
                poisoned = dict(payload, schema_version=version)
                with self.assertRaises(ValueError):
                    RequestProtocol.from_dict(poisoned)

        for retryable in ("false", "true", 0, 1, None):
            with self.subTest(retryable=retryable):
                poisoned = dict(payload, retryable=retryable)
                with self.assertRaises(ValueError):
                    RequestProtocol.from_dict(poisoned)

    def test_strict_v1_rejects_noncanonical_types_and_alias_status(self):
        payload = self.canonical_payload()
        vectors = (
            ("status", RequestStatus.ERROR),
            ("status", "TOOL_ERROR"),
            ("layer", RequestLayer.QUEUE),
            ("action", RequestAction.RETRY_REQUEST),
            ("code", "queue_timeout"),
            ("request_id", 12345678),
            ("search_id", 12345678),
        )
        for field, value in vectors:
            with self.subTest(field=field, value=value):
                poisoned = dict(payload)
                poisoned[field] = value
                with self.assertRaises(ValueError):
                    RequestProtocol.from_dict(poisoned)
        with self.assertRaises(ValueError):
            RequestProtocol.from_dict([])

    def test_explicit_legacy_reader_normalizes_named_aliases(self):
        result = RequestProtocol.from_legacy_dict(
            {
                "outcome": "TOOL_ERROR",
                "layer": "tool",
                "code": "PROVIDER_FAILED",
                "retryable": True,
                "recovery_action": "retry_search",
                "search_key": "sessionhash:12",
            }
        )

        self.assertEqual(result.status, RequestStatus.ERROR)
        self.assertEqual(result.code, "TOOL_FAILED")
        self.assertEqual(result.action, RequestAction.RETRY_SEARCH)
        self.assertEqual(result.search_id, "sessionhash:12")
        self.assertEqual(result.schema_version, REQUEST_PROTOCOL_SCHEMA_VERSION)

        success = RequestProtocol.from_legacy_dict(
            {"outcome": "SUCCESS", "code": "LEGACY_SUCCESS"}
        )
        self.assertEqual(success.code, "REQUEST_SUCCEEDED")
        self.assertEqual(success.status, RequestStatus.SUCCESS)

    def test_explicit_legacy_reader_rejects_conflicts_and_unknowns(self):
        vectors = (
            {
                "status": "SUCCESS",
                "outcome": "TOOL_ERROR",
                "code": "TOOL_FAILED",
            },
            {
                "outcome": "TOOL_ERROR",
                "code": "TOOL_FAILED",
                "retryable": False,
            },
            {
                "outcome": "TOOL_ERROR",
                "code": "TOOL_FAILED",
                "action": "retry_request",
                "recovery_action": "retry_search",
            },
            {
                "outcome": "SUCCESS",
                "code": "LEGACY_SUCCESS",
                "search_id": "search_12345678",
                "search_key": "search_87654321",
            },
            {"outcome": "ERROR", "code": "UNKNOWN_LEGACY_CODE"},
            {"outcome": "ERROR", "code": "TOOL_FAILED", "retryable": "true"},
            {"outcome": "ERROR", "code": "TOOL_FAILED", "detail": "private"},
            {"outcome": "ERROR", "code": "TOOL_FAILED", "schema_version": 1},
        )
        for payload in vectors:
            with self.subTest(payload=payload):
                with self.assertRaises((TypeError, ValueError)):
                    RequestProtocol.from_legacy_dict(payload)

    def test_rejects_unstable_codes_and_ids(self):
        with self.assertRaises(ValueError):
            RequestProtocol(RequestStatus.ERROR, RequestLayer.TOOL, "not stable")
        with self.assertRaises(ValueError):
            RequestProtocol(
                RequestStatus.ERROR,
                RequestLayer.TOOL,
                "TOOL_FAILED",
                request_id="short",
            )
        with self.assertRaises(ValueError):
            RequestProtocol.from_code("UNKNOWN_STABLE_CODE")
        for schema_version in (True, 1.0, "1", 2):
            with self.subTest(schema_version=schema_version):
                with self.assertRaises(ValueError):
                    RequestProtocol(
                        RequestStatus.SUCCESS,
                        RequestLayer.TOOL,
                        "REQUEST_SUCCEEDED",
                        schema_version=schema_version,
                    )
        for retryable in (0, 1, "false", None):
            with self.subTest(retryable=retryable):
                with self.assertRaises(ValueError):
                    RequestProtocol(
                        RequestStatus.SUCCESS,
                        RequestLayer.TOOL,
                        "REQUEST_SUCCEEDED",
                        retryable=retryable,
                    )


if __name__ == "__main__":
    unittest.main()

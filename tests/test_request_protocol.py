import unittest

from tiku_shared.request_protocol import (
    PROTOCOL_REASONS,
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
    new_request_id,
    new_search_id,
)


class RequestProtocolContractTest(unittest.TestCase):
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

    def test_login_rate_limit_is_a_retryable_login_error(self):
        result = RequestProtocol.from_code("LOGIN_RATE_LIMITED")

        self.assertEqual(result.status, RequestStatus.ERROR)
        self.assertEqual(result.layer, RequestLayer.LOGIN)
        self.assertTrue(result.retryable)
        self.assertEqual(result.action, RequestAction.RETRY_REQUEST)

    def test_invalid_login_request_stays_in_the_login_layer(self):
        result = RequestProtocol.from_code("LOGIN_REQUEST_INVALID")

        self.assertEqual(result.status, RequestStatus.NEEDS_INPUT)
        self.assertEqual(result.layer, RequestLayer.LOGIN)
        self.assertFalse(result.retryable)
        self.assertEqual(result.action, RequestAction.RELOGIN)

    def test_every_reason_code_matches_its_registry_key(self):
        for code in PROTOCOL_REASONS:
            self.assertEqual(RequestProtocol.from_code(code).code, code)

    def test_reads_legacy_tool_error_and_recovery_names(self):
        result = RequestProtocol.from_dict({
            "outcome": "TOOL_ERROR",
            "layer": "tool",
            "code": "PROVIDER_FAILED",
            "retryable": True,
            "recovery_action": "retry_search",
            "search_key": "sessionhash:12",
        })

        self.assertEqual(result.status, RequestStatus.ERROR)
        self.assertEqual(result.action, RequestAction.RETRY_SEARCH)
        self.assertEqual(result.search_id, "sessionhash:12")

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


if __name__ == "__main__":
    unittest.main()

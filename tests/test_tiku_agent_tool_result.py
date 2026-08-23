import unittest

from tiku_agent.tool_result import _PUBLIC_CODES_BY_OUTCOME, ToolOutcome, ToolResult
from tiku_shared.request_protocol import PROTOCOL_REASONS, RequestAction, RequestLayer


class ToolResultContractTest(unittest.TestCase):
    def test_five_outcomes_have_stable_serialized_contract(self):
        cases = [
            ToolResult.success(tool="example", code="DONE", data={"count": 1}),
            ToolResult.no_match(tool="example", code="EMPTY"),
            ToolResult.needs_input(
                tool="example",
                code="MISSING_CHAPTER",
                error="需要章节",
                next_state="WAIT_CHAPTER",
            ),
            ToolResult.partial(
                tool="example",
                code="SOME_TIMEOUTS",
                error="部分未完成",
                next_state="WAIT_CANDIDATE_CHOICE",
                retryable=True,
                error_category="external_timeout",
            ),
            ToolResult.tool_error(
                tool="example",
                code="PROVIDER_FAILED",
                error="服务暂不可用",
                retryable=True,
                error_category="external_service",
            ),
        ]

        self.assertEqual(
            [item.to_dict()["outcome"] for item in cases],
            [item.value for item in ToolOutcome],
        )
        self.assertEqual(
            [item.to_dict()["status"] for item in cases],
            ["SUCCESS", "NO_MATCH", "NEEDS_INPUT", "PARTIAL", "ERROR"],
        )
        self.assertEqual([item.to_dict()["tool"] for item in cases], ["example"] * 5)
        self.assertEqual([item.ok for item in cases], [True, True, True, True, False])
        self.assertEqual(
            [item.completed for item in cases],
            [True, True, False, False, False],
        )

    def test_legacy_ok_constructor_remains_temporarily_compatible(self):
        success = ToolResult(ok=True, data={"legacy": True})
        failure = ToolResult(ok=False, error="legacy failure")

        self.assertEqual(success.outcome, ToolOutcome.SUCCESS)
        self.assertEqual(success.code, "LEGACY_SUCCESS")
        self.assertEqual(failure.outcome, ToolOutcome.ERROR)
        self.assertEqual(failure.code, "LEGACY_TOOL_ERROR")

    def test_old_tool_error_value_is_read_as_public_error(self):
        result = ToolResult(outcome="TOOL_ERROR", code="PROVIDER_FAILED")

        self.assertEqual(result.outcome, ToolOutcome.ERROR)
        self.assertEqual(result.to_dict()["outcome"], "ERROR")
        self.assertEqual(result.to_dict()["layer"], "tool")

    def test_conflicting_legacy_ok_and_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            ToolResult(ok=False, outcome=ToolOutcome.NO_MATCH, code="EMPTY")

    def test_safe_facts_are_copied_and_serialized(self):
        facts = {"load_representation": "mixed", "automatic_search_supported": False}
        result = ToolResult.needs_input(
            tool="route_bank",
            code="LOAD_ROUTE_MIXED_REVIEW_REQUIRED",
            error="internal reason",
            next_state="WAIT_INPUT",
            safe_facts=facts,
            action=RequestAction.RETRY_UPLOAD,
        )

        facts["load_representation"] = "changed"

        self.assertEqual(
            result.safe_facts,
            {"load_representation": "mixed", "automatic_search_supported": False},
        )
        payload = result.to_dict()
        self.assertEqual(payload["safe_facts"], result.safe_facts)
        self.assertEqual(payload["action"], RequestAction.RETRY_UPLOAD.value)

    def test_all_factories_accept_safe_facts_and_action(self):
        factories = [
            lambda: ToolResult.success(
                code="DONE",
                safe_facts={"count": 1},
                action=RequestAction.RETRY_SEARCH,
            ),
            lambda: ToolResult.no_match(
                code="EMPTY",
                safe_facts={"count": 0},
                action=RequestAction.CHANGE_CHAPTER,
            ),
            lambda: ToolResult.needs_input(
                code="MISSING",
                error="legacy text",
                next_state="WAIT_INPUT",
                safe_facts={"missing": "chapter"},
                action=RequestAction.CHANGE_CHAPTER,
            ),
            lambda: ToolResult.partial(
                code="PARTIAL_RESULT",
                error="legacy text",
                next_state="WAIT_INPUT",
                safe_facts={"available": True},
                action=RequestAction.RETRY_SEARCH,
            ),
            lambda: ToolResult.tool_error(
                code="FAILED",
                error="internal failure",
                error_category="test",
                safe_facts={"retry_allowed": True},
                action=RequestAction.RETRY_REQUEST,
            ),
        ]

        results = [factory() for factory in factories]
        self.assertEqual(
            [result.safe_facts for result in results],
            [
                {"count": 1},
                {"count": 0},
                {"missing": "chapter"},
                {"available": True},
                {"retry_allowed": True},
            ],
        )
        self.assertEqual(
            [result.action for result in results],
            [
                RequestAction.RETRY_SEARCH,
                RequestAction.CHANGE_CHAPTER,
                RequestAction.CHANGE_CHAPTER,
                RequestAction.RETRY_SEARCH,
                RequestAction.RETRY_REQUEST,
            ],
        )

    def test_public_serialization_excludes_internal_text_and_payloads(self):
        result = ToolResult.tool_error(
            tool="route_bank",
            code="TOOL_FAILED",
            error="Traceback C:\\private\\provider.py token=secret",
            data={"raw_model_output": "LEAK_DATA"},
            error_category="provider_internal",
            safe_facts={"debug_note": "LEAK_FACT"},
            retryable=True,
            action=RequestAction.RETRY_REQUEST,
        )

        internal = result.to_dict()
        public = result.to_public_dict()

        self.assertIn("error", internal)
        self.assertEqual(
            public,
            {
                "outcome": "ERROR",
                "status": "ERROR",
                "layer": "tool",
                "code": "TOOL_FAILED",
                "completed": False,
                "retryable": True,
                "action": "retry_search",
            },
        )
        self.assertTrue({"error", "data", "safe_facts", "error_category"}.isdisjoint(public))

    def test_public_serialization_replaces_unstable_code_and_ids(self):
        result = ToolResult.tool_error(
            code="token=secret",
            error="internal",
            error_category="test",
        )
        result.request_id = "req_sk-proj-secret"
        result.search_id = "search_valid_01"

        public = result.to_public_dict()

        self.assertEqual(public["code"], "TOOL_FAILED")
        self.assertNotIn("request_id", public)
        self.assertEqual(public["search_id"], "search_valid_01")

    def test_public_serialization_does_not_trust_result_protocol_metadata(self):
        registered = ToolResult.needs_input(
            code="CHAPTER_REQUIRED",
            error="internal",
            next_state="WAIT_INPUT",
        )
        registered.layer = RequestLayer.LOGIN
        registered.retryable = True
        registered.action = RequestAction.RELOGIN

        public = registered.to_public_dict()

        self.assertEqual(public["layer"], "tool")
        self.assertFalse(public["retryable"])
        self.assertEqual(public["action"], "change_chapter")

        unregistered = ToolResult.needs_input(
            code="CANDIDATE_RANK_INVALID",
            error="internal",
            next_state="WAIT_INPUT",
        )
        unregistered.layer = RequestLayer.LOGIN
        unregistered.retryable = True
        unregistered.action = RequestAction.RELOGIN

        public = unregistered.to_public_dict()

        self.assertEqual(public["layer"], "tool")
        self.assertFalse(public["retryable"])
        self.assertEqual(public["action"], "")

    def test_public_serialization_uses_registered_recovery_semantics(self):
        cases = [
            (
                ToolResult.needs_input(
                    code="UNKNOWN_CHAPTER",
                    error="internal",
                    next_state="WAIT_CHAPTER",
                    action=RequestAction.RETRY_SEARCH,
                ),
                "NEEDS_INPUT",
                False,
                "change_chapter",
            ),
            (
                ToolResult.needs_input(
                    code="GLOBAL_SEARCH_IMAGE_REQUIRED",
                    error="internal",
                    next_state="WAIT_IMAGE",
                ),
                "NEEDS_INPUT",
                False,
                "retry_upload",
            ),
            (
                ToolResult.tool_error(
                    code="GLOBAL_SEARCH_UNSUPPORTED_ROUTE",
                    error="internal",
                    error_category="invalid_tool_input",
                    retryable=True,
                    action=RequestAction.RETRY_SEARCH,
                ),
                "ERROR",
                False,
                "",
            ),
        ]

        for result, outcome, retryable, action in cases:
            with self.subTest(code=result.code):
                public = result.to_public_dict()
                self.assertEqual(public["outcome"], outcome)
                self.assertEqual(public["retryable"], retryable)
                self.assertEqual(public["action"], action)

    def test_every_public_tool_code_has_registered_status_and_layer(self):
        for outcome, codes in _PUBLIC_CODES_BY_OUTCOME.items():
            for code in codes:
                with self.subTest(code=code):
                    reason = PROTOCOL_REASONS.get(code)
                    self.assertIsNotNone(reason)
                    self.assertEqual(reason.status, outcome)
                    self.assertEqual(reason.layer.value, "tool")

    def test_public_serialization_uses_consistent_fallback_for_each_outcome(self):
        results = [
            ToolResult.success(code="INTERNAL_DEBUG_STATE"),
            ToolResult.no_match(code="INTERNAL_DEBUG_STATE"),
            ToolResult.needs_input(
                code="TOOL_FAILED",
                error="internal",
                next_state="WAIT_INPUT",
            ),
            ToolResult.partial(
                code="INTERNAL_DEBUG_STATE",
                next_state="PARTIAL",
            ),
            ToolResult.tool_error(
                code="INTERNAL_DEBUG_STATE",
                error="internal",
                error_category="test",
            ),
        ]

        public = [result.to_public_dict() for result in results]

        self.assertEqual(
            [item["code"] for item in public],
            [
                "REQUEST_SUCCEEDED",
                "NO_MATCH",
                "TOOL_INPUT_REQUIRED",
                "PARTIAL_RESULT",
                "TOOL_FAILED",
            ],
        )
        self.assertEqual(
            [item["completed"] for item in public],
            [True, True, False, False, False],
        )
        self.assertEqual(
            [(item["retryable"], item["action"]) for item in public],
            [
                (False, ""),
                (False, "change_chapter"),
                (False, ""),
                (True, "retry_search"),
                (True, "retry_search"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

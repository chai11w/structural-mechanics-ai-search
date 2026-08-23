import unittest

from tiku_agent.tool_result import ToolOutcome, ToolResult
from tiku_shared.request_protocol import RequestAction


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


if __name__ == "__main__":
    unittest.main()

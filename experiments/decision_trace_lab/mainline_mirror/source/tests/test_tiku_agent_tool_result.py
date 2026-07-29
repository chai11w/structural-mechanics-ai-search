import unittest

from tiku_agent.tool_result import ToolOutcome, ToolResult


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
        self.assertEqual(failure.outcome, ToolOutcome.TOOL_ERROR)
        self.assertEqual(failure.code, "LEGACY_TOOL_ERROR")

    def test_conflicting_legacy_ok_and_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            ToolResult(ok=False, outcome=ToolOutcome.NO_MATCH, code="EMPTY")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from tiku_shared.trace_context import (
    TraceContext,
    current_request_id,
    current_trace_context,
    current_trace_id,
    is_valid_trace_id,
    new_trace_id,
    submit_with_trace_context,
    trace_context_scope,
)


class TraceContextTest(unittest.TestCase):
    def test_server_ids_are_unique_and_strictly_formatted(self):
        first = new_trace_id()
        second = new_trace_id()

        self.assertNotEqual(first, second)
        self.assertTrue(is_valid_trace_id(first))
        self.assertRegex(first, r"^trace_[0-9a-f]{32}$")
        self.assertFalse(is_valid_trace_id("trace_not-hex"))
        with self.assertRaisesRegex(ValueError, "invalid trace_id"):
            TraceContext(trace_id="req_12345678")

    def test_scope_exposes_request_and_restores_nested_context(self):
        outer = TraceContext.create(request_id="req_outer_1234")
        inner = TraceContext.create(request_id="req_inner_1234")

        self.assertIsNone(current_trace_context())
        self.assertEqual(current_trace_id(), "")
        self.assertEqual(current_request_id(), "")
        with trace_context_scope(outer):
            self.assertIs(current_trace_context(), outer)
            self.assertEqual(current_trace_id(), outer.trace_id)
            self.assertEqual(current_request_id(), "req_outer_1234")
            with trace_context_scope(inner):
                self.assertEqual(current_trace_id(), inner.trace_id)
                self.assertEqual(current_request_id(), "req_inner_1234")
            self.assertEqual(current_trace_id(), outer.trace_id)
        self.assertIsNone(current_trace_context())

    def test_thread_submission_copies_trace_without_leaking_it(self):
        context = TraceContext.create(request_id="req_thread_1234")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with trace_context_scope(context):
                result = submit_with_trace_context(
                    executor,
                    lambda: (current_trace_id(), current_request_id()),
                ).result()
            outside = executor.submit(
                lambda: (current_trace_id(), current_request_id())
            ).result()

        self.assertEqual(result, (context.trace_id, context.request_id))
        self.assertEqual(outside, ("", ""))

    def test_scope_restores_context_after_exception(self):
        context = TraceContext.create(request_id="req_failure_1234")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with trace_context_scope(context):
                raise RuntimeError("stop")

        self.assertIsNone(current_trace_context())


if __name__ == "__main__":
    unittest.main()

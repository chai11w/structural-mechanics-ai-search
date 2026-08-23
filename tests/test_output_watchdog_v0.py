import unittest

from tiku_agent.output_watchdog_v0 import guard_output, inspect_output


class OutputWatchdogV0Test(unittest.TestCase):
    def test_normal_text_passes_unchanged(self):
        result = guard_output("Found 2 candidates.", mode="enforce")
        self.assertEqual(result.action, "pass")
        self.assertEqual(result.text, "Found 2 candidates.")

    def test_observe_does_not_change_dangerous_text(self):
        result = guard_output("failed: C:\\secret\\answer.png", mode="observe")
        self.assertEqual(result.action, "replace")
        self.assertIn("C:\\secret", result.text)
        self.assertIn("local_path", result.reasons)

    def test_enforce_replaces_dangerous_text(self):
        result = guard_output(
            "Traceback (most recent call last): Exception: token=abc",
            mode="enforce",
            fallback="safe retry",
        )
        self.assertEqual(result.action, "replace")
        self.assertEqual(result.text, "safe retry")

    def test_missing_media_is_observed_when_text_claims_delivery(self):
        result = inspect_output(
            "答案发你了。", expected_media=1, delivered_media=0
        )
        self.assertEqual(result.action, "replace")
        self.assertIn("media_delivery_mismatch", result.reasons)

    def test_missing_media_does_not_affect_unrelated_text(self):
        result = inspect_output(
            "Please choose a candidate.", expected_media=1, delivered_media=0
        )
        self.assertEqual(result.action, "pass")

    def test_mild_text_can_be_polished(self):
        result = guard_output(
            "hello,  there.", mode="enforce", polisher=lambda _: "hello, there."
        )
        self.assertEqual(result.action, "polish")
        self.assertEqual(result.text, "hello, there.")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            guard_output("x", mode="invalid")


if __name__ == "__main__":
    unittest.main()

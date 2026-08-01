"""Offline contract tests for the independent sequential holdout."""

import unittest

from scripts.evaluate_shadow_admission_qwen_v0 import FIXTURE as DEVELOPMENT_FIXTURE
from scripts.evaluate_shadow_admission_qwen_v0 import load_admission_cases
from scripts.evaluate_shadow_sequential_holdout_qwen_v0 import (
    FAST_PATH_ONLY,
    FIXTURE,
    FUTURE_READY,
    attach_holdout_metadata,
    load_holdout_cases,
    summarize_holdout,
)


CONFIRMATION_FIXTURE = FIXTURE.with_name("shadow_sequential_confirmation_v0_cases.json")


class SequentialHoldoutFixtureTest(unittest.TestCase):
    def test_holdout_is_independent_balanced_and_has_atomic_guards(self):
        cases = load_holdout_cases(FIXTURE)
        development_texts = {case["text"] for case in load_admission_cases(DEVELOPMENT_FIXTURE)}
        positives = [case for case in cases if case["future_expectation"] == FUTURE_READY]
        atomic = [case for case in cases if case["future_expectation"] == FAST_PATH_ONLY]
        scenarios = {case["scenario"] for case in positives}

        self.assertEqual(len(cases), 24)
        self.assertEqual(len(positives), 18)
        self.assertEqual(len(atomic), 6)
        self.assertEqual(len(scenarios), 6)
        self.assertTrue(all(sum(case["scenario"] == item for case in positives) == 3 for item in scenarios))
        self.assertFalse({case["text"] for case in cases} & development_texts)

    def test_confirmation_split_is_new_and_excludes_the_failed_continue_show_class(self):
        development = load_admission_cases(DEVELOPMENT_FIXTURE)
        holdout = load_holdout_cases(FIXTURE)
        confirmation = load_holdout_cases(CONFIRMATION_FIXTURE)
        prior_texts = {case["text"] for case in development + holdout}
        positives = [case for case in confirmation if case["future_expectation"] == FUTURE_READY]
        self.assertEqual(len(confirmation), 15)
        self.assertEqual(len(positives), 10)
        self.assertEqual(len({case["scenario"] for case in positives}), 5)
        self.assertNotIn("continue_then_show", {case["scenario"] for case in positives})
        self.assertFalse({case["text"] for case in confirmation} & prior_texts)


class SequentialHoldoutSummaryTest(unittest.TestCase):
    @staticmethod
    def _record(case_id: str, scenario: str, *, ready: bool, expectation: str) -> dict:
        return {
            "case_id": case_id,
            "run": 1,
            "current_admitted": False,
            "observable_equal": True,
            "diagnostic": {
                "future_admission_ready": ready,
                "goal_covered": ready,
                "effective_forbidden_actions": [],
                "route": "shadow_actionable" if ready else "needs_confirmation",
            },
            "scenario": scenario,
            "future_expectation": expectation,
            "holdout_ok": ready if expectation == FUTURE_READY else True,
        }

    def test_all_positive_scenarios_must_be_stable(self):
        scenarios = (
            "show_then_select",
            "answered_show_then_select",
            "report_then_show",
            "resend_then_show",
            "explain_then_retry",
            "continue_then_show",
        )
        records = [
            self._record(f"positive-{index}", scenario, ready=True, expectation=FUTURE_READY)
            for index, scenario in enumerate(scenarios)
        ]
        records.append(self._record("atomic", "atomic_guard", ready=False, expectation=FAST_PATH_ONLY))
        summary = summarize_holdout(records)
        self.assertTrue(summary["candidate_ready"])
        self.assertEqual(summary["stable_scenarios"], summary["expected_scenarios"])

    def test_one_unstable_paraphrase_blocks_candidate_readiness(self):
        records = [
            self._record("good", "show_then_select", ready=True, expectation=FUTURE_READY),
            self._record("bad", "show_then_select", ready=False, expectation=FUTURE_READY),
            self._record("atomic", "atomic_guard", ready=False, expectation=FAST_PATH_ONLY),
        ]
        summary = summarize_holdout(records)
        self.assertFalse(summary["candidate_ready"])
        self.assertEqual(summary["stable_scenarios"], [])

    def test_metadata_is_attached_from_fixture_cases(self):
        cases = [{"id": "one", "scenario": "show_then_select", "future_expectation": FUTURE_READY}]
        records = [self._record("one", "", ready=True, expectation=FUTURE_READY)]
        records[0].pop("scenario")
        records[0].pop("future_expectation")
        records[0].pop("holdout_ok")
        attached = attach_holdout_metadata(records, cases)
        self.assertEqual(attached[0]["scenario"], "show_then_select")
        self.assertTrue(attached[0]["holdout_ok"])


if __name__ == "__main__":
    unittest.main()

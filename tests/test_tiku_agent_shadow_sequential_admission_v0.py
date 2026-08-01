"""Offline gold tests for the first-wave sequential admission classifier."""

import json
from pathlib import Path
import unittest

from scripts.evaluate_shadow_sequential_admission_v0 import evaluate_all
from tiku_agent.shadow_sequential_admission_v0 import (
    REPORT_THEN_SHOW,
    SHOW_THEN_SELECT,
    classify_sequential_shadow_admission,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


class SequentialAdmissionGoldTest(unittest.TestCase):
    def test_only_two_proven_scenarios_are_admitted_across_existing_splits(self):
        cases = (
            _load("shadow_admission_v0_cases.json")
            + _load("shadow_sequential_holdout_v0_cases.json")
            + _load("shadow_sequential_confirmation_v0_cases.json")
        )
        expected_ids = {
            "seq_show_then_select",
            "seq_report_then_show",
            "holdout_show_select_1",
            "holdout_show_select_2",
            "holdout_show_select_3",
            "holdout_report_show_1",
            "holdout_report_show_2",
            "holdout_report_show_3",
            "confirm_show_select_1",
            "confirm_show_select_2",
            "confirm_report_show_1",
            "confirm_report_show_2",
        }
        actual_ids = {
            case["id"]
            for case in cases
            if classify_sequential_shadow_admission(case["text"], phase=case["phase"]).admitted
        }
        self.assertEqual(actual_ids, expected_ids)

    def test_scenario_and_ordered_evidence_are_reported(self):
        show = classify_sequential_shadow_admission(
            "先把候选列表再发一下，然后选择候选2",
            phase="WAIT_CANDIDATE_CHOICE",
        )
        report = classify_sequential_shadow_admission(
            "这个答案不匹配，记录后返回候选列表",
            phase="ANSWERED",
        )
        self.assertEqual(show.scenario, SHOW_THEN_SELECT)
        self.assertEqual(report.scenario, REPORT_THEN_SHOW)
        self.assertEqual(tuple(item.action for item in show.evidence), ("show_candidates", "select_candidate"))
        self.assertLessEqual(show.evidence[0].end, show.evidence[1].start)

    def test_atomic_conditional_reversed_negated_and_uncertain_requests_are_rejected(self):
        cases = (
            ("候选列表再发一下", "WAIT_CANDIDATE_CHOICE"),
            ("选择候选2", "WAIT_CANDIDATE_CHOICE"),
            ("如果候选列表没问题，就再打开候选2", "WAIT_CANDIDATE_CHOICE"),
            ("选择候选2之后再返回候选列表", "WAIT_CANDIDATE_CHOICE"),
            ("先不要展示候选，然后选择候选2", "WAIT_CANDIDATE_CHOICE"),
            ("候选列表发回来，然后别选候选2", "WAIT_CANDIDATE_CHOICE"),
            ("先看候选列表，然后选择另一个", "WAIT_CANDIDATE_CHOICE"),
            ("先看候选列表，然后是不是选候选2？", "WAIT_CANDIDATE_CHOICE"),
            ("返回候选列表后标记答案不匹配", "ANSWERED"),
            ("答案可能不对，之后返回候选列表", "ANSWERED"),
            ("答案并没有不匹配，之后返回候选列表", "ANSWERED"),
            ("答案不对，但先别返回候选列表", "ANSWERED"),
        )
        for text, phase in cases:
            with self.subTest(text=text):
                self.assertFalse(classify_sequential_shadow_admission(text, phase=phase).admitted)

    def test_offline_evaluator_passes_development_holdout_and_confirmation(self):
        report = evaluate_all()
        self.assertTrue(report["offline_only"])
        self.assertFalse(report["runtime_wired"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["positive"], 12)
        self.assertEqual(report["negative"], 67)

    def test_answered_show_then_select_remains_outside_first_wave(self):
        decision = classify_sequential_shadow_admission(
            "先返回候选列表，再选择候选2",
            phase="ANSWERED",
        )
        self.assertFalse(decision.admitted)

    def test_classifier_is_phase_strict(self):
        text = "先把候选列表再发一下，然后选择候选2"
        for phase in ("IDLE", "WAIT_CHAPTER", "ANSWERED", "ERROR"):
            with self.subTest(phase=phase):
                self.assertFalse(classify_sequential_shadow_admission(text, phase=phase).admitted)


if __name__ == "__main__":
    unittest.main()

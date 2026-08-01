import json
from pathlib import Path
import unittest

from tiku_agent.safe_answer_context_v0 import (
    SafeAnswerValidationFacts,
    SafeConversationContext,
)
from tiku_agent.safe_answer_contract_v0 import validate_safe_answer_output_v0


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "safe_answer_state_consistency_v0_cases.json"
)


class SafeAnswerStateConsistencyDatasetV0Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_dataset_is_balanced_and_covers_the_user_phases(self):
        groups = self.dataset["groups"]
        self.assertEqual(
            {group["phase"] for group in groups},
            {
                "IDLE",
                "WAIT_CHAPTER",
                "WAIT_QUESTION_CHOICE",
                "WAIT_CANDIDATE_CHOICE",
                "ANSWERED",
                "NO_MATCH",
                "ERROR",
            },
        )
        accepted = sum(len(group["accept"]) for group in groups)
        rejected = sum(len(group["reject"]) for group in groups)
        self.assertEqual(accepted, 56)
        self.assertEqual(rejected, 56)

    def test_expected_safe_outputs_are_accepted(self):
        category = self.dataset["category"]
        for group in self.dataset["groups"]:
            context = SafeConversationContext(**group["context"])
            validation_facts = SafeAnswerValidationFacts(**group["validation_facts"])
            for text in group["accept"]:
                with self.subTest(phase=group["phase"], text=text):
                    validation = validate_safe_answer_output_v0(
                        text,
                        category,
                        context,
                        validation_facts,
                    )
                    self.assertTrue(validation.accepted, validation.reason)

    def test_expected_state_conflicts_are_rejected(self):
        category = self.dataset["category"]
        for group in self.dataset["groups"]:
            context = SafeConversationContext(**group["context"])
            validation_facts = SafeAnswerValidationFacts(**group["validation_facts"])
            for case in group["reject"]:
                with self.subTest(phase=group["phase"], text=case["text"]):
                    validation = validate_safe_answer_output_v0(
                        case["text"],
                        category,
                        context,
                        validation_facts,
                    )
                    self.assertFalse(validation.accepted)
                    self.assertEqual(validation.reason, case["reason"])


if __name__ == "__main__":
    unittest.main()

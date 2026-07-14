import unittest

from tiku_agent.intent_contract import STATE_IDLE, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_CANCELLED,
    PHASE_NO_MATCH,
    PHASE_PROCESSING,
    PHASE_READY_FOR_SEARCH,
    PHASE_READY_TO_ROUTE,
    AgentState,
)


class TikuAgentStateTest(unittest.TestCase):
    def test_state_has_agent_dialogue_fields(self):
        state = AgentState()
        self.assertEqual(
            set(state.to_dict()),
            {
                "session_id",
                "phase",
                "current_image_path",
                "current_question_image_path",
                "current_loads",
                "current_chapter",
                "current_route",
                "current_structure_type",
                "questions",
                "selected_question",
                "previous_question",
                "completed_questions",
                "candidates",
                "selected_rank",
                "last_answer_paths",
                "last_intent",
                "last_error",
                "revision_count",
                "pending_chapter",
                "global_search_offered",
            },
        )
        self.assertEqual(state.phase, STATE_IDLE)

    def test_backward_compatible_field_aliases(self):
        state = AgentState(image_path="q.jpg", chapter="4力法", loads=[{"type": "集中", "raw": "P"}])
        self.assertEqual(state.state, STATE_IDLE)
        self.assertEqual(state.image_path, "q.jpg")
        self.assertEqual(state.chapter, "4力法")
        self.assertEqual(state.loads[0]["raw"], "P")

        state.route = "symbolic"
        state.structure_type = "梁"
        state.state = PHASE_READY_FOR_SEARCH
        self.assertEqual(state.current_route, "symbolic")
        self.assertEqual(state.current_structure_type, "梁")
        self.assertEqual(state.phase, PHASE_READY_FOR_SEARCH)

    def test_round_trip_dict(self):
        state = AgentState(
            current_image_path="q.jpg",
            current_chapter="4力法",
            current_loads=[{"type": "集中", "raw": "P"}],
            last_answer_paths=["a.jpg"],
        )
        restored = AgentState.from_dict(state.to_dict())
        self.assertEqual(restored.current_image_path, "q.jpg")
        self.assertEqual(restored.current_chapter, "4力法")
        self.assertEqual(restored.current_loads[0]["raw"], "P")
        self.assertEqual(restored.last_answer_paths, ["a.jpg"])

    def test_legacy_state_without_v2_context_fields_still_loads(self):
        legacy = AgentState(current_image_path="q.jpg", current_chapter="4力法").to_dict()
        legacy.pop("previous_question")
        legacy.pop("completed_questions")
        legacy.pop("pending_chapter")
        legacy.pop("global_search_offered")

        restored = AgentState.from_dict(legacy)

        self.assertIsNone(restored.previous_question)
        self.assertEqual(restored.completed_questions, [])
        self.assertEqual(restored.pending_chapter, "")
        self.assertFalse(restored.global_search_offered)

    def test_global_search_offer_round_trips_and_is_consumed(self):
        state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
        )
        state.offer_global_search()
        restored = AgentState.from_dict(state.to_dict())
        self.assertTrue(restored.global_search_offered)
        self.assertTrue(restored.consume_global_search_offer())
        self.assertFalse(restored.global_search_offered)

    def test_start_search_resets_current_dialogue_context(self):
        state = AgentState(
            current_image_path="old.jpg",
            current_chapter="4力法",
            candidates=[{"rank": 1}],
            last_answer_paths=["a.jpg"],
        )
        state.start_search("new.jpg")
        self.assertEqual(state.phase, PHASE_PROCESSING)
        self.assertEqual(state.current_image_path, "new.jpg")
        self.assertEqual(state.current_chapter, "")
        self.assertEqual(state.candidates, [])
        self.assertEqual(state.last_answer_paths, [])

    def test_analysis_without_chapter_waits_for_chapter(self):
        state = AgentState()
        state.set_analysis(loads=[{"type": "集中", "raw": "P"}])
        self.assertEqual(state.phase, STATE_WAIT_CHAPTER)
        self.assertEqual(state.current_loads[0]["raw"], "P")

    def test_analysis_with_chapter_is_ready_to_route(self):
        state = AgentState()
        state.set_analysis(loads=[{"type": "均布", "raw": "q"}], chapter="2静定结构")
        self.assertEqual(state.phase, PHASE_READY_TO_ROUTE)
        self.assertEqual(state.current_chapter, "2静定结构")

    def test_correct_chapter_clears_old_candidates_and_counts_revision(self):
        state = AgentState(
            current_chapter="2静定结构",
            candidates=[{"rank": 1, "name": "old"}],
            selected_rank=1,
            last_answer_paths=["old_answer.jpg"],
        )
        state.correct_chapter("3静定结构位移")
        self.assertEqual(state.phase, PHASE_READY_TO_ROUTE)
        self.assertEqual(state.current_chapter, "3静定结构位移")
        self.assertEqual(state.revision_count, 1)
        self.assertEqual(state.candidates, [])
        self.assertIsNone(state.selected_rank)
        self.assertEqual(state.last_answer_paths, [])

    def test_set_chapter_without_correction_does_not_count_revision(self):
        state = AgentState(phase=STATE_WAIT_CHAPTER)
        state.set_chapter("5位移法")
        self.assertEqual(state.phase, PHASE_READY_TO_ROUTE)
        self.assertEqual(state.revision_count, 0)

    def test_set_route(self):
        state = AgentState()
        state.set_route("symbolic", structure_type="梁")
        self.assertEqual(state.phase, PHASE_READY_FOR_SEARCH)
        self.assertEqual(state.current_route, "symbolic")
        self.assertEqual(state.current_structure_type, "梁")

    def test_candidates_are_renumbered(self):
        state = AgentState()
        state.set_candidates([{"rank": 9, "name": "a"}, {"name": "b"}])
        self.assertEqual(state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(state.candidate_count, 2)
        self.assertEqual([item["rank"] for item in state.candidates], [1, 2])
        self.assertEqual(state.select_candidate(2)["name"], "b")
        self.assertEqual(state.selected_rank, 2)

    def test_no_candidates_is_terminal_no_match(self):
        state = AgentState()
        state.set_candidates([])
        self.assertEqual(state.phase, PHASE_NO_MATCH)
        self.assertTrue(state.is_terminal)

    def test_multi_question_selection_updates_current_question(self):
        state = AgentState(current_image_path="full.jpg")
        state.set_questions(
            [
                {"question_image_path": "q1.jpg", "chapter": "", "loads": [{"raw": "P"}]},
                {"question_image_path": "q2.jpg", "chapter": "4力法", "loads": [{"raw": "q"}]},
            ]
        )
        self.assertEqual(state.question_count, 2)
        question = state.select_question(2)
        self.assertEqual(question["question_image_path"], "q2.jpg")
        self.assertEqual(state.selected_question, 2)
        self.assertEqual(state.current_image_path, "full.jpg")
        self.assertEqual(state.current_question_image_path, "q2.jpg")
        self.assertEqual(state.active_image_path, "q2.jpg")
        self.assertEqual(state.current_chapter, "4力法")
        self.assertEqual(state.phase, PHASE_READY_TO_ROUTE)

    def test_multi_question_progress_and_pending_chapter_round_trip(self):
        state = AgentState(current_image_path="full.jpg")
        state.set_questions(
            [
                {"question_image_path": "q1.jpg", "chapter": "4力法"},
                {"question_image_path": "q2.jpg", "chapter": "5位移法"},
            ]
        )
        state.select_question(1)
        state.set_answer_paths(["a1.jpg"])
        state.select_question(2)
        state.set_pending_chapter("8影响线")

        restored = AgentState.from_dict(state.to_dict())

        self.assertEqual(restored.previous_question, 1)
        self.assertEqual(restored.completed_questions, [1])
        self.assertEqual(restored.pending_chapter, "8影响线")
        self.assertEqual(restored.consume_pending_chapter(), "8影响线")
        self.assertEqual(restored.pending_chapter, "")

    def test_answered_keeps_answer_paths_for_recall(self):
        state = AgentState()
        state.set_answer_paths(["answer1.jpg", "answer2.jpg"])
        self.assertEqual(state.phase, PHASE_ANSWERED)
        self.assertEqual(state.last_answer_paths, ["answer1.jpg", "answer2.jpg"])
        self.assertFalse(state.is_terminal)

    def test_remember_intent_and_error(self):
        state = AgentState()
        state.remember_intent({"intent": "set_chapter", "chapter": "4力法"})
        self.assertEqual(state.last_intent["intent"], "set_chapter")
        state.fail("boom")
        self.assertEqual(state.last_error, "boom")

    def test_cancel_is_terminal(self):
        state = AgentState()
        state.cancel()
        self.assertEqual(state.phase, PHASE_CANCELLED)
        self.assertTrue(state.is_terminal)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            AgentState(phase="BOGUS")
        with self.assertRaises(ValueError):
            AgentState(selected_rank=0)
        with self.assertRaises(ValueError):
            AgentState(revision_count=-1)


if __name__ == "__main__":
    unittest.main()

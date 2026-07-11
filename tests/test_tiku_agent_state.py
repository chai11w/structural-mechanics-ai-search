import unittest

from tiku_agent.intent import STATE_IDLE, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER
from tiku_agent.state import (
    STATE_CANCELLED,
    STATE_DONE,
    STATE_NO_MATCH,
    STATE_READY_FOR_SEARCH,
    STATE_READY_TO_ROUTE,
    AgentState,
)


class TikuAgentStateTest(unittest.TestCase):
    def test_state_has_minimal_11_fields(self):
        state = AgentState()
        self.assertEqual(
            set(state.to_dict()),
            {
                "session_id",
                "state",
                "image_path",
                "chapter",
                "loads",
                "route",
                "structure_type",
                "candidates",
                "selected_rank",
                "questions",
                "selected_question",
            },
        )
        self.assertEqual(state.state, STATE_IDLE)

    def test_round_trip_dict(self):
        state = AgentState(image_path="q.jpg", chapter="4力法", loads=[{"type": "集中", "raw": "P"}])
        restored = AgentState.from_dict(state.to_dict())
        self.assertEqual(restored.image_path, "q.jpg")
        self.assertEqual(restored.chapter, "4力法")
        self.assertEqual(restored.loads[0]["raw"], "P")

    def test_analysis_without_chapter_waits_for_chapter(self):
        state = AgentState()
        state.set_analysis(loads=[{"type": "集中", "raw": "P"}])
        self.assertEqual(state.state, STATE_WAIT_CHAPTER)
        self.assertEqual(state.loads[0]["raw"], "P")

    def test_analysis_with_chapter_is_ready_to_route(self):
        state = AgentState()
        state.set_analysis(loads=[{"type": "均布", "raw": "q"}], chapter="2静定结构")
        self.assertEqual(state.state, STATE_READY_TO_ROUTE)
        self.assertEqual(state.chapter, "2静定结构")

    def test_set_chapter_and_route(self):
        state = AgentState(state=STATE_WAIT_CHAPTER)
        state.set_chapter("5位移法")
        self.assertEqual(state.state, STATE_READY_TO_ROUTE)
        state.set_route("symbolic", structure_type="梁")
        self.assertEqual(state.state, STATE_READY_FOR_SEARCH)
        self.assertEqual(state.route, "symbolic")
        self.assertEqual(state.structure_type, "梁")

    def test_candidates_are_renumbered(self):
        state = AgentState()
        state.set_candidates([{"rank": 9, "name": "a"}, {"name": "b"}])
        self.assertEqual(state.state, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(state.candidate_count, 2)
        self.assertEqual([item["rank"] for item in state.candidates], [1, 2])
        self.assertEqual(state.select_candidate(2)["name"], "b")
        self.assertEqual(state.selected_rank, 2)

    def test_no_candidates_is_terminal_no_match(self):
        state = AgentState()
        state.set_candidates([])
        self.assertEqual(state.state, STATE_NO_MATCH)
        self.assertTrue(state.is_terminal)

    def test_multi_question_selection_updates_current_question(self):
        state = AgentState()
        state.set_questions(
            [
                {"image_path": "q1.jpg", "chapter": "", "loads": [{"raw": "P"}]},
                {"image_path": "q2.jpg", "chapter": "4力法", "loads": [{"raw": "q"}]},
            ]
        )
        self.assertEqual(state.question_count, 2)
        question = state.select_question(2)
        self.assertEqual(question["image_path"], "q2.jpg")
        self.assertEqual(state.selected_question, 2)
        self.assertEqual(state.image_path, "q2.jpg")
        self.assertEqual(state.chapter, "4力法")
        self.assertEqual(state.state, STATE_READY_TO_ROUTE)

    def test_done_and_cancel_are_terminal(self):
        done = AgentState()
        done.mark_done()
        self.assertEqual(done.state, STATE_DONE)
        self.assertTrue(done.is_terminal)

        cancelled = AgentState()
        cancelled.cancel()
        self.assertEqual(cancelled.state, STATE_CANCELLED)
        self.assertTrue(cancelled.is_terminal)

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            AgentState(state="BOGUS").validate()
        with self.assertRaises(ValueError):
            AgentState(selected_rank=0).validate()


if __name__ == "__main__":
    unittest.main()

import unittest

from tiku_agent.image_contracts import ImageTriageObservation
from tiku_agent.image_triage import build_handoff, finalize_route, parse_route_candidate


class ImageTriageTest(unittest.TestCase):
    def test_parse_route_requires_a_standalone_recommendation(self):
        self.assertEqual(parse_route_candidate("建议路线：A3\n原因：一题多图"), "A3")
        self.assertEqual(parse_route_candidate("A2：单题且完整"), "A2")
        with self.assertRaises(ValueError):
            parse_route_candidate("图中有 A30 标注")

    def test_a2_with_auxiliary_diagrams_is_downgraded_to_a3(self):
        observation = ImageTriageObservation(
            route_candidate="A2",
            question_count=1,
            original_structure_count=1,
            auxiliary_diagram_count=2,
            has_actual_load_evidence=True,
            image_recoverable=True,
            evidence=("一题三图",),
        )
        self.assertEqual(finalize_route(observation), "A3")
        handoff = build_handoff("download.png", observation)
        self.assertEqual(handoff.next_action, "a3_processing")
        self.assertEqual(handoff.to_dict()["source_image_path"], "download.png")

    def test_a2_handoff_keeps_image_and_reason_for_existing_search(self):
        observation = ImageTriageObservation(
            route_candidate="A2",
            question_count=1,
            original_structure_count=1,
            auxiliary_diagram_count=0,
            has_actual_load_evidence=True,
            image_recoverable=True,
            evidence=("单题、单原结构图、真实外荷载清楚",),
        )
        handoff = build_handoff("single.jpg", observation)
        self.assertEqual(handoff.route, "A2")
        self.assertEqual(handoff.next_action, "existing_search")
        self.assertEqual(handoff.reason, observation.evidence)

    def test_uncertain_a1_is_not_allowed_to_reject_structure_content(self):
        observation = ImageTriageObservation(
            route_candidate="A1",
            has_structure_content=True,
            evidence=("只有局部结构",),
        )
        self.assertEqual(finalize_route(observation), "A3")

    def test_clear_a1_stops_without_downstream_processing(self):
        observation = ImageTriageObservation(
            route_candidate="A1",
            has_structure_content=False,
            evidence=("明确是无关图片",),
        )
        handoff = build_handoff("wallpaper.jpg", observation)
        self.assertEqual(handoff.route, "A1")
        self.assertEqual(handoff.next_action, "stop")


if __name__ == "__main__":
    unittest.main()

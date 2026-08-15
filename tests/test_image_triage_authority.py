import unittest

from tiku_agent.image_contracts import ImageTriageObservation
from tiku_agent.image_triage_authority import (
    ImageTriageAuthority,
    QwenTriageReplyClient,
    normalize_triage_reply,
    triage_reply_is_usable,
)


def observation(route: str) -> ImageTriageObservation:
    if route == "A1":
        return ImageTriageObservation(
            route_candidate="A1",
            evidence=("图片是无关截图。",),
            has_structure_content=False,
            raw_text="建议路线：A1\n图片是无关截图。",
        )
    if route == "A2":
        return ImageTriageObservation(
            route_candidate="A2",
            evidence=("单题、单原结构图、实际荷载清楚。",),
            question_count=1,
            original_structure_count=1,
            auxiliary_diagram_count=0,
            has_actual_load_evidence=True,
            has_structure_content=True,
            image_recoverable=True,
            has_ambiguity=False,
            raw_text="建议路线：A2\n单题、单原结构图、实际荷载清楚。",
        )
    return ImageTriageObservation(
        route_candidate="A3",
        evidence=("一题多图，图形关系需要拆分。",),
        has_structure_content=True,
        raw_text="建议路线：A3\n一题多图，图形关系需要拆分。",
    )


class FakeObserver:
    def __init__(self, route: str) -> None:
        self.value = observation(route)

    def observe(self, _image_path):
        return self.value


class ImageTriageAuthorityTest(unittest.TestCase):
    def test_a3_reply_boundary_does_not_promise_background_processing(self):
        self.assertIn("当前没有自动拆图功能", QwenTriageReplyClient.SYSTEM_PROMPT)
        self.assertIn("不要让用户等待", QwenTriageReplyClient.SYSTEM_PROMPT)
        self.assertIn("不超过一百个汉字", QwenTriageReplyClient.SYSTEM_PROMPT)

    def test_reply_formula_markup_is_made_readable_for_the_web_ui(self):
        reply = normalize_triage_reply(
            r"图中同时有 $M_P$ 图和 $\bar{M}$ 图，请裁剪后重传。"
        )

        self.assertEqual(reply, "图中同时有 MP 图和 M1 图，请裁剪后重传。")
        self.assertNotIn("$", reply)
        self.assertNotIn("\\", reply)

    def test_a3_reply_must_be_short_honest_and_actionable(self):
        good = (
            "这张图包含多个结构图，暂时不能直接检索，当前没有自动拆图功能。"
            "请裁剪后重新上传，只保留完整原结构图、支座和实际荷载。"
        )
        waiting = "图形较复杂，后续会自动处理，请耐心等待。"
        no_action = "图形较复杂，当前没有自动拆图功能。"

        self.assertTrue(triage_reply_is_usable(good, "A3"))
        self.assertFalse(triage_reply_is_usable(waiting, "A3"))
        self.assertFalse(triage_reply_is_usable(no_action, "A3"))
        self.assertFalse(triage_reply_is_usable("太长" * 51, "A3"))

    def test_a1_and_a3_pass_the_handoff_to_the_second_qwen(self):
        for route in ("A1", "A3"):
            seen = []

            def reply_client(handoff):
                seen.append(handoff)
                return f"{route} 的自然说明"

            with self.subTest(route=route):
                decision = ImageTriageAuthority(
                    FakeObserver(route), reply_client
                ).decide("question.jpg")
                self.assertEqual(decision.handoff.route, route)
                self.assertEqual(decision.reply, f"{route} 的自然说明")
                self.assertEqual(decision.reply_source, "qwen_triage_reply")
                self.assertEqual(seen[0].observation.raw_text, observation(route).raw_text)

    def test_a2_does_not_call_the_result_reply_model(self):
        decision = ImageTriageAuthority(
            FakeObserver("A2"),
            lambda _handoff: self.fail("A2 must enter search without a reply call"),
        ).decide("question.jpg")

        self.assertEqual(decision.handoff.route, "A2")
        self.assertEqual(decision.reply, "")
        self.assertEqual(decision.reply_source, "")

    def test_reply_failure_keeps_the_safe_route_and_uses_chinese_fallback(self):
        def fail(_handoff):
            raise TimeoutError("late")

        decision = ImageTriageAuthority(FakeObserver("A3"), fail).decide("question.jpg")

        self.assertEqual(decision.handoff.route, "A3")
        self.assertEqual(decision.reply_source, "fixed_fallback")
        self.assertEqual(decision.fallback_reason, "TimeoutError")
        self.assertIn("当前也没有自动拆图功能", decision.reply)
        self.assertIn("裁剪后重新上传", decision.reply)


if __name__ == "__main__":
    unittest.main()

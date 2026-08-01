"""Tests for the Stage 5 shadow-planner prompt contract and client."""

import unittest

from tiku_agent.shadow_plan_v0 import (
    ShadowPlan,
    ShadowPlanStep,
    ShadowPlannerResult,
)
from tiku_agent.shadow_planner_v0 import (
    SHADOW_PLAN_PROMPT,
    ShadowPlannerV0,
    build_shadow_plan_prompt_v0,
    call_qwen_planner_v0,
    parse_shadow_plan_v0,
)


def _context_payload(**overrides) -> dict:
    payload = {
        "phase": "WAIT_CANDIDATE_CHOICE",
        "active_namespace": "candidate",
        "question_count": 0,
        "candidate_count": 3,
        "current_chapter": "4力法",
        "has_active_image": True,
        "has_answer": False,
        "global_search_offered": False,
        "continuation_available": False,
    }
    payload.update(overrides)
    return payload


def _valid_payload(**overrides) -> dict:
    payload = {
        "rewritten": {
            "text": "用户想在第2题和候选1之间选择，先看第2题的答案",
            "keywords": ["第2题", "看答案"],
            "reason": "原话省略了主语，补上具体题号和诉求",
        },
        "goal": "选第2题看答案",
        "steps": [
            {"action": "select_question", "params": {"question_index": 2}, "reason": "用户明确说第2题"},
            {"action": "select_candidate", "params": {"candidate_rank": 1}, "reason": ""},
        ],
        "stop_condition": "用户确认答案后",
    }
    payload.update(overrides)
    return payload


class BuildPromptTest(unittest.TestCase):
    def test_prompt_contains_user_text(self) -> None:
        prompt = build_shadow_plan_prompt_v0("帮我看看第二题的答案", _context_payload())
        self.assertIn("帮我看看第二题的答案", prompt)

    def test_prompt_contains_sanitized_context_only(self) -> None:
        prompt = build_shadow_plan_prompt_v0("x", _context_payload())
        for banned in ("session_id", ".jpg", "answer.png", "score", "stack"):
            self.assertNotIn(banned, prompt)
        self.assertIn("WAIT_CANDIDATE_CHOICE", prompt)
        self.assertIn("candidate_count", prompt)

    def test_prompt_instructs_rewrite_before_plan(self) -> None:
        prompt = build_shadow_plan_prompt_v0("x", _context_payload())
        self.assertIn("改写", prompt)
        self.assertIn("rewritten", prompt)

    def test_prompt_limits_steps(self) -> None:
        prompt = build_shadow_plan_prompt_v0("x", _context_payload())
        self.assertIn("最多 4 步", prompt)


class ParseShadowPlanTest(unittest.TestCase):
    def test_parse_valid_result(self) -> None:
        result = parse_shadow_plan_v0(_valid_payload())
        self.assertIsInstance(result, ShadowPlannerResult)
        self.assertEqual(result.rewritten_text, "用户想在第2题和候选1之间选择，先看第2题的答案")
        self.assertEqual(result.keywords, ("第2题", "看答案"))
        self.assertIn("补上", result.reason)
        self.assertIsInstance(result.plan, ShadowPlan)
        self.assertEqual(result.plan.goal, "选第2题看答案")
        self.assertEqual(len(result.plan.steps), 2)
        self.assertEqual(result.plan.steps[0].action, "select_question")

    def test_parse_unplannable_empty_steps(self) -> None:
        result = parse_shadow_plan_v0(
            _valid_payload(steps=[], goal="用户想放弃，无合法只读动作")
        )
        self.assertEqual(result.plan.steps, ())
        self.assertEqual(result.plan.source, "unplannable")

    def test_parse_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            parse_shadow_plan_v0(["not", "a", "plan"])

    def test_parse_rejects_missing_rewritten(self) -> None:
        payload = _valid_payload()
        del payload["rewritten"]
        with self.assertRaises(ValueError):
            parse_shadow_plan_v0(payload)

    def test_parse_rejects_empty_rewritten_text(self) -> None:
        with self.assertRaises(ValueError):
            parse_shadow_plan_v0(
                _valid_payload(
                    rewritten={"text": "", "keywords": [], "reason": ""}
                )
            )

    def test_parse_rejects_action_outside_universe(self) -> None:
        with self.assertRaises(ValueError):
            parse_shadow_plan_v0(_valid_payload(steps=[{"action": "store"}]))

    def test_parse_rejects_too_many_steps(self) -> None:
        with self.assertRaises(ValueError):
            parse_shadow_plan_v0(
                _valid_payload(steps=[{"action": "show_candidates"}] * 5)
            )


class ShadowPlannerClientTest(unittest.TestCase):
    def test_plan_parses_model_output(self) -> None:
        planner = ShadowPlannerV0(
            model_client=lambda _prompt: _valid_payload(
                goal="看第1个候选",
                steps=[{"action": "select_candidate", "params": {"candidate_rank": 1}}],
            )
        )
        result = planner.plan("选第一个", _context_payload())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.plan.steps[0].action, "select_candidate")
        self.assertTrue(result.rewritten_text)

    def test_plan_degrades_to_none_on_model_error(self) -> None:
        planner = ShadowPlannerV0(model_client=lambda _prompt: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIsNone(planner.plan("选第一个", _context_payload()))

    def test_plan_degrades_to_none_on_invalid_output(self) -> None:
        planner = ShadowPlannerV0(model_client=lambda _prompt: {"goal": "g", "steps": []})
        self.assertIsNone(planner.plan("选第一个", _context_payload()))

    def test_plan_degrades_to_none_on_non_dict(self) -> None:
        planner = ShadowPlannerV0(model_client=lambda _prompt: "not json")
        self.assertIsNone(planner.plan("选第一个", _context_payload()))

    def test_prompt_never_contains_forbidden_fields_across_client(self) -> None:
        seen: list[str] = []

        def fake_client(prompt: str) -> dict:
            seen.append(prompt)
            return _valid_payload(steps=[{"action": "show_candidates"}])

        planner = ShadowPlannerV0(model_client=fake_client)
        planner.plan("x", _context_payload())
        for banned in ("session_id", ".jpg", "answer.png", "score", "stack"):
            self.assertNotIn(banned, seen[0])


class QwenCallShapeTest(unittest.TestCase):
    def test_call_qwen_planner_requires_env_key(self) -> None:
        import os

        old = os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                call_qwen_planner_v0("prompt")
        finally:
            if old is not None:
                os.environ["DASHSCOPE_API_KEY"] = old

    def test_prompt_constant_is_non_empty(self) -> None:
        self.assertTrue(SHADOW_PLAN_PROMPT.strip())


if __name__ == "__main__":
    unittest.main()

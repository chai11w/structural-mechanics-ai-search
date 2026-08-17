import unittest

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.tools import ToolResult


class A3A2AdapterTests(unittest.TestCase):
    def test_verified_crop_skips_image_scope_analysis_and_uses_supplied_context(self):
        seen = {}

        def classify_structure(_image, *, classified, **_kwargs):
            seen["classified"] = classified
            return ToolResult.success(code="STRUCTURE_READY", data={"structure_type": "刚架"})

        tools = AgentToolbox(
            analyze_image=lambda *_args, **_kwargs: self.fail("must not rerun image analysis"),
            analyze_multi_image=lambda *_args, **_kwargs: self.fail("must not rerun page triage"),
            route_bank=lambda *_args, **_kwargs: ToolResult.success(code="ROUTE_READY", data={"route": "symbolic"}),
            classify_structure=classify_structure,
            coarse_search=lambda *_args, **_kwargs: ToolResult.success(code="SEARCH_READY", data={
                "candidates": [{"path": "q1.jpg", "candidate_key": "q1"}],
                "has_more": False,
            }),
            rerank_candidates=lambda _image, candidates, **_kwargs: ToolResult.success(code="RERANK_READY", data={
                "visible_candidates": candidates,
                "reranked": False,
            }),
        )
        agent = TikuSearchAgent(tools=tools, use_llm_intent=False)

        response = agent.handle_preanalyzed_image(
            "verified-crop.jpg",
            loads=[{"type": "集中", "raw": "P"}],
            chapter="4力法",
            context_text="试作图示刚架的 M 图。",
            classified={"chapter_evidence": "「M 图」"},
            search_id="search_a3adapter",
        )

        self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(response.state["current_chapter"], "4力法")
        self.assertEqual(response.state["current_question_image_path"], "verified-crop.jpg")
        self.assertEqual(
            seen["classified"]["visible_problem_text"],
            "试作图示刚架的 M 图。",
        )

    def test_unknown_chapter_asks_user_instead_of_searching(self):
        agent = TikuSearchAgent(use_llm_intent=False)

        response = agent.handle_preanalyzed_image(
            "verified-crop.jpg",
            loads=[{"type": "集中", "raw": "P"}],
            chapter="unknown",
            context_text="",
            search_id="search_a3unknown",
        )

        self.assertEqual(response.state["phase"], "WAIT_CHAPTER")
        self.assertEqual(response.state["current_chapter"], "")
        self.assertIn("不能确定", response.text)


if __name__ == "__main__":
    unittest.main()

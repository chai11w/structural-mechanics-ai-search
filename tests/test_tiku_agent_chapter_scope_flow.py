import unittest

from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.intent_runtime_v2 import build_runtime_context_v2
from tiku_agent.state import AgentState, STATE_WAIT_CHAPTER
from tiku_agent.tools import ToolResult


LOADS = [{"type": "集中", "raw": "P"}]


class ChapterScopeFlowTests(unittest.TestCase):
    def _tools(self, analysis=None):
        calls = {"coarse_chapters": [], "global": 0}
        analysis = dict(analysis or {})
        analysis.setdefault("loads", list(LOADS))

        def analyze_image(image_path, **_kwargs):
            return ToolResult.success(
                code="IMAGE_ANALYZED",
                data={
                    "image_path": str(image_path),
                    "loads": list(LOADS),
                    "chapter": analysis.get("chapter_hint", ""),
                    "classified": analysis,
                },
            )

        def coarse_search(_loads, *, chapter, **_kwargs):
            calls["coarse_chapters"].append(chapter)
            return ToolResult.success(
                code="SEARCH_READY",
                data={
                    "candidates": [{"path": "q1.jpg", "candidate_key": "q1"}],
                    "has_more": False,
                },
            )

        def global_search(*_args, **_kwargs):
            calls["global"] += 1
            return ToolResult.success(
                code="GLOBAL_SEARCH_READY",
                data={
                    "candidates": [
                        {
                            "path": "global.jpg",
                            "candidate_key": "global",
                            "chapter": "4力法",
                        }
                    ]
                },
            )

        tools = AgentToolbox(
            analyze_image=analyze_image,
            analyze_multi_image=lambda *_args, **_kwargs: self.fail(
                "prechecked A2 must skip multi-image analysis"
            ),
            route_bank=lambda *_args, **_kwargs: ToolResult.success(
                code="ROUTE_READY", data={"route": "main"}
            ),
            classify_structure=lambda *_args, **_kwargs: ToolResult.success(
                code="STRUCTURE_READY", data={"structure_type": ""}
            ),
            coarse_search=coarse_search,
            global_search=global_search,
            rerank_candidates=lambda _image, candidates, **_kwargs: ToolResult.success(
                code="RERANK_READY",
                data={"visible_candidates": candidates, "reranked": False},
            ),
        )
        return tools, calls

    def _direct_a2(self, analysis):
        tools, calls = self._tools(analysis)
        agent = TikuSearchAgent(
            tools=tools,
            use_llm_intent=False,
            enable_chapter_scope_fallback=True,
        )
        response = agent.handle_image("direct.jpg", prechecked_single=True)
        return agent, response, calls

    def _a3_to_a2(self, *, chapter="", context_text=""):
        tools, calls = self._tools()
        agent = TikuSearchAgent(
            tools=tools,
            use_llm_intent=False,
            enable_chapter_scope_fallback=True,
        )
        response = agent.handle_preanalyzed_image(
            "crop.jpg",
            loads=list(LOADS),
            chapter=chapter,
            context_text=context_text,
        )
        return agent, response, calls

    def test_direct_a2_supported_scope_searches_fixed_storage_key(self):
        _agent, response, calls = self._direct_a2(
            {
                "chapter_hint": "4力法",
                "chapter_confidence": 0.96,
                "visible_problem_text": "请用力法求解图示结构。",
            }
        )

        self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(calls["coarse_chapters"], ["4力法"])

    def test_direct_a2_unsupported_scope_stops_before_search(self):
        _agent, response, calls = self._direct_a2(
            {
                "chapter_hint": "unknown",
                "chapter_confidence": 0.8,
                "visible_problem_text": "求结构的自振频率。",
            }
        )

        self.assertEqual(response.intent, "out_of_scope")
        self.assertEqual(response.state["chapter_scope_topic_id"], "structural_dynamics")
        self.assertIn("结构动力学", response.text)
        self.assertIn("矩阵位移法", response.text)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_direct_a2_uncertain_scope_asks_instead_of_searching(self):
        _agent, response, calls = self._direct_a2(
            {
                "chapter_hint": "4力法",
                "chapter_confidence": 0.99,
                "visible_problem_text": "EI=200, P=20",
            }
        )

        self.assertEqual(response.state["phase"], STATE_WAIT_CHAPTER)
        self.assertEqual(
            response.text,
            "我还不能确定这题属于哪一章。你知道的话直接告诉我章节名称或解题方法；"
            "也可以让我全局搜索，不过会慢一点。",
        )
        self.assertEqual(calls["coarse_chapters"], [])

    def test_direct_a2_non_chinese_question_is_rejected(self):
        _agent, response, calls = self._direct_a2(
            {
                "chapter_hint": "4力法",
                "chapter_confidence": 0.99,
                "visible_problem_text": "Determine the bending moment diagram.",
            }
        )

        self.assertEqual(response.state["chapter_scope_topic_id"], "non_chinese_question")
        self.assertIn("没有中文", response.text)
        self.assertNotIn("当前支持", response.text)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_explicit_unsupported_image_overrides_a_pending_supported_chapter(self):
        tools, calls = self._tools(
            {
                "chapter_hint": "unknown",
                "chapter_confidence": 0.8,
                "visible_problem_text": "求结构的自振频率。",
            }
        )
        state = AgentState(pending_chapter="4力法")
        agent = TikuSearchAgent(
            state=state,
            tools=tools,
            use_llm_intent=False,
            enable_chapter_scope_fallback=True,
        )

        response = agent.handle_image("direct.jpg", prechecked_single=True)

        self.assertEqual(response.intent, "out_of_scope")
        self.assertEqual(response.state["current_chapter"], "")
        self.assertEqual(calls["coarse_chapters"], [])

    def test_a3_to_a2_uses_the_same_four_scope_outcomes(self):
        cases = (
            ("4力法", "请用力法求解。", "WAIT_CANDIDATE_CHOICE", ["4力法"]),
            ("unknown", "这是结构动力学题。", STATE_WAIT_CHAPTER, []),
            ("unknown", "EI=200, P=20", STATE_WAIT_CHAPTER, []),
            ("4力法", "Determine the redundant force.", STATE_WAIT_CHAPTER, []),
        )

        for chapter, text, expected_phase, expected_searches in cases:
            with self.subTest(text=text):
                _agent, response, calls = self._a3_to_a2(
                    chapter=chapter,
                    context_text=text,
                )
                self.assertEqual(response.state["phase"], expected_phase)
                self.assertEqual(calls["coarse_chapters"], expected_searches)

    def test_wait_chapter_unsupported_text_stops_and_lists_all_seven(self):
        agent, _response, calls = self._a3_to_a2(context_text="EI=200, P=20")

        response = agent.handle_text("这是动力学")

        self.assertEqual(response.intent, "out_of_scope")
        self.assertIn("结构动力学", response.text)
        for name in (
            "静定结构受力",
            "静定结构位移",
            "力法",
            "位移法",
            "力矩分配法",
            "矩阵位移法",
            "影响线",
        ):
            self.assertIn(name, response.text)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_numeric_chapter_stays_uncertain_and_second_reply_lists_scope(self):
        agent, _response, _calls = self._a3_to_a2(context_text="EI=200, P=20")

        first = agent.handle_text("第4章")
        second = agent.handle_text("4")

        self.assertEqual(first.state["phase"], STATE_WAIT_CHAPTER)
        self.assertNotIn("当前支持", first.text)
        self.assertIn("当前支持", second.text)

    def test_greeting_preserves_wait_state_but_mixed_method_searches(self):
        agent, _response, calls = self._a3_to_a2(context_text="EI=200, P=20")

        greeting = agent.handle_text("你好")
        searched = agent.handle_text("你好，这是力法")

        self.assertEqual(greeting.state["phase"], STATE_WAIT_CHAPTER)
        self.assertTrue(greeting.state["global_search_offered"])
        self.assertIn("还在等待章节判断", greeting.text)
        self.assertEqual(searched.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(calls["coarse_chapters"], ["4力法"])

    def test_unknown_reply_runs_authorized_global_search(self):
        agent, _response, calls = self._a3_to_a2(context_text="EI=200, P=20")

        response = agent.handle_text("我不知道，你搜吧")

        self.assertEqual(calls["global"], 1)
        self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")

    def test_natural_global_search_reply_remains_supported(self):
        for text in ("全题库搜一下", "可以，搜吧", "慢一点没关系，继续搜"):
            with self.subTest(text=text):
                agent, _response, calls = self._a3_to_a2(
                    context_text="EI=200, P=20"
                )

                response = agent.handle_text(text)

                self.assertEqual(calls["global"], 1)
                self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")

    def test_supported_scope_answer_keeps_external_load_limits(self):
        agent, _response, _calls = self._a3_to_a2(context_text="EI=200, P=20")
        agent.enable_safe_answer_v0 = True

        response = agent.handle_text("支持哪些章节")

        self.assertIn("矩阵位移法和影响线仅支持含具体外荷载的题目", response.text)

    def test_model_chapter_alias_is_mapped_by_catalog_before_search(self):
        tools, calls = self._tools()
        state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=list(LOADS),
            global_search_offered=True,
            chapter_scope_status="uncertain",
        )
        agent = TikuSearchAgent(
            state=state,
            tools=tools,
            llm_client=lambda _prompt: {
                "action": "set_chapter",
                "chapter_override": "渐近法",
                "chapter_target": "current_question",
                "confidence": 0.9,
                "reason": "用户描述的是渐近分配方法",
            },
            enable_chapter_scope_fallback=True,
        )

        response = agent.handle_text("好像是逐步分配杆端弯矩的那种方法")

        self.assertEqual(response.state["current_chapter"], "6力矩分配")
        self.assertEqual(calls["coarse_chapters"], ["6力矩分配"])

    def test_invalid_model_chapter_cannot_choose_an_excel(self):
        tools, calls = self._tools()
        state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=list(LOADS),
            global_search_offered=True,
            chapter_scope_status="uncertain",
        )
        agent = TikuSearchAgent(
            state=state,
            tools=tools,
            llm_client=lambda _prompt: {
                "action": "set_chapter",
                "chapter_override": "第11章",
                "chapter_target": "current_question",
                "confidence": 0.9,
                "reason": "猜测章号",
            },
            enable_chapter_scope_fallback=True,
        )

        response = agent.handle_text("好像是后面那一章")

        self.assertEqual(response.state["phase"], STATE_WAIT_CHAPTER)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_non_chinese_rejection_cannot_be_bypassed_by_text_chapter(self):
        agent, first, calls = self._a3_to_a2(
            chapter="4力法",
            context_text="Determine the redundant force.",
        )

        second = agent.handle_text("力法")

        self.assertEqual(first.state["chapter_scope_topic_id"], "non_chinese_question")
        self.assertEqual(second.state["chapter_scope_topic_id"], "non_chinese_question")
        self.assertIn("没有中文", second.text)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_context_llm_unsupported_topic_is_stopped_by_catalog(self):
        tools, calls = self._tools()
        state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=list(LOADS),
            global_search_offered=True,
            chapter_scope_status="uncertain",
        )
        agent = TikuSearchAgent(
            state=state,
            tools=tools,
            llm_client=lambda _prompt: {
                "action": "set_chapter",
                "chapter_override": "动力学",
                "chapter_target": "current_question",
                "confidence": 0.9,
                "reason": "用户在描述振动问题",
            },
            enable_chapter_scope_fallback=True,
        )

        response = agent.handle_text("好像是研究振动的那部分")

        self.assertEqual(response.intent, "out_of_scope")
        self.assertIn("结构动力学", response.text)
        self.assertEqual(calls["coarse_chapters"], [])

    def test_dispatch_revalidates_context_model_decision(self):
        tools, calls = self._tools()
        state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=list(LOADS),
            chapter_scope_status="uncertain",
        )
        agent = TikuSearchAgent(
            state=state,
            tools=tools,
            enable_chapter_scope_fallback=True,
        )
        decision = ActionDecisionV2(
            action="set_chapter",
            chapter_override="不存在的目录",
            chapter_target="current_question",
            source="context_llm",
            confidence=0.9,
        )

        response = agent._dispatch_v2(decision, build_runtime_context_v2(state))

        self.assertEqual(response.state["phase"], STATE_WAIT_CHAPTER)
        self.assertEqual(calls["coarse_chapters"], [])


if __name__ == "__main__":
    unittest.main()

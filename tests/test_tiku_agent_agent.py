import unittest

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.state import PHASE_ANSWERED, PHASE_ERROR, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER
from tiku_agent.tools import AgentToolConfig, ToolResult


class FakeTools:
    def __init__(self, *, chapter="4力法"):
        self.chapter = chapter
        self.search_chapters = []
        self.answers = {
            1: ["out/answer1.jpg"],
            2: ["out/answer2.jpg"],
        }
        self.analyze_image_calls = 0

    def toolbox(self):
        return AgentToolbox(
            analyze_image=self.analyze_image,
            analyze_multi_image=self.analyze_multi_image,
            prepare_question_units=self.prepare_question_units,
            route_bank=self.route_bank,
            classify_structure=self.classify_structure,
            coarse_search=self.coarse_search,
            rerank_candidates=self.rerank_candidates,
            answer_candidate=self.answer_candidate,
        )

    def analyze_image(self, image_path, *, chapter="auto", config=None, include_layout=False):
        self.analyze_image_calls += 1
        return ToolResult(
            ok=True,
            data={
                "image_path": str(image_path),
                "chapter": self.chapter,
                "loads": [{"type": "集中", "raw": "P"}],
            },
        )

    def analyze_multi_image(self, image_path, *, config=None):
        return ToolResult(
            ok=True,
            data={
                "is_multi": False,
                "questions": [],
                "single_analysis": {"loads": [{"type": "集中", "raw": "P"}], "chapter_hint": self.chapter},
            },
        )

    def prepare_question_units(self, image_path, questions, *, config=None):
        return ToolResult(ok=True, data={"questions": questions, "diagram_crops": {}})

    def route_bank(self, loads):
        return ToolResult(ok=True, data={"route": "main", "category": "main_numeric", "reason": "fake"})

    def classify_structure(self, image_path, *, route, classified=None, config=None):
        return ToolResult(ok=True, data={"structure_type": "", "source": "not_applicable"})

    def coarse_search(self, loads, *, chapter, route, structure_type="", top_k=None):
        self.search_chapters.append(chapter)
        return ToolResult(
            ok=True,
            data={
                "candidates": [
                    {"rank": 1, "path": f"{chapter}/q1.jpg", "name": "q1.jpg", "score": 0.8},
                    {"rank": 2, "path": f"{chapter}/q2.jpg", "name": "q2.jpg", "score": 0.7},
                ]
            },
        )

    def rerank_candidates(self, query_image_path, candidates, *, route, rerank_top=3, force_rerank=False):
        visible = []
        for item in candidates:
            copied = dict(item)
            copied["final_score"] = copied["score"]
            visible.append(copied)
        return ToolResult(ok=True, data={"reranked": True, "visible_candidates": visible, "rerank_note": ""})

    def answer_candidate(self, candidates, *, rank, copy_to_output=True, config=None):
        return ToolResult(
            ok=True,
            data={
                "rank": rank,
                "candidate": candidates[rank - 1],
                "answer_paths": self.answers[rank],
                "copied_paths": self.answers[rank],
            },
        )


class TikuSearchAgentTest(unittest.TestCase):
    def make_agent(self, fake_tools):
        return TikuSearchAgent(
            tools=fake_tools.toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
        )

    def make_v2_agent(self, fake_tools, *, llm_client=None):
        return TikuSearchAgent(
            tools=fake_tools.toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=llm_client is not None,
            llm_client=llm_client,
            intent_version="v2",
        )

    def test_v2_conversation_shell_preserves_search_state_and_calls_no_tools(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")
        phase_before = agent.state.phase
        candidates_before = list(agent.state.candidates)
        searches_before = list(fake.search_chapters)

        response = agent.handle_text("辛苦了")

        self.assertEqual(response.intent, "small_talk")
        self.assertIn("继续看题", response.text)
        self.assertEqual(agent.state.phase, phase_before)
        self.assertEqual(agent.state.candidates, candidates_before)
        self.assertEqual(fake.search_chapters, searches_before)

    def test_v2_model_action_without_positive_evidence_safely_clarifies(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(
            fake,
            llm_client=lambda _prompt: {"action": "resend_answer"},
        )
        agent.handle_image("q.jpg")
        searches_before = list(fake.search_chapters)

        response = agent.handle_text("忙吗，能接着看题不")

        self.assertEqual(response.intent, "clarification")
        self.assertIn("继续搜题", response.text)
        self.assertEqual(fake.search_chapters, searches_before)

    def test_v2_model_failure_safely_clarifies_without_running_tools(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(
            fake,
            llm_client=lambda _prompt: (_ for _ in ()).throw(RuntimeError("model unavailable")),
        )
        agent.handle_image("q.jpg")
        searches_before = list(fake.search_chapters)

        response = agent.handle_text("那个")

        self.assertEqual(response.intent, "clarification")
        self.assertNotIn("model unavailable", response.text)
        self.assertEqual(fake.search_chapters, searches_before)

    def test_v2_pending_chapter_is_consumed_by_next_single_image(self):
        fake = FakeTools(chapter="3静定结构位移")
        agent = self.make_v2_agent(fake)

        pending = agent.handle_text("待会传的题按力法")
        self.assertEqual(pending.intent, "set_chapter")
        self.assertEqual(agent.state.pending_chapter, "4力法")
        self.assertEqual(agent.state.phase, "IDLE")

        agent.handle_image("next.jpg")

        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(agent.state.pending_chapter, "")
        self.assertEqual(fake.search_chapters, ["4力法"])

    def test_v2_pending_chapter_waits_for_question_choice_on_multi_image(self):
        fake = FakeTools(chapter="")
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "", "question_image_path": "q1.jpg"},
                    {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "", "question_image_path": "q2.jpg"},
                ],
            },
        )
        fake.prepare_question_units = lambda image_path, questions, *, config=None: ToolResult(
            ok=True,
            data={"questions": questions, "diagram_crops": {"1": "q1.jpg", "2": "q2.jpg"}},
        )
        agent = self.make_v2_agent(fake)
        agent.handle_text("下一张按影响线")

        listed = agent.handle_image("multi.jpg")

        self.assertEqual(agent.state.phase, "WAIT_QUESTION_CHOICE")
        self.assertEqual(agent.state.pending_chapter, "8影响线")
        self.assertEqual(fake.search_chapters, [])
        self.assertIn("2 道题", listed.text)

        agent.handle_text("第二题")

        self.assertEqual(agent.state.pending_chapter, "")
        self.assertEqual(agent.state.current_chapter, "8影响线")
        self.assertEqual(fake.search_chapters, ["8影响线"])

    def test_v2_safe_clarification_recovers_on_the_next_explicit_turn(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(
            fake,
            llm_client=lambda _prompt: {
                "action": "clarification",
                "clarification_reason": "ambiguous_reference",
            },
        )
        agent.handle_image("q.jpg")
        candidates_before = list(agent.state.candidates)
        searches_before = list(fake.search_chapters)

        clarified = agent.handle_text("那个")

        self.assertEqual(clarified.intent, "clarification")
        self.assertEqual(agent.state.candidates, candidates_before)
        self.assertEqual(fake.search_chapters, searches_before)

        answered = agent.handle_text("第二个候选")

        self.assertEqual(answered.intent, "select_candidate")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer2.jpg"])
        self.assertEqual(fake.search_chapters, searches_before)

    def test_v2_previous_question_reference_uses_recorded_state_not_model_guess(self):
        fake = FakeTools(chapter="")
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": "q1.jpg"},
                    {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法", "question_image_path": "q2.jpg"},
                ],
            },
        )
        fake.prepare_question_units = lambda image_path, questions, *, config=None: ToolResult(
            ok=True,
            data={"questions": questions, "diagram_crops": {"1": "q1.jpg", "2": "q2.jpg"}},
        )
        agent = self.make_v2_agent(
            fake,
            llm_client=lambda _prompt: {"action": "select_question", "question_index": 2},
        )
        agent.handle_image("multi.jpg")
        agent.handle_text("第一题")
        agent.handle_text("第二题")

        returned = agent.handle_text("上一道")

        self.assertEqual(returned.intent, "select_question")
        self.assertEqual(agent.state.selected_question, 1)
        self.assertEqual(agent.state.previous_question, 2)

    def test_image_search_reaches_candidate_choice(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(agent.state.candidate_count, 2)
        self.assertIn("比较像", response.text)
        self.assertEqual(fake.search_chapters, ["4力法"])
        self.assertEqual(fake.analyze_image_calls, 0)

    def test_missing_chapter_then_user_supplies_chapter(self):
        fake = FakeTools(chapter="")
        agent = self.make_agent(fake)

        first = agent.handle_image("q.jpg")
        self.assertEqual(agent.state.phase, STATE_WAIT_CHAPTER)
        self.assertIn("不能确定", first.text)

        second = agent.handle_text("这题应该是第三章")
        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "3静定结构位移")
        self.assertEqual(fake.search_chapters, ["3静定结构位移"])
        self.assertIn("比较像", second.text)

    def test_unknown_scope_chapter_waits_for_user_instead_of_searching_unknown(self):
        fake = FakeTools(chapter="unknown")
        agent = self.make_agent(fake)

        response = agent.handle_image("diagram-only.jpg")

        self.assertEqual(agent.state.phase, STATE_WAIT_CHAPTER)
        self.assertEqual(agent.state.current_chapter, "")
        self.assertEqual(fake.search_chapters, [])
        self.assertIn("不能确定", response.text)

    def test_retry_text_reuses_saved_image_after_transient_failure(self):
        class RetryTools(FakeTools):
            def __init__(self):
                super().__init__(chapter="4力法")
                self.fail_once = True

            def analyze_multi_image(self, image_path, *, config=None):
                return ToolResult(ok=True, data={"is_multi": False, "questions": []})

            def analyze_image(self, image_path, *, chapter="auto", config=None, include_layout=False):
                if self.fail_once:
                    self.fail_once = False
                    return ToolResult(ok=False, error="HTTP Error 500: Internal Server Error")
                return super().analyze_image(image_path, chapter=chapter, config=config, include_layout=include_layout)

        fake = RetryTools()
        agent = self.make_agent(fake)

        failed = agent.handle_image("saved-question.jpg")
        self.assertEqual(agent.state.phase, PHASE_ERROR)
        self.assertIn("直接回复“重试”", failed.text)

        retried = agent.handle_text("重试")
        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_image_path, "saved-question.jpg")
        self.assertIn("比较像", retried.text)

    def test_select_answer_and_resend_answer(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        answer = agent.handle_text("1")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertIn("答案发你了", answer.text)

        resend = agent.handle_text("刚才答案再发我")
        self.assertIn("再发你一次", resend.text)
        self.assertEqual(resend.images, ["out/answer1.jpg"])

    def test_correct_chapter_after_answer_reruns_search(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")
        agent.handle_text("1")

        corrected = agent.handle_text("不对，应该是第三章")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "3静定结构位移")
        self.assertEqual(agent.state.revision_count, 1)
        self.assertEqual(agent.state.last_answer_paths, [])
        self.assertEqual(fake.search_chapters, ["4力法", "3静定结构位移"])
        self.assertIn("比较像", corrected.text)

    def test_choose_another_candidate_after_answer(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")
        agent.handle_text("1")

        answer = agent.handle_text("第二个")

        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer2.jpg"])
        self.assertIn("答案发你了", answer.text)

    def test_correct_chapter_with_method_name_after_candidates(self):
        fake = FakeTools(chapter="3静定结构位移")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        corrected = agent.handle_text("不对，这个按力法搜")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(agent.state.revision_count, 1)
        self.assertEqual(fake.search_chapters, ["3静定结构位移", "4力法"])
        self.assertIn("比较像", corrected.text)

    def test_cancel(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        response = agent.handle_text("取消")

        self.assertEqual(agent.state.phase, "CANCELLED")
        self.assertIn("取消", response.text)

    def test_unsupported_text_returns_message(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)

        response = agent.handle_text("帮我入库这道题")

        self.assertIn("没太明白", response.text)

    def test_greeting_introduces_agent_without_resetting_search_state(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")
        phase_before = agent.state.phase
        candidates_before = list(agent.state.candidates)

        response = agent.handle_text("你好啊")

        self.assertEqual(response.intent, "greeting")
        self.assertIn("我是力答", response.text)
        self.assertIn("结构力学题库", response.text)
        self.assertIn("发一张结构力学题图", response.text)
        self.assertEqual(agent.state.phase, phase_before)
        self.assertEqual(agent.state.candidates, candidates_before)

    def test_explains_sanitized_failure_reason_on_request(self):
        agent = self.make_agent(FakeTools(chapter="4力法"))
        agent.state.fail("Request timed out while reading C:\\private\\question.jpg")

        response = agent.handle_text("为什么失败")

        self.assertEqual(response.intent, "explain_failure")
        self.assertIn("响应超时", response.text)
        self.assertNotIn("private", response.text)

    def test_multi_question_selection_runs_selected_crop_with_chapter_override(self):
        fake = FakeTools(chapter="")
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "4", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": "crop4.jpg"},
                    {"label": "5", "loads": [{"type": "均布", "raw": "q"}], "chapter": "", "question_image_path": "crop5.jpg"},
                ],
            },
        )
        fake.prepare_question_units = lambda image_path, questions, *, config=None: ToolResult(
            ok=True,
            data={
                "questions": [
                    {**questions[0], "question_image_path": "crop4.jpg"},
                    {**questions[1], "question_image_path": "crop5.jpg"},
                ],
                "diagram_crops": {"4": "crop4.jpg", "5": "crop5.jpg"},
            },
        )
        agent = self.make_agent(fake)

        listed = agent.handle_image("multi.jpg")
        selected = agent.handle_text("第二题-2静定结构")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.selected_question, 2)
        self.assertEqual(agent.state.active_image_path, "crop5.jpg")
        self.assertEqual(agent.state.current_chapter, "2静定结构")
        self.assertEqual(fake.search_chapters, ["2静定结构"])
        self.assertIn("看到了 2 道题", listed.text)
        self.assertIn("比较像", selected.text)

    def test_multi_question_without_crop_skips_visual_rerank(self):
        fake = FakeTools(chapter="")
        rerank_inputs = []
        original_rerank = fake.rerank_candidates

        def record_rerank(query_image_path, candidates, **kwargs):
            rerank_inputs.append(query_image_path)
            return original_rerank(query_image_path, candidates, **kwargs)

        fake.rerank_candidates = record_rerank
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": ""},
                    {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法", "question_image_path": ""},
                ],
            },
        )
        fake.prepare_question_units = lambda image_path, questions, *, config=None: ToolResult(
            ok=True,
            data={"questions": questions, "diagram_crops": {}},
        )
        agent = self.make_agent(fake)

        agent.handle_image("multi.jpg")
        agent.handle_text("第一题")

        self.assertEqual(rerank_inputs, [None])

    def test_answered_multi_question_can_switch_to_next_question_naturally(self):
        fake = FakeTools(chapter="")
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": "crop1.jpg"},
                    {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法", "question_image_path": "crop2.jpg"},
                ],
            },
        )
        fake.prepare_question_units = lambda image_path, questions, *, config=None: ToolResult(
            ok=True,
            data={"questions": questions, "diagram_crops": {"1": "crop1.jpg", "2": "crop2.jpg"}},
        )
        agent = self.make_agent(fake)

        agent.handle_image("multi.jpg")
        agent.handle_text("第一题")
        agent.handle_text("1")
        response = agent.handle_text("那再帮我查一下第二个")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.selected_question, 2)
        self.assertEqual(agent.state.active_image_path, "crop2.jpg")
        self.assertEqual(agent.state.last_answer_paths, [])
        self.assertEqual(fake.search_chapters, ["4力法", "4力法"])
        self.assertIn("比较像", response.text)


if __name__ == "__main__":
    unittest.main()

import unittest

from tiku_agent import render
from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.state import AgentState, PHASE_ANSWERED, PHASE_ERROR, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER
from tiku_agent.tools import AgentToolConfig, ToolResult
from tiku_shared.request_protocol import RequestAction


class FakeTools:
    def __init__(self, *, chapter="4力法"):
        self.chapter = chapter
        self.search_chapters = []
        self.answers = {
            1: ["out/answer1.jpg"],
            2: ["out/answer2.jpg"],
        }
        self.global_search_calls = 0
        self.route_calls = 0
        self.structure_calls = 0
        self.global_candidates = [
            {
                "rank": 1,
                "path": "3静定结构位移/global1.jpg",
                "name": "global1.jpg",
                "score": 1.0,
                "rerank_score": 1.0,
                "chapter": "3静定结构位移",
                "source_chapters": ["3静定结构位移"],
            },
            {
                "rank": 2,
                "path": "4力法/global2.jpg",
                "name": "global2.jpg",
                "score": 1.0,
                "rerank_score": 0.98,
                "chapter": "4力法",
                "source_chapters": ["4力法"],
            },
        ]
        self.analyze_image_calls = 0
        self.search_exclusions = []

    def toolbox(self):
        return AgentToolbox(
            analyze_image=self.analyze_image,
            analyze_multi_image=self.analyze_multi_image,
            prepare_question_units=self.prepare_question_units,
            route_bank=self.route_bank,
            classify_structure=self.classify_structure,
            coarse_search=self.coarse_search,
            global_search=self.global_search,
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
        self.route_calls += 1
        return ToolResult(ok=True, data={"route": "main", "category": "main_numeric", "reason": "fake"})

    def classify_structure(self, image_path, *, route, classified=None, config=None):
        self.structure_calls += 1
        return ToolResult(ok=True, data={"structure_type": "", "source": "not_applicable"})

    def coarse_search(
        self,
        loads,
        *,
        chapter,
        route,
        structure_type="",
        top_k=None,
        exclude_candidate_keys=None,
    ):
        self.search_chapters.append(chapter)
        excluded = set(exclude_candidate_keys or [])
        self.search_exclusions.append(list(excluded))
        all_candidates = [
            {
                "path": f"{chapter}/q{index}.jpg",
                "name": f"q{index}.jpg",
                "score": 0.9 - index / 100,
                "candidate_key": f"{chapter}|main|q{index}.jpg",
            }
            for index in range(1, 5)
        ]
        available = [item for item in all_candidates if item["candidate_key"] not in excluded]
        selected = available[:2]
        return ToolResult(
            ok=True,
            data={
                "candidates": [dict(item, rank=rank) for rank, item in enumerate(selected, 1)],
                "has_more": len(available) > len(selected),
            },
        )

    def rerank_candidates(self, query_image_path, candidates, *, route, rerank_top=3, force_rerank=False):
        visible = []
        for item in candidates:
            copied = dict(item)
            copied["final_score"] = copied["score"]
            visible.append(copied)
        return ToolResult(ok=True, data={"reranked": True, "visible_candidates": visible, "rerank_note": ""})

    def global_search(self, loads, query_image_path, *, route, structure_type="", config=None):
        self.global_search_calls += 1
        return ToolResult(
            ok=True,
            data={"candidates": list(self.global_candidates)},
            next_state="WAIT_CANDIDATE_CHOICE" if self.global_candidates else "NO_MATCH",
        )

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

    def make_v2_agent(self, fake_tools, *, llm_client=None, enable_author_contact_fallback=False):
        return TikuSearchAgent(
            tools=fake_tools.toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=llm_client is not None,
            llm_client=llm_client,
            enable_author_contact_fallback=enable_author_contact_fallback,
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
        self.assertIn("选择候选编号", response.text)
        self.assertNotIn("继续搜", response.text)
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

    def test_v2_offers_and_executes_global_search_once(self):
        fake = FakeTools(chapter="")
        agent = self.make_v2_agent(fake)

        offered = agent.handle_image("q.jpg")

        self.assertTrue(agent.state.global_search_offered)
        self.assertIn("全局搜索", offered.text)
        searched = agent.handle_text("可以")

        self.assertEqual(searched.intent, "global_search")
        self.assertEqual(fake.global_search_calls, 1)
        self.assertFalse(agent.state.global_search_offered)
        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.candidate_count, 2)
        self.assertIn("3静定结构位移", searched.text)
        self.assertIn("4力法", searched.text)
        self.assertEqual(searched.images, ["3静定结构位移/global1.jpg", "4力法/global2.jpg"])

        answered = agent.handle_text("第二个候选")
        self.assertEqual(answered.intent, "select_candidate")
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer2.jpg"])

    def test_v2_global_search_explicit_negation_never_calls_tool(self):
        fake = FakeTools(chapter="")
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")

        declined = agent.handle_text("先不要全局搜")

        self.assertEqual(declined.intent, "clarification")
        self.assertEqual(fake.global_search_calls, 0)
        self.assertEqual(agent.state.phase, STATE_WAIT_CHAPTER)
        self.assertTrue(agent.state.global_search_offered)

    def test_search_progress_uses_actual_agent_stage_for_all_three_entry_paths(self):
        automatic_events = []
        automatic = TikuSearchAgent(
            tools=FakeTools(chapter="4力法").toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
            progress_reporter=lambda stage, message: automatic_events.append((stage, message)),
        )
        automatic.handle_image("automatic.jpg")
        self.assertEqual(automatic_events, [("searching", "正在按「4力法」搜索题目…")])

        chapter_events = []
        chapter_agent = TikuSearchAgent(
            tools=FakeTools(chapter="").toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
            progress_reporter=lambda stage, message: chapter_events.append((stage, message)),
        )
        chapter_agent.handle_image("chapter.jpg")
        self.assertEqual(chapter_events, [])
        chapter_agent.handle_text("按力法搜")
        self.assertEqual(chapter_events, [("searching", "正在按「4力法」搜索题目…")])

        global_events = []
        global_agent = TikuSearchAgent(
            tools=FakeTools(chapter="").toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
            progress_reporter=lambda stage, message: global_events.append((stage, message)),
        )
        global_agent.handle_image("global.jpg")
        global_agent.handle_text("全局搜索")
        self.assertEqual(
            global_events,
            [("global_searching", "正在全局搜索题目，可能需要一点时间…")],
        )

    def test_v2_global_search_no_match_keeps_normal_chapter_fallback(self):
        fake = FakeTools(chapter="")
        fake.global_candidates = []
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")

        missing = agent.handle_text("全局搜吧")

        self.assertEqual(missing.intent, "global_search")
        self.assertEqual(agent.state.phase, "NO_MATCH")
        self.assertIn("没有足够可靠", missing.text)

        chapter = agent.handle_text("按力法搜")
        self.assertEqual(chapter.intent, "set_chapter")
        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(fake.search_chapters, ["4力法"])

    def test_v2_global_search_single_result_names_source_chapter(self):
        fake = FakeTools(chapter="")
        fake.global_candidates = [fake.global_candidates[0]]
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")

        response = agent.handle_text("搜吧")

        self.assertEqual(response.intent, "global_search")
        self.assertEqual(agent.state.candidate_count, 1)
        self.assertIn("一道高相似题", response.text)
        self.assertIn("3静定结构位移", response.text)

    def test_v2_does_not_run_global_search_without_offer(self):
        fake = FakeTools(chapter="")
        agent = self.make_v2_agent(fake)
        agent.state = AgentState(
            phase=STATE_WAIT_CHAPTER,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
        )

        response = agent.handle_text("全局搜吧")

        self.assertEqual(response.intent, "reject")
        self.assertEqual(fake.global_search_calls, 0)

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

    def test_v2_answered_question_can_reselect_another_current_candidate(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")
        generation = agent.state.candidate_generation

        first_answer = agent.handle_text("第二个候选")
        second_answer = agent.handle_text("第一个候选")

        self.assertEqual(first_answer.intent, "select_candidate")
        self.assertEqual(second_answer.intent, "select_candidate")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.selected_rank, 1)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertEqual(agent.state.candidate_generation, generation)
        self.assertEqual(fake.search_chapters, ["4力法"])

    def test_v2_safe_answer_route_treats_yes_as_the_unique_candidate_confirmation(self):
        fake = FakeTools(chapter="4力法")
        candidate = {
            "rank": 1,
            "path": "4力法/q1.jpg",
            "name": "q1.jpg",
            "score": 0.99,
            "candidate_key": "4力法|main|q1.jpg",
        }
        state = AgentState(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            candidates=[candidate],
        )
        model_calls = []
        agent = TikuSearchAgent(
            state=state,
            tools=fake.toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=SafeAnswerGeneratorV0(
                lambda request: model_calls.append(request) or "不应调用模型。"
            ),
        )

        response = agent.handle_text("是")

        self.assertEqual(response.intent, "select_candidate")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.selected_rank, 1)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertEqual(model_calls, [])

    def test_v2_safe_answer_route_treats_no_as_unique_candidate_rejection(self):
        fake = FakeTools(chapter="4力法")
        candidate = {
            "rank": 1,
            "path": "4力法/q1.jpg",
            "name": "q1.jpg",
            "score": 0.99,
            "candidate_key": "4力法|main|q1.jpg",
        }
        state = AgentState(
            phase=STATE_WAIT_CANDIDATE_CHOICE,
            current_image_path="q.jpg",
            current_question_image_path="q.jpg",
            current_loads=[{"type": "集中", "raw": "P"}],
            current_chapter="4力法",
            candidates=[candidate],
            continuation_available=True,
        )
        model_calls = []
        agent = TikuSearchAgent(
            state=state,
            tools=fake.toolbox(),
            config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=False,
            enable_safe_answer_v0=True,
            safe_answer_generator_v0=SafeAnswerGeneratorV0(
                lambda request: model_calls.append(request) or "不应调用模型。"
            ),
            enable_author_contact_fallback=True,
        )

        response = agent.handle_text("不是")

        self.assertEqual(response.intent, "reject_candidates")
        self.assertEqual(
            response.text,
            "收到，目前没有更多相似候选题，你可以联系作者手搓。",
        )
        self.assertTrue(agent.state.current_candidates_rejected)
        self.assertEqual(response.media_kind, "")
        self.assertEqual(model_calls, [])

    def test_v2_explicit_candidate_selection_does_not_reselect_multi_question(self):
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
        agent = self.make_v2_agent(fake)
        agent.handle_image("multi.jpg")
        agent.handle_text("第二题")
        searches_before = list(fake.search_chapters)

        answered = agent.handle_text("选择候选 2")

        self.assertEqual(answered.intent, "select_candidate")
        self.assertEqual(agent.state.selected_question, 2)
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer2.jpg"])
        self.assertEqual(fake.search_chapters, searches_before)

        agent.handle_text("选择候选 1")
        bare_digit = agent.handle_text("2")

        self.assertEqual(bare_digit.intent, "select_candidate")
        self.assertEqual(agent.state.selected_question, 2)
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.current_chapter, "4力法")
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
        self.assertIn("全局搜索", first.text)

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

    def test_explicit_tool_error_enters_error_phase(self):
        fake = FakeTools(chapter="4力法")
        fake.analyze_multi_image = lambda image_path, *, config=None: ToolResult.tool_error(
            error="图片分析暂时不可用。",
            code="MULTI_DETECTION_FAILED",
            retryable=True,
            error_category="upstream",
        )
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(agent.state.phase, PHASE_ERROR)
        self.assertIn("图片分析暂时失败", agent.state.last_error)
        self.assertIn("重试", response.text)

    def test_unmapped_tool_feedback_never_echoes_internal_error(self):
        agent = self.make_agent(FakeTools(chapter="4力法"))
        raw = "Traceback: C:\\private\\token=secret mixed symbolic and numeric load"

        needs_input = ToolResult.needs_input(
            code="UNMAPPED_NEEDS_INPUT",
            error=raw,
            next_state="WAIT_INPUT",
        )
        response = agent._stop_for_tool_result(needs_input)
        self.assertIsNotNone(response)
        self.assertNotIn(raw, response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("secret", response.text)

        partial = ToolResult.partial(
            code="UNMAPPED_PARTIAL",
            error=raw,
            next_state="ERROR",
        )
        response = agent._stop_for_tool_result(partial)
        self.assertIsNotNone(response)
        self.assertNotIn(raw, response.text)

        self.assertNotIn(
            raw,
            render.render_tool_feedback(
                ToolResult.no_match(code="UNMAPPED_NO_MATCH", error=raw),
                context="no_match",
            ),
        )

    def test_route_needs_input_clarifies_without_entering_error_phase(self):
        fake = FakeTools(chapter="4力法")
        fake.route_bank = lambda loads: ToolResult.needs_input(
            error="请确认荷载是字母还是数值。",
            code="LOAD_ROUTE_NEEDS_REVIEW",
            next_state="WAIT_LOAD_CONFIRMATION",
            action=RequestAction.RETRY_UPLOAD,
        )
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(response.intent, "clarification")
        self.assertNotEqual(agent.state.phase, PHASE_ERROR)
        self.assertIn("荷载信息暂时无法可靠选择题库", response.text)
        self.assertEqual(response.protocol["action"], "retry_upload")

    def test_tool_protocol_uses_registered_recovery_metadata(self):
        fake = FakeTools(chapter="4力法")
        fake.route_bank = lambda loads: ToolResult.tool_error(
            error="internal route failure",
            code="BANK_ROUTE_FAILED",
            retryable=True,
            action=RequestAction.RETRY_SEARCH,
            error_category="internal_logic",
        )
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(response.protocol["code"], "BANK_ROUTE_FAILED")
        self.assertFalse(response.protocol["retryable"])
        self.assertEqual(response.protocol["action"], "")

    def test_partial_rerank_is_consumed_and_candidates_remain_available(self):
        fake = FakeTools(chapter="4力法")

        def partial_rerank(query_image_path, candidates, **kwargs):
            return ToolResult.partial(
                data={
                    "reranked": False,
                    "visible_candidates": candidates,
                    "rerank_note": "复筛未完成，已回退粗筛排序。",
                },
                code="RERANK_INCOMPLETE_COARSE_FALLBACK",
                next_state="WAIT_CANDIDATE_CHOICE",
                retryable=True,
                error_category="model_incomplete",
            )

        fake.rerank_candidates = partial_rerank
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.candidate_count, 2)
        self.assertEqual(response.images, ["4力法/q1.jpg", "4力法/q2.jpg"])
        self.assertIn("复筛未完成，已回退粗筛排序。", response.text)

    def test_partial_detection_and_structure_fallbacks_are_visible(self):
        fake = FakeTools(chapter="4力法")
        fake.analyze_multi_image = lambda *args, **kwargs: ToolResult.partial(
            data={"is_multi": False, "questions": []},
            error="多题判断未完成，已按单题流程继续。",
            code="MULTI_DETECTION_FALLBACK",
            next_state="READY_FOR_SINGLE_ANALYSIS",
            error_category="external_model",
        )
        fake.classify_structure = lambda *args, **kwargs: ToolResult.partial(
            data={"structure_type": "", "source": "vision_failed"},
            error="结构类型识别未完成，已跳过该筛选。",
            code="STRUCTURE_CLASSIFICATION_FALLBACK",
            next_state="READY_FOR_COARSE_SEARCH",
            retryable=True,
            error_category="external_model",
        )
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertIn("多题判断未完成，已按单题流程继续。", response.text)
        self.assertIn("结构类型识别未完成，已跳过该筛选。", response.text)

    def test_partial_multi_question_crop_is_visible(self):
        fake = FakeTools(chapter="4力法")
        fake.analyze_multi_image = lambda *args, **kwargs: ToolResult.success(
            data={"is_multi": True, "questions": []},
            code="MULTI_QUESTION_DETECTED",
        )
        fake.prepare_question_units = lambda *args, **kwargs: ToolResult.partial(
            data={
                "questions": [
                    {"question_index": 1, "label": "1"},
                    {"question_index": 2, "label": "2"},
                ],
                "diagram_crops": {},
            },
            error="部分题图裁剪未完成，仍可按题号继续。",
            code="MULTI_CROPS_UNAVAILABLE",
            next_state="WAIT_QUESTION_CHOICE",
            retryable=True,
            error_category="image_processing",
        )
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertIn("我在这张图里看到了 2 道题", response.text)
        self.assertIn("部分题图裁剪未完成，仍可按题号继续。", response.text)

    def test_low_reliability_rerank_enters_no_match_without_showing_candidates(self):
        fake = FakeTools(chapter="4力法")

        def no_reliable_rerank(query_image_path, candidates, **kwargs):
            return ToolResult.no_match(
                data={
                    "reranked": True,
                    "visible_candidates": [],
                    "best_final_score": 0.79,
                },
                error="未找到可靠相似题。",
                code="NO_RELIABLE_RERANK_CANDIDATES",
                next_state="NO_MATCH",
            )

        fake.rerank_candidates = no_reliable_rerank
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(agent.state.phase, "NO_MATCH")
        self.assertEqual(agent.state.candidate_count, 0)
        self.assertEqual(response.images, [])
        self.assertIn("没有找到足够可靠的相似候选题", response.text)

    def test_partial_global_search_enters_retryable_error_phase(self):
        fake = FakeTools(chapter="")
        fake.global_search = lambda *args, **kwargs: ToolResult.partial(
            data={"candidates": []},
            error="全局复筛只完成了部分候选。",
            code="GLOBAL_RERANK_INCOMPLETE",
            next_state="GLOBAL_SEARCH_RETRY",
            retryable=True,
            error_category="model_incomplete",
        )
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")

        response = agent.handle_text("可以全局搜")

        self.assertEqual(agent.state.phase, PHASE_ERROR)
        self.assertEqual(agent.state.last_error, "全局复筛未完成，请稍后重试。")
        self.assertIn("重试", response.text)

    def test_answer_no_match_keeps_candidate_choice_state(self):
        fake = FakeTools(chapter="4力法")
        fake.answer_candidate = lambda candidates, **kwargs: ToolResult.no_match(
            data={"rank": kwargs["rank"], "candidate": candidates[kwargs["rank"] - 1]},
            error="未找到该候选题对应的答案文件。",
            code="ANSWER_FILES_NOT_FOUND",
            next_state="WAIT_CANDIDATE_CHOICE",
        )
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        response = agent.handle_text("1")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.last_answer_paths, [])
        self.assertIn("未找到", response.text)

    def test_select_answer_and_resend_answer(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        answer = agent.handle_text("1")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertIn("答案发你了", answer.text)
        self.assertEqual(answer.media_kind, "answer")

        resend = agent.handle_text("刚才答案再发我")
        self.assertIn("再发你一次", resend.text)
        self.assertEqual(resend.images, ["out/answer1.jpg"])
        self.assertEqual(resend.media_kind, "answer")

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

        self.assertIn("不能直接", response.text)
        self.assertIn("确认流程", response.text)

    def test_greeting_introduces_agent_without_resetting_search_state(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")
        phase_before = agent.state.phase
        candidates_before = list(agent.state.candidates)

        response = agent.handle_text("你好啊")

        self.assertEqual(response.intent, "greeting")
        self.assertIn("在的", response.text)
        self.assertIn("继续选题", response.text)
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

    def test_candidate_rejection_stops_after_the_best_batch(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake, enable_author_contact_fallback=True)
        first = agent.handle_image("q.jpg")
        self.assertEqual(first.intent, "search_image")
        self.assertFalse(agent.state.continuation_available)

        rejected = agent.handle_text("没有")
        self.assertEqual(rejected.intent, "reject_candidates")
        self.assertTrue(agent.state.current_candidates_rejected)
        self.assertEqual(len(fake.search_chapters), 1)
        self.assertEqual(
            rejected.text,
            "收到，目前没有更多相似候选题，你可以联系作者手搓。",
        )

        # A persisted pre-release session may still claim another batch exists.
        agent.state.continuation_available = True
        continued = agent.handle_text("换一批")
        self.assertEqual(continued.intent, "clarification")
        self.assertEqual(
            continued.text,
            "收到，目前没有更多相似候选题，你可以联系作者手搓。",
        )
        self.assertEqual(
            continued.author_contact,
            {"label": "联系作者", "channel": "微信", "value": "jglxfd6666"},
        )
        self.assertEqual(len(fake.search_chapters), 1)
        self.assertFalse(agent.state.continuation_available)
        self.assertEqual(fake.route_calls, 1)
        self.assertEqual(fake.structure_calls, 1)

    def test_8896_author_fallback_keeps_continue_as_text_only(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake, enable_author_contact_fallback=True)
        agent.handle_image("q.jpg")

        rejected = agent.handle_text("没有我想要的")

        self.assertEqual(
            rejected.text,
            "收到，目前没有更多相似候选题，你可以联系作者手搓。",
        )
        self.assertEqual(rejected.media_kind, "")
        self.assertEqual(
            rejected.author_contact,
            {"label": "联系作者", "channel": "微信", "value": "jglxfd6666"},
        )

    def test_answer_mismatch_can_return_to_the_existing_candidates(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake)
        agent.handle_image("q.jpg")
        agent.handle_text("候选1")

        mismatch = agent.handle_text("这个答案不对")
        self.assertEqual(mismatch.intent, "report_answer_mismatch")
        self.assertTrue(agent.state.answer_mismatch_reported)

        shown = agent.handle_text("回到候选")
        self.assertEqual(shown.intent, "show_candidates")
        self.assertEqual(shown.images, ["4力法/q1.jpg", "4力法/q2.jpg"])

    def test_answer_mismatch_with_explicit_next_batch_stops_without_searching(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_v2_agent(fake, enable_author_contact_fallback=True)
        agent.handle_image("q.jpg")
        agent.handle_text("候选1")

        continued = agent.handle_text("这个答案不对，换一批")

        self.assertEqual(continued.intent, "clarification")
        self.assertEqual(
            continued.text,
            "收到，目前没有更多相似候选题，你可以联系作者手搓。",
        )
        self.assertEqual(len(fake.search_chapters), 1)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)


if __name__ == "__main__":
    unittest.main()

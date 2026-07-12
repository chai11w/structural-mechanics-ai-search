import unittest

from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.state import PHASE_ANSWERED, STATE_WAIT_CANDIDATE_CHOICE, STATE_WAIT_CHAPTER
from tiku_agent.tools import AgentToolConfig, ToolResult


class FakeTools:
    def __init__(self, *, chapter="4力法"):
        self.chapter = chapter
        self.search_chapters = []
        self.answers = {
            1: ["out/answer1.jpg"],
            2: ["out/answer2.jpg"],
        }

    def toolbox(self):
        return AgentToolbox(
            analyze_image=self.analyze_image,
            analyze_multi_question=self.analyze_multi_question,
            route_bank=self.route_bank,
            classify_structure=self.classify_structure,
            coarse_search=self.coarse_search,
            rerank_candidates=self.rerank_candidates,
            answer_candidate=self.answer_candidate,
        )

    def analyze_image(self, image_path, *, chapter="auto", config=None, include_layout=False):
        return ToolResult(
            ok=True,
            data={
                "image_path": str(image_path),
                "chapter": self.chapter,
                "loads": [{"type": "集中", "raw": "P"}],
            },
        )

    def analyze_multi_question(self, image_path, *, config=None):
        return ToolResult(ok=True, data={"is_multi": False, "questions": []})

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

    def test_image_search_reaches_candidate_choice(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)

        response = agent.handle_image("q.jpg")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(agent.state.candidate_count, 2)
        self.assertIn("检索完成", response.text)
        self.assertEqual(fake.search_chapters, ["4力法"])

    def test_missing_chapter_then_user_supplies_chapter(self):
        fake = FakeTools(chapter="")
        agent = self.make_agent(fake)

        first = agent.handle_image("q.jpg")
        self.assertEqual(agent.state.phase, STATE_WAIT_CHAPTER)
        self.assertIn("章节还不确定", first.text)

        second = agent.handle_text("这题应该是第三章")
        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "3静定结构位移")
        self.assertEqual(fake.search_chapters, ["3静定结构位移"])
        self.assertIn("检索完成", second.text)

    def test_select_answer_and_resend_answer(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        answer = agent.handle_text("1")
        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer1.jpg"])
        self.assertIn("out/answer1.jpg", answer.text)

        resend = agent.handle_text("刚才答案再发我")
        self.assertIn("out/answer1.jpg", resend.text)
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
        self.assertIn("3静定结构位移", corrected.text)

    def test_choose_another_candidate_after_answer(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")
        agent.handle_text("1")

        answer = agent.handle_text("第二个")

        self.assertEqual(agent.state.phase, PHASE_ANSWERED)
        self.assertEqual(agent.state.selected_rank, 2)
        self.assertEqual(agent.state.last_answer_paths, ["out/answer2.jpg"])
        self.assertIn("out/answer2.jpg", answer.text)

    def test_correct_chapter_with_method_name_after_candidates(self):
        fake = FakeTools(chapter="3静定结构位移")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        corrected = agent.handle_text("不对，这个按力法搜")

        self.assertEqual(agent.state.phase, STATE_WAIT_CANDIDATE_CHOICE)
        self.assertEqual(agent.state.current_chapter, "4力法")
        self.assertEqual(agent.state.revision_count, 1)
        self.assertEqual(fake.search_chapters, ["3静定结构位移", "4力法"])
        self.assertIn("4力法", corrected.text)

    def test_cancel(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)
        agent.handle_image("q.jpg")

        response = agent.handle_text("取消")

        self.assertEqual(agent.state.phase, "CANCELLED")
        self.assertIn("已取消", response.text)

    def test_unsupported_text_returns_message(self):
        fake = FakeTools(chapter="4力法")
        agent = self.make_agent(fake)

        response = agent.handle_text("帮我入库这道题")

        self.assertIn("不支持", response.text)

    def test_multi_question_selection_runs_selected_crop_with_chapter_override(self):
        fake = FakeTools(chapter="")
        fake.analyze_multi_question = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "4", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": "crop4.jpg"},
                    {"label": "5", "loads": [{"type": "均布", "raw": "q"}], "chapter": "", "question_image_path": "crop5.jpg"},
                ],
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
        self.assertIn("识别到多道题", listed.text)
        self.assertIn("检索完成", selected.text)

    def test_multi_question_without_crop_skips_visual_rerank(self):
        fake = FakeTools(chapter="")
        rerank_inputs = []
        original_rerank = fake.rerank_candidates

        def record_rerank(query_image_path, candidates, **kwargs):
            rerank_inputs.append(query_image_path)
            return original_rerank(query_image_path, candidates, **kwargs)

        fake.rerank_candidates = record_rerank
        fake.analyze_multi_question = lambda image_path, *, config=None: ToolResult(
            ok=True,
            data={
                "is_multi": True,
                "questions": [
                    {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": ""},
                    {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法", "question_image_path": ""},
                ],
            },
        )
        agent = self.make_agent(fake)

        agent.handle_image("multi.jpg")
        agent.handle_text("第一题")

        self.assertEqual(rerank_inputs, [None])


if __name__ == "__main__":
    unittest.main()

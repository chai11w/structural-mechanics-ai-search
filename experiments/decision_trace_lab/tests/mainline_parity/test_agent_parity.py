from __future__ import annotations

import copy
from pathlib import Path
import unittest
from uuid import uuid4

from mainline_mirror.integrity import activate_verified_source, verify_snapshot


activate_verified_source()

from mainline_mirror.observation.core import (  # noqa: E402
    HookManager, ObservedAgent, ObservedToolbox, _authorization_summary, _decision_summary,
)
from mainline_mirror.observation.storage import ObservationStore  # noqa: E402
from tiku_agent.agent import AgentToolbox, TikuSearchAgent  # noqa: E402
from tiku_agent.state import AgentState  # noqa: E402
from tiku_agent.tools import AgentToolConfig, ToolResult  # noqa: E402


TEST_TMP = Path(__file__).resolve().parents[2] / "runtime" / "test_tmp" / "mainline_parity"
TEST_TMP.mkdir(parents=True, exist_ok=True)


class DeterministicTools:
    def __init__(self, *, chapter: str = "4力法", multi: bool = False, no_match: bool = False, fail_at: str = "", rerank_incomplete: bool = False):
        self.chapter = chapter
        self.multi = multi
        self.no_match = no_match
        self.fail_at = fail_at
        self.rerank_incomplete = rerank_incomplete
        self.calls: list[dict] = []
        self.batch = 0

    def _result(self, name: str, result: ToolResult, params: dict | None = None) -> ToolResult:
        if self.fail_at == name:
            raise RuntimeError(f"{name} boundary failure")
        self.calls.append({"name": name, "params": copy.deepcopy(params or {}), "result": copy.deepcopy(result.to_dict())})
        return result

    def toolbox(self) -> AgentToolbox:
        return AgentToolbox(**{name: getattr(self, name) for name in (
            "analyze_multi_image", "prepare_question_units", "analyze_image", "route_bank",
            "classify_structure", "coarse_search", "global_search", "rerank_candidates", "answer_candidate",
        )})

    def analyze_multi_image(self, image_path, *, config=None):
        questions = [
            {"label": "1", "loads": [{"type": "集中", "raw": "P"}], "chapter": "4力法", "question_image_path": "q1.jpg"},
            {"label": "2", "loads": [{"type": "均布", "raw": "q"}], "chapter": "4力法", "question_image_path": "q2.jpg"},
        ]
        single = {"loads": [{"type": "集中", "raw": "P"}], "chapter_hint": self.chapter}
        return self._result("analyze_multi_image", ToolResult(True, {"is_multi": self.multi, "questions": questions if self.multi else [], "single_analysis": single}, next_state="READY_FOR_MULTI_DETAILS" if self.multi else "READY_FOR_SINGLE_ANALYSIS"))

    def prepare_question_units(self, image_path, questions, *, config=None):
        return self._result("prepare_question_units", ToolResult(True, {"questions": list(questions), "diagram_crops": {"1": "q1.jpg", "2": "q2.jpg"}}), {"question_count": len(questions)})

    def analyze_image(self, image_path, *, chapter="auto", include_layout=False, config=None):
        return self._result("analyze_image", ToolResult(True, {"image_path": str(image_path), "loads": [{"type": "集中", "raw": "P"}], "chapter": self.chapter}))

    def route_bank(self, loads):
        return self._result("route_bank", ToolResult(True, {"route": "main"}), {"load_count": len(loads)})

    def classify_structure(self, image_path, *, route, classified=None, config=None):
        return self._result("classify_structure", ToolResult(True, {"structure_type": "梁"}), {"route": route})

    def coarse_search(self, loads, *, chapter, route, structure_type="", top_k=None, exclude_candidate_keys=None):
        self.batch += 1
        excluded = list(exclude_candidate_keys or [])
        candidates = [] if self.no_match else [
            {"path": f"candidate-{self.batch}-1.jpg", "name": "one.jpg", "score": .9, "candidate_key": f"batch-{self.batch}-1"},
            {"path": f"candidate-{self.batch}-2.jpg", "name": "two.jpg", "score": .8, "candidate_key": f"batch-{self.batch}-2"},
        ]
        return self._result("coarse_search", ToolResult(True, {"candidates": candidates, "has_more": self.batch == 1}), {"chapter": chapter, "route": route, "structure_type": structure_type, "excluded": excluded})

    def global_search(self, loads, query_image_path, *, route, structure_type="", config=None):
        candidates = [] if self.no_match else [{"path": "global.jpg", "name": "global.jpg", "chapter": "4力法", "score": 1.0}]
        return self._result("global_search", ToolResult(True, {"candidates": candidates}, next_state="WAIT_CANDIDATE_CHOICE" if candidates else "NO_MATCH"), {"route": route})

    def rerank_candidates(self, query_image_path, candidates, *, route, rerank_top=3, force_rerank=False):
        payload = {
            "visible_candidates": [] if self.rerank_incomplete else list(candidates),
            "reranked": not self.rerank_incomplete,
            "rerank_complete": not self.rerank_incomplete,
            "rerank_note": "incomplete" if self.rerank_incomplete else "",
        }
        return self._result("rerank_candidates", ToolResult(True, payload), {"route": route, "candidate_count": len(candidates)})

    def answer_candidate(self, candidates, *, rank, copy_to_output=True, config=None):
        return self._result("answer_candidate", ToolResult(True, {"rank": rank, "copied_paths": [f"answer-{rank}.jpg"]}), {"rank": rank, "candidate_count": len(candidates)})


class RaisingStore(ObservationStore):
    def append_event(self, event):
        raise OSError("injected recorder failure")


SCENARIOS = {
    "single_unknown_wait_chapter": ({"chapter": ""}, [("image", "single.jpg")]),
    "set_chapter_after_unknown": ({"chapter": ""}, [("image", "single.jpg"), ("text", "按力法搜")]),
    "single_known_candidates": ({}, [("image", "single.jpg")]),
    "candidate_to_answer": ({}, [("image", "single.jpg"), ("text", "选择候选 1")]),
    "multi_image": ({"multi": True}, [("image", "multi.jpg")]),
    "multi_select_question": ({"multi": True}, [("image", "multi.jpg"), ("text", "第二题")]),
    "correct_chapter": ({}, [("image", "single.jpg"), ("text", "不对，按第三章搜")]),
    "reject_candidates": ({}, [("image", "single.jpg"), ("text", "候选都不对")]),
    "continue_search": ({}, [("image", "single.jpg"), ("text", "候选都不对"), ("text", "继续搜索")]),
    "show_candidates": ({}, [("image", "single.jpg"), ("text", "回到候选")]),
    "resend_answer": ({}, [("image", "single.jpg"), ("text", "选择候选 1"), ("text", "刚才答案再发我")]),
    "answer_mismatch": ({}, [("image", "single.jpg"), ("text", "选择候选 1"), ("text", "答案不匹配")]),
    "greeting": ({}, [("text", "你好")]),
    "small_talk_help": ({}, [("text", "你能做什么")]),
    "small_talk": ({}, [("text", "辛苦了")]),
    "ambiguous_number": ({}, [("text", "2")]),
    "llm_fallback": ({"llm": {"action": "clarification", "clarification_reason": "ambiguous_reference"}}, [("text", "那个")]),
    "unauthorized_global": ({"state": {"phase": "WAIT_CHAPTER", "current_image_path": "q.jpg", "current_question_image_path": "q.jpg", "current_loads": [{"type": "集中", "raw": "P"}]}}, [("text", "全局搜索")]),
    "authorized_global": ({"chapter": ""}, [("image", "single.jpg"), ("text", "可以")]),
    "no_match": ({"no_match": True}, [("image", "single.jpg")]),
    "no_match_recovery": ({"no_match": True}, [("image", "single.jpg"), ("text", "按第三章搜")]),
    "rerank_incomplete": ({"rerank_incomplete": True}, [("image", "single.jpg")]),
    "cancel_retry_explain": ({"state": {"phase": "ERROR", "current_image_path": "q.jpg", "last_error": "timeout"}}, [("text", "为什么失败"), ("text", "重试"), ("text", "取消")]),
}


class MainlineAgentParityTest(unittest.TestCase):
    def test_manifest_verifies_required_mainline_files(self):
        manifest = verify_snapshot()
        paths = {row["path"] for row in manifest["files"]}
        self.assertEqual(manifest["source_commit"], "bc27cba1339f8a73aee18c4a44e109cecd84bd3d")
        for required in ("tiku_agent/agent.py", "tiku_agent/intent_v2.py", "tiku_agent/action_permissions_v2.py", "tiku_agent/fastapi_demo.py", "tiku_agent/demo_web/demo.js", "search.py", "multi_agent_pipeline.py"):
            self.assertIn(required, paths)

    def test_default_entry_uses_mirror_and_legacy_launchers_are_omitted(self):
        lab = Path(__file__).resolve().parents[2]
        current = (lab / "app" / "run_mainline_observed_web.py").read_text(encoding="utf-8")
        self.assertIn("create_observed_app", current)
        self.assertNotIn("private_agent", current)
        for name in ("run_web_demo.py", "run_web_demo_offline.py"):
            self.assertFalse((lab / "app" / name).exists())

    def test_full_required_matrix_baseline_equals_observed(self):
        for name, (options, turns) in SCENARIOS.items():
            with self.subTest(name=name):
                baseline = self._run(options, turns, observed=False)
                boundary_reference = self._run(options, turns, observed=False, capture_boundaries=True)
                observed = self._run(options, turns, observed=True)
                self.assertEqual(baseline["responses"], observed["responses"])
                self.assertEqual(baseline["state"], observed["state"])
                self.assertEqual(baseline["calls"], observed["calls"])
                self.assertEqual(baseline["exception"], observed["exception"])
                self.assertEqual(baseline["responses"], boundary_reference["responses"])
                self.assertEqual(baseline["state"], boundary_reference["state"])
                self.assertEqual(
                    boundary_reference["intent_boundaries"],
                    [event["payload"] for event in observed["events"] if event["event_type"] == "intent_decided"],
                )
                self.assertEqual(
                    boundary_reference["authorization_boundaries"],
                    [event["payload"] for event in observed["events"] if event["event_type"] == "authorization_checked"],
                )
                expected_tools = [call["name"] for call in baseline["calls"]]
                self.assertEqual(expected_tools, [event["payload"]["tool_name"] for event in observed["events"] if event["event_type"] == "tool_started"])
                self.assertEqual(expected_tools, [event["payload"]["tool_name"] for event in observed["events"] if event["event_type"] == "tool_completed"])
                self.assertGreaterEqual(len(observed["events"]), 3)

    def test_tool_exception_boundary_and_type_are_identical(self):
        options = {"fail_at": "rerank_candidates"}
        baseline = self._run(options, [("image", "q.jpg")], observed=False)
        observed = self._run(options, [("image", "q.jpg")], observed=True)
        self.assertEqual(baseline["exception"], ("RuntimeError", "rerank_candidates boundary failure"))
        self.assertEqual(baseline["exception"], observed["exception"])
        self.assertEqual(baseline["state"], observed["state"])
        self.assertEqual(baseline["calls"], observed["calls"])

    def test_recorder_write_failure_is_business_fail_open(self):
        baseline = self._run({}, [("image", "q.jpg"), ("text", "候选1")], observed=False)
        observed = self._run({}, [("image", "q.jpg"), ("text", "候选1")], observed=True, raising_store=True)
        self.assertEqual(baseline["responses"], observed["responses"])
        self.assertEqual(baseline["state"], observed["state"])
        self.assertEqual(baseline["calls"], observed["calls"])

    def _run(self, options, turns, *, observed, raising_store=False, capture_boundaries=False):
        fake = DeterministicTools(
            chapter=options.get("chapter", "4力法"), multi=options.get("multi", False),
            no_match=options.get("no_match", False), fail_at=options.get("fail_at", ""),
            rerank_incomplete=options.get("rerank_incomplete", False),
        )
        state = AgentState(session_id="parity-session", **copy.deepcopy(options.get("state", {})))
        llm_payload = options.get("llm")
        base = TikuSearchAgent(
            state=state, tools=fake.toolbox(), config=AgentToolConfig(top_k=3, rerank_top=3),
            use_llm_intent=llm_payload is not None,
            llm_client=(lambda _prompt: copy.deepcopy(llm_payload)) if llm_payload is not None else None,
        )
        manager = HookManager()
        case_dir = TEST_TMP / uuid4().hex
        case_dir.mkdir(parents=True, exist_ok=True)
        store = RaisingStore(case_dir) if raising_store else ObservationStore(case_dir)
        agent = base
        intent_boundaries = []
        authorization_boundaries = []
        boundary_originals = []
        if capture_boundaries:
            import tiku_agent.agent as agent_module
            import tiku_agent.intent_v2 as intent_module

            original_decide = agent_module.decide_intent_v2
            original_authorize = intent_module.authorize_action_v2

            def capture_decide(*args, **kwargs):
                result = original_decide(*args, **kwargs)
                intent_boundaries.append(_decision_summary(result))
                return result

            def capture_authorize(decision, context):
                result = original_authorize(decision, context)
                authorization_boundaries.append(_authorization_summary(decision, context, result))
                return result

            boundary_originals = [(agent_module, "decide_intent_v2", original_decide), (intent_module, "authorize_action_v2", original_authorize)]
            agent_module.decide_intent_v2 = capture_decide
            intent_module.authorize_action_v2 = capture_authorize
        if observed:
            manager.install()
            base.tools = ObservedToolbox(base.tools)
            agent = ObservedAgent(base, store)
        responses = []
        exception = None
        try:
            for kind, value in turns:
                response = agent.handle_image(value) if kind == "image" else agent.handle_text(value)
                responses.append({"text": response.text, "images": response.images, "state": response.state, "intent": response.intent})
        except Exception as exc:  # boundary equality is asserted by the caller.
            exception = (type(exc).__name__, str(exc))
        finally:
            manager.uninstall()
            for owner, attribute, original in boundary_originals:
                setattr(owner, attribute, original)
        result = {
            "responses": responses, "state": base.state.to_dict(), "calls": fake.calls,
            "exception": exception, "events": store.events(),
            "intent_boundaries": intent_boundaries,
            "authorization_boundaries": authorization_boundaries,
        }
        return result


if __name__ == "__main__":
    unittest.main()

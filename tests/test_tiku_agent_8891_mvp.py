from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from scripts.run_tiku_agent_8891 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_argument_parser,
    build_runtime,
)
from tiku_agent.agent import AgentToolbox, TikuSearchAgent
from tiku_agent.image_contracts import ImageTriageObservation
from tiku_agent.image_triage import build_handoff
from tiku_agent.image_triage_authority import ImageTriageDecision
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_runtime import AgentProtocolError
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.tool_result import ToolResult


def triage_observation(route: str) -> ImageTriageObservation:
    if route == "A1":
        return ImageTriageObservation(
            route_candidate="A1",
            evidence=("没有可检索的结构力学内容。",),
            has_structure_content=False,
            raw_text="建议路线：A1",
        )
    if route == "A2":
        return ImageTriageObservation(
            route_candidate="A2",
            evidence=("单题、单结构图、实际荷载完整。",),
            question_count=1,
            original_structure_count=1,
            auxiliary_diagram_count=0,
            has_actual_load_evidence=True,
            has_structure_content=True,
            image_recoverable=True,
            has_ambiguity=False,
            raw_text="建议路线：A2",
        )
    return ImageTriageObservation(
        route_candidate="A3",
        evidence=("一题多图，需要拆解。",),
        has_structure_content=True,
        raw_text="建议路线：A3",
    )


class FakeAuthority:
    def __init__(self, route: str) -> None:
        handoff = build_handoff("", triage_observation(route))
        self.decision = ImageTriageDecision(
            handoff=handoff,
            reply=f"给用户的 {route} 说明" if route != "A2" else "",
            reply_source="qwen_triage_reply" if route != "A2" else "",
        )

    def decide(self, _image_path):
        return self.decision


class FailingAuthority:
    def decide(self, _image_path):
        raise TimeoutError("model timeout")


class SearchTools:
    def __init__(self) -> None:
        self.multi_calls = 0
        self.analysis_calls = 0

    def toolbox(self) -> AgentToolbox:
        return AgentToolbox(
            analyze_multi_image=self.analyze_multi,
            analyze_image=self.analyze_image,
            route_bank=lambda *_args, **_kwargs: ToolResult.success(
                code="ROUTED", data={"route": "main"}
            ),
            classify_structure=lambda *_args, **_kwargs: ToolResult.success(
                code="STRUCTURE_SKIPPED", data={"structure_type": ""}
            ),
            coarse_search=lambda *_args, **_kwargs: ToolResult.success(
                code="COARSE_FOUND",
                data={
                    "candidates": [
                        {
                            "rank": 1,
                            "path": "bank/q1.jpg",
                            "name": "q1.jpg",
                            "score": 1.0,
                        }
                    ]
                },
            ),
            rerank_candidates=lambda *_args, **_kwargs: ToolResult.success(
                code="RERANKED",
                data={
                    "reranked": True,
                    "visible_candidates": [
                        {
                            "rank": 1,
                            "path": "bank/q1.jpg",
                            "name": "q1.jpg",
                            "score": 1.0,
                        }
                    ],
                },
            ),
        )

    def analyze_multi(self, *_args, **_kwargs):
        self.multi_calls += 1
        raise AssertionError("A2 must not repeat the old multi-question check")

    def analyze_image(self, image_path, **_kwargs):
        self.analysis_calls += 1
        return ToolResult.success(
            code="IMAGE_ANALYZED",
            data={
                "image_path": str(image_path),
                "loads": [{"type": "集中", "raw": "10"}],
                "chapter": "2静定结构",
            },
        )


class TikuAgent8891MvpTest(unittest.TestCase):
    def make_runtime(self, route: str):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8891_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        source = root / "source.jpg"
        root.mkdir(parents=True)
        source.write_bytes(b"fake image")
        tools = SearchTools()
        runtime = AgentSessionRuntime(
            SQLiteSessionStore(root / "session.db"),
            artifacts=SessionArtifacts(root / "sessions"),
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=tools.toolbox(),
                use_llm_intent=False,
            ),
            image_triage_authority=FakeAuthority(route),
        )
        return runtime, source, tools

    def test_a1_and_a3_return_second_qwen_reply_without_searching(self):
        for route, code in (
            ("A1", "TRIAGE_A1_STOPPED"),
            ("A3", "TRIAGE_A3_REQUIRES_REUPLOAD"),
        ):
            with self.subTest(route=route):
                runtime, source, tools = self.make_runtime(route)
                response = runtime.handle_image(f"session-{route}", source)

                self.assertEqual(response.text, f"给用户的 {route} 说明")
                self.assertEqual(response.reply_source, "qwen_triage_reply")
                self.assertEqual(response.protocol["code"], code)
                self.assertEqual(response.protocol["status"], "NEEDS_INPUT")
                self.assertEqual(response.protocol["action"], "retry_upload")
                self.assertEqual(response.state["current_route"], route)
                self.assertEqual(tools.multi_calls, 0)
                self.assertEqual(tools.analysis_calls, 0)
                self.assertTrue(runtime.current_image_path(f"session-{route}").is_file())

    def test_a2_runs_exact_recognition_and_existing_search(self):
        runtime, source, tools = self.make_runtime("A2")

        response = runtime.handle_image("session-A2", source)

        self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(response.state["current_route"], "main")
        self.assertEqual(response.state["current_chapter"], "2静定结构")
        self.assertEqual(tools.multi_calls, 0)
        self.assertEqual(tools.analysis_calls, 1)
        self.assertEqual(len(response.images), 1)

    def test_prechecked_a2_entry_skips_triage_and_old_multi_question_check(self):
        runtime, source, tools = self.make_runtime("A3")

        response = runtime.handle_prechecked_image("session-prechecked-A2", source)

        self.assertEqual(response.state["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(tools.multi_calls, 0)
        self.assertEqual(tools.analysis_calls, 1)

    def test_first_triage_failure_is_a_public_retryable_error(self):
        runtime, source, _tools = self.make_runtime("A2")
        runtime.image_triage_authority = FailingAuthority()

        with self.assertRaises(AgentProtocolError) as raised:
            runtime.handle_image("session-failure", source)

        protocol = raised.exception.bind(
            request_id="req_12345678", search_id="search_12345678"
        )
        self.assertEqual(str(raised.exception), "图片检查暂时失败，请稍后重试。")
        self.assertEqual(protocol.code, "SERVICE_UNAVAILABLE")
        self.assertTrue(protocol.retryable)

    def test_launcher_is_isolated_and_disables_the_old_external_load_gate(self):
        parser = build_argument_parser()
        defaults = parser.parse_args([])
        self.assertEqual(DEFAULT_PORT, 8891)
        self.assertEqual(defaults.port, 8891)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_v2_validation_8891")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8891_session")
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8891_build_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        runtime = build_runtime(root, enable_triage=False)
        self.assertIsNone(runtime.external_load_screen)
        self.assertIsNone(runtime.image_triage_authority)


if __name__ == "__main__":
    unittest.main()

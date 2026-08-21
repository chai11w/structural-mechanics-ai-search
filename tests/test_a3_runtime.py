import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.a3_auto_crop import A3AutoCropPage, A3AutoCropTarget
from tiku_agent.a3_intent_v1 import A3IntentEngineV1
from tiku_agent.a3_models import (
    A3ModelError,
    A3UnitAnalysis,
    CropCompareResult,
    QwenA3PageObserver,
)
from tiku_agent.a3_page_parser import A3PageParseError, parse_a3_page_understanding
from tiku_agent.a3_runtime import (
    A3MvpRuntime,
    A3SessionState,
    A3_PHASE_A2_ACTIVE,
    A3_PHASE_COMPLETE,
    A3_PHASE_CROP_REQUIRED,
    A3_PHASE_WAIT_SELECTION,
    SQLiteA3SessionStore,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import create_app
from tiku_agent.image_contracts import ImageTriageObservation
from tiku_agent.image_triage import build_handoff
from tiku_agent.image_triage_authority import ImageTriageDecision
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_shared.request_protocol import RequestProtocol


def _page_payload() -> dict:
    units = []
    diagrams = []
    for index in (1, 2):
        units.append({
            "unit_id": f"g1-u{index}",
            "parent_question_label": "四",
            "question_label": str(index),
            "title_text": f"子题 {index} 条件",
            "shared_stem_text": "试作图示刚架的 M 图。",
            "visible_text": "10 kN, 4 m, A, B",
            "searchability": "searchable_candidate",
            "reason_codes": [],
            "diagram_ids": [f"d{index}"],
            "status": "clear",
            "evidence": ["结构、支座和外荷载完整"],
            "notes": "",
        })
        diagrams.append({
            "diagram_id": f"d{index}",
            "role": "original_structure",
            "group_id": "g1",
            "unit_ids": [f"g1-u{index}"],
            "status": "clear",
            "evidence": "完整结构图",
        })
    return {
        "schema_version": "a3-page-understanding-v2",
        "page_disposition": "has_searchable_candidates",
        "a3_reason_evidence": [{"code": "multi_question_page", "evidence": "两个结构图"}],
        "groups": [{
            "group_id": "g1",
            "parent_question_label": "四",
            "parent_title_text": "用力法计算图示结构。",
            "shared_stem_text": "试作图示刚架的 M 图。",
            "units": units,
        }],
        "diagrams": diagrams,
        "unassigned_content": [],
        "unknowns": [],
    }


def _single_page_payload() -> dict:
    payload = _page_payload()
    payload["a3_reason_evidence"] = []
    payload["groups"][0]["units"] = payload["groups"][0]["units"][:1]
    payload["diagrams"] = payload["diagrams"][:1]
    return payload


class FakeObserver:
    def __init__(self):
        self.calls = 0

    def observe(self, _image_path: Path):
        self.calls += 1
        return parse_a3_page_understanding(_page_payload())


class FakeSingleObserver:
    def observe(self, _image_path: Path):
        return parse_a3_page_understanding(_single_page_payload())


class FakeVerifier:
    verdict = "verified"
    checks = {
        "selected_diagram_match": True,
        "single_target_diagram": True,
        "structure_complete": True,
        "supports_complete": True,
        "external_loads_complete": True,
        "image_clear": True,
    }
    def __init__(self):
        self.calls = []

    def verify(self, _page, _crop, selected, _understanding):
        self.calls.append(selected["unit_id"])
        return CropCompareResult(
            selected_unit_id=selected["unit_id"],
            verdict=self.verdict,
            checks=dict(self.checks),
        )


class FakeAutoCropper:
    def __init__(self, *, first_status="auto_ready", second_status="review_required", error=None):
        self.first_status = first_status
        self.second_status = second_status
        self.error = error
        self.calls = []

    def ground(self, image_path, units, page_understanding):
        self.calls.append((Path(image_path), [unit["unit_id"] for unit in units], page_understanding))
        if self.error is not None:
            raise self.error
        first_bbox = (80, 100, 470, 460) if self.first_status != "no_target" else None
        second_bbox = (520, 100, 920, 480) if self.second_status != "no_target" else None
        auto_count = sum(status == "auto_ready" for status in (self.first_status, self.second_status))
        return A3AutoCropPage(
            page_status="ready" if auto_count == 2 else "partially_ready" if auto_count else "manual_required",
            targets=(
                A3AutoCropTarget(
                    target_id="c001",
                    unit_id="g1-u1",
                    question_label="四-1",
                    bbox=first_bbox,
                    status=self.first_status,
                    reason_codes=("crop_boundary_uncertain",) if self.first_status == "review_required" else (),
                    binding_evidence="左侧结构图",
                ),
                A3AutoCropTarget(
                    target_id="c002",
                    unit_id="g1-u2",
                    question_label="四-2",
                    bbox=second_bbox,
                    status=self.second_status,
                    reason_codes=("crop_boundary_uncertain",) if self.second_status == "review_required" else (),
                    binding_evidence="右侧结构图",
                ),
            ),
            unknowns=(),
        )


class FakeSingleAutoCropper:
    def ground(self, _image_path, _units, _page_understanding):
        return A3AutoCropPage(
            page_status="ready",
            targets=(A3AutoCropTarget(
                target_id="c001",
                unit_id="g1-u1",
                question_label="四-1",
                bbox=(80, 100, 920, 700),
                status="auto_ready",
                reason_codes=(),
                binding_evidence="唯一结构图",
            ),),
            unknowns=(),
        )


class FakeAnalyzer:
    def __init__(self):
        self.calls = 0

    def analyze(self, _crop_path: Path, _context_text: str):
        self.calls += 1
        return A3UnitAnalysis(
            loads=({"type": "集中", "raw": "P"},),
            chapter_hint="4力法",
            chapter_confidence=0.96,
            chapter_evidence="「M 图」",
            category="symbolic",
            load_details=(),
        )


class FakeA2Runtime:
    def __init__(self):
        self.sessions = {}
        self.media = {}
        self.clear_calls = []
        self.preanalyzed_calls = []
        self.prechecked_calls = []
        self.text_calls = []
        self.candidate_count = 1

    def handle_prechecked_image(self, session_id, image_path, **kwargs):
        self.prechecked_calls.append((session_id, Path(image_path), kwargs))
        self.sessions[session_id] = {
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 1,
            "candidate_generation": "1:1",
            "candidate_count": self.candidate_count,
            "chapter": "2静定结构",
            "search_id": "search_direct_a2",
        }
        return AgentResponse(
            text="我从题库里找到了最相似的一道题。",
            state={
                "phase": "WAIT_CANDIDATE_CHOICE",
                "candidate_count": self.candidate_count,
            },
            intent="search_image",
            protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict(),
        )

    def handle_preanalyzed_image(self, session_id, image_path, **kwargs):
        self.preanalyzed_calls.append((session_id, Path(image_path), kwargs))
        self.sessions[session_id] = {
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 1,
            "candidate_generation": "1:1",
            "candidate_count": self.candidate_count,
            "chapter": kwargs.get("chapter", ""),
            "search_id": "search_a2runtime",
        }
        return AgentResponse(
            text="我从题库里找到了最相似的一道题。",
            state={
                "phase": "WAIT_CANDIDATE_CHOICE",
                "candidate_count": self.candidate_count,
            },
            intent="search_image",
            protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict(),
        )

    def handle_text(self, session_id, text, **_kwargs):
        self.text_calls.append((session_id, text))
        if text == "算了":
            self.sessions.pop(session_id, None)
            return AgentResponse(
                text="好，已经取消了。",
                state={"phase": "CANCELLED"},
                intent="cancel",
                protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict(),
            )
        if text == "2静定结构":
            self.sessions[session_id]["phase"] = "WAIT_CANDIDATE_CHOICE"
            return AgentResponse(
                text="已按第 2 章继续检索。",
                state={"phase": "WAIT_CANDIDATE_CHOICE"},
                intent="set_chapter",
                protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict(),
            )
        self.sessions[session_id]["phase"] = "ANSWERED"
        return AgentResponse(
            text="找到了，答案发你了。",
            state={"phase": "ANSWERED"},
            intent="select_candidate",
            protocol=RequestProtocol.from_code("REQUEST_SUCCEEDED").to_dict(),
        )

    def session_snapshot(self, session_id):
        return self.sessions.get(session_id, {
            "session_valid": False,
            "phase": "IDLE",
            "has_active_image": False,
            "task_revision": 0,
            "candidate_generation": "",
            "candidate_count": 0,
            "chapter": "",
            "search_id": "",
        })

    def clear(self, session_id, *, preserve_artifacts=False):
        self.clear_calls.append((session_id, preserve_artifacts))
        self.sessions.pop(session_id, None)
        if not preserve_artifacts:
            self.media = {
                key: value for key, value in self.media.items() if key[0] != session_id
            }

    def purge_expired(self):
        return None

    def persist_media(self, session_id, source):
        path = Path(source).resolve()
        self.media[(session_id, path.name)] = path
        return path

    def resolve_media(self, session_id, filename, *, allow_preserved=False):
        if not allow_preserved and session_id not in self.sessions:
            return None
        path = self.media.get((session_id, filename))
        return path if path is not None and path.is_file() else None

    def record_protocol_event(self, *_args, **_kwargs):
        return None


def _triage_decision(route: str) -> ImageTriageDecision:
    if route == "A1":
        observation = ImageTriageObservation(
            route_candidate="A1",
            evidence=("没有结构力学题目。",),
            has_structure_content=False,
            raw_text="建议路线：A1",
        )
    elif route == "A2":
        observation = ImageTriageObservation(
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
    else:
        observation = ImageTriageObservation(
            route_candidate="A3",
            evidence=("包含多道题。",),
            has_structure_content=True,
            raw_text="建议路线：A3",
        )
    handoff = build_handoff("", observation)
    return ImageTriageDecision(
        handoff=handoff,
        reply="这张图片不是可检索的结构力学题，请重新上传。" if route == "A1" else "",
        reply_source="fixed_test" if route == "A1" else "",
    )


class FakeFlowAuthority:
    def __init__(self, route: str):
        self.route = route
        self.calls = 0

    def decide_for_full_flow(self, _image_path):
        self.calls += 1
        return _triage_decision(self.route)


class A3RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "page.jpg"
        Image.new("RGB", (800, 600), "white").save(self.source)
        self.verifier = FakeVerifier()
        self.a2 = FakeA2Runtime()
        self.analyzer = FakeAnalyzer()
        self.runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            unit_analyzer=self.analyzer,
        )

    def test_a3_cost_runs_keep_identity_and_workflow_search_id(self):
        class CapturingLedger:
            def __init__(self):
                self.collectors = []

            def write_run(self, collector, *, finished_at, outcome):
                del finished_at, outcome
                self.collectors.append(collector)

        ledger = CapturingLedger()
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "identity-a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "identity-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            cost_ledger=ledger,
        )

        runtime.handle_image("identity-flow", self.source, identity_key="invite-001")
        snapshot = runtime.session_snapshot("identity-flow")

        self.assertEqual(len(ledger.collectors), 1)
        self.assertEqual(ledger.collectors[0].identity_key, "invite-001")
        self.assertEqual(ledger.collectors[0].search_key, snapshot["workflow_search_id"])
        self.assertEqual(snapshot["search_id"], snapshot["workflow_search_id"])

    def test_intent_v1_cancel_scope_clarifies_before_mutating_state(self):
        self.runtime.intent_engine = A3IntentEngineV1()
        session_id = "a3-intent-cancel-scope"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")

        clarified = self.runtime.handle_text(session_id, "取消")

        self.assertEqual(clarified.intent, "a3_cancel_scope_clarification")
        self.assertIn("暂时没有改变当前进度", clarified.text)
        snapshot = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(snapshot["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(snapshot["selected_unit"]["unit_id"], "g1-u2")
        self.assertEqual(
            snapshot["pending_intent_clarification"]["options"],
            [
                "cancel_current_unit",
                "finish_page",
                "reset_session",
                "continue_current",
            ],
        )

        cancelled = self.runtime.handle_text(session_id, "1")

        self.assertEqual(cancelled.intent, "a3_current_unit_cancelled")
        snapshot = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(snapshot["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(snapshot["selected_unit"]["unit_id"], "")
        self.assertEqual(snapshot["completed_unit_ids"], [])
        self.assertEqual(snapshot["searched_unit_ids"], [])

    def test_intent_v1_explicit_page_finish_and_session_reset_have_distinct_scope(self):
        self.runtime.intent_engine = A3IntentEngineV1()
        session_id = "a3-intent-finish-page"
        self.runtime.handle_image(session_id, self.source)

        finished = self.runtime.handle_text(session_id, "结束这张图")

        self.assertEqual(finished.intent, "a3_page_finished")
        snapshot = self.runtime.session_snapshot(session_id)["a3"]
        self.assertTrue(snapshot["page_finished"])
        self.assertEqual(snapshot["phase"], A3_PHASE_COMPLETE)
        self.assertEqual(snapshot["remaining_count"], 0)
        stale = self.runtime.select_unit(session_id, "g1-u1")
        self.assertEqual(stale.intent, "stale_action")

        reset = self.runtime.handle_text(session_id, "开始新对话")

        self.assertEqual(reset.intent, "a3_session_reset")
        self.assertFalse(self.runtime.session_snapshot(session_id)["session_valid"])

    def test_intent_v1_uses_original_page_index_after_an_earlier_unit_completed(self):
        self.runtime.intent_engine = A3IntentEngineV1()
        session_id = "a3-stable-page-index"
        self.runtime.handle_image(session_id, self.source)
        state = self.runtime.store.load(session_id)
        state.completed_unit_ids = ["g1-u1"]
        state.phase = A3_PHASE_WAIT_SELECTION
        self.runtime.store.save(state)

        completed = self.runtime.handle_text(session_id, "第1题")

        self.assertEqual(completed.intent, "a3_unit_unavailable")
        snapshot = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(snapshot["selected_unit"]["unit_id"], "")
        self.assertEqual(
            [(item["unit_id"], item["page_index"]) for item in snapshot["units"]],
            [("g1-u1", 1), ("g1-u2", 2)],
        )

        selected = self.runtime.handle_text(session_id, "第2题")

        self.assertEqual(selected.intent, "a3_unit_selected")
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["selected_unit"]["unit_id"],
            "g1-u2",
        )

    def test_restored_unlabelled_units_receive_unique_page_ordinals(self):
        state = A3SessionState.from_dict({
            "session_id": "legacy-unlabelled",
            "entry_route": "A3",
            "phase": A3_PHASE_WAIT_SELECTION,
            "units": [
                {
                    "unit_id": "g1-u1",
                    "parent_question_label": "",
                    "question_label": "",
                    "display_label": "未标号题1",
                    "searchability": "searchable_candidate",
                },
                {
                    "unit_id": "g2-u1",
                    "parent_question_label": "",
                    "question_label": "",
                    "display_label": "未标号题1",
                    "searchability": "searchable_candidate",
                },
            ],
        })

        self.assertEqual(
            [unit["display_label"] for unit in state.units],
            ["未标号题1", "未标号题2"],
        )

    def test_full_flow_a2_skips_a3_crop_and_continues_in_original_a2(self):
        authority = FakeFlowAuthority("A2")
        self.runtime.image_triage_authority = authority

        response = self.runtime.handle_image("full-flow-a2", self.source)

        self.assertEqual(response.intent, "search_image")
        self.assertEqual(authority.calls, 1)
        self.assertEqual(self.runtime.page_observer.calls, 0)
        self.assertEqual(len(self.a2.prechecked_calls), 1)
        self.assertEqual(self.a2.preanalyzed_calls, [])
        snapshot = self.runtime.session_snapshot("full-flow-a2")
        self.assertEqual(snapshot["image_route"], "A2")
        self.assertEqual(snapshot["phase"], "WAIT_CANDIDATE_CHOICE")
        self.assertFalse(snapshot["a3"]["enabled"])

        answered = self.runtime.handle_text("full-flow-a2", "选择候选 1")

        self.assertEqual(answered.intent, "select_candidate")
        self.assertEqual(self.a2.text_calls, [("full-flow-a2", "选择候选 1")])

    def test_full_flow_a2_web_response_does_not_open_crop_ui(self):
        self.runtime.image_triage_authority = FakeFlowAuthority("A2")
        client = TestClient(create_app(runtime=self.runtime, incoming_dir=self.root / "incoming-a2"))

        uploaded = client.post(
            "/api/image",
            files={"file": ("single.jpg", self.source.read_bytes(), "image/jpeg")},
        )

        self.assertEqual(uploaded.status_code, 200)
        payload = uploaded.json()
        self.assertEqual(payload["session"]["image_route"], "A2")
        self.assertFalse(payload["session"]["a3"]["enabled"])
        self.assertNotIn("裁剪", payload["text"])

    def test_direct_a2_route_is_reclassified_to_a1_when_load_screen_says_no(self):
        authority = FakeFlowAuthority("A2")
        self.runtime.image_triage_authority = authority
        self.runtime.external_load_screen = lambda _path: "no"

        response = self.runtime.handle_image("direct-a2-no-load", self.source)

        self.assertEqual(response.intent, "image_triage_stop")
        self.assertEqual(response.protocol["code"], "TRIAGE_A1_STOPPED")
        self.assertEqual(self.a2.prechecked_calls, [])
        snapshot = self.runtime.session_snapshot("direct-a2-no-load")
        self.assertEqual(snapshot["image_route"], "A1")
        self.assertEqual(snapshot["phase"], "COMPLETE")

    def test_full_flow_a3_enters_existing_page_crop_flow(self):
        authority = FakeFlowAuthority("A3")
        self.runtime.image_triage_authority = authority

        response = self.runtime.handle_image("full-flow-a3", self.source)

        self.assertEqual(response.intent, "a3_page_ready")
        self.assertEqual(authority.calls, 1)
        self.assertEqual(self.runtime.page_observer.calls, 1)
        self.assertEqual(self.a2.prechecked_calls, [])
        snapshot = self.runtime.session_snapshot("full-flow-a3")
        self.assertEqual(snapshot["image_route"], "A3")
        self.assertTrue(snapshot["a3"]["enabled"])
        self.assertEqual(snapshot["a3"]["phase"], A3_PHASE_WAIT_SELECTION)

    def test_auto_crop_flow_grounds_page_once_and_keeps_partial_results(self):
        cropper = FakeAutoCropper()
        load_calls = []
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "auto-a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "auto-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=cropper,
            external_load_screen=lambda path: load_calls.append(Path(path)) or "yes",
        )

        response = runtime.handle_image("auto-page", self.source)

        self.assertEqual(response.intent, "a3_auto_crops_ready")
        self.assertEqual(len(cropper.calls), 1)
        snapshot = runtime.session_snapshot("auto-page")["a3"]
        self.assertTrue(snapshot["auto_crop_enabled"])
        self.assertEqual(snapshot["auto_crop_page_status"], "partially_ready")
        self.assertEqual(snapshot["units"][0]["grounding_status"], "auto_ready")
        self.assertEqual(snapshot["units"][1]["grounding_status"], "review_required")
        self.assertTrue(snapshot["units"][0]["crop_available"])
        self.assertTrue(snapshot["auto_crop_overlay_available"])
        self.assertTrue(runtime.current_auto_crop_overlay_path("auto-page").is_file())
        self.assertEqual(load_calls, [])

    def test_single_auto_crop_validates_and_enters_a2_without_user_selection(self):
        load_calls = []
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "single-auto.sqlite3"),
            artifacts=SessionArtifacts(self.root / "single-auto-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeSingleObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeSingleAutoCropper(),
            external_load_screen=lambda path: load_calls.append(Path(path)) or "yes",
        )

        response = runtime.handle_image("single-auto", self.source)

        self.assertEqual(response.intent, "search_image")
        snapshot = runtime.session_snapshot("single-auto")["a3"]
        self.assertEqual(snapshot["phase"], A3_PHASE_A2_ACTIVE)
        self.assertEqual(snapshot["requested_unit_ids"], ["g1-u1"])
        self.assertEqual(snapshot["units"][0]["validation_status"], "auto_ready")
        self.assertEqual(self.verifier.calls, ["g1-u1"])
        self.assertEqual(len(load_calls), 1)
        self.assertEqual(len(self.a2.prechecked_calls), 1)

    def test_auto_prepare_all_validates_every_unit_before_showing_selection(self):
        load_calls = []
        progress_events = []
        barrier = threading.Barrier(2)

        class ConcurrentVerifier(FakeVerifier):
            def verify(self, page, crop, selected, understanding):
                barrier.wait(timeout=2)
                return super().verify(page, crop, selected, understanding)

        verifier = ConcurrentVerifier()
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "auto-prepare-all.sqlite3"),
            artifacts=SessionArtifacts(self.root / "auto-prepare-all-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=verifier,
            auto_cropper=FakeAutoCropper(second_status="auto_ready"),
            auto_prepare_all_units=True,
            external_load_screen=lambda path: load_calls.append(Path(path)) or "yes",
        )

        response = runtime.handle_image(
            "auto-prepare-all",
            self.source,
            progress=lambda stage, message: progress_events.append((stage, message)),
        )

        self.assertEqual(response.intent, "a3_units_prepared")
        snapshot = runtime.session_snapshot("auto-prepare-all")["a3"]
        self.assertTrue(snapshot["auto_prepare_all_enabled"])
        self.assertTrue(snapshot["auto_prepare_all_units"])
        self.assertEqual(snapshot["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(snapshot["requested_unit_ids"], ["g1-u1", "g1-u2"])
        self.assertTrue(all(unit["requested"] for unit in snapshot["units"]))
        self.assertTrue(
            all(unit["validation_status"] == "auto_ready" for unit in snapshot["units"])
        )
        self.assertCountEqual(verifier.calls, ["g1-u1", "g1-u2"])
        self.assertEqual(len(load_calls), 2)
        self.assertEqual(self.a2.prechecked_calls, [])
        validation_progress = [
            message
            for stage, message in progress_events
            if stage == "a3_auto_validating"
        ]
        self.assertEqual(
            validation_progress,
            [
                "正在并发校验 2 张自动裁图…",
                "已完成 1/2 张自动裁图校验…",
                "已完成 2/2 张自动裁图校验…",
            ],
        )

    def test_auto_prepare_all_keeps_manual_fallback_for_unready_crop(self):
        load_calls = []
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "auto-prepare-partial.sqlite3"),
            artifacts=SessionArtifacts(self.root / "auto-prepare-partial-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(),
            auto_prepare_all_units=True,
            external_load_screen=lambda path: load_calls.append(Path(path)) or "yes",
        )

        response = runtime.handle_image("auto-prepare-partial", self.source)

        self.assertEqual(response.intent, "a3_units_prepared")
        snapshot = runtime.session_snapshot("auto-prepare-partial")["a3"]
        self.assertEqual(snapshot["requested_unit_ids"], ["g1-u1", "g1-u2"])
        self.assertTrue(all(unit["requested"] for unit in snapshot["units"]))
        self.assertEqual(
            [unit["validation_status"] for unit in snapshot["units"]],
            ["auto_ready", "manual_required"],
        )
        self.assertEqual(self.verifier.calls, ["g1-u1"])
        self.assertEqual(len(load_calls), 1)

        selected = runtime.select_unit(
            "auto-prepare-partial",
            "g1-u2",
            task_revision=1,
        )

        self.assertEqual(selected.intent, "a3_unit_selected")
        selected_snapshot = runtime.session_snapshot("auto-prepare-partial")["a3"]
        self.assertEqual(selected_snapshot["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertTrue(selected_snapshot["crop_draft"]["available"])

    def test_prepare_only_validates_requested_auto_crops_then_directly_enters_a2(self):
        load_calls = []
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "prepare-a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "prepare-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(second_status="auto_ready"),
            external_load_screen=lambda path: load_calls.append(Path(path)) or "yes",
        )
        runtime.handle_image("prepare-page", self.source)

        prepared = runtime.prepare_units(
            "prepare-page",
            ["g1-u1"],
            task_revision=1,
        )

        self.assertEqual(prepared.intent, "a3_units_prepared")
        snapshot = runtime.session_snapshot("prepare-page")["a3"]
        self.assertEqual(snapshot["units"][0]["validation_status"], "auto_ready")
        self.assertEqual(snapshot["units"][1]["validation_status"], "pending")
        self.assertEqual(self.verifier.calls, ["g1-u1"])
        self.assertEqual(len(load_calls), 1)

        selected = runtime.select_unit("prepare-page", "g1-u1", task_revision=1)

        self.assertEqual(selected.intent, "search_image")
        self.assertEqual(runtime.session_snapshot("prepare-page")["a3"]["phase"], A3_PHASE_A2_ACTIVE)
        self.assertEqual(len(self.a2.prechecked_calls), 1)
        _session, crop_path, kwargs = self.a2.prechecked_calls[0]
        self.assertTrue(crop_path.is_file())
        self.assertIn("子题 1 条件", kwargs["context_text"])

    def test_selected_review_target_uses_existing_manual_crop_with_prefilled_bounds(self):
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "manual-fallback.sqlite3"),
            artifacts=SessionArtifacts(self.root / "manual-fallback-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(),
            external_load_screen=lambda _path: "yes",
        )
        runtime.handle_image("manual-fallback", self.source)
        runtime.prepare_units("manual-fallback", ["g1-u2"], task_revision=1)

        response = runtime.select_unit("manual-fallback", "g1-u2", task_revision=1)

        self.assertEqual(response.intent, "a3_unit_selected")
        snapshot = runtime.session_snapshot("manual-fallback")["a3"]
        self.assertEqual(snapshot["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertTrue(snapshot["crop_draft"]["available"])
        self.assertEqual(snapshot["crop_draft"]["bounds"]["x"], 0.5)
        self.assertEqual(self.verifier.calls, [])

    def test_grounding_error_degrades_every_unit_to_manual_without_page_error(self):
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "grounding-error.sqlite3"),
            artifacts=SessionArtifacts(self.root / "grounding-error-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(error=RuntimeError("service unavailable")),
        )

        response = runtime.handle_image("grounding-error", self.source)

        self.assertEqual(response.intent, "a3_page_ready")
        snapshot = runtime.session_snapshot("grounding-error")["a3"]
        self.assertEqual(snapshot["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertFalse(snapshot["auto_crop_enabled"])
        self.assertTrue(all(
            unit["validation_status"] == "manual_required"
            for unit in snapshot["units"]
        ))

    def test_all_manual_grounding_skips_prepare_and_uses_v0_selection(self):
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "all-manual.sqlite3"),
            artifacts=SessionArtifacts(self.root / "all-manual-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(
                first_status="review_required",
                second_status="no_target",
            ),
        )

        response = runtime.handle_image("all-manual", self.source)

        self.assertEqual(response.intent, "a3_page_ready")
        snapshot = runtime.session_snapshot("all-manual")["a3"]
        self.assertFalse(snapshot["auto_crop_enabled"])
        self.assertTrue(snapshot["auto_crop_overlay_available"])
        selected = runtime.select_unit("all-manual", "g1-u1", task_revision=1)
        self.assertEqual(selected.intent, "a3_unit_selected")
        selected_snapshot = runtime.session_snapshot("all-manual")["a3"]
        self.assertEqual(selected_snapshot["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(selected_snapshot["crop_draft"]["bounds"]["x"], 0.06)

    def test_full_flow_a1_stops_without_a2_or_a3_processing(self):
        authority = FakeFlowAuthority("A1")
        self.runtime.image_triage_authority = authority

        response = self.runtime.handle_image("full-flow-a1", self.source)

        self.assertEqual(response.intent, "image_triage_stop")
        self.assertEqual(response.protocol["code"], "TRIAGE_A1_STOPPED")
        self.assertEqual(authority.calls, 1)
        self.assertEqual(self.runtime.page_observer.calls, 0)
        self.assertEqual(self.a2.prechecked_calls, [])
        snapshot = self.runtime.session_snapshot("full-flow-a1")
        self.assertEqual(snapshot["image_route"], "A1")
        self.assertFalse(snapshot["a3"]["enabled"])
        self.assertTrue(self.runtime.current_image_path("full-flow-a1").is_file())

    def test_verified_crop_carries_shared_context_into_a2_and_returns_to_remaining_unit(self):
        session_id = "a3-runtime-session"
        page = self.runtime.handle_image(session_id, self.source)
        self.assertEqual(page.intent, "a3_page_ready")
        self.assertEqual(self.runtime.session_snapshot(session_id)["a3"]["phase"], A3_PHASE_WAIT_SELECTION)

        selected = self.runtime.handle_text(session_id, "第2个")
        self.assertEqual(selected.intent, "a3_unit_selected")
        self.assertEqual(self.runtime.session_snapshot(session_id)["a3"]["selected_unit"]["unit_id"], "g1-u2")

        searched = self.runtime.handle_crop(
            session_id,
            {"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.5},
        )
        self.assertEqual(searched.intent, "search_image")
        self.assertEqual(
            searched.text,
            "我从题库里找到了与「四-2」最相似的一道题。你看看是不是这道。",
        )
        self.assertEqual(self.runtime.session_snapshot(session_id)["a3"]["phase"], A3_PHASE_A2_ACTIVE)
        _session, crop_path, kwargs = self.a2.prechecked_calls[0]
        with Image.open(crop_path) as crop_image:
            self.assertEqual(crop_image.size, (400, 300))
        self.assertIn("用力法计算图示结构。", kwargs["context_text"])
        self.assertIn("试作图示刚架的 M 图。", kwargs["context_text"])
        self.assertIn("子题 2 条件", kwargs["context_text"])
        self.assertNotIn("10 kN", kwargs["context_text"])
        self.assertEqual(self.a2.preanalyzed_calls, [])
        self.assertEqual(self.analyzer.calls, 0)

        answered = self.runtime.handle_text(session_id, "选择候选 1")
        self.assertEqual(
            answered.text,
            "「四-2」的题库答案找到了，已经发给你。这道题处理好了，这张图里还有 1 道可以继续查。",
        )
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["completed_unit_ids"], ["g1-u2"])

        self.runtime.handle_text(session_id, "第1个")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
        )
        completed = self.runtime.handle_text(session_id, "选择候选 1")
        self.assertEqual(
            completed.text,
            "「四-1」的题库答案找到了，已经发给你。这张图里的可处理题目已经全部完成。",
        )

    def test_review_required_preserves_crop_draft_and_does_not_enter_a2(self):
        session_id = "a3-review-session"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u1")
        self.verifier.verdict = "review_required"
        self.verifier.checks = {
            **self.verifier.checks,
            "external_loads_complete": False,
        }
        response = self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        self.assertEqual(response.intent, "a3_crop_review_required")
        self.assertEqual(
            response.text,
            "裁剪结果未通过，结构荷载不完整，请重新选择区域裁剪。",
        )
        self.assertEqual(self.a2.preanalyzed_calls, [])
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(a3["crop_review_feedback"], response.text)
        self.assertTrue(a3["crop_draft"]["available"])
        self.assertEqual(a3["crop_draft"]["bounds"]["width"], 0.7)

    def test_independent_no_load_screen_blocks_model_hallucination_before_a2(self):
        session_id = "a3-no-load-screen-session"
        self.runtime.external_load_screen = lambda _path: "no"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u1")

        response = self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        self.assertEqual(response.intent, "a3_crop_review_required")
        self.assertEqual(
            response.text,
            "裁剪结果未通过，未识别到结构荷载，请重新选择区域裁剪。",
        )
        self.assertEqual(self.a2.preanalyzed_calls, [])
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertTrue(a3["crop_review_required"])

    def test_external_load_screen_error_fails_closed_before_a2(self):
        session_id = "a3-load-screen-error-session"

        def fail(_path):
            raise TimeoutError("screen timed out")

        self.runtime.external_load_screen = fail
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u1")

        response = self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        self.assertEqual(response.intent, "a3_crop_review_required")
        self.assertEqual(response.text, "裁剪结果暂时无法确认外荷载，请重新提交裁剪。")
        self.assertEqual(self.a2.preanalyzed_calls, [])
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["phase"],
            A3_PHASE_CROP_REQUIRED,
        )

    def test_review_required_names_the_selected_question_on_binding_mismatch(self):
        session_id = "a3-mismatch-session"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u1")
        self.verifier.verdict = "review_required"
        self.verifier.checks = {
            **self.verifier.checks,
            "selected_diagram_match": False,
        }

        response = self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        self.assertRegex(
            response.text,
            r"^裁剪结果未通过，裁剪图不是.+，请重新选择区域裁剪。$",
        )

    def test_cancel_returns_to_page_units_and_natural_label_can_resume(self):
        session_id = "a3-cancel-session"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        cancelled = self.runtime.handle_text(session_id, "算了")

        self.assertEqual(cancelled.intent, "cancel")
        self.assertIn("还有 2 道", cancelled.text)
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["completed_unit_ids"], [])
        self.assertEqual(a3["selected_unit"]["unit_id"], "")

        resumed = self.runtime.handle_text(session_id, "我想搜 四-1")

        self.assertEqual(resumed.intent, "a3_unit_selected")
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["selected_unit"]["unit_id"],
            "g1-u1",
        )

    def test_missing_child_session_recovers_before_handling_current_selection(self):
        session_id = "a3-missing-child-session"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )
        self.a2.clear(session_id)

        resumed = self.runtime.handle_text(session_id, "我想搜 四-1")

        self.assertEqual(resumed.intent, "a3_unit_selected")
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(a3["selected_unit"]["unit_id"], "g1-u1")

    def test_active_a2_explicit_label_switches_without_forwarding_to_child(self):
        session_id = "a3-active-text-switch"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        switched = self.runtime.handle_text(session_id, "我想搜 四-1")

        self.assertEqual(switched.intent, "a3_unit_selected")
        self.assertIn("改查", switched.text)
        self.assertEqual(self.a2.text_calls, [])
        self.assertNotIn(session_id, self.a2.sessions)
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(a3["selected_unit"]["unit_id"], "g1-u1")

    def test_active_a2_selection_api_switches_to_another_unit(self):
        session_id = "a3-active-button-switch"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        switched = self.runtime.select_unit(session_id, "g1-u1", task_revision=1)

        self.assertEqual(switched.intent, "a3_unit_selected")
        self.assertNotIn(session_id, self.a2.sessions)
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(a3["selected_unit"]["unit_id"], "g1-u1")

    def test_active_a2_reselecting_current_unit_preserves_candidates(self):
        session_id = "a3-active-same-unit"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.select_unit(session_id, "g1-u2", task_revision=1)

        self.assertEqual(response.intent, "a3_unit_already_selected")
        self.assertIn("已有进度已保留", response.text)
        self.assertIn(session_id, self.a2.sessions)
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_A2_ACTIVE)
        self.assertEqual(a3["selected_unit"]["unit_id"], "g1-u2")

    def test_active_a2_reselect_request_returns_to_page_units(self):
        session_id = "a3-active-reselect"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "换个题重新搜")

        self.assertEqual(response.intent, "a3_reselect")
        self.assertIn("还有 1 道", response.text)
        self.assertEqual(self.a2.text_calls, [])
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["selected_unit"]["unit_id"], "")
        self.assertEqual(a3["searched_unit_ids"], ["g1-u2"])
        self.assertTrue(a3["units"][1]["searched"])

        repeated = self.runtime.select_unit(session_id, "g1-u2", task_revision=1)

        self.assertEqual(repeated.intent, "stale_action")

    def test_active_a2_bare_question_number_clarifies_candidate_namespace(self):
        session_id = "a3-active-namespace"
        self.a2.candidate_count = 2
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "第2题")

        self.assertEqual(response.intent, "a3_namespace_clarification")
        self.assertIn("候选 2", response.text)
        self.assertIn("图片中的第 2 道题", response.text)
        self.assertEqual(self.a2.text_calls, [])
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["phase"],
            A3_PHASE_A2_ACTIVE,
        )

    def test_active_a2_explicit_candidate_stays_in_a2(self):
        session_id = "a3-active-candidate"
        self.a2.candidate_count = 2
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "候选 2")

        self.assertEqual(response.intent, "select_candidate")
        self.assertEqual(self.a2.text_calls, [(session_id, "候选 2")])

    def test_active_a2_explicit_image_question_switches_units(self):
        session_id = "a3-active-image-question"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "图片第1题")

        self.assertEqual(response.intent, "a3_unit_selected")
        self.assertEqual(self.a2.text_calls, [])
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["selected_unit"]["unit_id"],
            "g1-u1",
        )

    def test_multiple_candidates_name_original_question(self):
        session_id = "a3-multiple-candidates"
        self.a2.candidate_count = 3
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u1")

        response = self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        self.assertEqual(
            response.text,
            "我从题库里找到了与「四-1」相似的 3 道题，已按相似度排序。",
        )

    def test_active_a2_chapter_number_is_forwarded_instead_of_switching_units(self):
        session_id = "a3-active-chapter"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )
        self.a2.sessions[session_id]["phase"] = "WAIT_CHAPTER"

        response = self.runtime.handle_text(session_id, "2静定结构")

        self.assertEqual(response.intent, "set_chapter")
        self.assertEqual(self.a2.text_calls, [(session_id, "2静定结构")])
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_A2_ACTIVE)
        self.assertEqual(a3["selected_unit"]["unit_id"], "g1-u2")

    def test_active_a2_ambiguous_switch_request_requires_one_unit(self):
        session_id = "a3-active-ambiguous-switch"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "我想搜第1题和第2题")

        self.assertEqual(response.intent, "a3_unit_clarification")
        self.assertEqual(self.a2.text_calls, [])
        self.assertEqual(
            self.runtime.session_snapshot(session_id)["a3"]["phase"],
            A3_PHASE_A2_ACTIVE,
        )

    def test_fastapi_exposes_selection_crop_and_session_contract(self):
        client = TestClient(create_app(runtime=self.runtime, incoming_dir=self.root / "incoming"))
        uploaded = client.post("/api/image", files={"file": ("page.jpg", self.source.read_bytes(), "image/jpeg")})
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["session"]["a3"]["phase"], A3_PHASE_WAIT_SELECTION)

        selected = client.post("/api/a3/select", json={"unit_id": "g1-u1", "task_revision": 1})
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["session"]["a3"]["phase"], A3_PHASE_CROP_REQUIRED)

        cropped = client.post(
            "/api/a3/crop/stream",
            json={
                "bounds": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                "unit_id": "g1-u1",
                "task_revision": 1,
            },
        )
        events = [json.loads(line) for line in cropped.text.splitlines() if line]
        self.assertEqual(events[-1]["type"], "result")
        crop_data = events[-1]["data"]
        self.assertEqual(crop_data["session"]["a3"]["phase"], A3_PHASE_A2_ACTIVE)
        self.assertTrue(crop_data["submitted_crop"].startswith("/api/media/"))
        self.assertEqual(client.get(crop_data["submitted_crop"]).status_code, 200)

        switched = client.post(
            "/api/a3/select",
            json={"unit_id": "g1-u2", "task_revision": 1},
        )
        self.assertEqual(switched.status_code, 200)
        switched_a3 = switched.json()["session"]["a3"]
        self.assertEqual(switched_a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(switched_a3["selected_unit"]["unit_id"], "g1-u2")
        self.assertEqual(client.get(crop_data["submitted_crop"]).status_code, 200)

        next_page = client.post(
            "/api/image",
            files={"file": ("next.jpg", self.source.read_bytes(), "image/jpeg")},
        )
        self.assertEqual(next_page.status_code, 200)
        self.assertEqual(client.get(crop_data["submitted_crop"]).status_code, 200)

    def test_fastapi_exposes_auto_prepare_overlay_and_direct_a2_stream(self):
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "auto-api.sqlite3"),
            artifacts=SessionArtifacts(self.root / "auto-api-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(second_status="auto_ready"),
            external_load_screen=lambda _path: "yes",
        )
        client = TestClient(create_app(runtime=runtime, incoming_dir=self.root / "auto-api-incoming"))
        uploaded = client.post(
            "/api/image",
            files={"file": ("page.jpg", self.source.read_bytes(), "image/jpeg")},
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["intent"], "a3_auto_crops_ready")
        self.assertEqual(client.get("/api/a3/overlay").status_code, 200)
        self.assertEqual(client.get("/api/a3/crop/g1-u1").status_code, 200)

        prepared = client.post(
            "/api/a3/prepare/stream",
            json={"unit_ids": ["g1-u1", "g1-u2"], "task_revision": 1},
        )
        events = [json.loads(line) for line in prepared.text.splitlines() if line]
        self.assertEqual(events[-1]["type"], "result")
        units = events[-1]["data"]["session"]["a3"]["units"]
        self.assertTrue(all(unit["validation_status"] == "auto_ready" for unit in units))

        selected = client.post(
            "/api/a3/select/stream",
            json={"unit_id": "g1-u2", "task_revision": 1},
        )
        select_events = [json.loads(line) for line in selected.text.splitlines() if line]
        self.assertEqual(select_events[-1]["type"], "result")
        select_data = select_events[-1]["data"]
        self.assertEqual(select_data["intent"], "search_image")
        self.assertEqual(select_data["session"]["a3"]["phase"], A3_PHASE_A2_ACTIVE)
        self.assertTrue(select_data["submitted_crop"].startswith("/api/media/"))

    def test_fastapi_upload_stream_auto_prepares_all_before_selection(self):
        runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "auto-upload-api.sqlite3"),
            artifacts=SessionArtifacts(self.root / "auto-upload-api-sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            auto_cropper=FakeAutoCropper(second_status="auto_ready"),
            auto_prepare_all_units=True,
            external_load_screen=lambda _path: "yes",
        )
        client = TestClient(
            create_app(runtime=runtime, incoming_dir=self.root / "auto-upload-api-incoming")
        )

        uploaded = client.post(
            "/api/image/stream",
            files={"file": ("page.jpg", self.source.read_bytes(), "image/jpeg")},
        )

        upload_events = [json.loads(line) for line in uploaded.text.splitlines() if line]
        self.assertEqual(upload_events[-1]["type"], "result")
        upload_data = upload_events[-1]["data"]
        self.assertEqual(upload_data["intent"], "a3_units_prepared")
        a3 = upload_data["session"]["a3"]
        self.assertTrue(a3["auto_prepare_all_units"])
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["requested_unit_ids"], ["g1-u1", "g1-u2"])
        self.assertTrue(all(unit["validation_status"] == "auto_ready" for unit in a3["units"]))

        selected = client.post(
            "/api/a3/select/stream",
            json={"unit_id": "g1-u1", "task_revision": 1},
        )
        select_events = [json.loads(line) for line in selected.text.splitlines() if line]
        self.assertEqual(select_events[-1]["data"]["intent"], "search_image")

    def test_uploading_next_page_keeps_previous_upload_available(self):
        client = TestClient(create_app(runtime=self.runtime, incoming_dir=self.root / "incoming"))
        first = client.post(
            "/api/image",
            files={"file": ("first.jpg", self.source.read_bytes(), "image/jpeg")},
        )
        first_url = first.json()["uploaded_image"]

        second = client.post(
            "/api/image",
            files={"file": ("second.jpg", self.source.read_bytes(), "image/jpeg")},
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(client.get(first_url).status_code, 200)
        self.assertTrue(self.a2.clear_calls[-1][1])

    def test_page_observer_returns_parser_error_to_bounded_schema_retry(self):
        invalid = _page_payload()
        invalid["groups"].append({
            "group_id": "empty-group",
            "parent_question_label": "",
            "parent_title_text": "",
            "shared_stem_text": "",
            "units": [],
        })
        invalid["diagrams"].append({
            "diagram_id": "empty-group-diagram",
            "role": "dimension_or_annotation",
            "group_id": "empty-group",
            "unit_ids": [],
            "status": "clear",
            "evidence": "empty group reference",
        })
        outputs = [json.dumps(invalid, ensure_ascii=False), json.dumps(_page_payload(), ensure_ascii=False)]

        class RetryObserver(QwenA3PageObserver):
            def __init__(self):
                super().__init__(max_attempts=2)
                self.requests = []

            def _call(self, **kwargs):
                self.requests.append(kwargs["user_content"][-1]["text"])
                return outputs[len(self.requests) - 1]

        observer = RetryObserver()
        failures = []

        result = observer.observe_with_diagnostics(
            self.source,
            on_validation_error=lambda attempt, exc: failures.append((attempt, exc)),
        )

        self.assertEqual(len(result.searchable_units), 2)
        self.assertEqual(len(observer.requests), 2)
        self.assertIn("empty groups are not allowed", observer.requests[1])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], 1)
        self.assertEqual(failures[0][1].code, "empty_group")

    def test_recovered_page_schema_failure_is_persisted(self):
        class RecoveringObserver:
            def observe_with_diagnostics(self, _image_path, *, on_validation_error):
                on_validation_error(
                    1,
                    A3PageParseError(
                        "diagram unit_ids must reference existing units",
                        code="invalid_reference",
                    ),
                )
                return parse_a3_page_understanding(_page_payload())

        self.runtime.page_observer = RecoveringObserver()

        response = self.runtime.handle_image("recovered-page-session", self.source)

        self.assertEqual(response.intent, "a3_page_ready")
        events = self.runtime.store.recent_page_errors()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["task_kind"],
            "a3_page_understanding_schema_attempt_1",
        )
        self.assertEqual(events[0]["error_code"], "invalid_reference")

    def test_page_schema_failure_persists_specific_diagnostic(self):
        class FailingObserver:
            def observe(self, _image_path):
                try:
                    raise A3PageParseError(
                        "diagram unit_ids must reference existing units",
                        code="invalid_reference",
                    )
                except A3PageParseError as exc:
                    raise A3ModelError("invalid A3 page understanding output") from exc

        self.runtime.page_observer = FailingObserver()

        response = self.runtime.handle_image("page-error-session", self.source)

        self.assertEqual(response.intent, "a3_page_error")
        state = self.runtime.store.load("page-error-session")
        self.assertIsNotNone(state)
        self.assertEqual(state.last_error, "A3ModelError")
        self.assertIn("invalid_reference", state.last_error_detail)
        self.assertIn("diagram unit_ids", state.last_error_detail)

        events = self.runtime.store.recent_page_errors()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["task_kind"], "a3_page_understanding")
        self.assertEqual(events[0]["error_type"], "A3ModelError")
        self.assertEqual(events[0]["error_code"], "invalid_reference")
        self.assertIn("A3PageParseError", events[0]["error_message"])

        self.runtime.store.clear("page-error-session")
        self.assertEqual(len(self.runtime.store.recent_page_errors()), 1)


if __name__ == "__main__":
    unittest.main()

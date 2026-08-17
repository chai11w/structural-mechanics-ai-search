import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent.a3_models import A3UnitAnalysis, CropCompareResult, QwenA3PageObserver
from tiku_agent.a3_page_parser import parse_a3_page_understanding
from tiku_agent.a3_runtime import (
    A3MvpRuntime,
    A3_PHASE_A2_ACTIVE,
    A3_PHASE_CROP_REQUIRED,
    A3_PHASE_WAIT_SELECTION,
    SQLiteA3SessionStore,
)
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import create_app
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


class FakeObserver:
    def observe(self, _image_path: Path):
        return parse_a3_page_understanding(_page_payload())


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
    def verify(self, _page, _crop, selected, _understanding):
        return CropCompareResult(
            selected_unit_id=selected["unit_id"],
            verdict=self.verdict,
            checks=dict(self.checks),
        )


class FakeAnalyzer:
    def analyze(self, _crop_path: Path, _context_text: str):
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
        self.preanalyzed_calls = []
        self.text_calls = []

    def handle_preanalyzed_image(self, session_id, image_path, **kwargs):
        self.preanalyzed_calls.append((session_id, Path(image_path), kwargs))
        self.sessions[session_id] = {
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 1,
            "candidate_generation": "1:1",
            "candidate_count": 1,
            "chapter": kwargs.get("chapter", ""),
            "search_id": "search_a2runtime",
        }
        return AgentResponse(
            text="我从题库里找到了最相似的一道题。",
            state={"phase": "WAIT_CANDIDATE_CHOICE"},
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

    def clear(self, session_id):
        self.sessions.pop(session_id, None)

    def purge_expired(self):
        return None

    def persist_media(self, _session_id, _source):
        return None

    def resolve_media(self, _session_id, _filename):
        return None

    def record_protocol_event(self, *_args, **_kwargs):
        return None


class A3RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "page.jpg"
        Image.new("RGB", (800, 600), "white").save(self.source)
        self.verifier = FakeVerifier()
        self.a2 = FakeA2Runtime()
        self.runtime = A3MvpRuntime(
            store=SQLiteA3SessionStore(self.root / "a3.sqlite3"),
            artifacts=SessionArtifacts(self.root / "sessions"),
            a2_runtime=self.a2,
            page_observer=FakeObserver(),
            crop_verifier=self.verifier,
            unit_analyzer=FakeAnalyzer(),
        )

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
        self.assertEqual(self.runtime.session_snapshot(session_id)["a3"]["phase"], A3_PHASE_A2_ACTIVE)
        _session, crop_path, kwargs = self.a2.preanalyzed_calls[0]
        with Image.open(crop_path) as crop_image:
            self.assertEqual(crop_image.size, (400, 300))
        self.assertIn("用力法计算图示结构。", kwargs["context_text"])
        self.assertIn("试作图示刚架的 M 图。", kwargs["context_text"])
        self.assertIn("子题 2 条件", kwargs["context_text"])
        self.assertNotIn("10 kN", kwargs["context_text"])
        self.assertEqual(kwargs["chapter"], "4力法")

        answered = self.runtime.handle_text(session_id, "选择候选 1")
        self.assertIn("还有 1 道", answered.text)
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["completed_unit_ids"], ["g1-u2"])

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

    def test_active_a2_reselect_request_returns_to_page_units(self):
        session_id = "a3-active-reselect"
        self.runtime.handle_image(session_id, self.source)
        self.runtime.select_unit(session_id, "g1-u2")
        self.runtime.handle_crop(
            session_id,
            {"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7},
        )

        response = self.runtime.handle_text(session_id, "换个题吧")

        self.assertEqual(response.intent, "a3_reselect")
        self.assertIn("还有 2 道", response.text)
        self.assertEqual(self.a2.text_calls, [])
        a3 = self.runtime.session_snapshot(session_id)["a3"]
        self.assertEqual(a3["phase"], A3_PHASE_WAIT_SELECTION)
        self.assertEqual(a3["selected_unit"]["unit_id"], "")

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
        self.assertEqual(events[-1]["data"]["session"]["a3"]["phase"], A3_PHASE_A2_ACTIVE)

        switched = client.post(
            "/api/a3/select",
            json={"unit_id": "g1-u2", "task_revision": 1},
        )
        self.assertEqual(switched.status_code, 200)
        switched_a3 = switched.json()["session"]["a3"]
        self.assertEqual(switched_a3["phase"], A3_PHASE_CROP_REQUIRED)
        self.assertEqual(switched_a3["selected_unit"]["unit_id"], "g1-u2")

    def test_page_observer_returns_parser_error_to_bounded_schema_retry(self):
        invalid = _page_payload()
        invalid["groups"].append({
            "group_id": "empty-group",
            "parent_question_label": "",
            "parent_title_text": "",
            "shared_stem_text": "",
            "units": [],
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

        result = observer.observe(self.source)

        self.assertEqual(len(result.searchable_units), 2)
        self.assertEqual(len(observer.requests), 2)
        self.assertIn("empty groups are not allowed", observer.requests[1])


if __name__ == "__main__":
    unittest.main()

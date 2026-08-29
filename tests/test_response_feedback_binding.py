from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from tiku_admin.reporting import _feedback_summary
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE, create_app
from tiku_agent.feedback_store import FEEDBACK_SCHEMA_VERSION, SQLiteFeedbackStore
from tiku_agent.session_artifacts import session_key
from tiku_agent.session_runtime import AgentProtocolError, SessionResponseSnapshotV1
from tiku_agent.task_state_contract import empty_task_state_snapshot
from tiku_shared.response_store import (
    ResponseProjection,
    ResponseStoreError,
    SQLiteResponseStore,
)


class ResponseBindingRuntime:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, object]] = {}

    def _snapshot(self, session_id: str) -> dict[str, object]:
        return self.snapshots.setdefault(session_id, {
            "session_valid": False,
            "phase": "IDLE",
            "has_active_image": False,
            "task_revision": 0,
            "candidate_generation": "",
            "candidate_count": 0,
            "chapter": "",
            "search_id": "",
            "workflow_search_id": "",
            "image_route": "",
        })

    @staticmethod
    def _freeze_response(
        response: AgentResponse,
        snapshot: dict[str, object],
        *,
        task_state_capabilities=None,
    ) -> AgentResponse:
        frozen = json.loads(json.dumps(snapshot, ensure_ascii=False))
        response.response_snapshot = frozen
        response.response_projection_snapshot = json.loads(
            json.dumps(frozen, ensure_ascii=False)
        )
        if task_state_capabilities is not None:
            response.response_task_state_snapshot = empty_task_state_snapshot()
        response.response_media_snapshot_captured = True
        return response

    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        identity_key: str = "",
        progress=None,
        task_state_capabilities=None,
    ) -> AgentResponse:
        del identity_key
        if progress is not None:
            progress("searching", "正在检索题库…")
        snapshot = self._snapshot(session_id)
        if text == "A3父任务":
            snapshot.update({
                "session_valid": True,
                "phase": "WAIT_UNIT_SELECTION",
                "has_active_image": True,
                "task_revision": 7,
                "candidate_count": 3,
                "chapter": "4力法",
                "search_id": "search_workflow_page_01",
                "workflow_search_id": "search_workflow_page_01",
                "image_route": "A3",
                "a3": {
                    "enabled": True,
                    "phase": "WAIT_UNIT_SELECTION",
                    "task_revision": 7,
                    "units": [
                        {"unit_id": "g1-u1", "page_index": 1},
                        {"unit_id": "g1-u2", "page_index": 2},
                        {"unit_id": "g1-u3", "page_index": 3},
                    ],
                },
            })
            return self._freeze_response(
                AgentResponse(text="已准备 3 道题。", intent="a3_units_prepared"),
                snapshot,
                task_state_capabilities=task_state_capabilities,
            )
        if text == "A3子题":
            snapshot.update({
                "session_valid": True,
                "phase": "WAIT_CANDIDATE_CHOICE",
                "has_active_image": True,
                "task_revision": 7,
                "candidate_count": 5,
                "chapter": "4力法",
                "search_id": "search_question_g1_u2_01",
                "workflow_search_id": "search_workflow_page_01",
                "image_route": "A3",
                "a3": {
                    "enabled": True,
                    "phase": "A2_ACTIVE",
                    "task_revision": 7,
                    "selected_unit": {"unit_id": "g1-u2", "page_index": 2},
                },
            })
            return self._freeze_response(
                AgentResponse(text="找到 5 道候选题。", intent="show_candidates"),
                snapshot,
                task_state_capabilities=task_state_capabilities,
            )

        old = text == "旧回复"
        snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 1 if old else 2,
            "candidate_count": 2 if old else 9,
            "chapter": "4力法" if old else "5位移法",
            "search_id": "search_old_reply_01" if old else "search_new_reply_02",
            "workflow_search_id": "search_history_workflow_01",
            "image_route": "A2",
            "a3": {"enabled": False},
        })
        return self._freeze_response(
            AgentResponse(
                text="这是旧回复。" if old else "这是新回复。",
                intent="show_candidates",
            ),
            snapshot,
            task_state_capabilities=task_state_capabilities,
        )

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        return json.loads(json.dumps(self._snapshot(session_id), ensure_ascii=False))

    def current_image_path(self, session_id: str):
        del session_id
        return None

    def session_response_snapshot_v1(
        self,
        session_id: str,
        *,
        capabilities=None,
        response_frozen: bool = False,
    ):
        del capabilities, response_frozen
        return SessionResponseSnapshotV1(
            uploaded_image_path=None,
            legacy_session=self.session_snapshot(session_id),
            task_state=empty_task_state_snapshot(),
        )

    def persist_media(self, session_id: str, source: Path):
        del session_id, source
        return None

    def resolve_media(self, session_id: str, filename: str):
        del session_id, filename
        return None

    def resolve_upload(self, session_id: str, filename: str):
        del session_id, filename
        return None

    def clear(self, session_id: str) -> None:
        self.snapshots.pop(session_id, None)


class FailingResponseStore(SQLiteResponseStore):
    def finalize(self, projection: ResponseProjection, *, cancelled=None):
        self.finalize_calls = getattr(self, "finalize_calls", 0) + 1
        del projection, cancelled
        raise ResponseStoreError("response database unavailable")


class ProtocolErrorRuntime(ResponseBindingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.handled_session_ids: list[str] = []

    def handle_text(
        self,
        session_id: str,
        text: str,
        *,
        identity_key: str = "",
        progress=None,
        task_state_capabilities=None,
    ) -> AgentResponse:
        if text not in {"JSON错误", "流错误"}:
            return super().handle_text(
                session_id,
                text,
                identity_key=identity_key,
                progress=progress,
                task_state_capabilities=task_state_capabilities,
            )
        del identity_key, progress, task_state_capabilities
        self.handled_session_ids.append(session_id)
        is_stream = text == "流错误"
        snapshot = self._snapshot(session_id)
        snapshot.update({
            "session_valid": True,
            "phase": "WAIT_CANDIDATE_CHOICE",
            "has_active_image": True,
            "task_revision": 32 if is_stream else 31,
            "candidate_generation": "generation_stream" if is_stream else "generation_json",
            "candidate_count": 6 if is_stream else 4,
            "chapter": "5位移法" if is_stream else "4力法",
            "search_id": "search_error_stream_01" if is_stream else "search_error_json_01",
            "workflow_search_id": (
                "search_error_stream_workflow_01"
                if is_stream
                else "search_error_json_workflow_01"
            ),
            "image_route": "A2",
            "a3": {"enabled": False},
        })
        error = AgentProtocolError("internal detail must stay private", code="AGENT_FAILED")
        error.response_snapshot = json.loads(
            json.dumps(snapshot, ensure_ascii=False)
        )
        raise error


class ResponseFeedbackBindingTest(unittest.TestCase):
    def make_directory(self) -> Path:
        directory = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"response_feedback_{uuid4().hex}"
        )
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return directory

    def make_app(self):
        directory = self.make_directory()
        response_store = SQLiteResponseStore(directory / "responses.sqlite3")
        feedback_store = SQLiteFeedbackStore(directory / "feedback.sqlite3")
        app = create_app(
            runtime=ResponseBindingRuntime(),
            response_store=response_store,
            feedback_store=feedback_store,
        )
        return app, response_store, feedback_store

    @staticmethod
    def result_event(response) -> dict[str, object]:
        events = [json.loads(line) for line in response.text.splitlines() if line]
        results = [event["data"] for event in events if event.get("type") == "result"]
        if len(results) != 1:
            raise AssertionError(f"expected one result event, got {events!r}")
        return results[0]

    @staticmethod
    def error_event(response) -> dict[str, object]:
        events = [json.loads(line) for line in response.text.splitlines() if line]
        errors = [event for event in events if event.get("type") == "error"]
        if len(errors) != 1:
            raise AssertionError(f"expected one error event, got {events!r}")
        return errors[0]

    @staticmethod
    def feedback_payload(response_id: str, *, message_id: str) -> dict[str, object]:
        return {
            "message_id": message_id,
            "rated_response_id": response_id,
            "rating": "positive",
            "tags": ["found_answer"],
            "detail": "反馈绑定测试",
            "conversation": [
                {
                    "me": False,
                    "message": "目标回复",
                    "messageId": message_id,
                    "responseId": response_id,
                }
            ],
        }

    @staticmethod
    def projection(
        *,
        identity_key: str,
        owner_session_key: str,
        search_id: str,
    ) -> ResponseProjection:
        return ResponseProjection(
            trace_id=f"trace_{uuid4().hex}",
            identity_key=identity_key,
            session_key=owner_session_key,
            request_id=f"req_{uuid4().hex}",
            status="SUCCESS",
            layer="tool",
            code="REQUEST_SUCCEEDED",
            workflow_search_id="search_owner_workflow_01",
            search_id=search_id,
            phase="WAIT_CANDIDATE_CHOICE",
            task_revision=1,
            candidate_count=2,
            chapter="4力法",
            image_route="A2",
            intent="show_candidates",
        )

    def test_json_and_stream_ids_are_committed_with_a3_parent_child_dimensions(self):
        app, response_store, _feedback_store = self.make_app()
        client = TestClient(app)

        parent_response = client.post("/api/message", json={"text": "A3父任务"})
        self.assertEqual(parent_response.status_code, 200, parent_response.text)
        parent_payload = parent_response.json()
        self.assertEqual(
            parent_payload["task_state"],
            empty_task_state_snapshot().to_dict(),
        )
        self.assertRegex(parent_payload["response_id"], r"^resp_[0-9a-f]{32}$")
        parent_record = response_store.get(parent_payload["response_id"])
        self.assertIsNotNone(parent_record)
        self.assertEqual(parent_record.response_mode, "json")
        self.assertEqual(parent_record.workflow_search_id, "search_workflow_page_01")
        self.assertEqual(parent_record.search_id, "")
        self.assertEqual(parent_record.unit_id, "")
        self.assertEqual(parent_record.task_revision, 7)
        self.assertEqual(parent_record.image_route, "A3")

        child_response = client.post(
            "/api/message/stream", json={"text": "A3子题"}
        )
        self.assertEqual(child_response.status_code, 200, child_response.text)
        child_payload = self.result_event(child_response)
        self.assertNotIn("task_state", child_payload)
        self.assertRegex(child_payload["response_id"], r"^resp_[0-9a-f]{32}$")
        self.assertNotEqual(child_payload["response_id"], parent_payload["response_id"])
        child_record = response_store.get(child_payload["response_id"])
        self.assertIsNotNone(child_record)
        self.assertEqual(child_record.response_mode, "stream")
        self.assertEqual(child_record.workflow_search_id, "search_workflow_page_01")
        self.assertEqual(child_record.search_id, "search_question_g1_u2_01")
        self.assertEqual(child_record.unit_id, "g1-u2")
        self.assertEqual(child_record.task_revision, 7)
        self.assertEqual(child_record.candidate_count, 5)
        self.assertEqual(child_record.identity_key, "local")
        self.assertEqual(
            child_record.session_key,
            session_key(client.cookies.get(SESSION_COOKIE)),
        )
        self.assertEqual(child_record.request_id, child_payload["request_id"])

    def test_reused_request_id_still_creates_distinct_trace_bound_responses(self):
        app, response_store, _feedback_store = self.make_app()
        client = TestClient(app)
        request_id = "req_0123456789abcdef0123456789abcdef"
        headers = {"X-Request-ID": request_id}

        first = client.post("/api/message", json={"text": "旧回复"}, headers=headers)
        second = client.post("/api/message", json={"text": "旧回复"}, headers=headers)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        first_payload = first.json()
        second_payload = second.json()
        self.assertEqual(first_payload["request_id"], request_id)
        self.assertEqual(second_payload["request_id"], request_id)
        self.assertNotEqual(first_payload["response_id"], second_payload["response_id"])
        first_record = response_store.get(first_payload["response_id"])
        second_record = response_store.get(second_payload["response_id"])
        self.assertIsNotNone(first_record)
        self.assertIsNotNone(second_record)
        self.assertEqual(first_record.request_id, request_id)
        self.assertEqual(second_record.request_id, request_id)
        self.assertNotEqual(first_record.trace_id, second_record.trace_id)

    def test_json_and_stream_protocol_errors_are_committed_and_rateable(self):
        directory = self.make_directory()
        runtime = ProtocolErrorRuntime()
        response_store = SQLiteResponseStore(directory / "responses.sqlite3")
        feedback_store = SQLiteFeedbackStore(directory / "feedback.sqlite3")
        app = create_app(
            runtime=runtime,
            response_store=response_store,
            feedback_store=feedback_store,
        )
        client = TestClient(app)

        json_response = client.post("/api/message", json={"text": "JSON错误"})
        self.assertEqual(json_response.status_code, 500, json_response.text)
        json_payload = json_response.json()
        self.assertEqual(
            json_payload["task_state"]["consistency"],
            {
                "status": "INCONSISTENT",
                "codes": ["CHILD_STATE_UNREADABLE"],
            },
        )
        self.assertRegex(json_payload["response_id"], r"^resp_[0-9a-f]{32}$")
        json_record = response_store.get(json_payload["response_id"])
        self.assertIsNotNone(json_record)
        self.assertEqual(json_record.request_id, json_payload["request_id"])
        self.assertEqual(json_record.status, "ERROR")
        self.assertEqual(json_record.layer, "tool")
        self.assertEqual(json_record.code, "AGENT_FAILED")
        self.assertTrue(json_record.retryable)
        self.assertEqual(json_record.action, "retry_search")
        self.assertEqual(json_record.workflow_search_id, "search_error_json_workflow_01")
        self.assertEqual(json_record.search_id, "search_error_json_01")
        self.assertEqual(json_record.phase, "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(json_record.task_revision, 31)
        self.assertEqual(json_record.candidate_count, 4)
        self.assertEqual(json_record.chapter, "4力法")
        self.assertEqual(json_record.image_route, "A2")
        self.assertEqual(json_record.intent, "request_error")
        self.assertEqual(json_record.response_mode, "json")

        json_feedback = client.post(
            "/api/feedback",
            json=self.feedback_payload(
                json_payload["response_id"],
                message_id="message_json_error_01",
            ),
        )
        self.assertEqual(json_feedback.status_code, 200, json_feedback.text)
        self.assertEqual(
            json_feedback.json()["feedback"]["rated_response_id"],
            json_payload["response_id"],
        )
        self.assertNotIn("response_id", json_feedback.json())

        stream_response = client.post(
            "/api/message/stream", json={"text": "流错误"}
        )
        self.assertEqual(stream_response.status_code, 200, stream_response.text)
        stream_payload = self.error_event(stream_response)
        self.assertNotIn("task_state", stream_payload)
        self.assertRegex(stream_payload["response_id"], r"^resp_[0-9a-f]{32}$")
        stream_record = response_store.get(stream_payload["response_id"])
        self.assertIsNotNone(stream_record)
        self.assertEqual(stream_record.request_id, stream_payload["request_id"])
        self.assertEqual(stream_record.status, "ERROR")
        self.assertEqual(stream_record.layer, "tool")
        self.assertEqual(stream_record.code, "AGENT_FAILED")
        self.assertTrue(stream_record.retryable)
        self.assertEqual(stream_record.action, "retry_search")
        self.assertEqual(
            stream_record.workflow_search_id,
            "search_error_stream_workflow_01",
        )
        self.assertEqual(stream_record.search_id, "search_error_stream_01")
        self.assertEqual(stream_record.phase, "WAIT_CANDIDATE_CHOICE")
        self.assertEqual(stream_record.task_revision, 32)
        self.assertEqual(stream_record.candidate_count, 6)
        self.assertEqual(stream_record.chapter, "5位移法")
        self.assertEqual(stream_record.image_route, "A2")
        self.assertEqual(stream_record.intent, "request_error")
        self.assertEqual(stream_record.response_mode, "stream")

        stream_feedback = client.post(
            "/api/feedback",
            json=self.feedback_payload(
                stream_payload["response_id"],
                message_id="message_stream_error_01",
            ),
        )
        self.assertEqual(stream_feedback.status_code, 200, stream_feedback.text)
        self.assertEqual(
            stream_feedback.json()["feedback"]["rated_response_id"],
            stream_payload["response_id"],
        )
        self.assertEqual(
            {item.rated_response_id for item in feedback_store.list_feedback()},
            {json_payload["response_id"], stream_payload["response_id"]},
        )

    def test_first_json_error_uses_one_session_for_runtime_cookie_and_record(self):
        directory = self.make_directory()
        runtime = ProtocolErrorRuntime()
        response_store = SQLiteResponseStore(directory / "responses.sqlite3")
        app = create_app(
            runtime=runtime,
            response_store=response_store,
            feedback_store=SQLiteFeedbackStore(directory / "feedback.sqlite3"),
        )
        client = TestClient(app)
        self.assertIsNone(client.cookies.get(SESSION_COOKIE))

        response = client.post("/api/message", json={"text": "JSON错误"})

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(len(runtime.handled_session_ids), 1)
        runtime_session_id = runtime.handled_session_ids[0]
        cookie_session_id = client.cookies.get(SESSION_COOKIE)
        self.assertEqual(cookie_session_id, runtime_session_id)
        self.assertIn(
            f"{SESSION_COOKIE}={runtime_session_id}",
            response.headers.get("set-cookie", ""),
        )
        record = response_store.get(response.json()["response_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record.session_key, session_key(runtime_session_id))
        self.assertEqual(record.session_key, session_key(cookie_session_id))

    def test_non_rateable_api_paths_never_return_response_ids(self):
        app, _response_store, _feedback_store = self.make_app()
        client = TestClient(app)

        json_responses = (
            client.get("/api/session"),
            client.post("/api/reset"),
            client.post("/api/feedback", json={}),
            client.get("/api/media/missing.jpg"),
            client.get("/api/upload/missing.jpg"),
        )

        for response in json_responses:
            self.assertNotIn("response_id", response.json(), response.text)

        invite_login = client.post(
            "/api/invite/login", data={"code": "invalid"}
        )
        self.assertTrue(
            invite_login.headers["content-type"].startswith("text/html"),
            invite_login.headers["content-type"],
        )
        self.assertNotIn("response_id", invite_login.text)

    def test_feedback_without_verified_conversation_target_is_rejected(self):
        app, _response_store, _feedback_store = self.make_app()
        client = TestClient(app)
        response_id = client.post("/api/message", json={"text": "旧回复"}).json()[
            "response_id"
        ]

        missing_target = client.post(
            "/api/feedback",
            json={
                "message_id": "message_unverified_01",
                "rated_response_id": response_id,
                "rating": "positive",
                "tags": ["found_answer"],
                "detail": "不应绕过目标校验",
            },
        )
        self.assertEqual(missing_target.status_code, 400)
        self.assertEqual(missing_target.json()["code"], "FEEDBACK_INVALID")

        mismatched_target = client.post(
            "/api/feedback",
            json={
                "message_id": "message_unverified_02",
                "rated_response_id": response_id,
                "rating": "positive",
                "tags": ["found_answer"],
                "detail": "不应重绑消息",
                "conversation": [
                    {
                        "me": False,
                        "messageId": "message_unverified_02",
                        "responseId": f"resp_{'a' * 32}",
                    }
                ],
            },
        )
        self.assertEqual(mismatched_target.status_code, 400)
        self.assertEqual(mismatched_target.json()["code"], "FEEDBACK_INVALID")

    def test_feedback_can_target_older_reply_and_uses_only_stored_projection(self):
        app, response_store, feedback_store = self.make_app()
        client = TestClient(app)
        old_payload = client.post("/api/message", json={"text": "旧回复"}).json()
        new_payload = client.post("/api/message", json={"text": "新回复"}).json()
        old_record = response_store.get(old_payload["response_id"])
        self.assertIsNotNone(old_record)

        payload = self.feedback_payload(
            old_payload["response_id"], message_id="message_old_reply_01"
        )
        payload["conversation"].insert(0, {
            "me": True,
            "message": "我发了一张题图。",
            "taskRevision": old_record.task_revision,
            "createdAt": 1_000,
        })
        payload["conversation"][-1].update({
            "taskRevision": old_record.task_revision,
            "candidateCount": old_record.candidate_count,
            "createdAt": 2_500,
        })
        payload.update({
            "task_revision": 999,
            "candidate_count": 999,
            "search_id": "search_spoofed",
            "workflow_search_id": "search_spoofed_workflow",
            "status": "ERROR",
            "layer": "feedback",
            "code": "AGENT_FAILED",
            "chapter": "伪造章节",
            "image_route": "A3",
            "intent": "a3_units_prepared",
            "search_duration_ms": 999999,
        })
        created = client.post("/api/feedback", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(
            created.json()["feedback"]["rated_response_id"],
            old_payload["response_id"],
        )
        saved = feedback_store.list_feedback()[0]
        self.assertEqual(saved.rated_response_id, old_payload["response_id"])
        self.assertEqual(saved.task_revision, old_record.task_revision)
        self.assertEqual(saved.candidate_count, old_record.candidate_count)
        self.assertEqual(saved.search_id, old_record.search_id)
        self.assertEqual(saved.workflow_search_id, old_record.workflow_search_id)
        self.assertEqual(saved.status, old_record.status)
        self.assertEqual(saved.layer, old_record.layer)
        self.assertEqual(saved.code, old_record.code)
        self.assertEqual(saved.chapter, old_record.chapter)
        self.assertEqual(saved.image_route, old_record.image_route)
        self.assertEqual(saved.intent, old_record.intent)
        self.assertEqual(saved.search_duration_ms, 1_500)

        payload.update({"rating": "negative", "tags": ["ranking_issue"]})
        updated = client.post("/api/feedback", json=payload)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(len(feedback_store.list_feedback()), 1)
        self.assertEqual(feedback_store.list_feedback()[0].rating, "negative")

        payload["rated_response_id"] = new_payload["response_id"]
        rebound = client.post("/api/feedback", json=payload)
        self.assertEqual(rebound.status_code, 400)
        self.assertEqual(rebound.json()["code"], "FEEDBACK_INVALID")
        self.assertEqual(len(feedback_store.list_feedback()), 1)

    def test_feedback_hides_forged_cross_session_and_cross_identity_targets(self):
        app, response_store, _feedback_store = self.make_app()
        owner = TestClient(app)
        response_id = owner.post("/api/message", json={"text": "旧回复"}).json()[
            "response_id"
        ]
        base = self.feedback_payload(response_id, message_id="message_owner_01")

        missing = dict(base)
        missing.pop("rated_response_id")
        malformed = {**base, "rated_response_id": "message_owner_01"}
        forged = {**base, "rated_response_id": f"resp_{'f' * 32}"}
        self.assertEqual(owner.post("/api/feedback", json=missing).status_code, 400)
        self.assertEqual(owner.post("/api/feedback", json=malformed).status_code, 400)
        forged_response = owner.post("/api/feedback", json=forged)
        self.assertEqual(forged_response.status_code, 404)

        outsider = TestClient(app)
        outsider.get("/")
        cross_session = outsider.post("/api/feedback", json=base)
        self.assertEqual(cross_session.status_code, 404)

        owner_session_key = session_key(owner.cookies.get(SESSION_COOKIE))
        foreign = response_store.finalize(self.projection(
            identity_key="invite-other",
            owner_session_key=owner_session_key,
            search_id="search_foreign_identity_01",
        ))
        cross_identity = owner.post("/api/feedback", json={
            **base,
            "rated_response_id": foreign.response_id,
        })
        self.assertEqual(cross_identity.status_code, 404)

        for hidden in (forged_response, cross_session, cross_identity):
            self.assertEqual(hidden.json()["code"], "FEEDBACK_INVALID")
            self.assertEqual(hidden.json()["status"], "NEEDS_INPUT")
            self.assertEqual(hidden.json()["layer"], "feedback")
            self.assertEqual(hidden.json()["detail"], forged_response.json()["detail"])

    def test_response_store_failure_never_returns_an_unbound_success(self):
        directory = self.make_directory()
        failing_store = FailingResponseStore(directory / "responses.sqlite3")
        app = create_app(
            runtime=ResponseBindingRuntime(),
            response_store=failing_store,
            feedback_store=SQLiteFeedbackStore(directory / "feedback.sqlite3"),
        )
        client = TestClient(app, raise_server_exceptions=False)

        json_response = client.post("/api/message", json={"text": "旧回复"})
        self.assertEqual(json_response.status_code, 500, json_response.text)
        self.assertEqual(json_response.json()["code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn("response_id", json_response.json())
        self.assertEqual(failing_store.finalize_calls, 1)

        stream_response = client.post(
            "/api/message/stream", json={"text": "新回复"}
        )
        self.assertEqual(stream_response.status_code, 200, stream_response.text)
        events = [json.loads(line) for line in stream_response.text.splitlines() if line]
        errors = [event for event in events if event.get("type") == "error"]
        results = [event for event in events if event.get("type") == "result"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(results, [])
        self.assertEqual(errors[0]["code"], "SERVICE_UNAVAILABLE")
        self.assertNotIn("response_id", errors[0])
        self.assertEqual(failing_store.finalize_calls, 2)

    def test_expired_response_rejects_new_feedback_but_owner_can_remove_old_feedback(self):
        directory = self.make_directory()
        now = [datetime(2026, 8, 26, 9, 0, tzinfo=UTC)]
        response_store = SQLiteResponseStore(
            directory / "responses.sqlite3",
            retention_days=1,
            clock=lambda: now[0],
        )
        feedback_store = SQLiteFeedbackStore(directory / "feedback.sqlite3")
        app = create_app(
            runtime=ResponseBindingRuntime(),
            response_store=response_store,
            feedback_store=feedback_store,
        )
        owner = TestClient(app)
        response_id = owner.post("/api/message", json={"text": "旧回复"}).json()[
            "response_id"
        ]
        payload = self.feedback_payload(response_id, message_id="message_expired_01")
        created = owner.post("/api/feedback", json=payload)
        self.assertEqual(created.status_code, 200, created.text)

        now[0] += timedelta(days=2)
        expired_update = owner.post("/api/feedback", json={
            **payload,
            "rating": "negative",
            "tags": ["ranking_issue"],
        })
        self.assertEqual(expired_update.status_code, 404)
        self.assertEqual(expired_update.json()["code"], "FEEDBACK_INVALID")

        outsider = TestClient(app)
        outsider.get("/")
        self.assertEqual(
            outsider.delete(f"/api/feedback/{response_id}").status_code,
            404,
        )
        removed = owner.delete(f"/api/feedback/{response_id}")
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertTrue(removed.json()["removed"])
        self.assertEqual(feedback_store.list_feedback(), [])

    def test_legacy_feedback_stays_unbound_and_new_bindings_are_unique(self):
        directory = self.make_directory()
        store = SQLiteFeedbackStore(directory / "feedback.sqlite3")
        legacy = store.upsert(
            message_id="message_legacy_01",
            rated_response_id=f"resp_{uuid4().hex}",
            identity_key="local",
            session_key="1" * 64,
            rating="negative",
            tags=("not_found",),
            detail="旧版反馈",
            task_revision=1,
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=0,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute("DROP INDEX idx_feedback_rated_response")
            connection.execute(
                "ALTER TABLE message_feedback DROP COLUMN rated_response_id"
            )
            connection.execute("ALTER TABLE message_feedback DROP COLUMN intent")
            connection.execute(
                "UPDATE message_feedback SET schema_version = 7 WHERE feedback_id = ?",
                (legacy.feedback_id,),
            )

        with sqlite3.connect(store.path) as connection:
            legacy_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(message_feedback)")
            }
        self.assertNotIn("rated_response_id", legacy_columns)
        self.assertNotIn("intent", legacy_columns)

        migrated = store.get_feedback(legacy.feedback_id)
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.rated_response_id, "")
        self.assertEqual(migrated.intent, "")
        summary = _feedback_summary(migrated)
        self.assertTrue(summary["legacy_response_binding"])
        self.assertEqual(summary["rated_response_id"], "")
        with sqlite3.connect(store.path) as connection:
            schema_version = connection.execute(
                "SELECT schema_version FROM message_feedback WHERE feedback_id = ?",
                (legacy.feedback_id,),
            ).fetchone()[0]
        self.assertEqual(schema_version, FEEDBACK_SCHEMA_VERSION)

        response_id = f"resp_{uuid4().hex}"
        first = store.upsert(
            message_id="message_bound_01",
            rated_response_id=response_id,
            identity_key="local",
            session_key="2" * 64,
            rating="positive",
            tags=("found_answer",),
            detail="第一次",
            task_revision=2,
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            intent="show_candidates",
        )
        second = store.upsert(
            message_id="message_bound_01",
            rated_response_id=response_id,
            identity_key="local",
            session_key="2" * 64,
            rating="negative",
            tags=("ranking_issue",),
            detail="更新",
            task_revision=2,
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            intent="show_candidates",
        )
        self.assertEqual(second.feedback_id, first.feedback_id)
        self.assertEqual(len(store.list_feedback()), 2)
        with self.assertRaisesRegex(ValueError, "rated response message id mismatch"):
            store.upsert(
                message_id="message_bound_02",
                rated_response_id=response_id,
                identity_key="local",
                session_key="2" * 64,
                rating="positive",
                tags=("found_answer",),
                detail="错误消息重绑",
                task_revision=2,
                phase="WAIT_CANDIDATE_CHOICE",
                candidate_count=3,
            )
        with self.assertRaisesRegex(ValueError, "message id is already bound"):
            store.upsert(
                message_id="message_bound_01",
                rated_response_id=f"resp_{uuid4().hex}",
                identity_key="local",
                session_key="2" * 64,
                rating="positive",
                tags=("found_answer",),
                detail="错误重绑",
                task_revision=2,
                phase="WAIT_CANDIDATE_CHOICE",
                candidate_count=3,
            )
        self.assertFalse(store.delete_by_response(
            rated_response_id=response_id,
            identity_key="other",
            session_key="2" * 64,
        ))
        self.assertFalse(store.delete_by_response(
            rated_response_id=response_id,
            identity_key="local",
            session_key="3" * 64,
        ))
        self.assertTrue(store.delete_by_response(
            rated_response_id=response_id,
            identity_key="local",
            session_key="2" * 64,
        ))
        self.assertEqual(len(store.list_feedback()), 1)


if __name__ == "__main__":
    unittest.main()

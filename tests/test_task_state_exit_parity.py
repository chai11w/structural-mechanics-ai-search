from __future__ import annotations

import io
import json
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from tiku_agent import task_state_contract as contract
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import SESSION_COOKIE, create_app
from tiku_agent.session_runtime import (
    AgentProtocolError,
    SessionResponseSnapshotError,
    SessionResponseSnapshotV1,
)
from tiku_shared.response_store import ResponseStoreError, SQLiteResponseStore
from tests.test_tiku_agent_fastapi_demo import FakeRuntime


def _a2_snapshot() -> contract.TaskStateSnapshotV1:
    return contract.TaskStateSnapshotV1(
        workflow=contract.empty_task_state_snapshot().workflow,
        active_child_task=contract.ChildTaskStateView(
            task_id="search_parity_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id="",
            task_revision=7,
            phase="WAIT_CANDIDATE_CHOICE",
            status=contract.STATUS_WAITING_USER,
            completed_steps=(
                contract.CHILD_STEP_QUESTION_ACCEPTED,
                contract.CHILD_STEP_QUESTION_ANALYZED,
                contract.CHILD_STEP_CHAPTER_RESOLVED,
                contract.CHILD_STEP_ROUTE_SELECTED,
                contract.CHILD_STEP_SEARCH_COMPLETED,
                contract.CHILD_STEP_CANDIDATES_READY,
            ),
            allowed_actions=(contract.ACTION_SELECT_CANDIDATE,),
            next_stage=contract.NEXT_SELECT_CANDIDATE,
            chapter="2静定结构",
            candidate_count=2,
            candidate_generation="7:1",
        ),
    )


def _a3_snapshot() -> contract.TaskStateSnapshotV1:
    unit = contract.UnitStateView(
        unit_id="g1-u1",
        page_index=1,
        display_label="四-1",
        status=contract.UNIT_ACTIVE,
    )
    return contract.TaskStateSnapshotV1(
        workflow=contract.WorkflowStateView(
            exists=True,
            workflow_id="search_parity_workflow_12345678",
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route="A3",
            task_revision=9,
            phase="A2_ACTIVE",
            status=contract.STATUS_RUNNING,
            completed_steps=(
                contract.WORKFLOW_STEP_IMAGE_ACCEPTED,
                contract.WORKFLOW_STEP_ROUTE_DECIDED,
                contract.WORKFLOW_STEP_PAGE_UNDERSTOOD,
                contract.WORKFLOW_STEP_UNIT_CATALOG_READY,
                contract.WORKFLOW_STEP_UNIT_SELECTED,
                contract.WORKFLOW_STEP_CHILD_STARTED,
            ),
            allowed_actions=(contract.ACTION_CANCEL_CURRENT_UNIT,),
            next_stage=contract.NEXT_FOLLOW_CHILD,
        ),
        active_child_task=contract.ChildTaskStateView(
            task_id="search_parity_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id=unit.unit_id,
            task_revision=7,
            phase="WAIT_CANDIDATE_CHOICE",
            status=contract.STATUS_WAITING_USER,
            completed_steps=(
                contract.CHILD_STEP_QUESTION_ACCEPTED,
                contract.CHILD_STEP_QUESTION_ANALYZED,
                contract.CHILD_STEP_CHAPTER_RESOLVED,
                contract.CHILD_STEP_ROUTE_SELECTED,
                contract.CHILD_STEP_SEARCH_COMPLETED,
                contract.CHILD_STEP_CANDIDATES_READY,
            ),
            allowed_actions=(contract.ACTION_SELECT_CANDIDATE,),
            next_stage=contract.NEXT_SELECT_CANDIDATE,
            chapter="2静定结构",
            candidate_count=2,
            candidate_generation="7:1",
        ),
        current_unit=unit,
        units=(unit,),
    )


def _legacy_snapshot(*, a3_enabled: bool) -> dict[str, object]:
    legacy: dict[str, object] = {
        "session_valid": True,
        "phase": "WAIT_CANDIDATE_CHOICE",
        "has_active_image": True,
        "task_revision": 7,
        "candidate_generation": "7:1",
        "candidate_count": 2,
        "chapter": "2静定结构",
        "search_id": "search_parity_child_12345678",
        "image_route": "A3" if a3_enabled else "A2",
        "a3": None,
    }
    if a3_enabled:
        legacy.update({
            "workflow_search_id": "search_parity_workflow_12345678",
            "a3": {
                "enabled": True,
                "phase": "A2_ACTIVE",
                "task_revision": 9,
                "units": [
                    {
                        "unit_id": "g1-u1",
                        "page_index": 1,
                        "display_label": "四-1",
                        "selected": True,
                        "requested": True,
                        "crop_available": True,
                    }
                ],
                "selected_unit": {
                    "unit_id": "g1-u1",
                    "display_label": "四-1",
                },
            },
        })
    return legacy


class _ExactParityRuntime(FakeRuntime):
    a3_enabled = True

    def __init__(self, image_path: Path):
        super().__init__(image_path)
        self.frozen_task_state = _a3_snapshot()
        self.frozen_legacy = _legacy_snapshot(a3_enabled=True)
        self.fail_operations: set[str] = set()
        self.live_snapshot_reads = 0
        self.combined_capture_calls = 0
        self.capability_calls: list[tuple[str, object]] = []

    def _legacy_copy(self) -> dict[str, object]:
        return json.loads(json.dumps(self.frozen_legacy, ensure_ascii=False))

    def _result(
        self,
        operation: str,
        session_id: str,
        *,
        progress=None,
        task_state_capabilities=None,
    ) -> AgentResponse:
        self.capability_calls.append((operation, task_state_capabilities))
        if progress is not None:
            progress("searching", self.progress_message)
        if operation in self.fail_operations:
            error = AgentProtocolError(
                "private exact parity failure",
                code="UPLOAD_PERSIST_FAILED",
            )
            error.response_snapshot = self._legacy_copy()
            error.response_task_state_snapshot = self.frozen_task_state
            raise error
        response = AgentResponse(
            text=f"{operation} complete",
            intent=(
                "a3_units_prepared"
                if operation == "prepare"
                else "public_response"
            ),
        )
        response.response_snapshot = self._legacy_copy()
        response.response_projection_snapshot = self._legacy_copy()
        response.response_task_state_snapshot = self.frozen_task_state
        response.response_media_snapshot_captured = True
        response.uploaded_image_path = self.image_path
        return response

    def handle_text(self, session_id: str, text: str, **kwargs) -> AgentResponse:
        del text
        return self._result(
            "message",
            session_id,
            progress=kwargs.get("progress"),
            task_state_capabilities=kwargs.get("task_state_capabilities"),
        )

    def handle_image(self, session_id: str, image_path: Path, **kwargs) -> AgentResponse:
        del image_path
        self.upload_session = session_id
        return self._result(
            "image",
            session_id,
            progress=kwargs.get("progress"),
            task_state_capabilities=kwargs.get("task_state_capabilities"),
        )

    def select_unit(self, session_id: str, unit_id: str, **kwargs) -> AgentResponse:
        del unit_id
        return self._result(
            "select",
            session_id,
            progress=kwargs.get("progress"),
            task_state_capabilities=kwargs.get("task_state_capabilities"),
        )

    def prepare_units(self, session_id: str, unit_ids, **kwargs) -> AgentResponse:
        del unit_ids
        return self._result(
            "prepare",
            session_id,
            progress=kwargs.get("progress"),
            task_state_capabilities=kwargs.get("task_state_capabilities"),
        )

    def handle_crop(self, session_id: str, bounds, **kwargs) -> AgentResponse:
        del bounds
        return self._result(
            "crop",
            session_id,
            progress=kwargs.get("progress"),
            task_state_capabilities=kwargs.get("task_state_capabilities"),
        )

    def clear(self, session_id: str, **kwargs) -> None:
        del kwargs
        if "reset" in self.fail_operations:
            error = AgentProtocolError(
                "private reset parity failure",
                code="UPLOAD_PERSIST_FAILED",
            )
            error.response_snapshot = self._legacy_copy()
            error.response_task_state_snapshot = self.frozen_task_state
            raise error
        super().clear(session_id)

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        del session_id
        self.live_snapshot_reads += 1
        raise AssertionError("typed parity exits must not read legacy live state")

    def session_response_snapshot_v1(
        self,
        session_id: str,
        *,
        capabilities=None,
        response_frozen: bool = False,
    ) -> SessionResponseSnapshotV1:
        del session_id, response_frozen
        self.combined_capture_calls += 1
        self.capability_calls.append(("session", capabilities))
        if "session" in self.fail_operations:
            error = SessionResponseSnapshotError(
                "private session parity failure",
                task_state=self.frozen_task_state,
            )
            error.response_snapshot = self._legacy_copy()
            raise error
        return SessionResponseSnapshotV1(
            uploaded_image_path=None,
            legacy_session=self._legacy_copy(),
            task_state=self.frozen_task_state,
        )


class _IncompleteResponseRuntime(FakeRuntime):
    def __init__(self, image_path: Path, *, a3_enabled: bool, parts: frozenset[str]):
        super().__init__(image_path)
        self.a3_enabled = a3_enabled
        self.parts = parts
        self.frozen_task_state = _a3_snapshot() if a3_enabled else _a2_snapshot()
        self.frozen_legacy = _legacy_snapshot(a3_enabled=a3_enabled)
        self.live_snapshot_reads = 0
        self.combined_capture_calls = 0

    def _freeze_response(
        self,
        session_id: str,
        response: AgentResponse,
        *,
        task_state_capabilities=None,
    ) -> AgentResponse:
        del session_id, task_state_capabilities
        if "legacy" in self.parts:
            response.response_snapshot = dict(self.frozen_legacy)
        if "projection" in self.parts:
            response.response_projection_snapshot = dict(self.frozen_legacy)
        if "typed" in self.parts:
            response.response_task_state_snapshot = self.frozen_task_state
        response.response_media_snapshot_captured = True
        return response

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        del session_id
        self.live_snapshot_reads += 1
        raise AssertionError("incomplete typed response must not read live state")

    def session_response_snapshot_v1(self, *args, **kwargs):
        del args, kwargs
        self.combined_capture_calls += 1
        raise AssertionError("incomplete typed response must not be recaptured")


class _IncompleteErrorRuntime(FakeRuntime):
    def __init__(self, image_path: Path, *, a3_enabled: bool, source: str):
        super().__init__(image_path)
        self.a3_enabled = a3_enabled
        self.source = source
        self.frozen_task_state = _a3_snapshot() if a3_enabled else _a2_snapshot()
        self.live_snapshot_reads = 0
        self.combined_capture_calls = 0

    def handle_text(self, session_id: str, text: str, **kwargs):
        del session_id, text, kwargs
        if self.source == "typed_exception":
            error = AgentProtocolError(
                "private typed-only failure",
                code="UPLOAD_PERSIST_FAILED",
            )
            error.response_task_state_snapshot = self.frozen_task_state
            raise error
        raise RuntimeError("private uncaptured failure")

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        del session_id
        self.live_snapshot_reads += 1
        raise AssertionError("error parity must not read legacy live state")

    def session_response_snapshot_v1(self, *args, **kwargs):
        del args, kwargs
        self.combined_capture_calls += 1
        return SessionResponseSnapshotV1(
            uploaded_image_path=None,
            legacy_session={},
            task_state=self.frozen_task_state,
        )


class _IncompleteResetRuntime(_ExactParityRuntime):
    def __init__(self, image_path: Path, *, a3_enabled: bool, carried_part: str):
        super().__init__(image_path)
        self.a3_enabled = a3_enabled
        self.carried_part = carried_part
        self.frozen_task_state = _a3_snapshot() if a3_enabled else _a2_snapshot()
        self.frozen_legacy = _legacy_snapshot(a3_enabled=a3_enabled)

    def clear(self, session_id: str, **kwargs) -> None:
        del session_id, kwargs
        error = AgentProtocolError(
            "private incomplete reset failure",
            code="UPLOAD_PERSIST_FAILED",
        )
        if self.carried_part == "legacy":
            error.response_snapshot = self._legacy_copy()
        else:
            error.response_task_state_snapshot = self.frozen_task_state
        raise error


class TaskStateExitParityTest(unittest.TestCase):
    @staticmethod
    def _image_bytes() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
        return buffer.getvalue()

    @staticmethod
    def _stream_events(response) -> list[dict[str, object]]:
        return [json.loads(line) for line in response.text.splitlines() if line]

    def test_nonempty_v1_is_exact_across_every_success_exit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "parity.jpg"
            Image.new("RGB", (8, 8), "white").save(image_path)
            runtime = _ExactParityRuntime(image_path)
            client = TestClient(create_app(runtime=runtime))
            client.cookies.set(SESSION_COOKIE, "task-state-parity-success")
            expected = runtime.frozen_task_state.to_dict()

            session_payload = client.get("/api/session").json()
            self.assertEqual(session_payload["task_state"], expected)
            self.assertNotIn("task_state", session_payload["session"])

            json_calls = (
                ("message", lambda: client.post("/api/message", json={"text": "继续"})),
                (
                    "image",
                    lambda: client.post(
                        "/api/image",
                        content=self._image_bytes(),
                        headers={"x-filename": "question.jpg"},
                    ),
                ),
                (
                    "select",
                    lambda: client.post(
                        "/api/a3/select",
                        json={"unit_id": "g1-u1", "task_revision": 9},
                    ),
                ),
            )
            for name, send in json_calls:
                with self.subTest(mode="json", route=name):
                    response = send()
                    self.assertEqual(response.status_code, 200, response.text)
                    payload = response.json()
                    self.assertEqual(payload["task_state"], expected)
                    self.assertNotIn("task_state", payload["session"])

            stream_calls = (
                ("message", lambda: client.post("/api/message/stream", json={"text": "继续"})),
                (
                    "image",
                    lambda: client.post(
                        "/api/image/stream",
                        files={
                            "file": (
                                "question.jpg",
                                self._image_bytes(),
                                "image/jpeg",
                            )
                        },
                    ),
                ),
                (
                    "select",
                    lambda: client.post(
                        "/api/a3/select/stream",
                        json={"unit_id": "g1-u1", "task_revision": 9},
                    ),
                ),
                (
                    "prepare",
                    lambda: client.post(
                        "/api/a3/prepare/stream",
                        json={"unit_ids": ["g1-u1"], "task_revision": 9},
                    ),
                ),
                (
                    "crop",
                    lambda: client.post(
                        "/api/a3/crop/stream",
                        json={
                            "bounds": {
                                "x": 0.1,
                                "y": 0.1,
                                "width": 0.8,
                                "height": 0.8,
                            },
                            "unit_id": "g1-u1",
                            "task_revision": 9,
                        },
                    ),
                ),
            )
            for name, send in stream_calls:
                with self.subTest(mode="stream", route=name):
                    response = send()
                    self.assertEqual(response.status_code, 200, response.text)
                    events = self._stream_events(response)
                    self.assertEqual(
                        [event["type"] for event in events],
                        ["progress", "result"],
                    )
                    self.assertNotIn("task_state", events[0])
                    self.assertNotIn("task_state", events[-1])
                    self.assertEqual(events[-1]["data"]["task_state"], expected)
                    self.assertNotIn("task_state", events[-1]["data"]["session"])

            reset = client.post("/api/reset")
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertEqual(
                reset.json()["task_state"],
                contract.empty_task_state_snapshot().to_dict(),
            )
            self.assertEqual(runtime.live_snapshot_reads, 0)

            for operation, capabilities in runtime.capability_calls:
                self.assertIsNotNone(capabilities, operation)
                self.assertTrue(capabilities.reset_session_available, operation)
                self.assertEqual(
                    capabilities.trusted_image_event,
                    operation == "image",
                    operation,
                )

    def test_nonempty_v1_is_exact_across_every_controlled_error_exit(self):
        http_cases = (
            ("session", lambda client: client.get("/api/session")),
            ("message", lambda client: client.post("/api/message", json={"text": "继续"})),
            (
                "image",
                lambda client: client.post(
                    "/api/image",
                    content=self._image_bytes(),
                    headers={"x-filename": "question.jpg"},
                ),
            ),
            (
                "select",
                lambda client: client.post(
                    "/api/a3/select",
                    json={"unit_id": "g1-u1", "task_revision": 9},
                ),
            ),
            ("reset", lambda client: client.post("/api/reset")),
        )
        stream_cases = (
            ("message", lambda client: client.post("/api/message/stream", json={"text": "继续"})),
            (
                "image",
                lambda client: client.post(
                    "/api/image/stream",
                    files={
                        "file": (
                            "question.jpg",
                            self._image_bytes(),
                            "image/jpeg",
                        )
                    },
                ),
            ),
            (
                "select",
                lambda client: client.post(
                    "/api/a3/select/stream",
                    json={"unit_id": "g1-u1", "task_revision": 9},
                ),
            ),
            (
                "prepare",
                lambda client: client.post(
                    "/api/a3/prepare/stream",
                    json={"unit_ids": ["g1-u1"], "task_revision": 9},
                ),
            ),
            (
                "crop",
                lambda client: client.post(
                    "/api/a3/crop/stream",
                    json={
                        "bounds": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.8,
                            "height": 0.8,
                        },
                        "unit_id": "g1-u1",
                        "task_revision": 9,
                    },
                ),
            ),
        )

        for mode, cases in (("http", http_cases), ("stream", stream_cases)):
            for operation, send in cases:
                with self.subTest(mode=mode, route=operation), tempfile.TemporaryDirectory() as temp_dir:
                    image_path = Path(temp_dir) / "parity-error.jpg"
                    Image.new("RGB", (8, 8), "white").save(image_path)
                    runtime = _ExactParityRuntime(image_path)
                    runtime.fail_operations.add(operation)
                    client = TestClient(
                        create_app(runtime=runtime),
                        raise_server_exceptions=False,
                    )
                    client.cookies.set(SESSION_COOKIE, "task-state-parity-error")

                    response = send(client)

                    expected = runtime.frozen_task_state.to_dict()
                    if mode == "http":
                        self.assertEqual(response.status_code, 500, response.text)
                        payload = response.json()
                        self.assertEqual(payload["task_state"], expected)
                        self.assertNotIn("data", payload)
                    else:
                        self.assertEqual(response.status_code, 200, response.text)
                        events = self._stream_events(response)
                        self.assertEqual(events[-1]["type"], "error")
                        self.assertEqual(events[-1]["task_state"], expected)
                        self.assertNotIn("task_state", events[-1].get("data", {}))
                        for progress_event in events[:-1]:
                            self.assertEqual(progress_event["type"], "progress")
                            self.assertNotIn("task_state", progress_event)
                    self.assertEqual(runtime.live_snapshot_reads, 0)

    def test_incomplete_business_read_sets_are_zero_io_and_fail_closed(self):
        shapes = {
            "all_missing": frozenset(),
            "legacy_only": frozenset({"legacy"}),
            "projection_only": frozenset({"projection"}),
            "typed_only": frozenset({"typed"}),
            "legacy_projection": frozenset({"legacy", "projection"}),
            "projection_typed": frozenset({"projection", "typed"}),
            "legacy_typed": frozenset({"legacy", "typed"}),
        }
        for a3_enabled in (False, True):
            expected_codes = (
                ["WORKFLOW_STATE_UNREADABLE", "CHILD_STATE_UNREADABLE"]
                if a3_enabled
                else ["CHILD_STATE_UNREADABLE"]
            )
            for shape, parts in shapes.items():
                for mode in ("json", "stream"):
                    with (
                        self.subTest(
                            a3_enabled=a3_enabled,
                            shape=shape,
                            mode=mode,
                        ),
                        tempfile.TemporaryDirectory() as temp_dir,
                    ):
                        image_path = Path(temp_dir) / "incomplete.jpg"
                        Image.new("RGB", (8, 8), "white").save(image_path)
                        runtime = _IncompleteResponseRuntime(
                            image_path,
                            a3_enabled=a3_enabled,
                            parts=parts,
                        )
                        client = TestClient(
                            create_app(runtime=runtime),
                            raise_server_exceptions=False,
                        )
                        client.cookies.set(
                            SESSION_COOKIE,
                            "task-state-incomplete-response",
                        )

                        with self.assertLogs(
                            "tiku_agent.fastapi_demo",
                            level="ERROR",
                        ):
                            response = client.post(
                                (
                                    "/api/message"
                                    if mode == "json"
                                    else "/api/message/stream"
                                ),
                                json={"text": "继续"},
                            )

                        if mode == "json":
                            self.assertEqual(response.status_code, 500, response.text)
                            task_state = response.json()["task_state"]
                        else:
                            self.assertEqual(response.status_code, 200, response.text)
                            events = self._stream_events(response)
                            self.assertEqual(events[-1]["type"], "error")
                            task_state = events[-1]["task_state"]

                        if parts == frozenset({"legacy", "typed"}):
                            self.assertEqual(
                                task_state,
                                runtime.frozen_task_state.to_dict(),
                            )
                        else:
                            self.assertEqual(
                                task_state["consistency"],
                                {"status": "INCONSISTENT", "codes": expected_codes},
                            )
                        self.assertEqual(runtime.live_snapshot_reads, 0)
                        self.assertEqual(runtime.combined_capture_calls, 0)

    def test_incomplete_error_sources_never_publish_typed_only_state(self):
        for a3_enabled in (False, True):
            expected_codes = (
                ["WORKFLOW_STATE_UNREADABLE", "CHILD_STATE_UNREADABLE"]
                if a3_enabled
                else ["CHILD_STATE_UNREADABLE"]
            )
            for source, expected_captures in (
                ("typed_exception", 0),
                ("empty_legacy_capture", 1),
            ):
                for mode in ("json", "stream"):
                    with (
                        self.subTest(
                            a3_enabled=a3_enabled,
                            source=source,
                            mode=mode,
                        ),
                        tempfile.TemporaryDirectory() as temp_dir,
                    ):
                        image_path = Path(temp_dir) / "incomplete-error.jpg"
                        Image.new("RGB", (8, 8), "white").save(image_path)
                        runtime = _IncompleteErrorRuntime(
                            image_path,
                            a3_enabled=a3_enabled,
                            source=source,
                        )
                        client = TestClient(
                            create_app(runtime=runtime),
                            raise_server_exceptions=False,
                        )
                        client.cookies.set(
                            SESSION_COOKIE,
                            "task-state-incomplete-error",
                        )

                        log_context = (
                            nullcontext()
                            if source == "typed_exception"
                            else self.assertLogs(
                                "tiku_agent.fastapi_demo",
                                level="ERROR",
                            )
                        )
                        with log_context:
                            response = client.post(
                                (
                                    "/api/message"
                                    if mode == "json"
                                    else "/api/message/stream"
                                ),
                                json={"text": "继续"},
                            )

                        task_state = (
                            response.json()["task_state"]
                            if mode == "json"
                            else self._stream_events(response)[-1]["task_state"]
                        )
                        self.assertEqual(
                            task_state["consistency"],
                            {"status": "INCONSISTENT", "codes": expected_codes},
                        )
                        self.assertNotEqual(
                            task_state,
                            runtime.frozen_task_state.to_dict(),
                        )
                        self.assertEqual(runtime.live_snapshot_reads, 0)
                        self.assertEqual(
                            runtime.combined_capture_calls,
                            expected_captures,
                        )

    def test_session_incomplete_capture_fails_closed_without_recapture(self):
        for a3_enabled in (False, True):
            expected_codes = (
                ["WORKFLOW_STATE_UNREADABLE", "CHILD_STATE_UNREADABLE"]
                if a3_enabled
                else ["CHILD_STATE_UNREADABLE"]
            )
            with (
                self.subTest(a3_enabled=a3_enabled),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                image_path = Path(temp_dir) / "incomplete-session.jpg"
                Image.new("RGB", (8, 8), "white").save(image_path)
                runtime = _IncompleteErrorRuntime(
                    image_path,
                    a3_enabled=a3_enabled,
                    source="empty_legacy_capture",
                )
                client = TestClient(
                    create_app(runtime=runtime),
                    raise_server_exceptions=False,
                )
                client.cookies.set(SESSION_COOKIE, "task-state-incomplete-session")

                with self.assertLogs("tiku_agent.fastapi_demo", level="ERROR"):
                    response = client.get("/api/session")

                self.assertEqual(response.status_code, 500, response.text)
                task_state = response.json()["task_state"]
                self.assertEqual(
                    task_state["consistency"],
                    {"status": "INCONSISTENT", "codes": expected_codes},
                )
                self.assertNotEqual(task_state, runtime.frozen_task_state.to_dict())
                self.assertEqual(runtime.live_snapshot_reads, 0)
                self.assertEqual(runtime.combined_capture_calls, 1)

    def test_reset_incomplete_error_pair_is_zero_io_and_fail_closed(self):
        for a3_enabled in (False, True):
            expected_codes = (
                ["WORKFLOW_STATE_UNREADABLE", "CHILD_STATE_UNREADABLE"]
                if a3_enabled
                else ["CHILD_STATE_UNREADABLE"]
            )
            for carried_part in ("legacy", "typed"):
                with (
                    self.subTest(
                        a3_enabled=a3_enabled,
                        carried_part=carried_part,
                    ),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    image_path = Path(temp_dir) / "incomplete-reset.jpg"
                    Image.new("RGB", (8, 8), "white").save(image_path)
                    runtime = _IncompleteResetRuntime(
                        image_path,
                        a3_enabled=a3_enabled,
                        carried_part=carried_part,
                    )
                    client = TestClient(
                        create_app(runtime=runtime),
                        raise_server_exceptions=False,
                    )
                    client.cookies.set(SESSION_COOKIE, "task-state-incomplete-reset")

                    response = client.post("/api/reset")

                    self.assertEqual(response.status_code, 500, response.text)
                    task_state = response.json()["task_state"]
                    self.assertEqual(
                        task_state["consistency"],
                        {"status": "INCONSISTENT", "codes": expected_codes},
                    )
                    self.assertNotEqual(
                        task_state,
                        runtime.frozen_task_state.to_dict(),
                    )
                    self.assertEqual(runtime.live_snapshot_reads, 0)
                    self.assertEqual(runtime.combined_capture_calls, 1)

    def test_response_store_failure_reuses_nonempty_v1_across_json_and_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "response-store-parity.jpg"
            Image.new("RGB", (8, 8), "white").save(image_path)

            class FailingResponseStore(SQLiteResponseStore):
                def __init__(self, path: Path):
                    super().__init__(path)
                    self.finalize_attempts = 0

                def finalize(self, *args, **kwargs):
                    del args, kwargs
                    self.finalize_attempts += 1
                    raise ResponseStoreError("private response-store parity failure")

            runtime = _ExactParityRuntime(image_path)
            store = FailingResponseStore(root / "responses.sqlite3")
            client = TestClient(
                create_app(runtime=runtime, response_store=store),
                raise_server_exceptions=False,
            )
            client.cookies.set(SESSION_COOKIE, "task-state-store-parity")
            expected = runtime.frozen_task_state.to_dict()

            with self.assertLogs("tiku_agent.fastapi_demo", level="ERROR"):
                json_response = client.post("/api/message", json={"text": "json"})
                stream_response = client.post(
                    "/api/message/stream",
                    json={"text": "stream"},
                )

            self.assertEqual(json_response.status_code, 500, json_response.text)
            json_payload = json_response.json()
            self.assertEqual(json_payload["code"], "SERVICE_UNAVAILABLE")
            self.assertEqual(json_payload["task_state"], expected)
            stream_events = self._stream_events(stream_response)
            self.assertEqual(stream_events[-1]["type"], "error")
            self.assertEqual(stream_events[-1]["code"], "SERVICE_UNAVAILABLE")
            self.assertEqual(stream_events[-1]["task_state"], expected)
            self.assertEqual(
                stream_events[-1]["task_state"],
                json_payload["task_state"],
            )
            self.assertEqual(store.finalize_attempts, 2)
            self.assertEqual(runtime.live_snapshot_reads, 0)
            self.assertEqual(runtime.combined_capture_calls, 0)

    def test_serialization_failure_reuses_nonempty_v1_across_json_and_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "serialization-parity.jpg"
            Image.new("RGB", (8, 8), "white").save(image_path)
            secret = "private-unserializable-parity-value"

            class UnserializableRuntime(_ExactParityRuntime):
                def handle_text(self, session_id: str, text: str, **kwargs):
                    response = super().handle_text(session_id, text, **kwargs)
                    response.author_contact = {"unsafe": {secret}}
                    return response

            runtime = UnserializableRuntime(image_path)
            client = TestClient(
                create_app(
                    runtime=runtime,
                    response_store=SQLiteResponseStore(root / "responses.sqlite3"),
                ),
                raise_server_exceptions=False,
            )
            client.cookies.set(SESSION_COOKIE, "task-state-serialization-parity")
            expected = runtime.frozen_task_state.to_dict()

            with self.assertLogs("tiku_agent.fastapi_demo", level="ERROR"):
                json_response = client.post("/api/message", json={"text": "json"})
                stream_response = client.post(
                    "/api/message/stream",
                    json={"text": "stream"},
                )

            self.assertEqual(json_response.status_code, 500, json_response.text)
            json_payload = json_response.json()
            self.assertEqual(json_payload["code"], "SERVICE_UNAVAILABLE")
            self.assertEqual(json_payload["task_state"], expected)
            stream_events = self._stream_events(stream_response)
            self.assertEqual(stream_events[-1]["type"], "error")
            self.assertEqual(stream_events[-1]["code"], "SERVICE_UNAVAILABLE")
            self.assertEqual(stream_events[-1]["task_state"], expected)
            self.assertEqual(
                stream_events[-1]["task_state"],
                json_payload["task_state"],
            )
            self.assertNotIn(secret, json_response.text)
            self.assertNotIn(secret, stream_response.text)
            self.assertEqual(runtime.live_snapshot_reads, 0)
            self.assertEqual(runtime.combined_capture_calls, 0)


if __name__ == "__main__":
    unittest.main()

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_a3_checkpoint_integration as a3_fixture
from tests import test_a2_checkpoint_integration as a2_fixture
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_store import CheckpointQueryScopeV1
from tiku_diagnostics.checkpoints import CheckpointDiagnosticService


class CheckpointPhase4AcceptanceTest(unittest.TestCase):
    trace = a3_fixture.A3CheckpointIntegrationTest.trace
    records = a3_fixture.A3CheckpointIntegrationTest.records
    business = a3_fixture.A3CheckpointIntegrationTest.business
    upload = a3_fixture.A3CheckpointIntegrationTest.upload

    def setUp(self):
        a3_fixture.A3CheckpointIntegrationTest.setUp(self)

    def service(self):
        owner = self.records()[-1]["owner"]
        service = CheckpointDiagnosticService(self.root, capacity=self.policy, actor_key="acceptance",
            scope=CheckpointQueryScopeV1(owner["identity_key"], owner["session_key"],
                                        owner["workflow_search_id"], owner["workflow_task_revision"]))
        service.store = self.store
        return service

    def answer(self):
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            return self.runtime.handle_text("session-test", "1", identity_key="invite_test")

    def public_result(self, response):
        state = self.a2.store.load("session-test")
        return {
            "text": response.text, "intent": response.intent, "media_kind": response.media_kind,
            "images": [sha256(Path(path).read_bytes()).hexdigest() for path in response.images],
            "protocol": {key: value for key, value in response.protocol.items()
                         if key not in {"search_id", "request_id", "trace_id", "response_id"}},
            "candidates": [{key: candidate.get(key) for key in
                            ("rank", "name", "score", "rerank_score", "final_score", "chapter")}
                           for candidate in state.candidates],
        }

    def test_real_a3_answer_trace_reaches_all_nine_stages_and_authorized_crop(self):
        self.upload()
        self.answer()
        service = self.service()
        query = service.query(trace_id=self.last_trace.trace_id)
        self.assertEqual(query["count"], 1)
        answer = query["checkpoints"][0]
        chain = service.chain(answer["checkpoint_id"])
        self.assertEqual(chain["stop_reason"], "complete")
        self.assertEqual(len(chain["checkpoints"]), 9)
        crop = next(link for link in answer["artifacts"] if link["role"] == "question_crop")
        image = service.artifact(answer["checkpoint_id"], crop["artifact_id"])
        self.assertTrue(image.content)
        self.assertEqual(image.descriptor.owner.unit_id, answer["owner"]["unit_id"])

    def test_capture_on_off_and_capacity_pressure_preserve_public_results_and_order(self):
        self.recorder.gate = A2CheckpointCaptureGateV1(enabled=False)
        baseline_search = self.public_result(self.upload())
        baseline_answer = self.public_result(self.answer())
        self.recorder.gate = A2CheckpointCaptureGateV1(enabled=True)
        self.assertEqual(self.public_result(self.upload()), baseline_search)
        self.assertEqual(self.public_result(self.answer()), baseline_answer)
        self.store.capacity = replace(self.policy, max_checkpoint_rows=1)
        self.assertEqual(self.public_result(self.upload()), baseline_search)
        self.assertEqual(self.public_result(self.answer()), baseline_answer)


class StandaloneCheckpointAcceptanceTest(unittest.TestCase):
    trace = a2_fixture.A2CheckpointIntegrationTest.trace
    records = a2_fixture.A2CheckpointIntegrationTest.records
    search = a2_fixture.A2CheckpointIntegrationTest.search
    service = CheckpointPhase4AcceptanceTest.service

    def setUp(self):
        a2_fixture.A2CheckpointIntegrationTest.setUp(self)

    def test_standalone_answer_trace_reaches_all_six_actual_stages(self):
        self.search()
        with self.trace(), patch("tiku_agent.tools.search.find_answer_files", return_value=[self.source]):
            self.runtime.handle_text("session-test", "1", identity_key="invite_test")
        service = self.service()
        answer = service.query(trace_id=self.last_trace.trace_id)["checkpoints"][0]
        chain = service.chain(answer["checkpoint_id"])
        self.assertEqual(chain["stop_reason"], "complete")
        self.assertEqual(len(chain["checkpoints"]), 6)


if __name__ == "__main__":
    unittest.main()

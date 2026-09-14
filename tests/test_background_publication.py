"""Delivery repair reuses business receipts and a stable response identity."""
from contextlib import closing
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from tiku_agent.background_publication import BackgroundPublication
from tiku_agent.execution_store import ExecutionError
from tiku_agent.execution_worker import BackgroundWorker
from tiku_shared.response_store import SQLiteResponseStore
from tests.test_execution_dispatch import DispatchFixture


class BackgroundPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = DispatchFixture(temporary.name)
        self.responses = SQLiteResponseStore(self.f.root / "responses.db")
        self.publication = BackgroundPublication(self.f.dispatch, self.responses)
        self.worker = BackgroundWorker(self.f.dispatch, self.f.root / "published-worker", publication=self.publication)

    def test_response_commit_ack_lost_reuses_same_id_without_business_replay(self):
        f, publication = self.f, self.publication
        ack, request, grant = f.accept()
        original = self.responses.finalize
        ids = []
        def lose_ack(projection):
            result = original(projection)
            ids.append(result.response_id)
            raise OSError("injected response commit ACK lost")
        with patch.object(self.responses, "finalize", lose_ack):
            self.worker.run_once()
        self.assertEqual(f.observe(request, grant)["status"], "SUCCEEDED")
        self.assertEqual(publication.view(ack["operation_id"])["status"], "PENDING")
        before = f.rows("execution_operations")
        # A fresh publisher has no original in-memory AgentResponse.
        rebuilt = BackgroundPublication(f.dispatch, SQLiteResponseStore(self.responses.path))
        f.clock[0] += 2
        rebuilt.repair_once()
        value = rebuilt.view(ack["operation_id"])
        self.assertEqual(value["status"], "READY")
        self.assertEqual(value["result"]["response_id"], ids[0])
        self.assertEqual(f.rows("execution_operations"), before)
        self.assertEqual(f.calls, ["hello"])
        with closing(sqlite3.connect(self.responses.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM public_responses").fetchone()[0], 1)

    def test_preparation_failure_preserves_receipt_and_bounded_repair(self):
        f, publication = self.f, self.publication
        ack, request, grant = f.accept()
        with patch.object(publication, "_prepare", side_effect=OSError("private publication path")):
            self.worker.run_once()
            for _ in range(5):
                f.clock[0] += 2
                publication.repair_once()
        self.assertEqual(f.observe(request, grant)["status"], "SUCCEEDED")
        view = publication.view(ack["operation_id"])
        self.assertEqual(view, {"status": "FAILED", "error_code": "EXECUTION_PUBLICATION_UNAVAILABLE"})
        self.assertEqual(f.rows("execution_publications")[0]["attempts"], 3)
        for _ in range(3):
            publication.view(ack["operation_id"])
        self.assertEqual(f.rows("execution_publications")[0]["attempts"], 3)
        self.assertEqual(f.calls, ["hello"])
        self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")

    def test_missing_draft_can_be_rebuilt_from_saved_response(self):
        f, publication = self.f, self.publication
        ack, request, grant = f.accept()
        with patch.object(publication, "prepare", side_effect=OSError("before draft")), patch.object(publication, "publish"):
            self.worker.run_once()
        self.assertEqual(f.observe(request, grant)["status"], "SUCCEEDED")
        self.assertEqual(f.rows("execution_publications"), [])
        publication.repair_once()
        self.assertEqual(publication.view(ack["operation_id"])["status"], "READY")
        self.assertEqual(f.calls, ["hello"])

    def test_publication_capacity_does_not_repeat_model_or_lose_business_receipt(self):
        f, publication = self.f, self.publication
        image = io.BytesIO()
        Image.new("RGB", (100, 100)).save(image, format="PNG")
        request, grant = f.request(), f.grant()
        ack = f.dispatch.accept("s", "invite", grant, request, "handle_image", {}, image=image.getvalue())
        with patch.object(publication, "MAX_TOTAL_MEDIA", 1):
            self.worker.run_once()
            for _ in range(3):
                f.clock[0] += 2
                publication.repair_once()
        self.assertEqual(f.observe(request, grant)["status"], "SUCCEEDED")
        self.assertEqual(publication.view(ack["operation_id"])["status"], "FAILED")
        self.assertEqual(f.rows("execution_public_media"), [])
        self.assertEqual(f.calls, ["image"])

    def test_expired_delivery_is_unavailable_and_never_rebuilt(self):
        f, publication = self.f, self.publication
        ack, request, grant = f.accept()
        self.worker.run_once()
        response_id = publication.view(ack["operation_id"])["result"]["response_id"]
        f.clock[0] += 31 * 86400
        publication.repair_once()
        self.assertEqual(publication.view(ack["operation_id"])["status"], "UNAVAILABLE")
        row = f.rows("execution_publications")[0]
        self.assertEqual(row["status"], "EXPIRED")
        self.assertIsNone(row["payload"])
        self.assertEqual(row["response_id"], response_id)
        self.assertEqual(f.calls, ["hello"])

    def test_process_death_after_response_commit_repairs_only_delivery(self):
        f, publication = self.f, self.publication
        ack, request, grant = f.accept()
        with patch.object(publication, 'publish'):
            self.worker.run_once()
        before = f.rows('execution_operations')
        script = '''
import json, os, sqlite3, sys
from tiku_shared.response_store import SQLiteResponseStore, ResponseProjection
conn = sqlite3.connect(sys.argv[1])
projection = conn.execute("SELECT projection FROM execution_publications WHERE operation_id=?", (sys.argv[3],)).fetchone()[0]
conn.execute("UPDATE execution_publications SET attempts=1,next_attempt=?,token='crashed' WHERE operation_id=?", (float(sys.argv[4])+15, sys.argv[3]))
conn.commit()
conn.close()
SQLiteResponseStore(sys.argv[2]).finalize(ResponseProjection(**json.loads(projection)))
os._exit(23)
'''
        child = subprocess.run([sys.executable, '-B', '-c', script, str(f.authority.path), str(self.responses.path),
                                ack['operation_id'], str(f.clock[0])], capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(child.returncode, 23, child.stderr.decode(errors='replace'))
        with closing(sqlite3.connect(self.responses.path)) as conn:
            original_id = conn.execute('SELECT response_id FROM public_responses').fetchone()[0]
        rebuilt = BackgroundPublication(f.dispatch, SQLiteResponseStore(self.responses.path))
        f.clock[0] += 16
        rebuilt.repair_once()
        self.assertEqual(rebuilt.view(ack['operation_id'])['result']['response_id'], original_id)
        self.assertEqual(f.rows('execution_operations'), before)
        self.assertEqual(f.calls, ['hello'])
        self.assertEqual(len(f.rows('execution_attempts')), 1)

    def test_abandoned_final_publication_attempt_expires_to_failure(self):
        f = self.f
        ack, _, _ = f.accept()
        with patch.object(self.publication, 'publish'):
            self.worker.run_once()
        with f.authority.transaction() as conn:
            conn.execute("UPDATE execution_publications SET attempts=3,next_attempt=?,token='crashed'", (f.clock[0] + 15,))
        self.publication.repair_once()
        self.assertEqual(self.publication.view(ack['operation_id'])['status'], 'PENDING')
        f.clock[0] += 16
        self.publication.repair_once()
        self.assertEqual(self.publication.view(ack['operation_id'])['status'], 'FAILED')
        self.assertEqual(f.calls, ['hello'])


if __name__ == "__main__":
    unittest.main()

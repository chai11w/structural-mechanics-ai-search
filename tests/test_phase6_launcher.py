"""Release wiring uses the real A2/A3 graph, without paid model calls."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from scripts.run_tiku_agent_phase6 import build_app
from tiku_admin.control_store import SQLiteControlStore


class Phase6LauncherTests(unittest.TestCase):
    def test_real_graph_authentication_stores_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = SQLiteControlStore(root / "control.sqlite3")
            _, code = control.create_invitation(label="isolated release test")
            for _ in range(2):
                app = build_app(root)
                with TestClient(app) as client:
                    landing = client.get("/", follow_redirects=False)
                    self.assertEqual((landing.status_code, landing.headers.get("location")), (303, "/invite"))
                    self.assertEqual(client.get("/invite").status_code, 200)
                    self.assertEqual(client.get("/api/jobs/session").status_code, 401)
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(client.post("/api/invite/login", data={"code": code}, follow_redirects=False).status_code, 303)
                    context = client.post("/api/jobs/session", headers={"X-Tiku-Background": "1"})
                    self.assertEqual(context.status_code, 200, context.text)
                    self.assertIn("tiku_phase6_8898_session", client.cookies)
                    self.assertIn("tiku_phase6_8898_invite", client.cookies)
                    self.assertEqual(app.state.background.dispatch.policy.max_concurrent, 1)
                    self.assertEqual(app.state.background.dispatch.policy.max_queued, 2)
                    self.assertEqual(app.state.background.dispatch.policy.queue_seconds, 55)
                    self.assertEqual(app.state.background.store.path, root / "execution.sqlite3")
                    self.assertEqual(client.post("/api/message", json={"text": "hello"}).status_code, 409)
                self.assertTrue(app.state.background.drain_result["drained"])
                with app.state.background.store.reading() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM execution_effects").fetchone()[0], 0)

    def test_missing_control_fails_without_silently_creating_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "control database not found"):
                build_app(root)
            self.assertFalse((root / "control.sqlite3").exists())

    def test_8790_and_feishu_runtime_are_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (".tmp_tiku_agent_v2_prod_8790", ".tmp_feishu_tiku"):
                root = Path(directory) / name
                with self.assertRaisesRegex(ValueError, "separate runtime"):
                    build_app(root)
                self.assertFalse(root.exists())

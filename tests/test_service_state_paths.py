"""Real shared control/feedback stores with separately configured service roots."""
from pathlib import Path
import gc
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from scripts import run_tiku_admin, run_tiku_agent_8790
from tiku_agent.checkpoint_contract import EvidenceCapacityPolicyV1


def capacity():
    return EvidenceCapacityPolicyV1(max_checkpoint_rows=100, max_artifact_rows=200,
        max_audit_rows=300, max_trace_rows=400, max_artifact_bytes=1_000_000,
        min_free_bytes=1, max_artifacts_per_checkpoint=5)


class ServiceStatePathsTest(unittest.TestCase):
    def test_admin_and_search_use_the_same_configured_control_and_feedback_stores(self):
        self.check_shared_stores(background=False)

    def test_background_preserves_session_and_shared_admin_stores(self):
        self.check_shared_stores(background=True)

    def check_shared_stores(self, *, background):
        with tempfile.TemporaryDirectory(prefix="lida service state ") as temporary, patch(
            "urllib.request.urlopen", side_effect=AssertionError("no provider calls in deployment test")
        ):
            root = Path(temporary)
            data, admin, shared = root / "search data", root / "operations", root / "control"
            data.mkdir()
            (data / "runtime").mkdir()
            control, feedback_path = shared / "control.sqlite3", shared / "feedback.sqlite3"
            admin_app = run_tiku_admin.build_app(admin_runtime=admin, source_runtime=data / "runtime",
                control_db=control, feedback_database=feedback_path)
            with TestClient(admin_app) as client:
                setup = client.post("/api/admin/setup", json={"password": "a-local-test-password",
                    "confirm_password": "a-local-test-password"})
                self.assertEqual(setup.status_code, 200)
                csrf = client.get("/api/admin/session").json()["csrf_token"]
                invitation = client.post("/api/admin/invitations", headers={"x-csrf-token": csrf},
                    json={"label": "local test"})
                self.assertEqual(invitation.status_code, 200)
                with patch.object(run_tiku_agent_8790, "create_app", wraps=run_tiku_agent_8790.create_app) as assembly:
                    search_app = run_tiku_agent_8790.build_app(data / "runtime", control_db=control,
                        feedback_database=feedback_path, evidence_data_root=data,
                        evidence_capacity=capacity(), checkpoint_retention_backup_root=root / "evidence backups",
                        checkpoint_retention_interval_seconds=60, checkpoint_retention_backup_keep_runs=3,
                        enable_durable_execution=background, background_execution=background,
                        background_production=background, public_origin="https://deployment.example.test" if background else "")
                try:
                    access = assembly.call_args.kwargs["invite_access"]
                    self.assertIsNotNone(access.authenticate_code(invitation.json()["code"]))
                    if background:
                        self.assertEqual(search_app.state.background.public_origin, "https://deployment.example.test")
                        with TestClient(search_app) as search:
                            search.cookies.set("tiku_agent_session", "existing-session")
                            identity = access.authenticate_code(invitation.json()["code"])
                            search.cookies.set(access.cookie_name, access.issue_cookie(identity))
                            session = search.post("/api/jobs/session", headers={"X-Tiku-Background": "1"})
                            self.assertEqual(session.status_code, 200, session.text)
                            self.assertTrue(all(cookie.value == "existing-session" for cookie in search.cookies.jar if cookie.name == "tiku_agent_session"))
                            self.assertEqual(session.json()["protocol_version"], 1)
                    feedback = assembly.call_args.kwargs["feedback_store"]
                    saved = feedback.upsert(message_id="m1", identity_key="i1", session_key="s1",
                        rating="negative", tags=(), detail="local test", task_revision=1,
                        phase="DONE", candidate_count=1, rated_response_id="resp_" + "1" * 32)
                    response = client.patch(f"/api/admin/feedback/{saved.feedback_number}/review",
                        headers={"x-csrf-token": csrf}, json={"review_status": "resolved", "admin_note": "checked"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(feedback.get_feedback(saved.feedback_id).review_status, "resolved")
                    self.assertEqual(feedback.cases_root, shared / "feedback_cases")
                    runner = search_app.state.checkpoint_retention_controller
                    self.assertEqual(runner.repository_root, data)
                    self.assertEqual(runner.runtime_root, data / "runtime")
                    self.assertTrue(control.is_file())
                    self.assertTrue(feedback_path.is_file())
                    self.assertTrue((admin / "invite_code_encryption.key").is_file())
                    self.assertFalse((admin / "control.sqlite3").exists())
                    self.assertFalse((data / "runtime/feedback.sqlite3").exists())
                    self.assertEqual(search_app.state.response_store.path, shared / "responses.sqlite3")
                finally:
                    search_app.state.trace_event_recorder.close()
            gc.collect()

    def test_background_profile_requires_explicit_shared_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "requires background execution"):
                run_tiku_agent_8790.build_app(root, background_production=True)
            with self.assertRaisesRegex(ValueError, "requires explicit control"):
                run_tiku_agent_8790.build_app(root, background_production=True, background_execution=True)
            with self.assertRaisesRegex(ValueError, "beside the control"):
                run_tiku_agent_8790.build_app(root, background_production=True, background_execution=True,
                    control_db=root / "shared/control.sqlite3", feedback_database=root / "else/feedback.sqlite3",
                    evidence_data_root=root.parent)

    def test_external_evidence_root_still_binds_one_exact_runtime_and_separate_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            options = dict(evidence_data_root=data, evidence_capacity=capacity(),
                checkpoint_retention_backup_root=root / "backups",
                checkpoint_retention_interval_seconds=60, checkpoint_retention_backup_keep_runs=3)
            for target in (data, root / "other/runtime", data / "nested/runtime"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "immediate child"):
                    run_tiku_agent_8790.build_app(target, **options)
                self.assertFalse((target / "trace_events.sqlite3").exists())
            with self.assertRaisesRegex(ValueError, "outside the repository"):
                run_tiku_agent_8790.build_app(data / "runtime", **{**options,
                    "checkpoint_retention_backup_root": data / "backups"})
            with self.assertRaisesRegex(ValueError, "requires evidence capacity"):
                run_tiku_agent_8790.build_app(data / "runtime", evidence_data_root=data)
            with self.assertRaisesRegex(ValueError, "inside the repository"):
                run_tiku_agent_8790.build_app(data / "runtime", **{**options, "evidence_data_root": None})


if __name__ == "__main__":
    unittest.main()

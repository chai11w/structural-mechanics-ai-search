from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
import sqlite3
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tiku_admin.app import create_admin_app
from tiku_admin.auth import SQLiteInviteAccess
from tiku_admin.control_store import SQLiteControlStore, cny_to_micros
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import SQLiteFeedbackStore


class TikuAdminTest(unittest.TestCase):
    def setUp(self):
        self.root = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"admin_{uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.control = SQLiteControlStore(self.root / "control.sqlite3")

    def test_control_store_hashes_credentials_and_revokes_old_invite_sessions(self):
        self.control.initialize_admin("a-secure-admin-password")
        self.assertTrue(self.control.verify_admin_password("a-secure-admin-password"))
        self.assertFalse(self.control.verify_admin_password("wrong-password"))

        invitation, code = self.control.create_invitation(label="首位内测用户")
        self.assertNotIn(code.encode("utf-8"), (self.root / "control.sqlite3").read_bytes())
        access = SQLiteInviteAccess(self.control, auth_max_age_seconds=60)
        identity = access.authenticate_code(code)
        self.assertEqual(identity.invite_id, invitation.invite_id)
        cookie = access.issue_cookie(identity, now=100)
        self.assertEqual(access.verify_cookie(cookie, now=120), identity)

        self.control.set_invitation_status(invitation.invite_id, "disabled")
        self.assertIsNone(access.verify_cookie(cookie, now=120))
        self.control.set_invitation_status(invitation.invite_id, "enabled")
        self.assertIsNone(access.verify_cookie(cookie, now=120))

        reset, new_code = self.control.reset_invitation_code(invitation.invite_id)
        self.assertNotEqual(code, new_code)
        self.assertGreater(reset.auth_version, invitation.auth_version)
        self.assertIsNone(access.authenticate_code(code))
        self.assertEqual(access.authenticate_code(new_code).invite_id, invitation.invite_id)

    def test_dynamic_settings_and_audit_are_persisted(self):
        values = self.control.update_settings(
            global_daily_budget_micros=cny_to_micros("42.50"),
            default_invite_daily_budget_micros=cny_to_micros("4.25"),
            feedback_retention_days=45,
        )
        self.assertEqual(values["global_daily_budget_micros"], 42_500_000)
        self.assertEqual(self.control.settings()["feedback_retention_days"], 45)
        self.assertEqual(self.control.list_audit()[0]["action"], "settings.update")

    def test_feedback_case_copies_visible_media_and_can_be_reviewed_and_purged(self):
        source = self.root / "question.png"
        source.write_bytes(b"fake-image")
        store = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        saved = store.upsert(
            message_id="message_12345678",
            identity_key="invite-001",
            session_key="session-hash",
            rating="negative",
            tags=("wrong_answer",),
            detail="答案不是这道题",
            task_revision=2,
            phase="ANSWER_SHOWN",
            candidate_count=3,
            search_key="session-hash:2",
            chapter="4力法",
            conversation=[
                {
                    "me": True,
                    "message": "我发了一张题图。",
                    "images": ["/api/upload/question.png"],
                    "createdAt": 1000,
                },
                {
                    "me": False,
                    "message": "这是题库答案。",
                    "messageId": "message_12345678",
                    "createdAt": 2000,
                },
            ],
            media_resolver=lambda value: source if value.endswith("question.png") else None,
            retention_days=30,
        )
        self.assertEqual(len(saved.conversation), 2)
        media_name = saved.conversation[0]["images"][0]
        self.assertTrue(store.resolve_case_media(saved.feedback_id, media_name).is_file())
        reviewed = store.update_review(
            saved.feedback_id, review_status="resolved", admin_note="已记录排序问题"
        )
        self.assertEqual(reviewed.review_status, "resolved")
        self.assertEqual(store.query_feedback(rating="negative")[1], 1)

        purged = store.purge_expired_cases(
            now=datetime.now(UTC) + timedelta(days=31)
        )
        self.assertEqual(purged, 1)
        self.assertEqual(store.get_feedback(saved.feedback_id).conversation, ())
        self.assertIsNone(store.resolve_case_media(saved.feedback_id, media_name))

    def test_admin_http_flow_covers_setup_invites_overview_feedback_and_settings(self):
        feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        costs = self.root / "model_costs.sqlite3"
        self._create_cost_schema(costs)
        reporter = AdminReporter(
            control_store=self.control,
            cost_database=costs,
            feedback_store=feedback,
        )
        client = TestClient(
            create_admin_app(
                control_store=self.control,
                reporter=reporter,
                feedback_store=feedback,
            )
        )

        root = client.get("/", follow_redirects=False)
        self.assertEqual(root.headers["location"], "/setup")
        setup = client.post(
            "/api/admin/setup",
            json={
                "password": "a-secure-admin-password",
                "confirm_password": "a-secure-admin-password",
            },
        )
        self.assertEqual(setup.status_code, 200)
        session = client.get("/api/admin/session").json()
        self.assertTrue(session["authenticated"])
        csrf = session["csrf_token"]

        self.assertEqual(
            client.post("/api/admin/invitations", json={"label": "无 CSRF"}).status_code,
            403,
        )
        created = client.post(
            "/api/admin/invitations",
            headers={"x-csrf-token": csrf},
            json={"label": "真实用户 A", "daily_budget_cny": "5.00"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        payload = created.json()
        invite_id = payload["invitation"]["invite_id"]
        self.assertTrue(payload["code"].startswith("TIKU-"))
        self.assertNotIn(payload["code"], client.get("/api/admin/invitations").text)

        self._insert_cost(costs, identity_key=invite_id, search_key="search-one")
        saved = feedback.upsert(
            message_id="message_abcdefgh",
            identity_key=invite_id,
            session_key="session-key",
            rating="negative",
            tags=("ranking_issue",),
            detail="正确题排在后面",
            task_revision=1,
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            search_key="search-one",
            chapter="4力法",
            conversation=[{
                "me": False,
                "message": "请选择候选题。",
                "messageId": "message_abcdefgh",
                "createdAt": 2000,
            }],
        )

        overview = client.get("/api/admin/overview").json()
        self.assertEqual(overview["today_searches"], 1)
        self.assertEqual(overview["pending_negative_feedback"], 1)
        self.assertEqual(overview["invites"][0]["today_cost_cny"], "1.25")
        filtered = client.get(
            "/api/admin/feedback",
            params={
                "tag": "ranking_issue",
                "date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["invite_label"], "真实用户 A")
        self.assertEqual(filtered.json()["items"][0]["cost"]["estimated_cost_cny"], "1.25")
        self.assertEqual(
            client.get("/api/admin/feedback", params={"tag": "wrong_answer"}).json()["total"],
            0,
        )
        self.assertEqual(
            client.get("/api/admin/feedback", params={"date": "2000-01-01"}).json()["total"],
            0,
        )
        self.assertEqual(
            client.get("/api/admin/feedback", params={"date": ""}).status_code,
            200,
        )
        self.assertEqual(
            client.get("/api/admin/feedback", params={"date": "not-a-date"}).status_code,
            400,
        )
        detail = client.get(f"/api/admin/feedback/{saved.feedback_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["conversation"][0]["message"], "请选择候选题。")
        reviewed = client.patch(
            f"/api/admin/feedback/{saved.feedback_id}/review",
            headers={"x-csrf-token": csrf},
            json={"review_status": "resolved", "admin_note": "已复核"},
        )
        self.assertEqual(reviewed.status_code, 200)

        settings = client.patch(
            "/api/admin/settings",
            headers={"x-csrf-token": csrf},
            json={
                "global_daily_budget_cny": "40",
                "default_invite_daily_budget_cny": "4",
                "feedback_retention_days": 60,
            },
        )
        self.assertEqual(settings.status_code, 200, settings.text)
        self.assertEqual(self.control.settings()["global_daily_budget_micros"], 40_000_000)

    @staticmethod
    def _create_cost_schema(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE model_cost_runs (
                    run_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                    identity_key TEXT NOT NULL, search_key TEXT NOT NULL,
                    task_kind TEXT NOT NULL, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL, outcome TEXT NOT NULL,
                    call_count INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    estimated_cost_micros INTEGER NOT NULL,
                    warning_codes_json TEXT NOT NULL, schema_version INTEGER NOT NULL
                )
                """
            )

    @staticmethod
    def _insert_cost(path: Path, *, identity_key: str, search_key: str) -> None:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                INSERT INTO model_cost_runs VALUES (
                    'run-1', 'session-key', ?, ?, 'image', ?, ?, 'candidates',
                    2, 100, 1250000, '[]', 2
                )
                """,
                (identity_key, search_key, now, now),
            )


if __name__ == "__main__":
    unittest.main()

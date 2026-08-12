from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from scripts import manage_tiku_admin
from tiku_admin.app import create_admin_app
from tiku_admin.auth import SQLiteInviteAccess
from tiku_admin.control_store import SQLiteControlStore, cny_to_micros
from tiku_admin.invite_vault import InvitationCodeVault, mask_invitation_code
from tiku_admin.reporting import AdminReporter
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.invite_access import InviteAccess, build_invitation_config


class TikuAdminTest(unittest.TestCase):
    def setUp(self):
        self.root = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"admin_{uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.key_path = self.root / "invite_code_encryption.key"
        self.vault = InvitationCodeVault.load_or_create(self.key_path)
        self.control = SQLiteControlStore(
            self.root / "control.sqlite3",
            invitation_vault=self.vault,
        )

    def test_control_store_hashes_credentials_and_revokes_old_invite_sessions(self):
        self.control.initialize_admin("a-secure-admin-password")
        self.assertTrue(self.control.verify_admin_password("a-secure-admin-password"))
        self.assertFalse(self.control.verify_admin_password("wrong-password"))

        invitation, code = self.control.create_invitation(label="首位内测用户")
        self.assertNotIn(code.encode("utf-8"), (self.root / "control.sqlite3").read_bytes())
        self.assertTrue(invitation.code_recoverable)
        self.assertEqual(invitation.code_preview, mask_invitation_code(code))
        self.assertEqual(self.control.reveal_invitation_code(invitation.invite_id), code)
        self.assertNotIn(self.key_path.read_bytes(), (self.root / "control.sqlite3").read_bytes())
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
        self.assertEqual(self.control.reveal_invitation_code(invitation.invite_id), new_code)

    def test_invitation_vault_binds_ciphertext_to_invitation_and_key(self):
        invitation, code = self.control.create_invitation(label="加密邀请码")
        other_vault = InvitationCodeVault.load_or_create(self.root / "other.key")
        with sqlite3.connect(self.control.path) as connection:
            encrypted = connection.execute(
                "SELECT encrypted_code FROM invitations WHERE invite_id = ?",
                (invitation.invite_id,),
            ).fetchone()[0]
        with self.assertRaisesRegex(ValueError, "invalid invitation ciphertext"):
            other_vault.open(invitation.invite_id, encrypted)
        with self.assertRaisesRegex(ValueError, "invalid invitation ciphertext"):
            self.vault.open("different-invite", encrypted)
        self.assertEqual(self.vault.open(invitation.invite_id, encrypted), code)

    def test_dynamic_settings_and_audit_are_persisted(self):
        values = self.control.update_settings(
            global_daily_budget_micros=cny_to_micros("42.50"),
            default_invite_daily_budget_micros=cny_to_micros("4.25"),
            feedback_retention_days=45,
        )
        self.assertEqual(values["global_daily_budget_micros"], 42_500_000)
        self.assertEqual(self.control.settings()["feedback_retention_days"], 45)
        self.assertEqual(self.control.list_audit()[0]["action"], "settings.update")

    def test_legacy_import_merges_with_existing_invites_and_preserves_old_cookie(self):
        current, current_code = self.control.create_invitation(label="后台新邀请码")
        legacy_data, legacy_codes = build_invitation_config(2)
        legacy_path = self.root / "legacy_invites.json"
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")
        legacy_access = InviteAccess(legacy_path, auth_max_age_seconds=60)
        legacy_identity = legacy_access.authenticate_code(legacy_codes[0][1])
        legacy_cookie = legacy_access.issue_cookie(legacy_identity, now=100)

        preflight = self.control.preflight_legacy_config(legacy_path)
        self.assertTrue(preflight.can_apply)
        self.assertEqual(preflight.existing_count, 1)
        self.assertEqual(preflight.insert_count, 2)
        self.assertEqual(preflight.cookie_secret_action, "replace_with_legacy")

        applied = self.control.import_legacy_config(legacy_path)
        self.assertEqual(applied.insert_count, 2)
        self.assertEqual(len(self.control.list_invitations(include_archived=True)), 3)
        legacy_record = self.control.get_invitation(legacy_codes[0][0])
        self.assertFalse(legacy_record.code_recoverable)
        self.assertEqual(legacy_record.code_preview, "")
        self.assertIsNone(self.control.reveal_invitation_code(legacy_record.invite_id))
        access = SQLiteInviteAccess(self.control, auth_max_age_seconds=60)
        self.assertEqual(access.authenticate_code(current_code).invite_id, current.invite_id)
        self.assertEqual(
            access.authenticate_code(legacy_codes[0][1]).invite_id,
            legacy_codes[0][0],
        )
        self.assertEqual(access.verify_cookie(legacy_cookie, now=120), legacy_identity)

        repeated = self.control.import_legacy_config(legacy_path)
        self.assertEqual(repeated.insert_count, 0)
        self.assertEqual(repeated.unchanged_count, 2)
        self.assertEqual(repeated.cookie_secret_action, "unchanged")
        self.assertEqual(len(self.control.list_invitations(include_archived=True)), 3)

        self.control.set_invitation_status(legacy_identity.invite_id, "disabled")
        strict = self.control.preflight_legacy_config(
            legacy_path, require_status_match=True
        )
        self.assertFalse(strict.can_apply)
        self.assertEqual(strict.unchanged_count, 1)
        self.assertEqual(
            [conflict.kind for conflict in strict.conflicts],
            ["invitation_status_mismatch"],
        )
        with self.assertRaisesRegex(ValueError, "has conflicts"):
            self.control.import_legacy_config(
                legacy_path, require_status_match=True
            )
        self.control.set_invitation_status(legacy_identity.invite_id, "enabled")
        version_strict = self.control.preflight_legacy_config(
            legacy_path, require_status_match=True
        )
        self.assertFalse(version_strict.can_apply)
        self.assertEqual(
            [conflict.kind for conflict in version_strict.conflicts],
            ["invitation_auth_version_mismatch"],
        )
        self.control.import_legacy_config(legacy_path)
        self.assertEqual(self.control.get_invitation(legacy_identity.invite_id).status, "enabled")
        self.assertIsNone(access.verify_cookie(legacy_cookie, now=120))

    def test_legacy_import_reports_conflicts_without_mutating_control_store(self):
        current, current_code = self.control.create_invitation(label="已有邀请码")
        original_secret = self.control.invite_cookie_secret
        legacy_data, _legacy_codes = build_invitation_config(1)
        legacy_data["invitations"] = [
            {
                "id": current.invite_id,
                "code_hash": sha256(b"different-code").hexdigest(),
                "enabled": True,
            },
            {
                "id": "different-invite-id",
                "code_hash": sha256(current_code.encode("utf-8")).hexdigest(),
                "enabled": True,
            },
        ]
        legacy_path = self.root / "conflicting_legacy_invites.json"
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        report = self.control.preflight_legacy_config(legacy_path)
        self.assertFalse(report.can_apply)
        self.assertEqual(report.insert_count, 0)
        self.assertEqual(
            {conflict.kind for conflict in report.conflicts},
            {"invite_id_hash_mismatch", "code_hash_owned_by_other_invitation"},
        )
        with self.assertRaisesRegex(ValueError, "has conflicts"):
            self.control.import_legacy_config(legacy_path)
        self.assertEqual(self.control.invite_cookie_secret, original_secret)
        self.assertEqual(len(self.control.list_invitations(include_archived=True)), 1)

    def test_manage_command_import_is_dry_run_unless_apply_is_explicit(self):
        self.control.initialize_admin("a-secure-admin-password")
        original_secret = self.control.invite_cookie_secret
        legacy_data, _legacy_codes = build_invitation_config(1)
        legacy_path = self.root / "legacy_invites.json"
        legacy_path.write_text(json.dumps(legacy_data), encoding="utf-8")
        arguments = [
            "manage_tiku_admin.py",
            "--control-db",
            str(self.control.path),
            "--import-invites",
            str(legacy_path),
        ]

        output = io.StringIO()
        with patch.object(sys, "argv", arguments), redirect_stdout(output):
            result = manage_tiku_admin.main()

        self.assertEqual(result, 0)
        self.assertIn("no changes written", output.getvalue())
        self.assertEqual(self.control.invite_cookie_secret, original_secret)
        self.assertEqual(self.control.list_invitations(include_archived=True), [])

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
        invitation_list = client.get("/api/admin/invitations")
        self.assertNotIn(payload["code"], invitation_list.text)
        listed = invitation_list.json()["items"][0]
        self.assertTrue(listed["code_recoverable"])
        self.assertEqual(listed["code_preview"], mask_invitation_code(payload["code"]))
        self.assertEqual(
            client.post(f"/api/admin/invitations/{invite_id}/reveal").status_code,
            403,
        )
        revealed = client.post(
            f"/api/admin/invitations/{invite_id}/reveal",
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(revealed.status_code, 200, revealed.text)
        self.assertEqual(revealed.json()["code"], payload["code"])
        self.assertEqual(revealed.headers["cache-control"], "private, no-store")
        self.assertEqual(self.control.list_audit()[0]["action"], "invitation.reveal")

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

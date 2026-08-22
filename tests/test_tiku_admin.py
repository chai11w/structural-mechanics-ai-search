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
from tiku_shared.model_costs import ModelCostCollector, SQLiteModelCostLedger


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

    def test_admin_ui_uses_user_for_invitation_labels(self):
        script = (
            Path(__file__).resolve().parents[1] / "tiku_admin" / "web" / "admin.js"
        ).read_text(encoding="utf-8")
        self.assertIn("<th>用户</th><th>状态</th><th>邀请码</th>", script)
        self.assertIn('<dt>用户</dt><dd>${escapeHtml(feedbackUserLabel(data))}</dd>', script)
        self.assertIn('for="filter-invite">用户</label>', script)
        self.assertIn("全部用户", script)
        self.assertIn("未命名用户", script)
        self.assertIn("已归档用户", script)
        self.assertIn("identity_status", script)
        self.assertNotIn("} · ${escapeHtml(item.invite_id)}", script)
        self.assertNotIn("未填写备注", script)
        self.assertIn('class="icon-button danger"', script)
        self.assertIn('class="archive-restore-button"', script)
        icons = (
            Path(__file__).resolve().parents[1] / "tiku_admin" / "web" / "lucide.svg"
        ).read_text(encoding="utf-8")
        self.assertIn('<symbol id="trash-2"', icons)
        self.assertIn('/assets/lucide.svg?v=20260812-3#${id}', script)

    def test_admin_feedback_ui_uses_real_chapter_options_and_archive_actions(self):
        script = (
            Path(__file__).resolve().parents[1] / "tiku_admin" / "web" / "admin.js"
        ).read_text(encoding="utf-8")
        self.assertIn('<th class="col-number">反馈编号</th>', script)
        self.assertIn('<th class="col-scope">范围</th>', script)
        self.assertIn('for="filter-chapter">章节</label><select', script)
        self.assertIn('for="filter-scope">反馈范围</label><select', script)
        self.assertIn('<option value="page"', script)
        self.assertIn('<option value="question"', script)
        self.assertIn('<option value="">全部章节</option>${chapterOptions}', script)
        self.assertNotIn('for="filter-tag">反馈原因</label>', script)
        self.assertIn("显示已归档", script)
        self.assertIn("流程耗时", script)
        self.assertIn("关联费用", script)
        self.assertIn("整页识别处理流程", script)
        self.assertIn("单题检索处理流程", script)
        self.assertIn("取消归档", script)
        self.assertIn("永久删除反馈", script)
        self.assertIn('data-feedback-action="delete"', script)
        self.assertIn('data-feedback-action="restore"', script)
        self.assertIn("const detailLink = item.archived_at ? ''", script)
        self.assertIn("data.total > filters.limit", script)
        self.assertIn("audit-pagination", script)
        self.assertNotIn("data.audit.slice(0, 10)", script)
        self.assertIn("filters.date || '选择日期'", script)
        self.assertIn("date-filter-value", script)
        self.assertIn("feedback-filter-select", script)
        self.assertIn("select.classList.toggle('is-placeholder', !select.value)", script)
        self.assertIn("feedbackSummaryCards", script)
        self.assertIn("feedback_scope", script)
        self.assertIn("today_page_searches", script)
        self.assertIn("today_question_searches", script)
        self.assertIn("整页框选结果", script)
        self.assertIn("message-overlay-missing", script)
        self.assertIn("这条反馈未保存整页框选结果。", script)
        self.assertNotIn("这条历史反馈提交时未保存整页框选结果。", script)
        styles = (
            Path(__file__).resolve().parents[1] / "tiku_admin" / "web" / "admin.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".feedback-filters .input, .feedback-filters .select { min-width: 0;", styles)
        self.assertIn(".feedback-filter-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));", styles)
        self.assertIn(".date-filter-input { position: absolute;", styles)
        self.assertIn(".feedback-filter-select { font-size: 13px; }", styles)
        self.assertIn(".feedback-filter-select.is-placeholder { color: #8b8b84; }", styles)
        self.assertIn(".feedback-filter-submit { height: 40px; min-height: 40px; }", styles)
        self.assertIn(".feedback-table { min-width: 1138px; table-layout: fixed; }", styles)
        self.assertIn(".feedback-table td { height: 70px; padding-block: 12px; }", styles)
        self.assertIn(".feedback-card-list { display: none; }", styles)
        self.assertIn(".feedback-list-table-wrap { display: none; }", styles)
        self.assertIn(".feedback-scope.scope-page", styles)
        self.assertIn(".message-overlay { margin: 13px 0 0;", styles)
        icons = (
            Path(__file__).resolve().parents[1] / "tiku_admin" / "web" / "lucide.svg"
        ).read_text(encoding="utf-8")
        self.assertIn('<symbol id="calendar-days"', icons)

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
            search_duration_ms=12_340,
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
        self.assertRegex(saved.feedback_number, r"^FB-\d{8}-[0-9A-F]{10}$")
        self.assertEqual(saved.search_duration_ms, 12_340)
        self.assertEqual(store.list_chapters(), ["4力法"])
        media_name = saved.conversation[0]["images"][0]
        self.assertTrue(store.resolve_case_media(saved.feedback_id, media_name).is_file())
        reviewed = store.update_review(
            saved.feedback_number, review_status="resolved", admin_note="已记录排序问题"
        )
        self.assertEqual(reviewed.review_status, "resolved")
        self.assertEqual(store.query_feedback(rating="negative")[1], 1)
        archived = store.set_archived(saved.feedback_number, archived=True)
        self.assertTrue(archived.archived_at)
        self.assertEqual(store.query_feedback(rating="negative")[1], 0)
        self.assertEqual(
            store.query_feedback(rating="negative", include_archived=True)[1], 1
        )
        newer = store.upsert(
            message_id="message_87654321",
            identity_key="invite-001",
            session_key="newer-session",
            rating="positive",
            tags=("found_answer",),
            detail="较新的反馈",
            task_revision=3,
            phase="ANSWER_SHOWN",
            candidate_count=1,
            search_key="newer-session:3",
            chapter="4力法",
            conversation=[],
            retention_days=365,
        )
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE message_feedback SET created_at = ? WHERE feedback_id = ?",
                ((datetime.now(UTC) + timedelta(seconds=1)).isoformat(), newer.feedback_id),
            )
        ordered, _total = store.query_feedback(include_archived=True)
        self.assertEqual(ordered[0].detail, "较新的反馈")
        restored = store.set_archived(saved.feedback_number, archived=False)
        self.assertEqual(restored.archived_at, "")

        purged = store.purge_expired_cases(
            now=datetime.now(UTC) + timedelta(days=31)
        )
        self.assertEqual(purged, 1)
        self.assertEqual(store.get_feedback(saved.feedback_id).conversation, ())
        self.assertIsNone(store.resolve_case_media(saved.feedback_id, media_name))
        store.set_archived(saved.feedback_number, archived=True)
        self.assertTrue(store.delete_archived(saved.feedback_number))
        self.assertIsNone(store.get_feedback(saved.feedback_number))

    def test_feedback_media_route_rejects_unreferenced_case_files(self):
        source = self.root / "current-page.jpg"
        source.write_bytes(b"current-page")
        store = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        target_id = "message_current_page"
        saved = store.upsert(
            message_id=target_id,
            identity_key="invite-media",
            session_key="session-media",
            rating="positive",
            tags=("found_answer",),
            detail="",
            task_revision=1,
            phase="WAIT_CHAPTER",
            candidate_count=0,
            conversation=[
                {
                    "me": True,
                    "message": "当前整页",
                    "images": ["/api/upload/current-page.jpg"],
                    "taskRevision": 1,
                },
                {
                    "me": False,
                    "message": "当前回复",
                    "messageId": target_id,
                    "taskRevision": 1,
                },
            ],
            media_resolver=lambda value: source if value.endswith(source.name) else None,
        )
        allowed_name = saved.conversation[0]["images"][0]
        orphan_name = "old-page.jpg"
        (store.cases_root / saved.feedback_id / orphan_name).write_bytes(b"old-page")
        reporter = AdminReporter(
            control_store=self.control,
            cost_databases=(),
            feedback_store=store,
        )
        client = TestClient(
            create_admin_app(
                control_store=self.control,
                reporter=reporter,
                feedback_store=store,
            )
        )
        setup = client.post(
            "/api/admin/setup",
            json={
                "password": "a-secure-admin-password",
                "confirm_password": "a-secure-admin-password",
            },
        )
        self.assertEqual(setup.status_code, 200)

        media_root = f"/api/admin/feedback/{saved.feedback_id}/media"
        self.assertEqual(client.get(f"{media_root}/{allowed_name}").status_code, 200)
        self.assertEqual(client.get(f"{media_root}/{orphan_name}").status_code, 404)

    def test_feedback_reporting_scopes_legacy_case_to_current_uploaded_page(self):
        store = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        target_id = "message_current_page"
        saved = store.upsert(
            message_id=target_id,
            identity_key="invite-legacy",
            session_key="session-legacy",
            rating="positive",
            tags=("found_answer",),
            detail="框选正确",
            task_revision=2,
            phase="WAIT_CHAPTER",
            candidate_count=9,
            conversation=[
                {
                    "me": True,
                    "message": "当前整页",
                    "images": ["/api/upload/current.jpg"],
                    "taskRevision": 2,
                },
                {
                    "me": False,
                    "message": "已准备 9 道题。",
                    "messageId": target_id,
                    "taskRevision": 2,
                },
            ],
            media_resolver=lambda _value: None,
        )
        polluted = [
            {"role": "user", "message": "上一页", "images": ["old.jpg"], "task_revision": 1},
            {"role": "assistant", "message": "上一页结果", "images": [], "message_id": "message_old", "task_revision": 1},
            {"role": "user", "message": "当前整页", "images": ["current.jpg"], "task_revision": 2},
            {
                "role": "assistant",
                "message": "已准备 9 道题。",
                "images": [],
                "a3_overlay": "overlay.jpg",
                "intent": "a3_units_prepared",
                "message_id": target_id,
                "task_revision": 2,
            },
        ]
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE message_feedback SET conversation_json = ?, "
                "feedback_scope = '', schema_version = 6 WHERE feedback_id = ?",
                (json.dumps(polluted, ensure_ascii=False), saved.feedback_id),
            )

        reporter = AdminReporter(
            control_store=self.control,
            cost_databases=(),
            feedback_store=store,
        )
        detail = reporter.feedback_detail(saved.feedback_number)
        self.assertEqual(detail["feedback_scope"], "page")
        self.assertEqual(len(detail["conversation"]), 2)
        self.assertEqual(detail["conversation"][0]["message"], "当前整页")
        self.assertTrue(detail["conversation"][1]["a3_overlay"].endswith("/overlay.jpg"))
        summary = reporter.feedback_list()["items"][0]
        self.assertEqual(summary["feedback_scope"], "page")
        self.assertTrue(summary["preview_image"].endswith("/current.jpg"))

    def test_feedback_detail_combines_a3_workflow_and_a2_question_costs(self):
        self.control.initialize_admin("a-secure-admin-password")
        invitation, _code = self.control.create_invitation(label="流程用户")
        other_invitation, _other_code = self.control.create_invitation(label="其他流程用户")
        feedback = SQLiteFeedbackStore(self.root / "feedback.sqlite3")
        workflow_db = self.root / "model_costs.sqlite3"
        question_db = self.root / "a2" / "model_costs.sqlite3"
        started_at = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()

        workflow = ModelCostCollector(
            run_id="workflow-run",
            identity_key=invitation.invite_id,
            search_key="workflow-search",
            task_kind="a3_auto_crop_grounding",
            started_at=started_at,
        )
        workflow.record(
            provider="zhipu",
            model="glm-5v-turbo",
            call_type="glm_a3_page_auto_crop",
            status="success",
            started_at=started_at,
            finished_at=started_at,
            latency_ms=500,
            usage={"input_tokens": 1000, "output_tokens": 100},
        )
        SQLiteModelCostLedger(workflow_db).write_run(
            workflow, finished_at=started_at, outcome="success"
        )
        with sqlite3.connect(workflow_db) as connection:
            connection.execute(
                "UPDATE model_cost_calls SET pricing_status = 'missing_price', "
                "price_version = '', estimated_cost_micros = 0"
            )
            connection.execute(
                "UPDATE model_cost_runs SET estimated_cost_micros = 0, "
                "warning_codes_json = '[\"PRICE_MISSING_OR_OUTSIDE_TIER\"]'"
            )

        question = ModelCostCollector(
            run_id="question-run",
            identity_key=invitation.invite_id,
            search_key="question-search",
            task_kind="image",
            started_at=started_at,
        )
        question.record(
            provider="dashscope",
            model="qwen3.7-plus",
            call_type="qwen_shape_rerank",
            status="success",
            started_at=started_at,
            finished_at=started_at,
            latency_ms=700,
            usage={"input_tokens": 1000, "output_tokens": 100},
        )
        SQLiteModelCostLedger(question_db).write_run(
            question, finished_at=started_at, outcome="success"
        )
        intent = ModelCostCollector(
            run_id="intent-run",
            identity_key=invitation.invite_id,
            search_key="question-search",
            task_kind="a3_intent",
            started_at=started_at,
        )
        intent.record(
            provider="dashscope",
            model="qwen3.7-plus",
            call_type="qwen_a3_intent_decision",
            status="success",
            started_at=started_at,
            finished_at=started_at,
            latency_ms=120,
            usage={"input_tokens": 0, "output_tokens": 0},
        )
        SQLiteModelCostLedger(question_db).write_run(
            intent, finished_at=started_at, outcome="success"
        )
        colliding = ModelCostCollector(
            run_id="other-user-colliding-run",
            identity_key=other_invitation.invite_id,
            search_key="workflow-search",
            task_kind="a3_page_understanding_retry",
            started_at=started_at,
        )
        SQLiteModelCostLedger(workflow_db).write_run(
            colliding, finished_at=started_at, outcome="success"
        )
        saved = feedback.upsert(
            message_id="message_trace_01",
            identity_key=invitation.invite_id,
            session_key="session-key",
            rating="negative",
            tags=("ranking_issue",),
            detail="需要查看完整流程",
            task_revision=1,
            phase="WAIT_CANDIDATE_CHOICE",
            candidate_count=3,
            search_key="question-search",
            search_id="question-search",
            workflow_search_id="workflow-search",
            image_route="A3",
        )
        reporter = AdminReporter(
            control_store=self.control,
            cost_databases=(workflow_db, question_db),
            feedback_store=feedback,
        )

        detail = reporter.feedback_detail(saved.feedback_id)

        self.assertEqual(detail["cost"]["estimated_cost_micros"], 10_000)
        self.assertEqual(detail["cost"]["route"], "A3")
        self.assertTrue(detail["cost"]["historical_reprice_applied"])
        self.assertEqual(
            [step["key"] for step in detail["cost"]["flow"]],
            ["a3_auto_crop_grounding", "a3_intent", "image"],
        )
        intent_step = detail["cost"]["flow"][1]
        self.assertEqual(intent_step["title"], "A3 意图理解")
        self.assertEqual(intent_step["calls"][0]["label"], "Qwen A3 意图判断")
        self.assertEqual(
            {item["model"] for item in detail["cost"]["models"]},
            {"glm-5v-turbo", "qwen3.7-plus"},
        )
        self.assertEqual(detail["feedback_scope"], "question")

        target_created_at = datetime.fromisoformat(started_at) + timedelta(seconds=2)
        page_saved = feedback.upsert(
            message_id="message_page_trace_01",
            identity_key=invitation.invite_id,
            session_key="session-key",
            rating="negative",
            tags=("irrelevant_results",),
            detail="整页框选需要复核",
            task_revision=1,
            phase="WAIT_UNIT_SELECTION",
            candidate_count=2,
            search_key="question-search",
            search_id="question-search",
            workflow_search_id="workflow-search",
            image_route="A3",
            conversation=[{
                "me": False,
                "message": "已准备 2 道题。",
                "messageId": "message_page_trace_01",
                 "intent": "a3_units_prepared",
                 "taskRevision": 1,
                 "createdAt": int(target_created_at.timestamp() * 1000),
             }],
         )
        late_page_run = ModelCostCollector(
            run_id="late-page-run",
            identity_key=invitation.invite_id,
            search_key="workflow-search",
            task_kind="a3_page_understanding_retry",
            started_at=(target_created_at + timedelta(seconds=2)).isoformat(),
        )
        SQLiteModelCostLedger(workflow_db).write_run(
            late_page_run,
            finished_at=(target_created_at + timedelta(seconds=2)).isoformat(),
            outcome="success",
        )
        page_detail = reporter.feedback_detail(page_saved.feedback_id)
        self.assertEqual(page_detail["feedback_scope"], "page")
        self.assertEqual(
            [step["key"] for step in page_detail["cost"]["flow"]],
            ["a3_auto_crop_grounding"],
        )
        self.assertLess(
            page_detail["cost"]["estimated_cost_micros"],
            detail["cost"]["estimated_cost_micros"],
        )
        skewed_saved = feedback.upsert(
            message_id="message_page_skewed_01",
            identity_key=invitation.invite_id,
            session_key="session-key",
            rating="negative",
            tags=("irrelevant_results",),
            detail="设备时间异常时仍应显示费用",
            task_revision=1,
            phase="WAIT_UNIT_SELECTION",
            candidate_count=2,
            search_key="question-search",
            search_id="question-search",
            workflow_search_id="workflow-search",
            image_route="A3",
            conversation=[{
                "me": False,
                "message": "已准备 2 道题。",
                "messageId": "message_page_skewed_01",
                "intent": "a3_units_prepared",
                "taskRevision": 1,
                "createdAt": 2_000,
            }],
        )
        skewed_detail = reporter.feedback_detail(skewed_saved.feedback_id)
        self.assertGreater(skewed_detail["cost"]["estimated_cost_micros"], 0)
        self.assertEqual(reporter.feedback_list(feedback_scope="page")["total"], 2)
        self.assertEqual(reporter.feedback_list(feedback_scope="question")["total"], 1)
        with self.assertRaisesRegex(ValueError, "feedback scope"):
            reporter.feedback_list(feedback_scope="invalid")
        self.assertEqual(reporter.overview()["today_searches"], 1)

    def test_usage_reports_uploaded_pages_and_a2_questions_as_separate_metrics(self):
        first, _first_code = self.control.create_invitation(label="整页用户")
        second, _second_code = self.control.create_invitation(label="历史整页用户")
        costs = self.root / "model_costs.sqlite3"
        ledger = SQLiteModelCostLedger(costs)
        now = datetime.now(UTC).isoformat()
        sequence = 0

        def record(identity_key: str, search_key: str, task_kind: str) -> None:
            nonlocal sequence
            sequence += 1
            ledger.write_run(
                ModelCostCollector(
                    run_id=f"usage-run-{sequence}",
                    session_key=f"session-{identity_key}",
                    identity_key=identity_key,
                    search_key=search_key,
                    task_kind=task_kind,
                    started_at=now,
                ),
                finished_at=now,
                outcome="success",
            )

        # One A3 upload emits several workflow stages but is one page.
        record(first.invite_id, "workflow-page", "image_triage")
        record(first.invite_id, "workflow-page", "a3_page_understanding")
        record(first.invite_id, "workflow-page", "a3_page_understanding_retry")
        record(first.invite_id, "workflow-page", "a3_auto_crop_grounding")
        # Retries for one A2 question keep the same search key and count once.
        record(first.invite_id, "question-one", "image")
        record(first.invite_id, "question-one", "image")
        record(first.invite_id, "question-two", "a3_verified_image")

        # A direct A2 upload is still one user-initiated page.  Its parent
        # triage key counts the page and its child A2 key counts the question.
        record(first.invite_id, "workflow-direct-a2", "image_triage")
        record(first.invite_id, "workflow-direct-a2", "a3_external_load_screen")
        record(first.invite_id, "question-direct-a2", "image")

        # Historical deployments can lack triage records.  A decisive A3 page
        # stage remains sufficient.  Reused imported keys remain separate per
        # identity for both page and question totals.
        record(second.invite_id, "workflow-page", "a3_page_understanding")
        record(second.invite_id, "question-one", "image")
        record("", "anonymous-page", "image_triage")
        record("", "anonymous-question", "image")

        reporter = AdminReporter(
            control_store=self.control,
            cost_database=costs,
            feedback_store=SQLiteFeedbackStore(self.root / "feedback.sqlite3"),
        )
        overview = reporter.overview()

        self.assertEqual(overview["today_page_searches"], 4)
        self.assertEqual(overview["today_question_searches"], 5)
        self.assertEqual(overview["today_searches"], 5)
        by_invite = {item["invite_id"]: item for item in overview["invites"]}
        self.assertEqual(by_invite[first.invite_id]["today_page_searches"], 2)
        self.assertEqual(by_invite[first.invite_id]["today_question_searches"], 3)
        self.assertEqual(by_invite[first.invite_id]["today_searches"], 3)
        self.assertEqual(by_invite[second.invite_id]["today_page_searches"], 1)
        self.assertEqual(by_invite[second.invite_id]["today_question_searches"], 1)

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
            search_duration_ms=8_765,
            search_key="search-one",
            chapter="4力法",
            conversation=[{
                "me": False,
                "message": "请选择候选题。",
                "messageId": "message_abcdefgh",
                "createdAt": int(datetime.now(UTC).timestamp() * 1000),
            }],
        )

        overview = client.get("/api/admin/overview").json()
        self.assertEqual(overview["today_searches"], 1)
        self.assertEqual(overview["today_question_searches"], 1)
        self.assertEqual(overview["today_page_searches"], 0)
        self.assertEqual(overview["invites"][0]["today_searches"], 1)
        self.assertEqual(overview["invites"][0]["today_question_searches"], 1)
        self.assertEqual(overview["invites"][0]["today_page_searches"], 0)
        self.assertEqual(overview["pending_negative_feedback"], 1)
        self.assertEqual(overview["invites"][0]["today_cost_cny"], "1.25")
        filtered = client.get(
            "/api/admin/feedback",
            params={
                "chapter": "4力法",
                "date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["invite_label"], "真实用户 A")
        self.assertEqual(filtered.json()["items"][0]["feedback_scope"], "question")
        self.assertEqual(filtered.json()["chapters"], ["4力法"])
        self.assertRegex(
            filtered.json()["items"][0]["feedback_number"],
            r"^FB-\d{8}-[0-9A-F]{10}$",
        )
        self.assertEqual(filtered.json()["items"][0]["cost"]["estimated_cost_cny"], "1.25")
        self.assertEqual(
            client.get("/api/admin/feedback", params={"chapter": "不存在"}).json()["total"],
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
            client.get(
                "/api/admin/feedback", params={"feedback_scope": "question"}
            ).json()["total"],
            1,
        )
        self.assertEqual(
            client.get(
                "/api/admin/feedback", params={"feedback_scope": "page"}
            ).json()["total"],
            0,
        )
        self.assertEqual(
            client.get(
                "/api/admin/feedback", params={"feedback_scope": "invalid"}
            ).status_code,
            400,
        )
        self.assertEqual(
            client.get("/api/admin/feedback", params={"date": "not-a-date"}).status_code,
            400,
        )
        detail = client.get(f"/api/admin/feedback/{saved.feedback_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["feedback_scope"], "question")
        self.assertEqual(detail.json()["conversation"][0]["message"], "请选择候选题。")
        self.assertEqual(detail.json()["search_duration_ms"], 8_765)
        self.assertEqual(
            client.get(f"/api/admin/feedback/{saved.feedback_number}").status_code,
            200,
        )
        reviewed = client.patch(
            f"/api/admin/feedback/{saved.feedback_number}/review",
            headers={"x-csrf-token": csrf},
            json={"review_status": "resolved", "admin_note": "已复核"},
        )
        self.assertEqual(reviewed.status_code, 200)

        archived = client.post(
            f"/api/admin/feedback/{saved.feedback_number}/archive",
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual(client.get("/api/admin/feedback").json()["total"], 0)
        archived_list = client.get(
            "/api/admin/feedback", params={"include_archived": "true"}
        ).json()
        self.assertEqual(archived_list["total"], 1)
        self.assertTrue(archived_list["items"][0]["archived_at"])
        self.assertEqual(
            client.get(f"/api/admin/feedback/{saved.feedback_number}").status_code,
            409,
        )
        restored = client.post(
            f"/api/admin/feedback/{saved.feedback_number}/restore",
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(restored.status_code, 200, restored.text)

        self.control.set_invitation_status(invite_id, "archived")
        archived_user_feedback = client.get(
            "/api/admin/feedback", params={"identity_status": "archived"}
        )
        self.assertEqual(archived_user_feedback.status_code, 200)
        self.assertEqual(archived_user_feedback.json()["total"], 1)
        self.assertEqual(
            archived_user_feedback.json()["items"][0]["identity_key"], invite_id
        )
        self.assertEqual(
            client.get(
                "/api/admin/feedback", params={"identity_status": "invalid"}
            ).status_code,
            400,
        )
        blocked_delete = client.delete(
            f"/api/admin/invitations/{invite_id}", headers={"x-csrf-token": csrf}
        )
        self.assertEqual(blocked_delete.status_code, 409)
        restored_invite = client.post(
            f"/api/admin/invitations/{invite_id}/status",
            headers={"x-csrf-token": csrf},
            json={"status": "enabled"},
        )
        self.assertEqual(restored_invite.status_code, 200)

        unused, _unused_code = self.control.create_invitation(label="未使用用户")
        self.control.set_invitation_status(unused.invite_id, "archived")
        deleted_invite = client.delete(
            f"/api/admin/invitations/{unused.invite_id}",
            headers={"x-csrf-token": csrf},
        )
        self.assertEqual(deleted_invite.status_code, 200, deleted_invite.text)
        self.assertIsNone(self.control.get_invitation(unused.invite_id))

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
        settings_page = client.get(
            "/api/admin/settings", params={"audit_limit": 2, "audit_offset": 1}
        )
        self.assertEqual(settings_page.status_code, 200)
        self.assertEqual(settings_page.json()["audit_limit"], 2)
        self.assertEqual(settings_page.json()["audit_offset"], 1)
        self.assertGreaterEqual(settings_page.json()["audit_total"], 2)

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

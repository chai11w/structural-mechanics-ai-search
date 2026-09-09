from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tiku_agent.execution_maintenance import inspect_execution, plan_cost_reconciliation, apply_cost_reconciliation
from tiku_agent.execution_store import ExecutionError
from tests import test_execution_effects as effects_fixture


class ExecutionMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = effects_fixture.ExecutionEffectsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backups = tempfile.TemporaryDirectory()
        self.addCleanup(self.backups.cleanup)
        self.backup = Path(self.backups.name) / "reviewed"

    def pending(self, text="success"):
        f = self.fixture
        with patch.object(f.ledger, "write_run", side_effect=OSError("ledger unavailable")):
            f.runtime.handle_text("s", text, operation_request=f.request())
        return f.rows("execution_cost_runs")[0]["run_id"]

    def plan(self, **kwargs):
        f = self.fixture
        return plan_cost_reconciliation(f.store.path, f.ledger.path, **kwargs)

    def apply(self, plan):
        f = self.fixture
        return apply_cost_reconciliation(f.ops, f.ledger.path, plan, backup_dir=self.backup)

    def test_inspect_and_plan_are_read_only_and_exclude_private_state(self):
        run_id = self.pending()
        f = self.fixture
        before = f.store.path.read_bytes()
        before_wal = Path(str(f.store.path) + "-wal").read_bytes() if Path(str(f.store.path) + "-wal").exists() else None
        result = inspect_execution(f.store.path)
        plan = self.plan()
        self.assertEqual(f.store.path.read_bytes(), before)
        if before_wal is not None:
            self.assertEqual(Path(str(f.store.path) + "-wal").read_bytes(), before_wal)
        self.assertEqual(result["pending_cost_runs"], 1)
        self.assertEqual(plan["items"][0]["run_id"], run_id)
        self.assertEqual(plan["items"][0]["reason"], "READY")
        encoded = json.dumps({"result":result, "plan":plan})
        self.assertNotIn(str(f.root), encoded)
        self.assertNotIn("saved result", encoded)
        self.assertNotIn("provider-result", encoded)

    def test_reviewed_reconciliation_backs_up_both_databases_and_does_not_call_model(self):
        self.pending()
        f = self.fixture
        result = self.apply(self.plan())
        self.assertEqual(result["confirmed_runs"], 1)
        self.assertEqual(f.calls, ["success"])
        self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "CONFIRMED")
        for name in ("execution", "costs"):
            with closing(sqlite3.connect(self.backup / (name + ".sqlite3"))) as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))

    def test_evidence_changed_after_review_is_rejected_before_backup_or_write(self):
        self.pending()
        f = self.fixture
        plan = self.plan()
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_collectors SET closed=closed+1")
        with self.assertRaises(ExecutionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_PLAN_CHANGED")
        self.assertFalse(self.backup.exists())

    def test_confirmed_and_unsent_calls_are_reconciled_without_inventing_a_response(self):
        from tiku_agent.execution_effects import ExecutionEffects
        f = self.fixture
        sent = ExecutionEffects.model_sent
        count = 0
        def fail_second(observer, call_id):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("before second send")
            return sent(observer, call_id)
        with patch.object(ExecutionEffects, "model_sent", new=fail_second):
            with self.assertRaisesRegex(OSError, "before second send"):
                f.runtime.handle_text("s", "schema_retry", operation_request=f.request())
        plan = self.plan()
        self.assertEqual(plan["items"][0]["confirmed_calls"], 1)
        self.assertEqual(plan["items"][0]["not_sent_calls"], 1)
        self.assertEqual(self.apply(plan)["confirmed_runs"], 1)
        self.assertEqual(f.calls, ["schema_retry"])
        effects = f.rows("execution_effects")
        self.assertCountEqual([row["status"] for row in effects], ["CONFIRMED", "NOT_SENT"])
        self.assertIsNone(next(row["record"] for row in effects if row["status"] == "NOT_SENT"))
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))

    def test_prepared_call_changed_to_sent_after_review_is_not_closed(self):
        from tiku_agent.execution_effects import ExecutionEffects
        f = self.fixture
        with patch.object(ExecutionEffects, "model_sent", side_effect=OSError("before send")):
            with self.assertRaises(OSError):
                f.runtime.handle_text("s", "success", operation_request=f.request())
        plan = self.plan()
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_effects SET status='SENT'")
        with self.assertRaises(ExecutionError) as error:
            self.apply(plan)
        self.assertEqual(error.exception.code, "EXECUTION_MAINTENANCE_PLAN_CHANGED")
        self.assertFalse(self.backup.exists())
        self.assertEqual(f.rows("execution_effects")[0]["status"], "SENT")
        self.assertEqual(self.plan()["items"][0]["reason"], "UNKNOWN_USAGE")

    def test_committed_ledger_with_lost_ack_is_confirmed_without_duplicate_charge(self):
        from tiku_agent.execution_effects import ExecutionEffects
        f = self.fixture
        with patch.object(ExecutionEffects, "cost_confirmed", side_effect=OSError("ack lost")):
            f.runtime.handle_text("s", "success", operation_request=f.request())
        self.apply(self.plan())
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*),sum(total_tokens) FROM model_cost_calls").fetchone(), (1, 120))
        self.assertEqual(f.calls, ["success"])

    def test_ledger_conflict_remains_visible_after_apply_rolls_back(self):
        from tiku_agent.execution_effects import ExecutionEffects
        f = self.fixture
        with patch.object(ExecutionEffects, "cost_confirmed", side_effect=OSError("ack lost")):
            f.runtime.handle_text("s", "success", operation_request=f.request())
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            conn.execute("UPDATE model_cost_calls SET total_tokens=999")
            conn.commit()
        with self.assertRaises(ExecutionError) as caught:
            self.apply(self.plan())
        self.assertEqual(caught.exception.code, "EXECUTION_COST_CONFLICT")
        self.assertEqual(f.rows("execution_cost_outbox")[0]["status"], "CONFLICT")
        self.assertEqual(self.plan()["items"][0]["reason"], "CONFLICT")
        self.assertEqual(json.loads((self.backup / "result.json").read_text())["status"], "FAILED")

    def test_interruption_after_ledger_commit_can_be_replanned_without_model_replay(self):
        from tiku_shared.model_costs import SQLiteModelCostLedger
        self.pending()
        write = SQLiteModelCostLedger.write_run
        def ack_lost(ledger, *args, **kwargs):
            write(ledger, *args, **kwargs)
            raise OSError("after ledger commit")
        with patch.object(SQLiteModelCostLedger, "write_run", ack_lost):
            with self.assertRaises(OSError):
                self.apply(self.plan())
        self.backup = self.backup.with_name("replanned")
        self.apply(self.plan())
        f = self.fixture
        with closing(sqlite3.connect(f.ledger.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM model_cost_calls").fetchone()[0], 1)
        self.assertEqual(f.calls, ["success"])

    def test_unknown_usage_is_visible_and_cannot_be_reconciled_as_zero(self):
        self.pending("missing_usage")
        plan = self.plan()
        self.assertEqual(plan["items"][0]["reason"], "UNKNOWN_USAGE")
        with self.assertRaises(ExecutionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_NOTHING_READY")
        self.assertFalse(self.backup.exists())

    def test_wrong_ledger_and_expired_plan_are_rejected(self):
        self.pending()
        f = self.fixture
        wrong = plan_cost_reconciliation(f.store.path, f.root / "wrong.db")
        self.assertEqual(wrong["items"][0]["reason"], "WRONG_LEDGER")
        plan = self.plan()
        f.store.now = lambda: plan["expires_at"] + 1
        with self.assertRaises(ExecutionError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.code, "EXECUTION_MAINTENANCE_PLAN_CHANGED")

    def test_cli_requires_review_hash_and_defaults_to_bounded_inspection(self):
        from scripts.tiku_execution_maintain import main
        self.pending()
        f = self.fixture
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["inspect", "--database", str(f.store.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["pending_cost_runs"], 1)
        plan = self.plan()
        path = f.root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            result = main(["cost-apply", "--database", str(f.store.path), "--ledger", str(f.ledger.path),
                           "--plan", str(path), "--confirm-plan-hash", "wrong", "--backup-dir", str(self.backup)])
        self.assertEqual(result, 1)
        self.assertFalse(self.backup.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["cost-apply", "--database", str(f.store.path), "--ledger", str(f.ledger.path),
                  "--plan", str(path), "--confirm-plan-hash", plan["plan_hash"], "--backup-dir", str(self.backup),
                  "--run-id", "silently-narrowing-a-reviewed-plan-is-not-supported"])
        self.assertFalse(self.backup.exists())
        with redirect_stdout(io.StringIO()):
            result = main(["cost-apply", "--database", str(f.store.path), "--ledger", str(f.ledger.path),
                           "--plan", str(path), "--confirm-plan-hash", plan["plan_hash"], "--backup-dir", str(self.backup)])
        self.assertEqual(result, 0)
        self.assertEqual(f.calls, ["success"])
        self.assertTrue((self.backup / "execution.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()

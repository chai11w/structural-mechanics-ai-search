"""Reserves bound uncertain bills while independent tasks remain usable."""
from dataclasses import replace
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import test_execution_effects as fixtures
from tiku_agent.execution_receipts import ReceiptJournal, recover_accounting
from tiku_agent.execution_store import ExecutionError, digest
from tiku_agent.execution_operations import OperationStore
from tiku_agent.execution_store import ExecutionStore


class AccountingIsolationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionEffectsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.store.policy = replace(self.f.store.policy, max_global_unresolved_cost_calls=100)

    def unknown(self, sid='old'):
        f = self.f
        f.assert_code('EXECUTION_UNKNOWN', lambda: f.runtime.handle_text(sid, 'timeout', operation_request=f.request(sid)))
        return f.rows('execution_effects')[-1]

    def test_unknown_is_reserved_and_another_task_can_complete(self):
        f = self.f
        previous = self.unknown()
        self.assertEqual(f.ops.cost_exposure('local'), {'reserved_micros': 1_000_000, 'unresolved_calls': 1})
        f.runtime.handle_text('new', 'success', operation_request=f.request('new'))
        self.assertEqual(f.calls, ['timeout', 'success'])
        self.assertEqual(f.rows('execution_effects')[0], previous)
        self.assertEqual(len(f.rows('execution_cost_outbox')), 1)

    def test_cancel_and_new_day_do_not_erase_reserve(self):
        f = self.f
        effect = self.unknown()
        with f.store.transaction() as conn:
            conn.execute("UPDATE execution_operations SET status='CANCELLED',updated=updated-86400 WHERE id=?", (effect['operation_id'],))
        self.assertEqual(f.ops.cost_exposure('local')['reserved_micros'], 1_000_000)
        self.assertEqual(f.ops.cost_exposure('different-invite')['reserved_micros'], 0)

    def test_identity_incident_limit_does_not_block_another_identity(self):
        f = self.f
        self.unknown()
        f.store.policy = replace(f.store.policy, max_identity_unresolved_cost_calls=1)
        f.assert_code('EXECUTION_COST_PENDING', lambda: f.ops.ensure_cost_available(identity_digest=digest('local')))
        f.ops.ensure_cost_available(identity_digest=digest('another-invite'))
        f.store.policy = replace(f.store.policy, max_global_unresolved_cost_calls=1)
        f.assert_code('EXECUTION_COST_PENDING', lambda: f.ops.ensure_cost_available(identity_digest=digest('another-invite')))

    def test_identity_and_global_budget_include_unknown_reserve(self):
        f = self.f
        self.unknown()
        f.runtime._per_identity_daily_budget_micros = 500_000
        f.assert_code('INVITE_DAILY_QUOTA_EXCEEDED', lambda: f.runtime.ensure_budget_available('local'))
        f.runtime.ensure_budget_available('another-invite')
        f.runtime._daily_budget_micros = 500_000
        f.assert_code('GLOBAL_DAILY_QUOTA_EXCEEDED', lambda: f.runtime.ensure_budget_available('another-invite'))

    def test_lost_primary_receipt_recovers_without_replaying_business_or_model(self):
        f = self.f
        request = f.request()
        with patch('tiku_agent.execution_receipts.apply_receipt', side_effect=sqlite3.OperationalError('secret provider payload')):
            with self.assertRaises(sqlite3.OperationalError):
                f.runtime.handle_text('s', 'success', operation_request=request)
        self.assertEqual(f.rows('execution_effects')[0]['status'], 'UNKNOWN')
        journal = ReceiptJournal(f.store)
        with journal.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM receipts').fetchone()[0], 1)
            diagnostics = conn.execute('SELECT details FROM diagnostics').fetchall()
            self.assertNotIn('secret provider payload', str(diagnostics))
            self.assertIn('OperationalError', str(diagnostics))
        restarted = OperationStore(ExecutionStore(f.store.path, policy=f.store.policy))
        result = recover_accounting(restarted, [f.ledger])
        self.assertEqual(result, {'receipts_recovered': 1, 'cost_runs_settled': 1})
        self.assertEqual(f.ops.cost_exposure()['reserved_micros'], 0)
        self.assertEqual(f.rows('execution_effects')[0]['status'], 'CONFIRMED')
        f.assert_code('EXECUTION_UNKNOWN', lambda: f.runtime.handle_text('s', 'success', operation_request=request))
        self.assertEqual(f.calls, ['success'])

    def test_already_queued_task_continues_after_another_task_loses_usage(self):
        from test_execution_dispatch import DispatchFixture
        with tempfile.TemporaryDirectory() as root:
            f = DispatchFixture(root)
            f.accept('timeout', sid='first')
            _, req, grant = f.accept(sid='second')
            self.assertTrue(f.worker.run_once())
            self.assertTrue(f.worker.run_once())
            self.assertEqual(f.observe(req, grant, 'second')['status'], 'SUCCEEDED')
            self.assertEqual(f.calls, ['timeout', 'hello'])
            with ReceiptJournal(f.authority).connect() as conn:
                details = str(conn.execute('SELECT details FROM diagnostics').fetchall())
                self.assertIn('TimeoutError', details)
                self.assertNotIn('private timeout details', details)

    def test_receipt_cannot_be_applied_to_another_attempt(self):
        from tiku_agent.execution_receipts import apply_receipt
        f = self.f
        effect = self.unknown()
        previous = dict(effect)
        f.assert_code('EXECUTION_COST_CONFLICT', lambda: apply_receipt(
            f.store, effect['call_id'], 'wrong-attempt', {'record': None, 'confirmed': False, 'usage_known': False}))
        self.assertEqual(f.rows('execution_effects')[0], previous)

    def test_unreadable_existing_ledger_does_not_look_like_zero_spending(self):
        f = self.f
        f.runtime.handle_text('s', 'success', operation_request=f.request())
        with patch('tiku_shared.model_costs._create_schema', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                f.ledger.estimated_cost_micros_since('2000-01-01T00:00:00+00:00')
        self.assertEqual(recover_accounting(f.ops, [f.ledger]), {'receipts_recovered': 0, 'cost_runs_settled': 0})

    def test_unknown_usage_is_never_automatically_reconciled(self):
        f = self.f
        previous = self.unknown()
        self.assertEqual(recover_accounting(f.ops, [f.ledger])['cost_runs_settled'], 0)
        self.assertEqual(f.rows('execution_effects')[0], previous)
        self.assertEqual(f.calls, ['timeout'])

    def test_ledger_outage_keeps_reserve_then_recovers_exact_local_write(self):
        f = self.f
        original = sqlite3.connect
        def fail(path, *args, **kwargs):
            if str(path) == str(f.ledger.path):
                raise sqlite3.OperationalError('ledger unavailable')
            return original(path, *args, **kwargs)
        with patch('tiku_shared.model_costs.sqlite3.connect', side_effect=fail):
            f.runtime.handle_text('s', 'success', operation_request=f.request())
        pending = f.rows('execution_effects')[0]
        record = json.loads(pending['record'])
        expected = record['estimated_cost_micros'] if record['pricing_status'] == 'priced' else f.store.policy.unknown_cost_reserve_micros
        self.assertEqual(f.ops.cost_exposure()['reserved_micros'], expected)
        self.assertEqual(recover_accounting(f.ops, [f.ledger])['cost_runs_settled'], 1)
        self.assertEqual(f.ops.cost_exposure()['reserved_micros'], 0)
        self.assertEqual(f.calls, ['success'])


if __name__ == '__main__':
    unittest.main()

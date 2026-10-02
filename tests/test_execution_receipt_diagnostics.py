"""Network diagnostics retain bounded error structure without provider text."""

import errno
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError

from tiku_agent.execution_receipts import ReceiptJournal, inspect_receipts
from tiku_agent.execution_store import ExecutionError


SECRET = 'secret-key https://private.example/request?token=secret request-body'


class UnprintableError(RuntimeError):
    def __str__(self):
        raise AssertionError(SECRET)

    def __repr__(self):
        raise AssertionError(SECRET)


def raise_network_error(depth):
    if depth:
        raise_network_error(depth - 1)
    else:
        error = OSError(errno.ETIMEDOUT, SECRET)
        error.winerror = 10060
        raise error


class ReceiptDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'execution.sqlite3'
        self.journal = ReceiptJournal(SimpleNamespace(path=self.database))

    def diagnose(self, error):
        self.journal.failure('operation-id', 'call-id', 'provider_call', error)
        result = inspect_receipts(self.database)
        self.assertEqual(result['pending_receipts'], 0)
        self.assertEqual(len(result['recent_errors']), 1)
        row = result['recent_errors'][0]
        self.assertEqual(row['operation_id'], 'operation-id')
        self.assertEqual(row['call_id'], 'call-id')
        self.assertEqual(row['stage'], 'provider_call')
        encoded = json.dumps(row['details'])
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn('private.example', encoded)
        self.assertNotIn('request-body', encoded)
        return row['details']

    def test_urlerror_retains_network_codes_and_only_last_eight_frames(self):
        try:
            raise_network_error(12)
        except OSError as reason:
            error = URLError(reason)
        details = self.diagnose(error)
        self.assertEqual(details['type'], 'URLError')
        inner = details['reason']
        self.assertEqual(inner['type'], 'TimeoutError')
        self.assertEqual(inner['errno'], errno.ETIMEDOUT)
        self.assertEqual(inner['winerror'], 10060)
        self.assertEqual(len(inner['frames']), 8)
        for frame in inner['frames']:
            self.assertEqual(set(frame), {'file', 'function', 'line'})
            self.assertEqual(frame['file'], 'test_execution_receipt_diagnostics.py')
            self.assertEqual(frame['function'], 'raise_network_error')
            self.assertGreater(frame['line'], 0)

    def test_string_reason_is_not_saved(self):
        details = self.diagnose(URLError(SECRET))
        self.assertEqual(details, {'type': 'URLError', 'frames': []})

    def test_exception_reason_is_never_stringified_and_only_integer_codes_are_saved(self):
        reason = UnprintableError(SECRET)
        reason.errno = 104
        reason.winerror = 10054
        error = URLError(reason)
        error.errno = SECRET
        error.winerror = True
        details = self.diagnose(error)
        self.assertNotIn('errno', details)
        self.assertNotIn('winerror', details)
        self.assertEqual(details['reason'], {
            'type': 'UnprintableError', 'errno': 104, 'winerror': 10054, 'frames': [],
        })

    def test_reason_chain_stops_after_two_nested_exceptions(self):
        error = URLError(URLError(URLError(UnprintableError(SECRET))))
        details = self.diagnose(error)
        self.assertEqual(details['reason']['reason'], {'type': 'URLError', 'frames': []})

    def test_cyclic_reason_and_cause_do_not_revisit_exceptions(self):
        error = URLError(SECRET)
        inner = URLError(error)
        error.reason = inner
        error.__cause__ = inner
        inner.__cause__ = error
        details = self.diagnose(error)
        self.assertEqual(details['reason'], {'type': 'URLError', 'frames': []})
        self.assertNotIn('cause', details)
        self.assertNotIn('cause', details['reason'])

    def test_cause_is_a_compact_non_recursive_summary(self):
        cause = OSError(errno.ECONNRESET, SECRET)
        cause.winerror = 10054
        cause.reason = UnprintableError(SECRET)
        cause.__cause__ = UnprintableError(SECRET)
        error = UnprintableError(SECRET)
        error.__cause__ = cause
        details = self.diagnose(error)
        self.assertEqual(details['cause'], {
            'type': 'ConnectionResetError', 'errno': errno.ECONNRESET, 'winerror': 10054,
        })

    def test_diagnostic_database_failure_preserves_original_exception(self):
        original = URLError(UnprintableError(SECRET))
        with patch.object(self.journal, 'connect', side_effect=sqlite3.OperationalError(SECRET)):
            with self.assertRaises(URLError) as caught:
                try:
                    raise original
                except URLError as error:
                    self.journal.failure('operation-id', 'call-id', 'provider_call', error)
                    raise
        self.assertIs(caught.exception, original)
        self.assertFalse(self.journal.path.exists())

    def test_bad_exception_attribute_does_not_discard_safe_outer_diagnostics(self):
        class BadAttributeError(UnprintableError):
            @property
            def reason(self):
                raise RuntimeError(SECRET)

        details = self.diagnose(BadAttributeError(SECRET))
        self.assertEqual(details, {'type': 'BadAttributeError', 'frames': []})

    def test_existing_execution_code_and_sqlite_code_are_preserved(self):
        self.journal.failure('operation-id', 'call-id', 'execution', ExecutionError('EXECUTION_UNKNOWN'))
        with sqlite3.connect(':memory:') as connection:
            try:
                connection.execute('SELECT * FROM missing_diagnostic_test_table')
            except sqlite3.Error as error:
                self.journal.failure('operation-id', 'call-id', 'cost_ledger', error)
        rows = inspect_receipts(self.database)['recent_errors']
        self.assertEqual(rows[0]['details']['sqlite_error'], 'SQLITE_ERROR')
        self.assertEqual(rows[1]['details']['code'], 'EXECUTION_UNKNOWN')


if __name__ == '__main__':
    unittest.main()

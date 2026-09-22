"""Small independent receipt journal; no prompts, images, keys or error text.

This journal only repairs local accounting evidence. It cannot execute a provider
request, authorize an operation, or publish a business result.
"""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
import traceback

from tiku_agent.execution_store import ExecutionError, canonical


def inspect_receipts(database, limit=20):
    path = Path(database).with_name(Path(database).stem + '_receipts.sqlite3')
    if not path.exists():
        return {'pending_receipts': 0, 'recent_errors': []}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
        count = conn.execute('SELECT count(*) FROM receipts').fetchone()[0]
        rows = conn.execute('SELECT operation_id,call_id,stage,details,created FROM diagnostics ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return {'pending_receipts': count, 'recent_errors': [dict(zip(
        ('operation_id', 'call_id', 'stage', 'details', 'created'), (row[0], row[1], row[2], json.loads(row[3]), row[4]))) for row in rows]}


class ReceiptJournal:
    def __init__(self, store):
        self.path = store.path.with_name(store.path.stem + '_receipts.sqlite3')

    @contextmanager
    def connect(self):
        if self.path.exists() and self.path.stat().st_size > 64 * 1024 * 1024:
            raise ExecutionError('EXECUTION_CAPACITY')
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS receipts (call_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS diagnostics (id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, call_id TEXT NOT NULL, stage TEXT NOT NULL, details TEXT NOT NULL, created REAL NOT NULL)')
            yield conn

    def save(self, call_id, attempt_id, record, confirmed, usage_known):
        payload = canonical({'record': record.to_dict() if record is not None else None,
                             'confirmed': bool(confirmed), 'usage_known': bool(usage_known)})
        if len(payload.encode()) > 16384:
            raise ExecutionError('EXECUTION_CAPACITY')
        with self.connect() as conn:
            previous = conn.execute('SELECT attempt_id,payload FROM receipts WHERE call_id=?', (call_id,)).fetchone()
            if previous:
                if previous != (attempt_id, payload):
                    raise ExecutionError('EXECUTION_COST_CONFLICT')
                return
            if conn.execute('SELECT count(*) FROM receipts').fetchone()[0] >= 10000:
                raise ExecutionError('EXECUTION_CAPACITY')
            conn.execute('INSERT INTO receipts VALUES (?,?,?,?)', (call_id, attempt_id, payload, time.time()))

    def remove(self, call_id, attempt_id):
        with self.connect() as conn:
            conn.execute('DELETE FROM receipts WHERE call_id=? AND attempt_id=?', (call_id, attempt_id))

    def failure(self, operation_id, call_id, stage, exc):
        # Never stringify exceptions: provider errors may contain request bodies.
        clean = lambda value: re.sub(r'[^A-Za-z0-9_.-]', '_', str(value))[:100]
        details = {'type': clean(type(exc).__name__), 'frames': [
            {'file': clean(Path(frame.filename).name), 'function': clean(frame.name), 'line': frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)[-8:]]}
        if isinstance(exc, sqlite3.Error):
            details['sqlite_error'] = clean(getattr(exc, 'sqlite_errorname', ''))
        if isinstance(exc, ExecutionError):
            details['code'] = clean(exc.code)
        try:
            with self.connect() as conn:
                conn.execute('INSERT INTO diagnostics(operation_id,call_id,stage,details,created) VALUES (?,?,?,?,?)',
                             (operation_id, call_id, clean(stage), canonical(details), time.time()))
                conn.execute('DELETE FROM diagnostics WHERE id NOT IN (SELECT id FROM diagnostics ORDER BY id DESC LIMIT 2000)')
        except Exception:
            pass  # A diagnostic failure must not replace the original exception.


def apply_receipt(store, call_id, attempt_id, payload):
    record = payload['record']
    encoded = canonical(record) if record is not None else None
    status = 'CONFIRMED' if payload['confirmed'] else 'UNKNOWN'
    with store.transaction() as conn:
        row = conn.execute('SELECT * FROM execution_effects WHERE call_id=? AND attempt_id=?', (call_id, attempt_id)).fetchone()
        if row is None or row['status'] in {'PREPARED', 'NOT_SENT'}:
            raise ExecutionError('EXECUTION_COST_CONFLICT')
        if record is not None and (record['call_id'] != call_id or record['provider'] != row['provider'] or record['model'] != row['model'] or record['call_type'] != row['call_type']):
            raise ExecutionError('EXECUTION_COST_CONFLICT')
        if row['status'] == 'CONFIRMED':
            if row['record'] != encoded or status != 'CONFIRMED' or bool(row['usage_known']) != bool(payload['usage_known']):
                raise ExecutionError('EXECUTION_COST_CONFLICT')
            return
        conn.execute('UPDATE execution_effects SET status=?,record=?,updated=?,usage_known=? WHERE call_id=?',
                     (status, encoded, store.clock(conn), int(payload['usage_known']), call_id))


def recover_accounting(operations, ledgers, limit=20):
    """Bounded local-only retry. Late evidence never resumes the old operation."""
    from tiku_agent.execution_effects import reconcile_cost, stage_unwritten_cost
    from tiku_agent.execution_store import digest
    store = operations.authority
    journal = ReceiptJournal(store)
    recovered = settled = 0
    if journal.path.exists():
        with journal.connect() as conn:
            receipts = conn.execute('SELECT call_id,attempt_id,payload FROM receipts ORDER BY created LIMIT ?', (limit,)).fetchall()
        for call_id, attempt_id, payload in receipts:
            try:
                apply_receipt(store, call_id, attempt_id, json.loads(payload))
                journal.remove(call_id, attempt_id)
                recovered += 1
            except Exception as exc:
                journal.failure('', call_id, 'receipt_recovery', exc)
    targets = {digest(str(Path(ledger.path).resolve()).casefold()): ledger for ledger in ledgers if ledger is not None}
    with store.reading() as conn:
        runs = conn.execute(
            "SELECT r.run_id,r.metadata,r.ledger_key AS target_key,c.ledger_key FROM execution_collectors r "
            "JOIN execution_cost_runs link ON link.run_id=r.run_id "
            "JOIN execution_operations o ON o.id=link.operation_id "
            "LEFT JOIN execution_cost_outbox c ON c.run_id=r.run_id "
            "WHERE o.status NOT IN ('RUNNING','REGISTERED') "
            "AND (c.run_id IS NULL OR c.status='PENDING') "
            "AND NOT EXISTS (SELECT 1 FROM execution_effects e WHERE e.run_id=r.run_id "
            "AND (e.status IN ('SENT','UNKNOWN') OR (e.status='CONFIRMED' AND (e.record IS NULL OR e.usage_known=0)))) "
            "LIMIT ?", (limit,)).fetchall()
    for run in runs:
        target = run['ledger_key'] or run['target_key']
        keys = [target] if target else json.loads(run['metadata'])['ledger_keys']
        # A3/child share one ledger in production. Ambiguous legacy targets must
        # still go through the explicit reviewed maintenance plan.
        matches = [targets[key] for key in keys if key in targets]
        if len(matches) != 1:
            continue
        try:
            stage_unwritten_cost(operations, matches[0], run['run_id'])
            reconcile_cost(operations, matches[0], run['run_id'])
            settled += 1
        except Exception as exc:
            journal.failure('', '', 'cost_recovery', exc)
    return {'receipts_recovered': recovered, 'cost_runs_settled': settled}

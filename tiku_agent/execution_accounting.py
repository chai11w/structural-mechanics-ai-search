"""Pending accounting consumes a bounded reserve, never invents a paid receipt."""
import json
import time

from tiku_agent.execution_store import ExecutionError


def cost_exposure(conn, policy, identity_digest=None, *, now=None):
    """Include failed/abandoned accounting across days until evidence is settled.

    Confirmed-but-unwritten usage reserves its recorded estimate. Unknown usage
    reserves the greater of that estimate and the configured contingency amount.
    The contingency is an operational estimate, not a provider billing bound.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT e.status,e.record,e.usage_known,o.identity,o.status AS operation_status,"
        "o.lease_until,r.closed,c.status AS cost_status FROM execution_effects e "
        "JOIN execution_operations o ON o.id=e.operation_id "
        "LEFT JOIN execution_collectors r ON r.run_id=e.run_id "
        "LEFT JOIN execution_cost_outbox c ON c.run_id=e.run_id "
        "WHERE e.status IN ('SENT','UNKNOWN','CONFIRMED') "
        "AND (c.run_id IS NULL OR c.status<>'CONFIRMED' OR e.usage_known=0)"
        " AND (o.status<>'RUNNING' OR o.lease_until<=? OR r.closed=2 OR e.status='UNKNOWN')"
        + (" AND o.identity=?" if identity_digest is not None else ""),
        (now, identity_digest) if identity_digest is not None else (now,),
    ).fetchall()
    reserve = unresolved = 0
    for row in rows:
        record = json.loads(row['record']) if row['record'] else {}
        amount = max(0, int(record.get('estimated_cost_micros', 0)))
        known = row['status'] == 'CONFIRMED' and row['usage_known'] and record.get('pricing_status') == 'priced'
        if not known:
            amount = max(amount, policy.unknown_cost_reserve_micros)
        reserve += amount
        unresolved += 1
    return {'reserved_micros': reserve, 'unresolved_calls': unresolved}


def check_cost_admission(conn, policy, identity_digest=None, *, now=None):
    # Broken ownership or a conflicting immutable ledger cannot be priced safely.
    if conn.execute(
        "SELECT 1 FROM execution_cost_outbox c LEFT JOIN execution_cost_runs r ON r.run_id=c.run_id "
        "LEFT JOIN execution_operations o ON o.id=r.operation_id "
        "WHERE c.status<>'CONFIRMED' AND (c.status<>'PENDING' OR o.id IS NULL) LIMIT 1"
    ).fetchone():
        raise ExecutionError('EXECUTION_COST_PENDING')
    if cost_exposure(conn, policy, now=now)['unresolved_calls'] >= policy.max_global_unresolved_cost_calls:
        raise ExecutionError('EXECUTION_COST_PENDING')
    if identity_digest is not None and cost_exposure(conn, policy, identity_digest, now=now)['unresolved_calls'] >= policy.max_identity_unresolved_cost_calls:
        raise ExecutionError('EXECUTION_COST_PENDING')

"""Exercise detached HTTP jobs offline in a new, isolated runtime directory."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', help='New directory inside this checkout\'s .tmp_phase6_runtime')
    args = parser.parse_args(argv)
    from scripts.run_phase6_kernel_probe import isolated_root
    from fastapi.testclient import TestClient
    from tests.test_background_http import BackgroundHttpFixture
    from tests.test_background_stream import close_stream

    root = isolated_root(args.runtime_dir)
    h = BackgroundHttpFixture(root)
    with TestClient(h.app) as client:
        context = h.login(client)
        headers = h.headers(context)
        h.f.release.clear()
        try:
            ack = client.post('/api/jobs', json={'kind': 'handle_text', 'parameters': {'text': 'offline probe'}}, headers=headers)
            assert ack.status_code == 202
            operation_id = ack.json()['job']['operation_id']
            assert h.f.entered.wait(5)
            messages = asyncio.run(close_stream(h.app, '/api/jobs/' + operation_id + '/stream', dict(client.cookies)))
            assert messages[0]['status'] == 200
        finally:
            h.f.release.set()
        result = h.wait_result(client, operation_id)
        assert result['publication']['status'] == 'READY'
        envelope = json.loads(headers['X-Tiku-Operation'])
        for _ in range(3):
            observed = client.get('/api/jobs/lookup', params={'key': envelope['key'], 'epoch': envelope['epoch']})
            assert observed.json()['job']['publication']['result'] == result['publication']['result']
        assert len(h.f.calls) == 1
    counts = {}
    for name, path, table in [('execution', h.f.authority.path, 'execution_attempts'),
                              ('responses', h.responses.path, 'public_responses'),
                              ('trace', h.trace_store.path, 'trace_events')]:
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            counts[name] = conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
    assert counts['execution'] == counts['responses'] == 1
    assert h.recorder.health()['duplicate_terminals'] == 0
    report = {'scope': 'phase6.3 offline HTTP/ASGI with synthetic provider', 'status': result['status'],
              'publication': result['publication']['status'], 'provider_calls': len(h.f.calls),
              'consumer_disconnected': True, 'repeated_queries': 3, 'counts': counts,
              'drain': h.app.state.background.drain_result, 'runtime_dir': str(root)}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

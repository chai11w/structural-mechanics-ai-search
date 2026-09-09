"""Local-only browser acceptance fixture; all models/tools and media are fake.

Run as ``python -B -m tests.phase5_browser_fixture``. The process owns a fresh
TemporaryDirectory and port 8910 only. Stop this exact process after acceptance.
This module is an explicit test entry point, never imported by a launcher.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import threading
from unittest.mock import patch
from fastapi.responses import RedirectResponse
import uvicorn
from tests.test_execution_handoffs import ExecutionHandoffTests
from tiku_agent.fastapi_demo import create_app, SESSION_COOKIE

def main():
    fixture = ExecutionHandoffTests()
    fixture.setUp()
    release = threading.Event()
    entered = threading.Event()
    original_factory = fixture.a2.agent_factory
    def blocked_factory(state):
        agent = original_factory(state)
        original = agent.tools.analyze_image
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(240):
                raise RuntimeError('fixture timeout')
            return original(*args, **kwargs)
        agent.tools.analyze_image = blocked
        return agent

    app = create_app(runtime=fixture.a3, incoming_dir=fixture.root/'incoming')
    @app.get('/fixture/start')
    def start(mode: str = 'ready'):
        release.set()
        fixture.a2.agent_factory = original_factory
        fixture.a3.clear('s', operation_request=fixture.request())
        fixture.prepare()
        if mode == 'crash':
            with patch.object(fixture.a3, '_after_a2_response', side_effect=RuntimeError('fixture parent crash')):
                try: fixture.crop()
                except RuntimeError: pass
        if mode == 'blocked':
            release.clear()
            entered.clear()
            fixture.a2.agent_factory = blocked_factory
        response = RedirectResponse('/')
        response.set_cookie(SESSION_COOKIE, 's', httponly=True, samesite='strict')
        return response

    @app.get('/fixture/status')
    def status():
        return {'entered':entered.is_set(), 'released':release.is_set(), 'analysis_count':fixture.analysis_count,
            'operations':[{k:r[k] for k in ('id','kind','status')} for r in fixture.rows('execution_operations')]}

    @app.post('/fixture/release')
    def unblock():
        release.set()
        return {'released':True}

    try:
        uvicorn.run(app, host='127.0.0.1', port=8910, log_level='warning')
    finally:
        release.set()
        fixture.doCleanups()


if __name__ == "__main__":
    main()

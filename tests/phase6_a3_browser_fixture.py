"""Real A3/A2 state with synthetic models for phase 6.4 browser acceptance."""
import json
import socket
import sys

from fastapi import Request
from fastapi.responses import RedirectResponse
import uvicorn
from tiku_agent.state import AgentState
from tiku_agent.tools import ToolResult

from tests.test_background_http_a3 import BackgroundA3HttpTests
from tests.test_a3_runtime import FakeAutoCropper


def main():
    test = BackgroundA3HttpTests()
    test.setUp()
    fixture = test.h
    app = fixture.app
    image = test.f.root / 'synthetic-page.png'
    image.write_bytes(test.f.image)
    # The unit fixture uses nonexistent bank paths. Browser acceptance needs
    # real private bytes for frozen publication and feedback, still synthetic.
    tools = test.f.a2.agent_factory(AgentState()).tools
    candidate = {'rank': 1, 'path': str(image), 'name': 'synthetic-page.png', 'score': 0.9}
    tools.coarse_search = lambda *a, **kw: ToolResult(ok=True, data={'candidates': [candidate]})
    tools.rerank_candidates = lambda *a, **kw: ToolResult(ok=True, data={'reranked': False, 'visible_candidates': [candidate]})
    tools.answer_candidate = lambda *a, **kw: ToolResult(ok=True, data={'copied_paths': [str(image)]})
    if '--automatic' in sys.argv:
        test.f.a3.auto_cropper = FakeAutoCropper(second_status='auto_ready')

    @app.middleware('http')
    async def login(request: Request, call_next):
        if request.url.path != '/fixture/start':
            return await call_next(request)
        cookie = fixture.access.issue_cookie(fixture.access.authenticate_code(fixture.code))
        response = RedirectResponse('/')
        response.set_cookie(fixture.access.cookie_name, cookie, httponly=True, samesite='strict')
        return response

    @app.get('/fixture/counts')
    def counts():
        with test.f.store.reading() as conn:
            operations = [dict(row) for row in conn.execute('SELECT id,kind,status FROM execution_operations')]
        return {'calls': list(test.f.calls), 'operations': operations}

    @app.post('/fixture/automatic')
    def automatic():
        test.f.a3.auto_cropper = FakeAutoCropper(second_status='auto_ready')
        return {'automatic': True}

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        print(json.dumps({'port': listener.getsockname()[1], 'image': str(image)}), flush=True)
        try:
            uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[listener])
        finally:
            test.doCleanups()


if __name__ == '__main__':
    main()

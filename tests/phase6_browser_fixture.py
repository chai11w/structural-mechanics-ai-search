"""Isolated local browser fixture: ephemeral port, private data, counted fake calls.

No production configuration, credentials, providers or services are loaded.
"""
import gc
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

from fastapi import Request
from fastapi.responses import RedirectResponse
import uvicorn

from tests.test_background_http import BackgroundHttpFixture


def main():
    with tempfile.TemporaryDirectory(prefix="tiku-phase6-browser-") as directory:
        fixture = BackgroundHttpFixture(directory)
        fixture.f.clock[0] = time.time()
        fixture.f.authority.now = time.time
        # Extend only the synthetic provider gate for manual browser acceptance.
        real_release = threading.Event()
        real_release.set()
        class Gate:
            def wait(self, timeout=None):
                return real_release.wait(240)
            def set(self):
                real_release.set()
            def clear(self):
                real_release.clear()
        fixture.f.release = Gate()
        app = fixture.app

        @app.middleware('http')
        async def start(request: Request, call_next):
            if request.url.path != '/fixture/start':
                return await call_next(request)
            # This explicit local-only test path generates a fresh test login;
            # its generated code never leaves the server or appears in logs.
            cookie = fixture.access.issue_cookie(fixture.access.authenticate_code(fixture.code))
            response = RedirectResponse('/')
            response.set_cookie(fixture.access.cookie_name, cookie, httponly=True, samesite='strict')
            return response

        @app.post('/fixture/gate')
        async def gate(request: Request):
            data = await request.json()
            fixture.f.release.set() if data.get('release') else fixture.f.release.clear()
            return {'released': real_release.is_set()}

        @app.get('/fixture/counts')
        def counts():
            with fixture.f.authority.reading() as conn:
                operations = [dict(row) for row in conn.execute('SELECT id,kind,status FROM execution_operations')]
                attempts = conn.execute('SELECT count(*) FROM execution_attempts').fetchone()[0]
            return {'calls': len(fixture.f.calls), 'attempts': attempts, 'operations': operations}

        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            print(json.dumps({'port': port, 'runtime': directory}), flush=True)
            try:
                uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[listener])
            finally:
                fixture.f.release.set()
                gc.collect()


if __name__ == '__main__':
    main()

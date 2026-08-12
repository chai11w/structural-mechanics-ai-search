from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from threading import Thread
import unittest
from uuid import uuid4

from scripts.verify_tiku_control_runtime import _local_base_url, verify_runtime
from tiku_admin.control_store import SQLiteControlStore


class TikuControlRuntimeVerifierTest(unittest.TestCase):
    def test_accepts_only_explicit_local_http_origins(self):
        self.assertEqual(
            _local_base_url("http://127.0.0.1:8790/"),
            "http://127.0.0.1:8790",
        )
        self.assertEqual(
            _local_base_url("http://localhost:8797"),
            "http://localhost:8797",
        )
        for invalid in (
            "https://127.0.0.1:8790",
            "http://example.com:8790",
            "http://127.0.0.1",
            "http://127.0.0.1:8790/path",
            "http://user:password@127.0.0.1:8790",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _local_base_url(invalid)

    def test_verifier_logs_in_revokes_and_archives_its_temporary_invitation(self):
        root = (
            Path(__file__).resolve().parents[1]
            / ".tmp_tests"
            / f"control_verifier_{uuid4().hex}"
        )
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        control = SQLiteControlStore(root / "control.sqlite3")
        handler = _runtime_handler(control)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        result = verify_runtime(
            control_db=control.path,
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
        )

        self.assertEqual(
            result,
            {
                "health": "ok",
                "login": "ok",
                "session": "ok",
                "dynamic_revocation": "ok",
            },
        )
        records = control.list_invitations(include_archived=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "archived")


def _runtime_handler(control: SQLiteControlStore):
    sessions: dict[str, tuple[str, int]] = {}

    class RuntimeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"status": "ok"})
                return
            if self.path == "/api/session":
                token = self._cookie("validation_session")
                identity = sessions.get(token)
                active = (
                    control.active_invitation(*identity)
                    if identity is not None
                    else None
                )
                if active is None:
                    self._json(401, {"detail": "unauthorized"})
                    return
                self._json(200, {"session": {"session_valid": False, "phase": "IDLE"}})
                return
            self._json(404, {"detail": "not found"})

        def do_POST(self):
            if self.path != "/api/invite/login":
                self._json(404, {"detail": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("ascii")
            code = body.removeprefix("code=")
            from urllib.parse import unquote_plus

            record = control.authenticate_invitation(unquote_plus(code))
            if record is None:
                self._json(401, {"detail": "invalid code"})
                return
            token = uuid4().hex
            sessions[token] = (record.invite_id, record.auth_version)
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", f"validation_session={token}; Path=/")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *args):
            return

        def _cookie(self, name: str) -> str:
            for item in self.headers.get("Cookie", "").split(";"):
                key, _, value = item.strip().partition("=")
                if key == name:
                    return value
            return ""

        def _json(self, status: int, body: dict[str, object]):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return RuntimeHandler


if __name__ == "__main__":
    unittest.main()

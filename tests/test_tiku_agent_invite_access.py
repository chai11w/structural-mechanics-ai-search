from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from tiku_agent.invite_access import InviteAccess, build_invitation_config


class InviteAccessTest(unittest.TestCase):
    def make_access(self, count: int = 2) -> tuple[InviteAccess, list[tuple[str, str]], Path]:
        directory = Path(__file__).resolve().parents[1] / ".tmp_tests" / f"invites_{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        config, codes = build_invitation_config(count)
        path = directory / "invite_access.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return InviteAccess(path, auth_max_age_seconds=60), codes, path

    def test_config_keeps_only_hashes_and_authenticates_each_generated_code(self):
        access, codes, path = self.make_access()
        config_text = path.read_text(encoding="utf-8")

        for invite_id, code in codes:
            self.assertNotIn(code, config_text)
            self.assertEqual(access.authenticate_code(code).invite_id, invite_id)
        self.assertIsNone(access.authenticate_code("not-an-invitation"))

    def test_signed_cookie_rejects_tampering_and_expiry(self):
        access, codes, _path = self.make_access(1)
        identity = access.authenticate_code(codes[0][1])
        cookie = access.issue_cookie(identity, now=100)

        self.assertEqual(access.verify_cookie(cookie, now=159), identity)
        self.assertIsNone(access.verify_cookie(cookie + "x", now=159))
        self.assertIsNone(access.verify_cookie(cookie, now=161))


if __name__ == "__main__":
    unittest.main()

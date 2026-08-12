"""Verify a live local 8790 service against its shared administrator control store."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import CookieJar


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from tiku_admin.control_store import SQLiteControlStore


def verify_runtime(
    *,
    control_db: str | Path,
    base_url: str = "http://127.0.0.1:8790",
    timeout_seconds: float = 5.0,
) -> dict[str, object]:
    clean_url = _local_base_url(base_url)
    store = SQLiteControlStore(control_db)
    expires_at = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    record, code = store.create_invitation(
        label="maintenance-runtime-validation",
        expires_at=expires_at,
        actor="maintenance-verifier",
    )
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        health = _json_request(opener, f"{clean_url}/health", timeout_seconds)
        if health.get("status") != "ok" and health.get("ok") is not True:
            raise RuntimeError("8790 health check did not report ok")
        login_request = Request(
            f"{clean_url}/api/invite/login",
            data=urlencode({"code": code}).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with opener.open(login_request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"unexpected invitation login status: {response.status}")
        session = _json_request(opener, f"{clean_url}/api/session", timeout_seconds)
        if not isinstance(session.get("session"), dict):
            raise RuntimeError("invitation login did not expose the session contract")
        store.set_invitation_status(
            record.invite_id, "disabled", actor="maintenance-verifier"
        )
        try:
            _json_request(opener, f"{clean_url}/api/session", timeout_seconds)
        except HTTPError as exc:
            if exc.code != 401:
                raise
        else:
            raise RuntimeError("disabled invitation session remained valid")
        return {
            "health": "ok",
            "login": "ok",
            "session": "ok",
            "dynamic_revocation": "ok",
        }
    finally:
        current = store.get_invitation(record.invite_id)
        if current is not None and current.status != "archived":
            store.set_invitation_status(
                record.invite_id, "archived", actor="maintenance-verifier"
            )


def _json_request(opener, url: str, timeout_seconds: float) -> dict[str, object]:
    with opener.open(url, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _local_base_url(value: str) -> str:
    clean = str(value or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base URL must be a local HTTP origin")
    if parsed.port is None:
        raise ValueError("base URL must include an explicit local port")
    return clean


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify local 8790 login and dynamic revocation through a control database"
    )
    parser.add_argument("--control-db", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    result = verify_runtime(
        control_db=args.control_db,
        base_url=args.base_url,
        timeout_seconds=max(1.0, min(30.0, args.timeout_seconds)),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

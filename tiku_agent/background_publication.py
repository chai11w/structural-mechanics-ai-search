"""Durable public output, independent of connections and business re-execution."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import threading
from uuid import uuid4

from PIL import Image

from tiku_agent.execution_runtime import decode_response
from tiku_agent.execution_store import ExecutionError, canonical, session_key
from tiku_agent.task_state_public import public_task_state_snapshot
from tiku_shared.request_protocol import RequestProtocol
from tiku_shared.response_store import ResponseProjection


class BackgroundPublication:
    MAX_MEDIA_BYTES = 15 * 1024 * 1024
    MAX_TOTAL_MEDIA = 64 * 1024 * 1024
    MAX_TOTAL_PAYLOAD = 32 * 1024 * 1024
    MAX_MEDIA_COUNT = 32
    MAX_ATTEMPTS = 3

    def __init__(self, dispatch, responses):
        self.dispatch, self.store, self.responses = dispatch, dispatch.store, responses
        self._stop = threading.Event()
        self._thread = None
        self.trace = None
        with self.store.transaction() as conn:
            for statement in """
                CREATE TABLE IF NOT EXISTS execution_publications (
                    operation_id TEXT PRIMARY KEY REFERENCES execution_operations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, payload TEXT, projection TEXT,
                    response_id TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    token TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, expires REAL NOT NULL,
                    payload_bytes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS execution_public_media (
                    operation_id TEXT NOT NULL REFERENCES execution_publications(operation_id) ON DELETE CASCADE,
                    media_key TEXT NOT NULL, mime TEXT NOT NULL, body BLOB NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(operation_id,media_key));
            """.split(";"):
                if statement.strip():
                    conn.execute(statement)
            row = conn.execute("SELECT value FROM execution_meta WHERE key='publication_schema'").fetchone()
            if row and row[0] != "1":
                raise ExecutionError("EXECUTION_SCHEMA_UNSUPPORTED")
            conn.execute("INSERT OR IGNORE INTO execution_meta VALUES ('publication_schema','1')")

    def prepare(self, writer, response, encoded):
        """Freeze media while the original response still owns its files.

        Failures do not erase the business receipt. After its commit a bounded
        publisher can rebuild from that receipt; readers never perform repair.
        """
        with self.store.transaction() as conn:
            operation = dict(conn.execute("SELECT * FROM execution_operations WHERE id=?", (writer.operation_id,)).fetchone())
            private = conn.execute("SELECT session_id,identity_key FROM execution_dispatch_inputs WHERE operation_id=?", (writer.operation_id,)).fetchone()
        self._prepare(operation, dict(private), response, encoded)

    def _prepare(self, operation, private, response, encoded):
        from tiku_agent.fastapi_demo import _public_session_snapshot, _public_response_protocol
        operation_id = operation["id"]
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM execution_publications WHERE operation_id=? AND payload IS NOT NULL", (operation_id,)).fetchone():
                return
        if not hasattr(response, "text") or response.response_task_state_snapshot is None:
            raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
        snapshot = _public_session_snapshot(response.response_snapshot)
        task_state = public_task_state_snapshot(response.response_task_state_snapshot)
        for section in ("workflow", "active_child_task"):
            if isinstance(task_state.get(section), dict):
                task_state[section]["allowed_actions"] = []
        protocol = _public_response_protocol(RequestProtocol.from_dict(response.protocol)) if response.protocol else RequestProtocol.from_code("REQUEST_SUCCEEDED")
        protocol = replace(protocol, request_id="req_" + operation_id)
        media = []
        media_bytes = 0
        refs = {}
        files = encoded.get("files", {})
        if len(files) > self.MAX_MEDIA_COUNT:
            raise ExecutionError("EXECUTION_PUBLICATION_CAPACITY")
        for index, (name, expected) in enumerate(files.items()):
            path = Path(name)
            if not expected or not path.is_file() or path.stat().st_size > self.MAX_MEDIA_BYTES:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            with path.open("rb") as handle:
                body = handle.read(self.MAX_MEDIA_BYTES + 1)
            media_bytes += len(body)
            if len(body) > self.MAX_MEDIA_BYTES or media_bytes > self.MAX_TOTAL_MEDIA:
                raise ExecutionError("EXECUTION_PUBLICATION_CAPACITY")
            if hashlib.sha256(body).hexdigest() != expected:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            with Image.open(io.BytesIO(body)) as opened:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(opened.format)
            if mime is None:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            key = f"m{index}"
            refs[name] = f"/api/jobs/{operation_id}/media/{key}"
            media.append((key, mime, body, expected))
        urls = [refs[str(path)] for path in response.images]
        payload = {"text": response.text, "images": urls,
                   "uploaded_image": refs.get(str(response.uploaded_image_path), ""),
                   "submitted_crop": refs.get(str(response.submitted_crop_path), ""),
                   "feedback_images": ([{"url": refs[str(response.feedback_overlay_path)], "kind": "overlay"}]
                                       if str(response.feedback_overlay_path) in refs else []),
                   "media": ({"kind": response.media_kind, "status": "complete", "requested_count": len(urls), "delivered_count": len(urls)} if urls else None),
                   "intent": response.intent, "author_contact": dict(response.author_contact),
                   "session": snapshot, "task_state": task_state, "snapshot_role": "historical",
                   "origin": {"operation_id": operation_id, "epoch": operation["epoch"]}, **protocol.to_dict()}
        a3 = snapshot.get("a3") or {}
        selected = a3.get("selected_unit") or {}
        raw_snapshot = response.response_snapshot or {}
        route = str(raw_snapshot.get("image_route") or "").strip().upper()
        route = route if route in {"A1", "A2", "A3"} else ""
        workflow = str(raw_snapshot.get("workflow_search_id") or "").strip()
        search = str(protocol.search_id or raw_snapshot.get("search_id") or "").strip()
        if route == "A2" and not workflow:
            workflow = search
        if route in {"A1", "A3"} and workflow and search == workflow:
            search = ""
        projection = ResponseProjection(
            trace_id="trace_" + operation_id, identity_key=private["identity_key"], session_key=session_key(private["session_id"]),
            request_id="req_" + operation_id, status=protocol.status.value, layer=protocol.layer.value,
            code=protocol.code, retryable=protocol.retryable, action=protocol.action.value,
            workflow_search_id=workflow, search_id=search, image_route=route,
            unit_id=str(selected.get("unit_id") or ""), phase=snapshot["phase"], task_revision=snapshot["task_revision"],
            candidate_count=snapshot["candidate_count"], chapter=snapshot["chapter"],
            intent=response.intent or "public_response", image_count=len(urls), text_length=len(response.text),
            media_status="complete" if urls else "", response_mode="json")
        text = canonical(payload)
        projection_text = canonical(projection.to_dict())
        size = len(text.encode()) + len(projection_text.encode())
        media_size = sum(len(row[2]) for row in media)
        if size > self.store.policy.max_result_bytes:
            raise ExecutionError("EXECUTION_PUBLICATION_CAPACITY")
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            previous = conn.execute("SELECT payload FROM execution_publications WHERE operation_id=?", (operation_id,)).fetchone()
            if previous and previous[0] is not None:
                return
            used = conn.execute("SELECT coalesce(sum(length(body)),0) FROM execution_public_media").fetchone()[0]
            used_payload = conn.execute("SELECT coalesce(sum(payload_bytes),0) FROM execution_publications").fetchone()[0]
            if used + media_size > self.MAX_TOTAL_MEDIA or used_payload + size > self.MAX_TOTAL_PAYLOAD:
                raise ExecutionError("EXECUTION_PUBLICATION_CAPACITY")
            self.store.storage_capacity(extra=3 * (size + media_size) + 8192)
            conn.execute("INSERT INTO execution_publications (operation_id,status,payload,projection,updated,expires,payload_bytes) VALUES (?,'PENDING',?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET payload=excluded.payload,projection=excluded.projection,payload_bytes=excluded.payload_bytes",
                         (operation_id, text, projection_text, now, now + 30 * 86400, size))
            for key, mime, body, sha in media:
                conn.execute("INSERT INTO execution_public_media VALUES (?,?,?,?,?)", (operation_id, key, mime, body, sha))

    def publish(self, operation_id):
        token = uuid4().hex
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            operation = conn.execute("SELECT * FROM execution_operations WHERE id=? AND status='SUCCEEDED'", (operation_id,)).fetchone()
            if operation is None:
                return
            conn.execute("INSERT OR IGNORE INTO execution_publications (operation_id,status,updated,expires) VALUES (?,'PENDING',?,?)", (operation_id, now, now + 30 * 86400))
            row = conn.execute("SELECT * FROM execution_publications WHERE operation_id=?", (operation_id,)).fetchone()
            if row["status"] != "PENDING" or row["attempts"] >= self.MAX_ATTEMPTS or row["next_attempt"] > now or row["expires"] <= now:
                return
            conn.execute("UPDATE execution_publications SET attempts=attempts+1,token=?,next_attempt=?,updated=? WHERE operation_id=?", (token, now + 15, now, operation_id))
            operation = dict(operation)
        try:
            if row["payload"] is None:
                if operation["producer"] != self.dispatch.operations.current_producer:
                    raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
                with self.store.transaction() as conn:
                    private = conn.execute("SELECT session_id,identity_key FROM execution_dispatch_inputs WHERE operation_id=?", (operation_id,)).fetchone()
                if private is None:
                    raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
                encoded = json.loads(operation["result"])
                self._prepare(operation, dict(private), decode_response(encoded), encoded)
            with self.store.transaction() as conn:
                row = conn.execute("SELECT * FROM execution_publications WHERE operation_id=?", (operation_id,)).fetchone()
            record = self.responses.finalize(ResponseProjection(**json.loads(row["projection"])))
            with self.store.transaction() as conn:
                now = self.store.clock(conn)
                changed = conn.execute("UPDATE execution_publications SET status='READY',response_id=?,error_code='',token='',updated=?,expires=min(expires,?) WHERE operation_id=? AND token=?", (record.response_id, now, datetime.fromisoformat(record.expires_at).timestamp(), operation_id, token)).rowcount
                if changed:
                    conn.execute("UPDATE execution_dispatch SET progress_version=progress_version+1 WHERE operation_id=?", (operation_id,))
        except Exception:
            with self.store.transaction() as conn:
                now = self.store.clock(conn)
                conn.execute("UPDATE execution_publications SET status=CASE WHEN attempts>=? THEN 'FAILED' ELSE 'PENDING' END,error_code='EXECUTION_PUBLICATION_UNAVAILABLE',token='',next_attempt=?,updated=? WHERE operation_id=? AND token=?", (self.MAX_ATTEMPTS, now + 1, now, operation_id, token))
                conn.execute("UPDATE execution_dispatch SET progress_version=progress_version+1 WHERE operation_id=?", (operation_id,))

    def view(self, operation_id, *, include_result=True):
        with self.store.reading() as conn:
            row = conn.execute("SELECT * FROM execution_publications WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return {"status": "PENDING", "error_code": ""}
            if row["expires"] <= self.store.read_clock(conn):
                return {"status": "UNAVAILABLE", "error_code": "EXECUTION_PUBLICATION_EXPIRED"}
            result = {"status": row["status"], "error_code": row["error_code"]}
            if row["status"] == "READY" and include_result:
                payload = json.loads(row["payload"])
                payload["response_id"] = row["response_id"]
                result["result"] = payload
            return result

    def read_media(self, operation_id, key):
        with self.store.reading() as conn:
            row = conn.execute("SELECT m.* FROM execution_public_media m JOIN execution_publications p USING(operation_id) WHERE m.operation_id=? AND m.media_key=? AND p.status='READY' AND p.expires>?", (operation_id, key, self.store.read_clock(conn))).fetchone()
            if row is None or hashlib.sha256(row["body"]).hexdigest() != row["sha256"]:
                raise ExecutionError("EXECUTION_RESULT_UNAVAILABLE")
            return row["body"], row["mime"]

    def repair_once(self):
        self.dispatch.maintain()
        if self.trace is not None:
            self.trace.maintain()
        with self.store.transaction() as conn:
            now = self.store.clock(conn)
            # A crash on the last reserved attempt must not leave PENDING forever.
            abandoned = conn.execute("UPDATE execution_publications SET status='FAILED',token='',error_code='EXECUTION_PUBLICATION_UNAVAILABLE',updated=? WHERE status='PENDING' AND attempts>=? AND next_attempt<=? RETURNING operation_id", (now, self.MAX_ATTEMPTS, now)).fetchall()
            for row in abandoned:
                conn.execute("UPDATE execution_dispatch SET progress_version=progress_version+1 WHERE operation_id=?", (row[0],))
            ids = [row[0] for row in conn.execute("SELECT o.id FROM execution_operations o JOIN execution_dispatch d ON d.operation_id=o.id LEFT JOIN execution_publications p ON p.operation_id=o.id WHERE o.status='SUCCEEDED' AND (p.operation_id IS NULL OR (p.status='PENDING' AND p.attempts<? AND p.next_attempt<=? AND p.expires>?)) ORDER BY o.updated LIMIT 10", (self.MAX_ATTEMPTS, now, now))]
            expired = [row[0] for row in conn.execute("SELECT p.operation_id FROM execution_publications p JOIN execution_operations o ON o.id=p.operation_id WHERE p.expires<=? AND p.status<>'EXPIRED' AND o.status IN ('SUCCEEDED','FAILED','CANCELLED') LIMIT 100", (now,))]
            for operation_id in expired:
                conn.execute("DELETE FROM execution_public_media WHERE operation_id=?", (operation_id,))
                conn.execute("UPDATE execution_publications SET status='EXPIRED',payload=NULL,projection=NULL,payload_bytes=0 WHERE operation_id=?", (operation_id,))
                conn.execute("UPDATE execution_dispatch SET progress_version=progress_version+1 WHERE operation_id=?", (operation_id,))
        for operation_id in ids:
            self.publish(operation_id)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("publisher already started")
        def run():
            while not self._stop.is_set():
                try:
                    self.repair_once()
                except Exception:
                    pass  # keep the original receipt; HTTP readers never repair
                self._stop.wait(0.25)
        self._thread = threading.Thread(target=run, name="tiku-publication", daemon=True)
        self._thread.start()

    def close(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        return not self._thread or not self._thread.is_alive()

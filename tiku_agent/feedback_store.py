"""Small, privacy-bounded feedback store for assistant messages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import sqlite3
from threading import Lock
from typing import Callable, Sequence
from uuid import uuid4


FEEDBACK_SCHEMA_VERSION = 8
FEEDBACK_SCOPES = frozenset({"page", "question"})
FEEDBACK_RESPONSE_ID_RE = re.compile(r"^resp_[0-9a-f]{32}$")
FEEDBACK_INTENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_PAGE_FEEDBACK_INTENTS = frozenset({
    "a3_page_error",
    "a3_page_ready",
    "a3_auto_crops_ready",
    "a3_units_prepared",
})


@dataclass(frozen=True)
class MessageFeedback:
    feedback_id: str
    feedback_number: str
    message_id: str
    rated_response_id: str
    identity_key: str
    session_key: str
    rating: str
    tags: tuple[str, ...]
    detail: str
    task_revision: int
    phase: str
    candidate_count: int
    search_duration_ms: int
    search_key: str
    request_id: str
    search_id: str
    status: str
    layer: str
    code: str
    chapter: str
    image_route: str
    workflow_search_id: str
    intent: str
    feedback_scope: str
    conversation: tuple[dict[str, object], ...]
    review_status: str
    admin_note: str
    archived_at: str
    case_expires_at: str
    case_purged_at: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def scope_feedback_conversation(
    conversation: Sequence[dict[str, object]], target_message_id: str
) -> list[dict[str, object]]:
    """Keep only the current uploaded page through the rated assistant reply."""

    messages = [dict(item) for item in conversation if isinstance(item, dict)]
    clean_target = str(target_message_id or "").strip()
    if not clean_target:
        return []
    target_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if str(
                messages[index].get("messageId")
                or messages[index].get("message_id")
                or ""
            ).strip()
            == clean_target
        ),
        -1,
    )
    if target_index < 0:
        return []

    visible = messages[: target_index + 1]

    def revision(item: dict[str, object]) -> int:
        try:
            return max(
                0,
                int(item.get("taskRevision") or item.get("task_revision") or 0),
            )
        except (TypeError, ValueError):
            return 0

    def is_user(item: dict[str, object]) -> bool:
        return (
            str(item.get("role") or "").strip().lower() == "user"
            or bool(item.get("me"))
        )

    target_revision = revision(visible[-1])
    start_index = len(visible) - 1
    if target_revision > 0:
        for index in range(len(visible) - 1, -1, -1):
            item = visible[index]
            images = item.get("images") if isinstance(item.get("images"), list) else []
            if (
                is_user(item)
                and revision(item) == target_revision
                and any(str(value or "").strip() for value in images)
            ):
                start_index = index
                break
        else:
            for index in range(len(visible) - 1, -1, -1):
                item = visible[index]
                if is_user(item) and revision(item) == target_revision:
                    start_index = index
                    break
    else:
        for index in range(len(visible) - 1, -1, -1):
            if is_user(visible[index]):
                start_index = index
                break
    return visible[start_index:]


def classify_feedback_scope(
    target_message: dict[str, object] | None,
    *,
    image_route: str = "",
) -> str:
    """Classify the rated reply without trusting a client-provided scope value."""

    target = target_message if isinstance(target_message, dict) else {}
    intent = str(target.get("intent") or "").strip().lower()
    has_page_overlay = bool(
        str(target.get("a3Overlay") or target.get("a3_overlay") or "").strip()
    )
    route = str(image_route or "").strip().upper()
    if has_page_overlay or intent in _PAGE_FEEDBACK_INTENTS:
        # Historical cases did not always persist image_route, while these
        # intents and the overlay are emitted only by the server-side A3 flow.
        return "page"
    if route == "A3" and intent.startswith("a3_page_"):
        return "page"
    return "question"


class SQLiteFeedbackStore:
    def __init__(self, path: str | Path, *, cases_root: str | Path | None = None) -> None:
        self.path = Path(path)
        self.cases_root = Path(cases_root or self.path.with_name("feedback_cases")).resolve()
        self._lock = Lock()

    def upsert(
        self,
        *,
        message_id: str,
        identity_key: str,
        session_key: str,
        rating: str,
        tags: tuple[str, ...],
        detail: str,
        task_revision: int,
        phase: str,
        candidate_count: int,
        rated_response_id: str,
        search_duration_ms: int = 0,
        search_key: str = "",
        request_id: str = "",
        search_id: str = "",
        status: str = "SUCCESS",
        layer: str = "tool",
        code: str = "REQUEST_SUCCEEDED",
        chapter: str = "",
        image_route: str = "",
        workflow_search_id: str = "",
        intent: str = "",
        conversation: list[dict[str, object]] | None = None,
        media_resolver: Callable[[str], Path | None] | None = None,
        retention_days: int = 30,
    ) -> MessageFeedback:
        now = datetime.now(UTC).isoformat()
        clean_rated_response_id = str(rated_response_id or "").strip()
        if not FEEDBACK_RESPONSE_ID_RE.fullmatch(clean_rated_response_id):
            raise ValueError("invalid rated response id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                connection.row_factory = sqlite3.Row
                _create_schema(connection)
                base_select = """
                    SELECT feedback_id, feedback_number, message_id, rated_response_id,
                           identity_key, session_key, created_at, conversation_json,
                           case_expires_at, case_purged_at, review_status, admin_note,
                           archived_at
                    FROM message_feedback
                """
                existing = connection.execute(
                    base_select + " WHERE rated_response_id = ?",
                    (clean_rated_response_id,),
                ).fetchone()
                if existing is not None and (
                    str(existing["identity_key"]) != str(identity_key)
                    or str(existing["session_key"]) != str(session_key)
                ):
                    raise ValueError("rated response is already bound")
                if existing is not None and str(existing["message_id"]) != message_id:
                    raise ValueError("rated response message id mismatch")
                message_binding = connection.execute(
                    base_select
                    + " WHERE identity_key = ? AND session_key = ? AND message_id = ?",
                    (identity_key, session_key, message_id),
                ).fetchone()
                if message_binding is not None and str(
                    message_binding["rated_response_id"]
                ) != clean_rated_response_id:
                    raise ValueError("message id is already bound")
                stored_message_id = (
                    str(existing["message_id"]) if existing is not None else message_id
                )
                feedback_id = str(existing["feedback_id"]) if existing else uuid4().hex
                created_at = str(existing["created_at"]) if existing else now
                feedback_number = (
                    str(existing["feedback_number"])
                    if existing
                    else _feedback_number(feedback_id, created_at)
                )
                conversation_json = str(existing["conversation_json"]) if existing else "[]"
                case_expires_at = str(existing["case_expires_at"]) if existing else ""
                case_purged_at = str(existing["case_purged_at"]) if existing else ""
                review_status = str(existing["review_status"]) if existing else "pending"
                admin_note = str(existing["admin_note"]) if existing else ""
                archived_at = str(existing["archived_at"]) if existing else ""
                if conversation is not None:
                    sanitized = self._capture_conversation(
                        feedback_id,
                        conversation,
                        target_message_id=stored_message_id,
                        media_resolver=media_resolver,
                    )
                    conversation_json = json.dumps(
                        sanitized, ensure_ascii=False, separators=(",", ":")
                    )
                    expiry = datetime.now(UTC).timestamp() + max(1, min(365, int(retention_days))) * 86400
                    case_expires_at = datetime.fromtimestamp(expiry, UTC).isoformat()
                    case_purged_at = ""
                clean_image_route = (
                    str(image_route).strip().upper()
                    if str(image_route).strip().upper() in {"A1", "A2", "A3"}
                    else ""
                )
                clean_intent = str(intent or "").strip()
                if clean_intent and not FEEDBACK_INTENT_RE.fullmatch(clean_intent):
                    raise ValueError("invalid response intent")
                stored_conversation = _conversation_from_json(conversation_json)
                # New HTTP feedback supplies the server-owned response intent.
                # Keep the sanitized conversation fallback for direct/legacy
                # store callers that predate the authoritative intent field.
                scope_target = (
                    {"intent": clean_intent}
                    if clean_intent
                    else _target_feedback_message(stored_conversation, stored_message_id)
                )
                feedback_scope = classify_feedback_scope(
                    scope_target,
                    image_route=clean_image_route,
                )
                connection.execute(
                    """
                    INSERT INTO message_feedback (
                        feedback_id, feedback_number, message_id, rated_response_id,
                        identity_key, session_key, rating,
                        tags_json, detail, task_revision, phase, candidate_count,
                        search_duration_ms, search_key, request_id, search_id,
                        status, layer, code, chapter, image_route, workflow_search_id, intent,
                        feedback_scope,
                        conversation_json,
                        review_status, admin_note, archived_at,
                        case_expires_at, case_purged_at, created_at, updated_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity_key, session_key, message_id) DO UPDATE SET
                        rated_response_id = excluded.rated_response_id,
                        rating = excluded.rating,
                        tags_json = excluded.tags_json,
                        detail = excluded.detail,
                        task_revision = excluded.task_revision,
                        phase = excluded.phase,
                        candidate_count = excluded.candidate_count,
                        search_duration_ms = excluded.search_duration_ms,
                        search_key = excluded.search_key,
                        request_id = excluded.request_id,
                        search_id = excluded.search_id,
                        status = excluded.status,
                        layer = excluded.layer,
                        code = excluded.code,
                        chapter = excluded.chapter,
                        image_route = excluded.image_route,
                        workflow_search_id = excluded.workflow_search_id,
                        intent = excluded.intent,
                        feedback_scope = excluded.feedback_scope,
                        conversation_json = excluded.conversation_json,
                        case_expires_at = excluded.case_expires_at,
                        case_purged_at = excluded.case_purged_at,
                        updated_at = excluded.updated_at,
                        schema_version = excluded.schema_version
                    """,
                    (
                        feedback_id,
                        feedback_number,
                        stored_message_id,
                        clean_rated_response_id,
                        identity_key,
                        session_key,
                        rating,
                        json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                        detail,
                        max(0, int(task_revision)),
                        phase,
                        max(0, int(candidate_count)),
                        max(0, min(86_400_000, int(search_duration_ms or 0))),
                        str(search_key).strip(),
                        str(request_id).strip(),
                        str(search_id or search_key).strip(),
                        str(status or "SUCCESS").strip().upper(),
                        str(layer or "tool").strip().lower(),
                        str(code or "REQUEST_SUCCEEDED").strip().upper(),
                        str(chapter).strip()[:80],
                        clean_image_route,
                        str(workflow_search_id or search_id or search_key).strip(),
                        clean_intent,
                        feedback_scope,
                        conversation_json,
                        review_status,
                        admin_note,
                        archived_at,
                        case_expires_at,
                        case_purged_at,
                        created_at,
                        now,
                        FEEDBACK_SCHEMA_VERSION,
                    ),
                )
        return MessageFeedback(
            feedback_id=feedback_id,
            feedback_number=feedback_number,
            message_id=stored_message_id,
            rated_response_id=clean_rated_response_id,
            identity_key=identity_key,
            session_key=session_key,
            rating=rating,
            tags=tags,
            detail=detail,
            task_revision=max(0, int(task_revision)),
            phase=phase,
            candidate_count=max(0, int(candidate_count)),
            search_duration_ms=max(0, min(86_400_000, int(search_duration_ms or 0))),
            search_key=str(search_key).strip(),
            request_id=str(request_id).strip(),
            search_id=str(search_id or search_key).strip(),
            status=str(status or "SUCCESS").strip().upper(),
            layer=str(layer or "tool").strip().lower(),
            code=str(code or "REQUEST_SUCCEEDED").strip().upper(),
            chapter=str(chapter).strip()[:80],
            image_route=clean_image_route,
            workflow_search_id=str(workflow_search_id or search_id or search_key).strip(),
            intent=clean_intent,
            feedback_scope=feedback_scope,
            conversation=tuple(stored_conversation),
            review_status=review_status,
            admin_note=admin_note,
            archived_at=archived_at,
            case_expires_at=case_expires_at,
            case_purged_at=case_purged_at,
            created_at=created_at,
            updated_at=now,
        )

    def list_feedback(self, *, include_archived: bool = False) -> list[MessageFeedback]:
        if not self.path.is_file():
            return []
        with self._lock:
            with sqlite3.connect(self.path) as connection:
                connection.row_factory = sqlite3.Row
                _create_schema(connection)
                where = "" if include_archived else "WHERE archived_at = ''"
                rows = connection.execute(
                    f"SELECT * FROM message_feedback {where} ORDER BY created_at DESC, updated_at DESC, feedback_id DESC"
                ).fetchall()
        return [_feedback_from_row(row) for row in rows]

    def query_feedback(
        self,
        *,
        rated_response_id: str = "",
        rating: str = "",
        feedback_scope: str = "",
        identity_key: str = "",
        identity_keys: Sequence[str] | None = None,
        chapter: str = "",
        review_status: str = "",
        status: str = "",
        layer: str = "",
        code: str = "",
        request_id: str = "",
        search_id: str = "",
        include_archived: bool = False,
        created_from: str = "",
        created_before: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MessageFeedback], int]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("rated_response_id", rated_response_id),
            ("rating", rating),
            ("feedback_scope", feedback_scope),
            ("identity_key", identity_key),
            ("chapter", chapter),
            ("review_status", review_status),
            ("status", status),
            ("layer", layer),
            ("code", code),
            ("request_id", request_id),
            ("search_id", search_id),
        ):
            clean = str(value or "").strip()
            if clean:
                clauses.append(f"{column} = ?")
                parameters.append(clean)
        clean_identity_keys = [
            str(value).strip() for value in (identity_keys or []) if str(value).strip()
        ]
        if clean_identity_keys:
            placeholders = ", ".join("?" for _ in clean_identity_keys)
            clauses.append(f"identity_key IN ({placeholders})")
            parameters.extend(clean_identity_keys)
        elif identity_keys is not None:
            clauses.append("1 = 0")
        if not include_archived:
            clauses.append("archived_at = ''")
        if str(created_from or "").strip():
            clauses.append("created_at >= ?")
            parameters.append(str(created_from).strip())
        if str(created_before or "").strip():
            clauses.append("created_at < ?")
            parameters.append(str(created_before).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = min(100, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        if not self.path.is_file():
            return [], 0
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM message_feedback {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM message_feedback {where} ORDER BY created_at DESC, updated_at DESC, feedback_id DESC LIMIT ? OFFSET ?",
                [*parameters, safe_limit, safe_offset],
            ).fetchall()
        return [_feedback_from_row(row) for row in rows], total

    def get_feedback(self, feedback_id: str) -> MessageFeedback | None:
        if not self.path.is_file():
            return None
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            row = connection.execute(
                "SELECT * FROM message_feedback WHERE feedback_id = ? OR feedback_number = ?",
                (str(feedback_id), str(feedback_id).upper()),
            ).fetchone()
        return _feedback_from_row(row) if row else None

    def list_chapters(self) -> list[str]:
        if not self.path.is_file():
            return []
        with self._lock, sqlite3.connect(self.path) as connection:
            _create_schema(connection)
            rows = connection.execute(
                "SELECT DISTINCT chapter FROM message_feedback "
                "WHERE TRIM(chapter) != '' ORDER BY chapter COLLATE NOCASE"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def count_for_identity(self, identity_key: str) -> int:
        if not self.path.is_file():
            return 0
        with self._lock, sqlite3.connect(self.path) as connection:
            _create_schema(connection)
            row = connection.execute(
                "SELECT COUNT(*) FROM message_feedback WHERE identity_key = ?",
                (str(identity_key),),
            ).fetchone()
        return int(row[0]) if row else 0

    def update_review(
        self, feedback_id: str, *, review_status: str, admin_note: str
    ) -> MessageFeedback:
        clean_status = str(review_status).strip()
        if clean_status not in {"pending", "resolved", "no_action"}:
            raise ValueError("invalid feedback review status")
        clean_note = str(admin_note or "").strip()
        if len(clean_note) > 2000:
            raise ValueError("administrator note is too long")
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            cursor = connection.execute(
                """
                UPDATE message_feedback
                SET review_status = ?, admin_note = ?, updated_at = ?
                WHERE feedback_id = ? OR feedback_number = ?
                """,
                (
                    clean_status,
                    clean_note,
                    datetime.now(UTC).isoformat(),
                    str(feedback_id),
                    str(feedback_id).upper(),
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("feedback not found")
            row = connection.execute(
                "SELECT * FROM message_feedback WHERE feedback_id = ? OR feedback_number = ?",
                (str(feedback_id), str(feedback_id).upper()),
            ).fetchone()
        return _feedback_from_row(row)

    def set_archived(self, feedback_id: str, *, archived: bool) -> MessageFeedback:
        now = datetime.now(UTC).isoformat()
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            cursor = connection.execute(
                "UPDATE message_feedback SET archived_at = ?, updated_at = ? "
                "WHERE feedback_id = ? OR feedback_number = ?",
                (now if archived else "", now, str(feedback_id), str(feedback_id).upper()),
            )
            if cursor.rowcount != 1:
                raise KeyError("feedback not found")
            row = connection.execute(
                "SELECT * FROM message_feedback WHERE feedback_id = ? OR feedback_number = ?",
                (str(feedback_id), str(feedback_id).upper()),
            ).fetchone()
        return _feedback_from_row(row)

    def delete_archived(self, feedback_id: str) -> bool:
        if not self.path.is_file():
            return False
        removed_id = ""
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            row = connection.execute(
                "SELECT feedback_id, archived_at FROM message_feedback "
                "WHERE feedback_id = ? OR feedback_number = ?",
                (str(feedback_id), str(feedback_id).upper()),
            ).fetchone()
            if row is None:
                return False
            if not str(row["archived_at"]):
                raise ValueError("feedback must be archived before deletion")
            removed_id = str(row["feedback_id"])
            connection.execute(
                "DELETE FROM message_feedback WHERE feedback_id = ?", (removed_id,)
            )
        self._clear_case(removed_id)
        return True

    def resolve_case_media(self, feedback_id: str, media_name: str) -> Path | None:
        name = Path(str(media_name)).name
        if name != str(media_name) or not name:
            return None
        case_dir = (self.cases_root / str(feedback_id)).resolve()
        candidate = (case_dir / name).resolve()
        return candidate if candidate.parent == case_dir and candidate.is_file() else None

    def purge_expired_cases(self, *, now: datetime | None = None) -> int:
        if not self.path.is_file():
            return 0
        cutoff = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        purged = 0
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            rows = connection.execute(
                """
                SELECT feedback_id FROM message_feedback
                WHERE case_expires_at != '' AND case_expires_at <= ? AND case_purged_at = ''
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                feedback_id = str(row["feedback_id"])
                self._clear_case(feedback_id)
                connection.execute(
                    """
                    UPDATE message_feedback
                    SET conversation_json = '[]', case_purged_at = ?, updated_at = ?
                    WHERE feedback_id = ?
                    """,
                    (cutoff, cutoff, feedback_id),
                )
                purged += 1
        return purged

    def delete_by_response(
        self,
        *,
        rated_response_id: str,
        identity_key: str,
        session_key: str,
    ) -> bool:
        """Delete only a response-bound row; legacy feedback remains read-only."""

        clean_response_id = str(rated_response_id or "").strip()
        if not FEEDBACK_RESPONSE_ID_RE.fullmatch(clean_response_id):
            raise ValueError("invalid rated response id")
        if not self.path.is_file():
            return False
        feedback_id = ""
        with self._lock, sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            _create_schema(connection)
            row = connection.execute(
                """
                SELECT feedback_id FROM message_feedback
                WHERE rated_response_id = ? AND identity_key = ? AND session_key = ?
                """,
                (clean_response_id, identity_key, session_key),
            ).fetchone()
            feedback_id = str(row["feedback_id"]) if row else ""
            cursor = connection.execute(
                """
                DELETE FROM message_feedback
                WHERE rated_response_id = ? AND identity_key = ? AND session_key = ?
                """,
                (clean_response_id, identity_key, session_key),
            )
            removed = cursor.rowcount > 0
        if removed and feedback_id:
            self._clear_case(feedback_id)
        return removed

    def _capture_conversation(
        self,
        feedback_id: str,
        conversation: list[dict[str, object]],
        *,
        target_message_id: str,
        media_resolver: Callable[[str], Path | None] | None,
    ) -> list[dict[str, object]]:
        if not isinstance(conversation, list) or len(conversation) > 50:
            raise ValueError("conversation must contain no more than 50 messages")
        conversation = scope_feedback_conversation(conversation, target_message_id)
        self._clear_case(feedback_id)
        case_dir = (self.cases_root / feedback_id).resolve()
        if case_dir.parent != self.cases_root:
            raise ValueError("invalid feedback case directory")
        sanitized: list[dict[str, object]] = []
        total_text = 0
        for raw in conversation:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("message") or "")[:5000]
            total_text += len(text)
            if total_text > 60_000:
                raise ValueError("conversation text is too large")
            images: list[str] = []
            raw_images = raw.get("images") if isinstance(raw.get("images"), list) else []
            for url in raw_images[:8]:
                source = media_resolver(str(url)) if media_resolver is not None else None
                if source is None or not source.is_file():
                    continue
                case_dir.mkdir(parents=True, exist_ok=True)
                suffix = source.suffix.lower() if len(source.suffix) <= 8 else ".bin"
                target = case_dir / f"{uuid4().hex}{suffix or '.bin'}"
                shutil.copy2(source, target)
                images.append(target.name)
            overlay_name = ""
            overlay_url = str(raw.get("a3Overlay") or raw.get("a3_overlay") or "").strip()
            if overlay_url:
                source = media_resolver(overlay_url) if media_resolver is not None else None
                if source is not None and source.is_file():
                    case_dir.mkdir(parents=True, exist_ok=True)
                    suffix = source.suffix.lower() if len(source.suffix) <= 8 else ".bin"
                    target = case_dir / f"{uuid4().hex}{suffix or '.bin'}"
                    shutil.copy2(source, target)
                    overlay_name = target.name
            sanitized.append({
                "role": (
                    "user"
                    if bool(raw.get("me"))
                    or str(raw.get("role") or "").strip().lower() == "user"
                    else "assistant"
                ),
                "message": text,
                "images": images,
                "a3_overlay": overlay_name,
                "image_alt": str(raw.get("imageAlt") or "题目图片")[:100],
                "intent": str(raw.get("intent") or "")[:80],
                "variant": str(raw.get("variant") or "")[:40],
                "task_revision": max(0, int(raw.get("taskRevision") or 0)),
                "message_id": str(raw.get("messageId") or "")[:80],
                "created_at": max(0, int(raw.get("createdAt") or 0)),
            })
        return sanitized

    def _clear_case(self, feedback_id: str) -> None:
        target = (self.cases_root / str(feedback_id)).resolve()
        if target.parent != self.cases_root:
            raise ValueError("invalid feedback case directory")
        if target.is_dir():
            shutil.rmtree(target)

def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_feedback (
            feedback_id TEXT PRIMARY KEY,
            feedback_number TEXT NOT NULL UNIQUE,
            message_id TEXT NOT NULL,
            rated_response_id TEXT NOT NULL DEFAULT '',
            identity_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            rating TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            detail TEXT NOT NULL,
            task_revision INTEGER NOT NULL,
            phase TEXT NOT NULL,
            candidate_count INTEGER NOT NULL,
            search_duration_ms INTEGER NOT NULL DEFAULT 0,
            search_key TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            search_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'SUCCESS',
            layer TEXT NOT NULL DEFAULT 'tool',
            code TEXT NOT NULL DEFAULT 'REQUEST_SUCCEEDED',
            chapter TEXT NOT NULL DEFAULT '',
            image_route TEXT NOT NULL DEFAULT '',
            workflow_search_id TEXT NOT NULL DEFAULT '',
            intent TEXT NOT NULL DEFAULT '',
            feedback_scope TEXT NOT NULL DEFAULT 'question',
            conversation_json TEXT NOT NULL DEFAULT '[]',
            review_status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT NOT NULL DEFAULT '',
            archived_at TEXT NOT NULL DEFAULT '',
            case_expires_at TEXT NOT NULL DEFAULT '',
            case_purged_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            UNIQUE(identity_key, session_key, message_id)
        )
        """
    )
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(message_feedback)")
    }
    migrations = {
        "feedback_number": "TEXT NOT NULL DEFAULT ''",
        "rated_response_id": "TEXT NOT NULL DEFAULT ''",
        "search_duration_ms": "INTEGER NOT NULL DEFAULT 0",
        "search_key": "TEXT NOT NULL DEFAULT ''",
        "request_id": "TEXT NOT NULL DEFAULT ''",
        "search_id": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'SUCCESS'",
        "layer": "TEXT NOT NULL DEFAULT 'tool'",
        "code": "TEXT NOT NULL DEFAULT 'REQUEST_SUCCEEDED'",
        "chapter": "TEXT NOT NULL DEFAULT ''",
        "image_route": "TEXT NOT NULL DEFAULT ''",
        "workflow_search_id": "TEXT NOT NULL DEFAULT ''",
        "intent": "TEXT NOT NULL DEFAULT ''",
        "feedback_scope": "TEXT NOT NULL DEFAULT ''",
        "conversation_json": "TEXT NOT NULL DEFAULT '[]'",
        "review_status": "TEXT NOT NULL DEFAULT 'pending'",
        "admin_note": "TEXT NOT NULL DEFAULT ''",
        "archived_at": "TEXT NOT NULL DEFAULT ''",
        "case_expires_at": "TEXT NOT NULL DEFAULT ''",
        "case_purged_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in migrations.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE message_feedback ADD COLUMN {name} {definition}")
    scope_rows = connection.execute(
        """
        SELECT feedback_id, message_id, image_route, conversation_json
        FROM message_feedback
        WHERE feedback_scope NOT IN ('page', 'question')
        """
    ).fetchall()
    for row in scope_rows:
        conversation = _conversation_from_json(str(row[3]))
        feedback_scope = classify_feedback_scope(
            _target_feedback_message(conversation, str(row[1])),
            image_route=str(row[2]),
        )
        connection.execute(
            "UPDATE message_feedback SET feedback_scope = ? WHERE feedback_id = ?",
            (feedback_scope, str(row[0])),
        )
    connection.execute(
        "UPDATE message_feedback SET schema_version = ? WHERE schema_version < ?",
        (FEEDBACK_SCHEMA_VERSION, FEEDBACK_SCHEMA_VERSION),
    )
    rows = connection.execute(
        "SELECT feedback_id, created_at FROM message_feedback WHERE feedback_number = ''"
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE message_feedback SET feedback_number = ? WHERE feedback_id = ?",
            (_feedback_number(str(row[0]), str(row[1])), str(row[0])),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_number "
        "ON message_feedback(feedback_number)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_rated_response "
        "ON message_feedback(rated_response_id) WHERE rated_response_id != ''"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_identity_updated "
        "ON message_feedback(identity_key, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_review_updated "
        "ON message_feedback(review_status, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_protocol "
        "ON message_feedback(status, layer, code, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_search_id "
        "ON message_feedback(search_id, updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedback_scope_updated "
        "ON message_feedback(feedback_scope, updated_at)"
    )


def _feedback_from_row(row: sqlite3.Row) -> MessageFeedback:
    return MessageFeedback(
        feedback_id=str(row["feedback_id"]),
        feedback_number=str(row["feedback_number"]),
        message_id=str(row["message_id"]),
        rated_response_id=str(row["rated_response_id"]),
        identity_key=str(row["identity_key"]),
        session_key=str(row["session_key"]),
        rating=str(row["rating"]),
        tags=tuple(json.loads(str(row["tags_json"]))),
        detail=str(row["detail"]),
        task_revision=int(row["task_revision"]),
        phase=str(row["phase"]),
        candidate_count=int(row["candidate_count"]),
        search_duration_ms=int(row["search_duration_ms"]),
        search_key=str(row["search_key"]),
        request_id=str(row["request_id"]),
        search_id=str(row["search_id"] or row["search_key"]),
        status=str(row["status"]),
        layer=str(row["layer"]),
        code=str(row["code"]),
        chapter=str(row["chapter"]),
        image_route=str(row["image_route"]),
        workflow_search_id=str(row["workflow_search_id"] or row["search_id"] or row["search_key"]),
        intent=str(row["intent"]),
        feedback_scope=(
            str(row["feedback_scope"])
            if str(row["feedback_scope"]) in FEEDBACK_SCOPES
            else "question"
        ),
        conversation=tuple(_conversation_from_json(str(row["conversation_json"]))),
        review_status=str(row["review_status"]),
        admin_note=str(row["admin_note"]),
        archived_at=str(row["archived_at"]),
        case_expires_at=str(row["case_expires_at"]),
        case_purged_at=str(row["case_purged_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _feedback_number(feedback_id: str, created_at: str) -> str:
    date_part = str(created_at)[:10].replace("-", "")
    if len(date_part) != 8 or not date_part.isdigit():
        date_part = "00000000"
    return f"FB-{date_part}-{str(feedback_id)[:10].upper()}"


def _conversation_from_json(value: str) -> list[dict[str, object]]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _target_feedback_message(
    conversation: Sequence[dict[str, object]], message_id: str
) -> dict[str, object]:
    clean_target = str(message_id or "").strip()
    for item in reversed(conversation):
        item_id = str(item.get("messageId") or item.get("message_id") or "").strip()
        if item_id == clean_target:
            return dict(item)
    return {}

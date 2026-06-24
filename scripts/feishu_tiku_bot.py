"""
Feishu bot MVP for the structure-mechanics question bank.

The first version is intentionally project-local and dry-run friendly:
- the Feishu HTTP/event shell is present;
- text/image session state is implemented;
- local dry-run commands can exercise image -> chapter -> choice;
- real Feishu image download/upload is isolated behind small client methods.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from multi_agent_pipeline import MultiAgentCoordinator  # noqa: E402
from search import ANSWER_OUTPUT, answer, cfg  # noqa: E402


FEISHU_OPEN_API = "https://open.feishu.cn/open-apis"
CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配"]
DEFAULT_SESSION_TTL_SECONDS = 10 * 60
CHAPTER_MODE_AUTO = "auto"
CHAPTER_MODE_MANUAL = "manual"
CHAPTER_MODE_TOGGLE = "toggle"


@dataclass
class FeishuTikuOptions:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str | None = None
    dry_run: bool = False
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    temp_dir: Path = BASE / ".tmp_feishu_tiku"
    top_k: int = 5
    rerank_top: int = 3
    max_message_age_seconds: int = 15 * 60
    working_reaction: str | None = "OK"


@dataclass
class TikuSession:
    state: str = "idle"
    image_path: Path | None = None
    chapter: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


@dataclass
class BotResponse:
    texts: list[str] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)


class TikuSessionStore:
    def __init__(self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, TikuSession] = {}
        self._lock = threading.Lock()

    def get(self, sender: str) -> TikuSession:
        with self._lock:
            session = self._sessions.get(sender)
            if session is None or self._is_expired(session):
                session = TikuSession()
                self._sessions[sender] = session
            return session

    def save(self, sender: str, session: TikuSession) -> None:
        session.updated_at = time.time()
        with self._lock:
            self._sessions[sender] = session

    def clear(self, sender: str) -> None:
        with self._lock:
            self._sessions.pop(sender, None)

    def _is_expired(self, session: TikuSession) -> bool:
        return time.time() - session.updated_at > self.ttl_seconds


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, dry_run: bool = False) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.dry_run = dry_run
        self._tenant_access_token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def reply_text(self, message_id: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] reply_text {message_id}: {text}", flush=True)
            return
        token = self.tenant_access_token()
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        request_json(
            f"{FEISHU_OPEN_API}/im/v1/messages/{message_id}/reply",
            method="POST",
            payload=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    def reply_image(self, message_id: str, image_path: Path) -> None:
        if self.dry_run:
            print(f"[dry-run] reply_image {message_id}: {image_path}", flush=True)
            return
        image_key = self.upload_image(image_path)
        token = self.tenant_access_token()
        body = {
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        request_json(
            f"{FEISHU_OPEN_API}/im/v1/messages/{message_id}/reply",
            method="POST",
            payload=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    def add_reaction(self, message_id: str, emoji_type: str) -> None:
        if self.dry_run:
            print(f"[dry-run] add_reaction {message_id}: {emoji_type}", flush=True)
            return
        token = self.tenant_access_token()
        body = {"reaction_type": {"emoji_type": emoji_type}}
        request_json(
            f"{FEISHU_OPEN_API}/im/v1/messages/{message_id}/reactions",
            method="POST",
            payload=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    def download_message_image(self, message_id: str, image_key: str, output_path: Path) -> Path:
        """Download an image resource from Feishu.

        The exact Feishu resource endpoint can vary by app permission/version. Keep
        the method isolated so the state machine can be tested without network.
        """
        if self.dry_run:
            raise RuntimeError("dry-run cannot download Feishu image resources")
        token = self.tenant_access_token()
        url = (
            f"{FEISHU_OPEN_API}/im/v1/messages/{message_id}/resources/"
            f"{image_key}?type=image"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            output_path.write_bytes(response.read())
        return output_path

    def upload_image(self, image_path: Path) -> str:
        if self.dry_run:
            return f"dry-run:{image_path.name}"
        token = self.tenant_access_token()
        boundary = f"----tiku{int(time.time() * 1000)}"
        image_bytes = image_path.read_bytes()
        body = build_multipart_form_data(
            boundary,
            fields={"image_type": "message"},
            files={"image": (image_path.name, image_bytes, "image/jpeg")},
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        data = request_json(
            f"{FEISHU_OPEN_API}/im/v1/images",
            method="POST",
            raw_body=body,
            headers=headers,
        )
        image_key = str((data.get("data") or {}).get("image_key") or "")
        if not image_key:
            raise RuntimeError(f"Feishu image upload returned no image_key: {data}")
        return image_key

    def tenant_access_token(self) -> str:
        now = time.time()
        with self._lock:
            if self._tenant_access_token and now < self._token_expires_at:
                return self._tenant_access_token
            data = request_json(
                f"{FEISHU_OPEN_API}/auth/v3/tenant_access_token/internal",
                method="POST",
                payload={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError(f"failed to get tenant_access_token: {data}")
            expire = int(data.get("expire") or 7200)
            self._tenant_access_token = token
            self._token_expires_at = now + max(60, expire - 120)
            return token


class TikuBot:
    def __init__(
        self,
        *,
        options: FeishuTikuOptions,
        coordinator: MultiAgentCoordinator | None = None,
        sessions: TikuSessionStore | None = None,
    ) -> None:
        self.options = options
        self.coordinator = coordinator or MultiAgentCoordinator(top_k=options.top_k)
        self.sessions = sessions or TikuSessionStore(options.session_ttl_seconds)
        self._chapter_modes: dict[str, str] = {}
        self._mode_lock = threading.Lock()

    def receive_image(self, sender: str, image_path: Path) -> BotResponse:
        if self.chapter_mode(sender) == CHAPTER_MODE_AUTO:
            session = TikuSession(state="searching", image_path=image_path)
            return self._search(sender, session, "auto")
        session = TikuSession(state="waiting_chapter", image_path=image_path)
        self.sessions.save(sender, session)
        return BotResponse(texts=[format_chapter_prompt(image_path)])

    def receive_text(self, sender: str, text: str) -> BotResponse:
        clean = text.strip()
        session = self.sessions.get(sender)

        if is_cancel(clean):
            self.sessions.clear(sender)
            return BotResponse(texts=["已取消本次搜题。"])

        mode = parse_chapter_mode(clean)
        if mode:
            if mode == CHAPTER_MODE_TOGGLE:
                mode = (
                    CHAPTER_MODE_MANUAL
                    if self.chapter_mode(sender) == CHAPTER_MODE_AUTO
                    else CHAPTER_MODE_AUTO
                )
            self.set_chapter_mode(sender, mode)
            self.sessions.clear(sender)
            if mode == CHAPTER_MODE_MANUAL:
                return BotResponse(texts=["已切换到手动章节模式。请重新发送题图，我会先让你选择章节。发送 a 可切回自动。"])
            return BotResponse(texts=["已切换到自动章节模式。请重新发送题图，我会先尝试自动识别章节。发送 a 可切回手动。"])

        if session.state == "waiting_chapter":
            chapter = parse_chapter(clean)
            if not chapter:
                return BotResponse(texts=[format_chapter_prompt(session.image_path)])
            if not session.image_path:
                self.sessions.clear(sender)
                return BotResponse(texts=["这次会话里没有题图，请先发送图片。"])
            return self._search(sender, session, chapter)

        if session.state == "waiting_choice":
            choice = parse_choice(clean)
            if choice is None:
                return BotResponse(texts=["回复 0/1/2/3 获取对应答案；0 表示没有想要的，退出本次搜题。"])
            return self._answer_choice(sender, session, choice)

        chapter = parse_chapter(clean)
        if chapter:
            return BotResponse(texts=["请先发送题目图片，然后我会用这个章节检索。"])
        mode_label = "自动章节" if self.chapter_mode(sender) == CHAPTER_MODE_AUTO else "手动章节"
        return BotResponse(texts=[f"请先发送题目图片。当前模式：{mode_label}。发送“手动”或“自动”可切换。"])

    def _search(self, sender: str, session: TikuSession, chapter: str) -> BotResponse:
        assert session.image_path is not None
        result = self.coordinator.search_image(
            session.image_path,
            chapter,
            rerank=True,
            rerank_top=self.options.rerank_top,
        )
        if result.route.route == "needs_chapter":
            session.state = "waiting_chapter"
            self.sessions.save(sender, session)
            return BotResponse(texts=[format_chapter_prompt(session.image_path, result)])
        if result.route.route == "needs_review":
            self.sessions.clear(sender)
            return BotResponse(
                texts=[
                    "这张题图暂时不能自动检索，需要人工复核。\n"
                    f"分类：{result.route.category}"
                ]
            )
        if not result.results:
            self.sessions.clear(sender)
            return BotResponse(texts=["没有找到匹配题目。"])

        top_results = result.results[:3]
        session.state = "waiting_choice"
        session.chapter = result.chapter or chapter
        session.results = top_results
        self.sessions.save(sender, session)

        chapter_text = result.chapter or chapter
        texts = [format_candidate_reply(chapter_text, top_results)]
        images = [Path(item["path"]) for item in top_results]
        return BotResponse(texts=texts, images=images)

    def chapter_mode(self, sender: str) -> str:
        with self._mode_lock:
            return self._chapter_modes.get(sender, CHAPTER_MODE_AUTO)

    def set_chapter_mode(self, sender: str, mode: str) -> None:
        with self._mode_lock:
            self._chapter_modes[sender] = mode

    def _answer_choice(self, sender: str, session: TikuSession, choice: int) -> BotResponse:
        if choice == 0:
            self.sessions.clear(sender)
            return BotResponse(texts=["已退出本次搜题。"])
        if choice < 1 or choice > len(session.results):
            return BotResponse(texts=[f"当前只有 {len(session.results)} 个候选，请回复 0-{len(session.results)}。"])

        if self.options.dry_run:
            selected = Path(session.results[choice - 1]["path"])
            self.sessions.clear(sender)
            return BotResponse(texts=[f"[dry-run] 将发送第 {choice} 名答案。"], images=[selected])

        before = answer_output_files()
        answer(choice)
        after = answer_output_files()
        new_files = [path for path in after if path not in before]
        answer_images = new_files or after[-3:]
        self.sessions.clear(sender)
        if not answer_images:
            return BotResponse(texts=["已执行答案提取，但没有找到输出图片。"])
        return BotResponse(texts=[f"第 {choice} 名答案："], images=answer_images)


class MockCoordinator:
    def __init__(self, image_paths: list[Path], *, auto_needs_chapter: bool = False) -> None:
        self.image_paths = image_paths
        self.auto_needs_chapter = auto_needs_chapter

    def search_image(
        self,
        image_path: Path,
        chapter: str,
        *,
        rerank: bool = True,
        rerank_top: int = 3,
    ) -> Any:
        del image_path, rerank, rerank_top
        if self.auto_needs_chapter and str(chapter).strip().lower() == "auto":
            return type(
                "MockPipelineResult",
                (),
                {
                    "route": type(
                        "MockRoute",
                        (),
                        {"route": "needs_chapter", "category": "main_numeric"},
                    )(),
                    "results": [],
                    "chapter": None,
                    "chapter_hint": "unknown",
                    "chapter_confidence": 0.1,
                    "chapter_evidence": "mock",
                },
            )()
        return type(
            "MockPipelineResult",
            (),
            {
                "route": type(
                    "MockRoute",
                    (),
                    {"route": "main", "category": "main_numeric"},
                )(),
                "chapter": "5位移法",
                "chapter_hint": "5位移法",
                "chapter_confidence": 1.0,
                "chapter_evidence": "mock",
                "results": [
                    {
                        "rank": index,
                        "path": str(path),
                        "name": path.name,
                        "score": 1.0,
                    }
                    for index, path in enumerate(self.image_paths, 1)
                ],
            },
        )()


class FeishuTikuBridge:
    def __init__(self, bot: TikuBot, client: FeishuClient, options: FeishuTikuOptions) -> None:
        self.bot = bot
        self.client = client
        self.options = options
        self._seen_event_ids: set[str] = set()

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "encrypt" in payload:
            return {"ok": False, "error": "encrypted events are not supported in MVP"}

        if is_url_verification(payload):
            if not self._valid_token(payload):
                return {"ok": False, "error": "invalid verification token"}
            return {"challenge": payload.get("challenge")}

        if not self._valid_token(payload):
            return {"ok": False, "error": "invalid verification token"}

        header = payload.get("header") or {}
        event_type = header.get("event_type") or payload.get("type")
        if event_type != "im.message.receive_v1":
            return {"ok": True, "ignored": event_type or "unknown"}

        event_id = str(header.get("event_id") or "")
        if event_id and event_id in self._seen_event_ids:
            return {"ok": True, "duplicate": event_id}
        if event_id:
            self._seen_event_ids.add(event_id)

        event = payload.get("event") or {}
        message = event.get("message") or {}
        message_id = str(message.get("message_id") or "")
        sender = extract_sender(event)
        if not message_id:
            return {"ok": True, "ignored": "missing-message-id"}
        if is_stale_message(extract_message_created_at(payload, event, message), self.options.max_message_age_seconds):
            return {"ok": True, "ignored": "stale-message", "message_id": message_id}

        thread = threading.Thread(
            target=self._process_and_reply,
            args=(message_id, sender, message),
            daemon=True,
        )
        thread.start()
        return {"ok": True, "accepted": message_id}

    def _process_and_reply(self, message_id: str, sender: str, message: dict[str, Any]) -> None:
        try:
            response = self._response_for_message(message_id, sender, message)
        except Exception as exc:
            response = BotResponse(texts=[f"处理失败：{exc}"])
        for text in response.texts:
            self.client.reply_text(message_id, text)
        for image in response.images:
            self.client.reply_image(message_id, image)

    def _response_for_message(self, message_id: str, sender: str, message: dict[str, Any]) -> BotResponse:
        message_type = message.get("message_type")
        if message_type == "text":
            return self.bot.receive_text(sender, extract_text_message(message))
        if message_type == "image":
            image_key = extract_image_key(message)
            if not image_key:
                return BotResponse(texts=["收到图片消息，但没有拿到 image_key。"])
            self._mark_working_async(message_id)
            output = self.options.temp_dir / "incoming" / f"{message_id}_{image_key}.jpg"
            image_path = self.client.download_message_image(message_id, image_key, output)
            return self.bot.receive_image(sender, image_path)
        return BotResponse(texts=["当前只支持题图图片和章节/编号文字。"])

    def _mark_working_async(self, message_id: str) -> None:
        if not self.options.working_reaction:
            return
        thread = threading.Thread(
            target=self._mark_working,
            args=(message_id,),
            daemon=True,
        )
        thread.start()

    def _mark_working(self, message_id: str) -> None:
        emoji_type = self.options.working_reaction
        if not emoji_type:
            return
        try:
            self.client.add_reaction(message_id, emoji_type)
            print(f"reacted to {message_id}: {emoji_type}", flush=True)
        except Exception as exc:
            print(f"failed to react {message_id}: {exc}", file=sys.stderr, flush=True)

    def _valid_token(self, payload: dict[str, Any]) -> bool:
        expected = self.options.verification_token
        if not expected:
            return True
        actual = (payload.get("header") or {}).get("token") or payload.get("token")
        return actual == expected


class FeishuHandler(BaseHTTPRequestHandler):
    bridge: FeishuTikuBridge

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid json"}, status=400)
            return

        if self.path not in {"/feishu/events", "/"}:
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return

        result = self.bridge.handle_payload(payload)
        status = 200 if result.get("ok", True) else 403
        self._send_json(result, status=status)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def format_chapter_prompt(image_path: Path | None = None, result: Any | None = None) -> str:
    prefix = "收到题图" if image_path else "请选择章节"
    lines = [f"{prefix}，请选择章节："]
    if result is not None:
        hint = getattr(result, "chapter_hint", "")
        confidence = getattr(result, "chapter_confidence", 0.0)
        evidence = getattr(result, "chapter_evidence", "")
        if hint and hint != "unknown":
            lines.append(f"自动识别不够确定：{hint}（{round(float(confidence) * 100)}%）")
        elif evidence:
            lines.append("未能自动确定章节。")
    lines.extend(f"- {chapter}" for chapter in CHAPTERS)
    lines.append("回复章节号 2/3/4/5/6，或直接回复章节名；回复 0 退出。")
    lines.append("发送 a 可在自动/手动章节模式之间切换。")
    return "\n".join(lines)


def format_candidate_reply(chapter: str, results: list[dict[str, Any]]) -> str:
    scores = "、".join(format_result_score(item) for item in results)
    return "\n".join([
        f"章节：{chapter}",
        f"下面是相似题目 Top {len(results)}，相似比分别为：{scores}",
        "0：结束",
        "a：切换手动识别章节",
    ])


def format_result_score(item: dict[str, Any]) -> str:
    score = item.get("final_score") if item.get("final_score") is not None else item.get("score", 0)
    try:
        return f"{round(float(score) * 100)}%"
    except (TypeError, ValueError):
        return "未知"


def parse_chapter(text: str) -> str | None:
    clean = text.strip()
    if clean.isdigit():
        for chapter in CHAPTERS:
            if chapter.startswith(clean):
                return chapter
    for chapter in CHAPTERS:
        if clean == chapter or clean in chapter:
            return chapter
    return None


def parse_choice(text: str) -> int | None:
    clean = text.strip()
    if not re.fullmatch(r"[0-3]", clean):
        return None
    return int(clean)


def parse_chapter_mode(text: str) -> str | None:
    clean = text.strip().lower()
    if clean == "a":
        return CHAPTER_MODE_TOGGLE
    if clean in {"自动", "auto"}:
        return CHAPTER_MODE_AUTO
    if clean in {"手动", "manual", "m"}:
        return CHAPTER_MODE_MANUAL
    return None


def is_cancel(text: str) -> bool:
    return text.strip().lower() in {"0", "取消", "cancel", "退出", "算了"}


def answer_output_files() -> list[Path]:
    root = Path(ANSWER_OUTPUT)
    if not root.exists():
        return []
    patterns = ["*.jpg", "*.jpeg", "*.png", "*.webp"]
    files: list[Path] = []
    for pattern in patterns:
        files.extend(root.glob(pattern))
    return sorted(files, key=lambda path: path.stat().st_mtime)


def extract_text_message(message: dict[str, Any]) -> str:
    if message.get("message_type") != "text":
        return ""
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(content.get("text") or "").strip()


def extract_image_key(message: dict[str, Any]) -> str:
    try:
        content = json.loads(str(message.get("content") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(content.get("image_key") or "").strip()


def extract_sender(event: dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    for key in ("user_id", "open_id", "union_id"):
        value = sender_id.get(key)
        if value:
            return str(value)
    return "feishu"


def is_url_verification(payload: dict[str, Any]) -> bool:
    payload_type = payload.get("type") or (payload.get("header") or {}).get("type")
    return payload_type == "url_verification" and "challenge" in payload


def extract_message_created_at(
    payload: dict[str, Any],
    event: dict[str, Any],
    message: dict[str, Any],
) -> float | None:
    header = payload.get("header") or {}
    for candidate in (
        message.get("create_time"),
        message.get("update_time"),
        event.get("create_time"),
        header.get("create_time"),
        header.get("event_create_time"),
        header.get("timestamp"),
    ):
        parsed = parse_feishu_timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def parse_feishu_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 10_000_000_000_000:
        return timestamp / 1_000_000
    if timestamp > 10_000_000_000:
        return timestamp / 1000
    return timestamp


def is_stale_message(created_at: float | None, max_age_seconds: int) -> bool:
    if created_at is None or max_age_seconds <= 0:
        return False
    return time.time() - created_at > max_age_seconds


def request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if raw_body is not None:
        body = raw_body
        request_headers = {}
    else:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu HTTP error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
            if attempt == 3:
                raise RuntimeError(f"Feishu request failed after retries: {exc}") from exc
            time.sleep(0.8 * attempt)
    else:
        raise RuntimeError(f"Feishu request failed: {last_error}")

    code = data.get("code", 0)
    if code not in {0, "0"}:
        raise RuntimeError(f"Feishu API error: {data}")
    return data


def build_multipart_form_data(
    boundary: str,
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    for name, (filename, content, content_type) in files.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            content,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks)


def load_options(args: argparse.Namespace) -> FeishuTikuOptions:
    app_id = get_env_or_user(args.app_id_env) or cfg.get("feishu_app_id", "")
    app_secret = get_env_or_user(args.app_secret_env) or cfg.get("feishu_app_secret", "")
    verification_token = get_env_or_user(args.verification_token_env) or cfg.get("feishu_verification_token")
    return FeishuTikuOptions(
        app_id=app_id,
        app_secret=app_secret,
        verification_token=verification_token,
        dry_run=args.dry_run,
        session_ttl_seconds=args.session_ttl_minutes * 60,
        temp_dir=Path(args.temp_dir),
        top_k=args.top,
        rerank_top=args.rerank_top,
        max_message_age_seconds=args.max_message_age_minutes * 60,
        working_reaction=args.working_reaction or None,
    )


def get_env_or_user(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "")
    except OSError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="结构力学题库飞书机器人")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--dry-run", action="store_true", help="不调用飞书发送/下载接口")
    parser.add_argument("--app-id-env", default="FEISHU_TIKU_APP_ID")
    parser.add_argument("--app-secret-env", default="FEISHU_TIKU_APP_SECRET")
    parser.add_argument("--verification-token-env", default="FEISHU_TIKU_VERIFICATION_TOKEN")
    parser.add_argument("--temp-dir", default=str(BASE / ".tmp_feishu_tiku"))
    parser.add_argument("--session-ttl-minutes", type=int, default=10)
    parser.add_argument("--max-message-age-minutes", type=int, default=15)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--rerank-top", type=int, default=3)
    parser.add_argument(
        "--working-reaction",
        default="OK",
        help="收到图片后给原消息添加的 emoji_type；留空则关闭",
    )

    sub = parser.add_subparsers(dest="cmd")
    dry = sub.add_parser("dry-run-flow", help="用本地图片模拟图片->章节流程")
    dry.add_argument("--image", required=True, help="本地题图路径")
    dry.add_argument("--chapter", required=True, help="章节编号或章节名")
    dry.add_argument("--choice", type=int, help="可选：继续模拟选择答案 0/1/2/3")
    dry.add_argument(
        "--real-search",
        action="store_true",
        help="调用真实 Qwen/题库检索；默认只用本地 mock 结果测试状态机",
    )
    return parser


def print_response(response: BotResponse) -> None:
    for text in response.texts:
        print(text)
    for image in response.images:
        print(f"[image] {image}")


def run_dry_flow(args: argparse.Namespace, options: FeishuTikuOptions) -> int:
    options.dry_run = True
    coordinator = None
    if not args.real_search:
        image = Path(args.image)
        coordinator = MockCoordinator([image, image, image])
    bot = TikuBot(options=options, coordinator=coordinator)
    sender = "dry-run"
    bot.set_chapter_mode(sender, CHAPTER_MODE_MANUAL)
    print_response(bot.receive_image(sender, Path(args.image)))
    print_response(bot.receive_text(sender, args.chapter))
    if args.choice is not None:
        print_response(bot.receive_text(sender, str(args.choice)))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    options = load_options(args)

    if args.cmd == "dry-run-flow":
        return run_dry_flow(args, options)

    if not options.dry_run and (not options.app_id or not options.app_secret):
        raise SystemExit("missing Feishu app id/secret; set env vars or use --dry-run")

    client = FeishuClient(options.app_id, options.app_secret, dry_run=options.dry_run)
    bot = TikuBot(options=options)
    FeishuHandler.bridge = FeishuTikuBridge(bot=bot, client=client, options=options)
    server = ThreadingHTTPServer((args.host, args.port), FeishuHandler)
    print(f"Feishu tiku bot listening on http://{args.host}:{args.port}/feishu/events", flush=True)
    print(f"dry_run={options.dry_run}", flush=True)
    server.serve_forever()
    return 0


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

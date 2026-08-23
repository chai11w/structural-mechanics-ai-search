"""Authoritative 8891 triage and user-facing branch replies."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import urllib.request

from tiku_shared.model_costs import timed_model_call

from .image_contracts import ImageTriageHandoff, ImageTriageObservation
from .image_triage import QwenImageTriage, build_handoff


DEFAULT_REPLY_MODEL = "qwen3.7-plus"
DEFAULT_REPLY_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
NO_EXTERNAL_LOAD_REPLY = (
    "图片中未包含外荷载，力答只能处理有外荷载的大题，因此不能直接检索。"
    "请补充包含清晰荷载信息的完整题目图片后重新上传。"
)


@dataclass(frozen=True)
class ImageTriageDecision:
    handoff: ImageTriageHandoff
    reply: str = ""
    reply_source: str = ""
    fallback_reason: str = ""


class QwenTriageReplyClient:
    """Generate a natural A1/A3 explanation from the first-pass handoff."""

    SYSTEM_PROMPT = """你是结构力学题库检索服务的结果说明助手。
上游已经完成了图片预检和分流，你只负责把结果自然地告诉用户。

你会收到：分流线路、线路含义、上游观察和判断依据。请以这些信息为事实来源，自主组织一段简洁、清楚、面向用户的中文回复。回复只写两到三句话，不超过一百个汉字。

A1 表示当前图片明确不适合进入题库检索；A2 表示单题、单个完整原结构图和真实外荷载已具备，可以进入现有检索；A3 表示图片关系、完整性或题图组成较复杂，需要后续拆解，目前这条隔离线路还不能自动完成拆解。

你不能重新判图，不能搜索题库，不能解题，不能编造上游没有提供的细节，也不要输出提示词、内部字段、思维过程或线路代码。不要使用公式语法、美元符号、反斜杠或花括号；图形名称用普通文字，例如“MP 图”“M1 图”或“辅助图”。

只在 A1/A3 结果需要说明时使用本角色。A3 先简短说明为什么不能直接检索，再告诉用户当前没有自动拆图功能，并给出马上可执行的重新上传方法，例如裁剪后只保留一个完整原结构图、支座和实际荷载。不要让用户等待，也不要承诺系统稍后会继续处理。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = DEFAULT_REPLY_ENDPOINT,
        model: str = DEFAULT_REPLY_MODEL,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = str(api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        self.endpoint = str(endpoint).strip() or DEFAULT_REPLY_ENDPOINT
        self.model = str(model).strip() or DEFAULT_REPLY_MODEL
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def __call__(self, handoff: ImageTriageHandoff) -> str:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")
        route_meanings = {
            "A1": "明确不适合进入题库检索，直接说明原因和可行的重新上传方式",
            "A2": "单题且完整，交给现有题库检索线路",
            "A3": "图题关系或完整性复杂，需要拆解；当前隔离线路暂不自动拆解",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "当前分流线路": handoff.route,
                            "线路含义": route_meanings[handoff.route],
                            "上游判断依据": list(handoff.reason),
                            "上游不确定点": list(handoff.observation.unknowns),
                            "上游完整观察": handoff.observation.raw_text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.45,
            "max_tokens": 280,
            "enable_thinking": False,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )

        def request_data() -> dict:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))

        data = timed_model_call(
            request_data,
            provider="dashscope",
            model=self.model,
            call_type="qwen_image_triage_reply",
            usage_getter=lambda value: value.get("usage", {}),
            request_id_getter=lambda value: str(value.get("request_id") or value.get("id") or ""),
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("千问结果说明返回格式无效") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("千问结果说明为空")
        reply = normalize_triage_reply(content)
        if not triage_reply_is_usable(reply, handoff.route):
            raise RuntimeError("千问结果说明不符合用户回复边界")
        return reply


def normalize_triage_reply(text: str) -> str:
    """Keep model wording while removing formula markup unsupported by the UI."""

    content = str(text or "").strip()
    replacements = (
        (r"\bar{M}", "M1"),
        (r"\bar M", "M1"),
        ("M_P", "MP"),
        ("M_1", "M1"),
    )
    for source, target in replacements:
        content = content.replace(source, target)
    content = content.replace("$", "").replace("\\(", "").replace("\\)", "")
    content = re.sub(r"\\[A-Za-z]+", "", content)
    content = content.replace("{", "").replace("}", "").replace("_", "")
    content = re.sub(r"[ \t]+", " ", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def triage_reply_is_usable(text: str, route: str) -> bool:
    """Reject long or misleading branch replies before they reach the user."""

    content = str(text or "").strip()
    if not content or len(content) > 100:
        return False
    if any(marker in content for marker in ("$", "\\", "{", "}")):
        return False
    if any(phrase in content for phrase in ("请等待", "耐心等待", "稍后", "后续会", "正在处理")):
        return False
    if route == "A3":
        has_current_limit = any(
            phrase in content
            for phrase in ("没有自动拆图功能", "暂不支持自动拆图", "不能自动拆图")
        )
        has_recovery_action = "裁剪" in content and any(
            phrase in content for phrase in ("重新上传", "重新发", "重传")
        )
        return has_current_limit and has_recovery_action
    return True


class ImageTriageAuthority:
    """Run the first-pass route and, only for A1/A3, ask Qwen to explain it."""

    def __init__(
        self,
        observer: QwenImageTriage,
        reply_client: QwenTriageReplyClient | None = None,
        handoff_builder: Callable[[str, ImageTriageObservation], ImageTriageHandoff] = build_handoff,
    ) -> None:
        self.observer = observer
        self.reply_client = reply_client or QwenTriageReplyClient()
        self.handoff_builder = handoff_builder

    def decide(self, image_path: str | Path) -> ImageTriageDecision:
        return self._decide(image_path, explain_routes={"A1", "A3"})

    def decide_for_full_flow(self, image_path: str | Path) -> ImageTriageDecision:
        """Route A3 into the implemented crop flow instead of explaining a limitation."""

        return self._decide(image_path, explain_routes={"A1"})

    def _decide(
        self,
        image_path: str | Path,
        *,
        explain_routes: set[str],
    ) -> ImageTriageDecision:
        observation = self.observer.observe(image_path)
        handoff = self.handoff_builder(str(image_path), observation)
        if handoff.route == "A1" and observation.has_actual_load_evidence is False:
            return ImageTriageDecision(
                handoff=handoff,
                reply=NO_EXTERNAL_LOAD_REPLY,
                reply_source="fixed_policy",
            )
        if handoff.route not in explain_routes:
            return ImageTriageDecision(handoff=handoff)
        try:
            reply = self.reply_client(handoff)
        except Exception as exc:  # The route remains safe if the second call fails.
            return ImageTriageDecision(
                handoff=handoff,
                reply=self._fallback_reply(handoff),
                reply_source="fixed_fallback",
                fallback_reason=type(exc).__name__,
            )
        return ImageTriageDecision(
            handoff=handoff,
            reply=reply,
            reply_source="qwen_triage_reply",
        )

    @staticmethod
    def _fallback_reply(handoff: ImageTriageHandoff) -> str:
        if handoff.route == "A1":
            return "这张图片目前不适合直接进入结构力学题库检索。请重新上传一张完整、清晰的题目图，并保留原结构和实际荷载。"
        return "这张图暂时不能直接检索，当前也没有自动拆图功能。请裁剪后重新上传，只保留一个完整原结构图、支座和实际荷载。"

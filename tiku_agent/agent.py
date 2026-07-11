"""Single-question orchestration layer for the isolated question-bank Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tiku_agent import render
from tiku_agent.intent import (
    IntentResult,
    parse_user_intent,
)
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    AgentState,
)
from tiku_agent.tools import (
    AgentToolConfig,
    ToolResult,
    analyze_image_tool,
    answer_candidate_tool,
    classify_structure_tool,
    coarse_search_tool,
    rerank_candidates_tool,
    route_bank_tool,
)


@dataclass
class AgentResponse:
    text: str
    images: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    intent: str = ""


@dataclass
class AgentToolbox:
    analyze_image: Callable[..., ToolResult] = analyze_image_tool
    route_bank: Callable[..., ToolResult] = route_bank_tool
    classify_structure: Callable[..., ToolResult] = classify_structure_tool
    coarse_search: Callable[..., ToolResult] = coarse_search_tool
    rerank_candidates: Callable[..., ToolResult] = rerank_candidates_tool
    answer_candidate: Callable[..., ToolResult] = answer_candidate_tool


class TikuSearchAgent:
    """Orchestrate intent, state, and tools for the first single-question Agent."""

    def __init__(
        self,
        *,
        state: AgentState | None = None,
        tools: AgentToolbox | None = None,
        config: AgentToolConfig | None = None,
        intent_parser: Callable[..., IntentResult] = parse_user_intent,
        use_llm_intent: bool = True,
        llm_client: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.state = state or AgentState()
        self.tools = tools or AgentToolbox()
        self.config = config or AgentToolConfig()
        self.intent_parser = intent_parser
        self.use_llm_intent = use_llm_intent
        self.llm_client = llm_client

    def handle_image(self, image_path: str | Path) -> AgentResponse:
        intent = self.intent_parser(
            state=self.state.phase,
            image_path=image_path,
            candidate_count=self.state.candidate_count,
            question_count=self.state.question_count,
            use_llm=self.use_llm_intent,
            llm_client=self.llm_client,
        )
        return self._dispatch(intent)

    def handle_text(self, text: str) -> AgentResponse:
        intent = self.intent_parser(
            text,
            state=self.state.phase,
            candidate_count=self.state.candidate_count,
            question_count=self.state.question_count,
            use_llm=self.use_llm_intent,
            llm_client=self.llm_client,
        )
        return self._dispatch(intent)

    def _dispatch(self, intent: IntentResult) -> AgentResponse:
        self.state.remember_intent(intent.to_dict())
        if intent.intent == "cancel":
            self.state.cancel()
            return self._response(render.render_cancelled(), intent)
        if intent.intent == "resend_answer":
            return self._response(render.render_resend_answer(self.state), intent, images=self.state.last_answer_paths)
        if not intent.ok:
            return self._response(render.render_unsupported(intent.error), intent)
        if intent.intent == "search_image":
            return self._start_image_search(str(intent.data.get("image_path") or ""))
        if intent.intent == "set_chapter":
            return self._set_or_correct_chapter(str(intent.data.get("chapter") or ""), intent)
        if intent.intent == "select_candidate":
            return self._answer_candidate(int(intent.data["rank"]), intent)
        if intent.intent == "select_question":
            return self._response(render.render_unsupported("多题选择的编排还没接入；当前先支持单题检索。"), intent)
        return self._response(render.render_unsupported(intent.error), intent)

    def _start_image_search(self, image_path: str) -> AgentResponse:
        if not image_path:
            return self._fail("没有收到图片路径。")
        self.state.start_search(image_path)
        analyzed = self.tools.analyze_image(image_path, chapter="auto", config=self.config)
        if not analyzed.ok:
            return self._fail(analyzed.error)
        self.state.set_analysis(
            loads=analyzed.data.get("loads", []),
            chapter=analyzed.data.get("chapter") or "",
            question_image_path=analyzed.data.get("image_path") or image_path,
        )
        if self.state.phase == "WAIT_CHAPTER":
            return self._response(render.render_chapter_prompt(self.state), IntentResult("search_image"))
        return self._run_search()

    def _set_or_correct_chapter(self, chapter: str, intent: IntentResult) -> AgentResponse:
        if not chapter:
            return self._response(render.render_unsupported("章节无法识别，请说第几章或方法名。"), intent)
        if not self.state.current_loads:
            self.state.set_chapter(chapter)
            return self._response("已记录章节，请先发题图后再检索。", intent)

        should_correct = bool(self.state.candidates or self.state.last_answer_paths or self.state.current_chapter)
        if should_correct:
            self.state.correct_chapter(chapter)
        else:
            self.state.set_chapter(chapter)
        return self._run_search(intent=intent)

    def _run_search(self, *, intent: IntentResult | None = None) -> AgentResponse:
        routed = self.tools.route_bank(self.state.current_loads)
        if not routed.ok:
            return self._fail(routed.error or routed.data.get("reason", "无法确定检索库"))
        route = str(routed.data.get("route") or "")
        self.state.set_route(route)

        structured = self.tools.classify_structure(
            self.state.active_image_path or None,
            route=route,
            classified=None,
            config=self.config,
        )
        if not structured.ok:
            return self._fail(structured.error)
        structure_type = str(structured.data.get("structure_type") or "")
        self.state.set_route(route, structure_type=structure_type)

        coarse = self.tools.coarse_search(
            self.state.current_loads,
            chapter=self.state.current_chapter,
            route=route,
            structure_type=structure_type,
            top_k=self.config.top_k,
        )
        if not coarse.ok:
            return self._fail(coarse.error)
        candidates = list(coarse.data.get("candidates") or [])
        if not candidates:
            self.state.set_candidates([])
            return self._response(render.render_no_match(self.state), intent or IntentResult("search_image"))

        reranked = self.tools.rerank_candidates(
            self.state.active_image_path or None,
            candidates,
            route=route,
            rerank_top=self.config.rerank_top,
        )
        if not reranked.ok:
            return self._fail(reranked.error)
        visible = list(reranked.data.get("visible_candidates") or candidates)
        self.state.set_candidates(visible)
        text = render.render_candidates(
            self.state,
            reranked=bool(reranked.data.get("reranked")),
            note=str(reranked.data.get("rerank_note") or ""),
        )
        return self._response(text, intent or IntentResult("search_image"), images=[str(item.get("path")) for item in visible if item.get("path")])

    def _answer_candidate(self, rank: int, intent: IntentResult) -> AgentResponse:
        try:
            self.state.select_candidate(rank)
        except ValueError as exc:
            return self._response(render.render_unsupported(str(exc)), intent)

        answered = self.tools.answer_candidate(self.state.candidates, rank=rank, config=self.config)
        if not answered.ok:
            return self._fail(answered.error)
        paths = list(answered.data.get("copied_paths") or answered.data.get("answer_paths") or [])
        self.state.set_answer_paths([str(path) for path in paths])
        return self._response(render.render_answer(self.state), intent, images=self.state.last_answer_paths)

    def _fail(self, error: str) -> AgentResponse:
        self.state.fail(error)
        return self._response(render.render_error(error), IntentResult("unsupported", ok=False, error=error))

    def _response(self, text: str, intent: IntentResult, *, images: list[str] | None = None) -> AgentResponse:
        return AgentResponse(text=text, images=list(images or []), state=self.state.to_dict(), intent=intent.intent)

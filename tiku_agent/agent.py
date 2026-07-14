"""Single-question orchestration layer for the isolated question-bank Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tiku_agent import render
from tiku_agent.action_decision_v2 import ActionDecisionV2
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent import (
    IntentResult,
    parse_user_intent,
)
from tiku_agent.intent_runtime_v2 import (
    INTENT_VERSION_V1,
    INTENT_VERSION_V2,
    INTENT_VERSIONS,
    adapt_decision_v2,
    build_runtime_context_v2,
)
from tiku_agent.intent_v2 import call_qwen_decision_v2, decide_intent_v2
from tiku_agent.reply_shell_v2 import is_reply_shell_action, render_reply_shell_v2
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
    analyze_multi_image_tool,
    answer_candidate_tool,
    classify_structure_tool,
    coarse_search_tool,
    global_search_tool,
    rerank_candidates_tool,
    route_bank_tool,
    prepare_question_units_tool,
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
    analyze_multi_image: Callable[..., ToolResult] = analyze_multi_image_tool
    prepare_question_units: Callable[..., ToolResult] = prepare_question_units_tool
    route_bank: Callable[..., ToolResult] = route_bank_tool
    classify_structure: Callable[..., ToolResult] = classify_structure_tool
    coarse_search: Callable[..., ToolResult] = coarse_search_tool
    global_search: Callable[..., ToolResult] = global_search_tool
    rerank_candidates: Callable[..., ToolResult] = rerank_candidates_tool
    answer_candidate: Callable[..., ToolResult] = answer_candidate_tool


class TikuSearchAgent:
    """Orchestrate isolated single- and multi-question retrieval flows."""

    def __init__(
        self,
        *,
        state: AgentState | None = None,
        tools: AgentToolbox | None = None,
        config: AgentToolConfig | None = None,
        intent_parser: Callable[..., IntentResult] = parse_user_intent,
        use_llm_intent: bool = True,
        llm_client: Callable[[str], dict[str, Any]] | None = None,
        intent_version: str = INTENT_VERSION_V1,
    ) -> None:
        if intent_version not in INTENT_VERSIONS:
            raise ValueError(f"Unsupported intent version: {intent_version}")
        self.state = state or AgentState()
        self.tools = tools or AgentToolbox()
        self.config = config or AgentToolConfig()
        self.intent_parser = intent_parser
        self.use_llm_intent = use_llm_intent
        self.llm_client = llm_client
        self.intent_version = intent_version

    def handle_image(self, image_path: str | Path) -> AgentResponse:
        if self.intent_version == INTENT_VERSION_V2:
            context = build_runtime_context_v2(self.state, trusted_image_event=True)
            decision = decide_intent_v2(
                None,
                context,
                event_type="image",
                llm_client=self._v2_llm_client(),
            )
            return self._dispatch_v2(decision, context, image_path=image_path)
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
        if self.intent_version == INTENT_VERSION_V2:
            context = build_runtime_context_v2(self.state)
            decision = decide_intent_v2(
                text,
                context,
                llm_client=self._v2_llm_client(),
            )
            return self._dispatch_v2(decision, context)
        if self.state.phase == PHASE_ERROR and text.strip() in {"重试", "再试", "再试一次"} and self.state.current_image_path:
            return self._start_image_search(self.state.current_image_path)
        intent = self.intent_parser(
            text,
            state=self.state.phase,
            candidate_count=self.state.candidate_count,
            question_count=self.state.question_count,
            use_llm=self.use_llm_intent,
            llm_client=self.llm_client,
        )
        return self._dispatch(intent)

    def _dispatch_v2(
        self,
        decision: ActionDecisionV2,
        context: ConversationContextV2,
        *,
        image_path: str | Path | None = None,
    ) -> AgentResponse:
        self.state.remember_intent(decision.to_dict())
        if is_reply_shell_action(decision.action):
            return AgentResponse(
                text=render_reply_shell_v2(decision, context),
                state=self.state.to_dict(),
                intent=decision.action,
            )
        return self._dispatch(
            adapt_decision_v2(decision, image_path=image_path),
            remember=False,
        )

    def _v2_llm_client(self) -> Callable[[str], dict[str, Any]] | None:
        if not self.use_llm_intent:
            return None
        return self.llm_client or call_qwen_decision_v2

    def _dispatch(self, intent: IntentResult, *, remember: bool = True) -> AgentResponse:
        if remember:
            self.state.remember_intent(intent.to_dict())
        if intent.intent == "cancel":
            self.state.cancel()
            return self._response(render.render_cancelled(), intent)
        if intent.intent == "resend_answer":
            return self._response(render.render_resend_answer(self.state), intent, images=self.state.last_answer_paths)
        if intent.intent == "explain_failure":
            return self._response(render.render_failure_explanation(self.state), intent)
        if intent.intent == "retry_search":
            return self._start_image_search(self.state.current_image_path)
        if intent.intent == "greeting":
            return self._response(render.render_greeting(), intent)
        if not intent.ok:
            return self._response(render.render_unsupported(intent.error), intent)
        if intent.intent == "search_image":
            return self._start_image_search(
                str(intent.data.get("image_path") or ""),
                chapter_override=str(intent.data.get("chapter_override") or ""),
            )
        if intent.intent == "global_search":
            return self._run_global_search(intent)
        if intent.intent == "set_chapter":
            return self._set_or_correct_chapter(
                str(intent.data.get("chapter") or ""),
                intent,
                chapter_target=str(intent.data.get("chapter_target") or "current_question"),
            )
        if intent.intent == "select_candidate":
            return self._answer_candidate(int(intent.data["rank"]), intent)
        if intent.intent == "select_question":
            return self._select_question(intent)
        return self._response(render.render_unsupported(intent.error), intent)

    def _start_image_search(
        self,
        image_path: str,
        *,
        chapter_override: str = "",
    ) -> AgentResponse:
        if not image_path:
            return self._fail("没有收到图片路径。")
        pending_chapter = chapter_override or self.state.pending_chapter
        self.state.start_search(image_path)
        if pending_chapter:
            self.state.set_pending_chapter(pending_chapter)
        multi = self.tools.analyze_multi_image(image_path, config=self.config)
        if multi.ok and multi.data.get("is_multi"):
            prepared = self.tools.prepare_question_units(
                image_path,
                list(multi.data.get("questions") or []),
                config=self.config,
            )
            if not prepared.ok:
                return self._fail(prepared.error)
            self.state.set_questions(list(prepared.data.get("questions") or []))
            return self._response(render.render_multi_question_list(self.state), IntentResult("search_image"))
        scope_analysis = multi.data.get("single_analysis") if multi.ok else None
        if isinstance(scope_analysis, dict):
            chapter_hint = str(scope_analysis.get("chapter_hint") or "").strip()
            # `unknown` is a model sentinel, not a chapter name.  Keep the
            # session in WAIT_CHAPTER so a pure diagram never searches a
            # fictional `unknown.xlsx` file.
            if chapter_hint.lower() == "unknown":
                chapter_hint = ""
            analyzed = ToolResult(
                ok=True,
                data={
                    "image_path": image_path,
                    "loads": scope_analysis.get("loads", []),
                    "chapter": chapter_hint,
                },
            )
        else:
            analyzed = self.tools.analyze_image(image_path, chapter="auto", config=self.config)
            if not analyzed.ok:
                return self._fail(analyzed.error)
        self.state.set_analysis(
            loads=analyzed.data.get("loads", []),
            chapter=pending_chapter or analyzed.data.get("chapter") or "",
            question_image_path=analyzed.data.get("image_path") or image_path,
        )
        if pending_chapter:
            self.state.consume_pending_chapter()
        if self.state.phase == "WAIT_CHAPTER":
            if self.intent_version == INTENT_VERSION_V2:
                self.state.offer_global_search()
            return self._response(render.render_chapter_prompt(self.state), IntentResult("search_image"))
        return self._run_search()

    def _set_or_correct_chapter(
        self,
        chapter: str,
        intent: IntentResult,
        *,
        chapter_target: str = "current_question",
    ) -> AgentResponse:
        if not chapter:
            return self._response(render.render_unsupported(), intent)
        if chapter_target == "next_image":
            self.state.set_pending_chapter(chapter)
            return self._response(f"好，下一张题图按{chapter}检索。", intent)
        if not self.state.current_loads:
            self.state.set_chapter(chapter)
            return self._response("好，等你把题图发来。", intent)

        should_correct = bool(self.state.candidates or self.state.last_answer_paths or self.state.current_chapter)
        if should_correct:
            self.state.correct_chapter(chapter)
        else:
            self.state.set_chapter(chapter)
        return self._run_search(intent=intent, classified=self._selected_question())

    def _select_question(self, intent: IntentResult) -> AgentResponse:
        pending_chapter = self.state.pending_chapter
        chapter_override = str(intent.data.get("chapter_override") or pending_chapter or "") or None
        try:
            question = self.state.select_question(
                int(intent.data["question_index"]),
                chapter_override=chapter_override,
            )
        except ValueError as exc:
            return self._response(render.render_unsupported(str(exc)), intent)
        if pending_chapter:
            self.state.consume_pending_chapter()
        if self.state.phase == "WAIT_CHAPTER":
            if self.intent_version == INTENT_VERSION_V2:
                self.state.offer_global_search()
            return self._response(render.render_chapter_prompt(self.state), intent)
        return self._run_search(intent=intent, classified=question)

    def _run_search(self, *, intent: IntentResult | None = None, classified: dict[str, Any] | None = None) -> AgentResponse:
        routed = self.tools.route_bank(self.state.current_loads)
        if not routed.ok:
            return self._fail(routed.error or routed.data.get("reason", "无法确定检索库"))
        route = str(routed.data.get("route") or "")
        self.state.set_route(route)

        structured = self.tools.classify_structure(
            self.state.active_image_path or None,
            route=route,
            classified=classified,
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
            self._rerank_query_image_path(),
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

    def _run_global_search(self, intent: IntentResult) -> AgentResponse:
        if not self.state.consume_global_search_offer():
            return self._response(render.render_unsupported(), intent)

        routed = self.tools.route_bank(self.state.current_loads)
        if not routed.ok:
            return self._fail(routed.error or routed.data.get("reason", "无法确定检索库"))
        route = str(routed.data.get("route") or "")
        self.state.set_route(route)

        structured = self.tools.classify_structure(
            self.state.active_image_path or None,
            route=route,
            classified=self._selected_question(),
            config=self.config,
        )
        if not structured.ok:
            return self._fail(structured.error)
        structure_type = str(structured.data.get("structure_type") or "")
        self.state.set_route(route, structure_type=structure_type)

        searched = self.tools.global_search(
            self.state.current_loads,
            self._rerank_query_image_path(),
            route=route,
            structure_type=structure_type,
            config=self.config,
        )
        if not searched.ok:
            return self._fail(searched.error)
        candidates = list(searched.data.get("candidates") or [])
        self.state.set_candidates(candidates)
        if not candidates:
            return self._response(render.render_global_no_match(), intent)
        return self._response(
            render.render_global_candidates(self.state),
            intent,
            images=[str(item.get("path")) for item in candidates if item.get("path")],
        )

    def _selected_question(self) -> dict[str, Any] | None:
        index = self.state.selected_question
        if index is None or not 1 <= index <= len(self.state.questions):
            return None
        return dict(self.state.questions[index - 1])

    def _rerank_query_image_path(self) -> str | None:
        if self.state.selected_question is not None:
            return self.state.current_question_image_path or None
        return self.state.active_image_path or None

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

"""Single-question orchestration layer for the isolated question-bank Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tiku_agent import render
from tiku_agent.action_decision_v2 import (
    SAFETY_ACTIONS,
    TASK_ACTIONS,
    ActionDecisionV2,
)
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_contract import IntentResult
from tiku_agent.intent_runtime_v2 import (
    adapt_decision_v2,
    build_runtime_context_v2,
)
from tiku_agent.intent_v2 import call_qwen_decision_v2, decide_intent_v2
from tiku_agent.reply_shell_v2 import is_reply_shell_action, render_reply_shell_v2
from tiku_agent.safe_answer_context_v0 import (
    SafeConversationContext,
    build_safe_answer_context,
)
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.safe_answer_policy_v0 import evaluate_safe_answer_policy
from tiku_agent.safe_answer_reply_v0 import render_safe_answer_v0
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    AgentState,
)
from tiku_agent.tools import (
    AgentToolConfig,
    ToolOutcome,
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
    reply_source: str = ""
    fallback_reason: str = ""


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
        use_llm_intent: bool = True,
        llm_client: Callable[[str], dict[str, Any]] | None = None,
        progress_reporter: Callable[[str, str], None] | None = None,
        enable_safe_answer_v0: bool = False,
        safe_answer_generator_v0: SafeAnswerGeneratorV0 | None = None,
    ) -> None:
        self.state = state or AgentState()
        self.tools = tools or AgentToolbox()
        self.config = config or AgentToolConfig()
        self.use_llm_intent = use_llm_intent
        self.llm_client = llm_client
        self.progress_reporter = progress_reporter
        self.enable_safe_answer_v0 = enable_safe_answer_v0
        self.safe_answer_generator_v0 = safe_answer_generator_v0

    def handle_image(self, image_path: str | Path) -> AgentResponse:
        context = build_runtime_context_v2(self.state, trusted_image_event=True)
        decision = decide_intent_v2(
            None,
            context,
            event_type="image",
            llm_client=self._v2_llm_client(),
        )
        return self._dispatch_v2(decision, context, image_path=image_path)

    def handle_text(self, text: str) -> AgentResponse:
        if self.enable_safe_answer_v0:
            safe_decision = evaluate_safe_answer_policy(text)
            if safe_decision.eligible:
                if safe_decision.category == "general":
                    context = build_runtime_context_v2(self.state)
                    decision = decide_intent_v2(
                        text,
                        context,
                        llm_client=self._v2_llm_client(),
                    )
                    if decision.action in TASK_ACTIONS | SAFETY_ACTIONS:
                        return self._dispatch_v2(decision, context)
                return self._safe_answer_response(text, safe_decision.category)
        context = build_runtime_context_v2(self.state)
        decision = decide_intent_v2(
            text,
            context,
            llm_client=self._v2_llm_client(),
        )
        return self._dispatch_v2(decision, context)

    def _safe_answer_response(self, text: str, category: str) -> AgentResponse:
        context = self._safe_answer_context()
        if self.safe_answer_generator_v0 is not None:
            try:
                generated = self.safe_answer_generator_v0.generate(text, context)
            except Exception:  # noqa: BLE001 - preserve the reviewed fixed fallback.
                generated = None
            if generated is not None and generated.source != "not_called":
                return AgentResponse(
                    text=generated.text,
                    state=self.state.to_dict(),
                    intent="safe_answer",
                    reply_source=generated.source,
                    fallback_reason=generated.fallback_reason,
                )
        return AgentResponse(
            text=render_safe_answer_v0(category, context),
            state=self.state.to_dict(),
            intent="safe_answer",
            reply_source="fixed_fallback",
            fallback_reason=(
                "generator_error" if self.safe_answer_generator_v0 is not None else ""
            ),
        )

    def _safe_answer_context(self) -> SafeConversationContext | None:
        """Build the whitelisted state summary, degrading to None on any failure.

        A malformed AgentState must never break the safe-answer seam: when the
        summary cannot be derived, generation and the fixed fallback both run
        state-free (context=None), which is the pre-wiring behavior.
        """
        try:
            return build_safe_answer_context(self.state)
        except Exception:  # noqa: BLE001 - degraded fallback must stay safe.
            return None

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
        if intent.intent == "reject_candidates":
            self.state.reject_current_candidates()
            return self._response(render.render_candidates_rejected(self.state), intent)
        if intent.intent == "continue_search":
            self.state.reject_current_candidates()
            return self._run_search(intent=intent, classified=self._selected_question(), continuing=True)
        if intent.intent == "show_candidates":
            return self._response(
                render.render_existing_candidates(self.state),
                intent,
                images=[str(item.get("path")) for item in self.state.candidates if item.get("path")],
            )
        if intent.intent == "report_answer_mismatch":
            self.state.report_answer_mismatch()
            return self._response(render.render_answer_mismatch(self.state), intent)
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
        stopped = self._stop_for_tool_result(multi, allow_partial=True)
        if stopped is not None:
            return stopped
        if multi.ok and multi.data.get("is_multi"):
            prepared = self.tools.prepare_question_units(
                image_path,
                list(multi.data.get("questions") or []),
                config=self.config,
            )
            stopped = self._stop_for_tool_result(prepared, allow_partial=True)
            if stopped is not None:
                return stopped
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
            analyzed = ToolResult.success(
                tool="analyze_image",
                code="SCOPE_ANALYSIS_REUSED",
                data={
                    "image_path": image_path,
                    "loads": scope_analysis.get("loads", []),
                    "chapter": chapter_hint,
                },
            )
        else:
            analyzed = self.tools.analyze_image(image_path, chapter="auto", config=self.config)
        stopped = self._stop_for_tool_result(analyzed, allow_needs_input=True)
        if stopped is not None:
            return stopped
        self.state.set_analysis(
            loads=analyzed.data.get("loads", []),
            chapter=pending_chapter or analyzed.data.get("chapter") or "",
            question_image_path=analyzed.data.get("image_path") or image_path,
        )
        if pending_chapter:
            self.state.consume_pending_chapter()
        if self.state.phase == "WAIT_CHAPTER":
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
            self.state.offer_global_search()
            return self._response(render.render_chapter_prompt(self.state), intent)
        return self._run_search(intent=intent, classified=question)

    def _run_search(
        self,
        *,
        intent: IntentResult | None = None,
        classified: dict[str, Any] | None = None,
        continuing: bool = False,
    ) -> AgentResponse:
        chapter = self.state.current_chapter
        message = f"正在按「{chapter}」搜索题目…" if chapter else "正在搜索题目…"
        self._report_progress("searching", message)
        if continuing and self.state.current_route:
            route = self.state.current_route
            structure_type = self.state.current_structure_type
        else:
            routed = self.tools.route_bank(self.state.current_loads)
            stopped = self._stop_for_tool_result(routed)
            if stopped is not None:
                return stopped
            route = str(routed.data.get("route") or "")
            self.state.set_route(route)

            structured = self.tools.classify_structure(
                self.state.active_image_path or None,
                route=route,
                classified=classified,
                config=self.config,
            )
            stopped = self._stop_for_tool_result(structured, allow_partial=True)
            if stopped is not None:
                return stopped
            structure_type = str(structured.data.get("structure_type") or "")
            self.state.set_route(route, structure_type=structure_type)

        coarse_kwargs: dict[str, Any] = {
            "chapter": self.state.current_chapter,
            "route": route,
            "structure_type": structure_type,
            "top_k": self.config.top_k,
        }
        if continuing:
            coarse_kwargs["exclude_candidate_keys"] = list(self.state.attempted_candidate_keys)
        coarse = self.tools.coarse_search(self.state.current_loads, **coarse_kwargs)
        stopped = self._stop_for_tool_result(coarse)
        if stopped is not None:
            return stopped
        candidates = list(coarse.data.get("candidates") or [])
        self.state.record_search_batch(candidates, has_more=bool(coarse.data.get("has_more")))
        if not candidates:
            self.state.set_candidates([])
            text = render.render_no_more_candidates(self.state) if continuing else render.render_no_match(self.state)
            return self._response(text, intent or IntentResult("search_image"))

        reranked = self.tools.rerank_candidates(
            self._rerank_query_image_path(),
            candidates,
            route=route,
            rerank_top=self.config.rerank_top,
        )
        stopped = self._stop_for_tool_result(reranked, allow_partial=True)
        if stopped is not None:
            return stopped
        if reranked.outcome is ToolOutcome.NO_MATCH:
            self.state.set_candidates([])
            return self._response(
                reranked.error or "未找到可靠相似题。",
                intent or IntentResult("search_image"),
            )
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

        self._report_progress("global_searching", "正在全局搜索题目，可能需要一点时间…")

        routed = self.tools.route_bank(self.state.current_loads)
        stopped = self._stop_for_tool_result(routed)
        if stopped is not None:
            return stopped
        route = str(routed.data.get("route") or "")
        self.state.set_route(route)

        structured = self.tools.classify_structure(
            self.state.active_image_path or None,
            route=route,
            classified=self._selected_question(),
            config=self.config,
        )
        stopped = self._stop_for_tool_result(structured, allow_partial=True)
        if stopped is not None:
            return stopped
        structure_type = str(structured.data.get("structure_type") or "")
        self.state.set_route(route, structure_type=structure_type)

        searched = self.tools.global_search(
            self.state.current_loads,
            self._rerank_query_image_path(),
            route=route,
            structure_type=structure_type,
            config=self.config,
        )
        stopped = self._stop_for_tool_result(searched)
        if stopped is not None:
            return stopped
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
        stopped = self._stop_for_tool_result(answered)
        if stopped is not None:
            return stopped
        if answered.outcome is ToolOutcome.NO_MATCH:
            return self._response(
                answered.error or "未找到该候选题对应的答案文件。",
                intent,
            )
        paths = list(answered.data.get("copied_paths") or answered.data.get("answer_paths") or [])
        self.state.set_answer_paths([str(path) for path in paths])
        return self._response(render.render_answer(self.state), intent, images=self.state.last_answer_paths)

    def _fail(self, error: str) -> AgentResponse:
        self.state.fail(error)
        return self._response(render.render_error(error), IntentResult("unsupported", ok=False, error=error))

    def _stop_for_tool_result(
        self,
        result: ToolResult,
        *,
        allow_partial: bool = False,
        allow_needs_input: bool = False,
    ) -> AgentResponse | None:
        """Apply the five-state contract before consuming tool data."""

        if result.outcome is ToolOutcome.TOOL_ERROR:
            return self._fail(result.error or "工具执行失败，请稍后重试。")
        if result.outcome is ToolOutcome.NEEDS_INPUT and not allow_needs_input:
            message = result.error or "需要补充信息后才能继续。"
            return self._response(message, IntentResult("clarification"))
        if result.outcome is ToolOutcome.PARTIAL and not allow_partial:
            return self._fail(result.error or "工具只完成了部分处理，请稍后重试。")
        return None

    def _response(self, text: str, intent: IntentResult, *, images: list[str] | None = None) -> AgentResponse:
        return AgentResponse(text=text, images=list(images or []), state=self.state.to_dict(), intent=intent.intent)

    def _report_progress(self, stage: str, message: str) -> None:
        if self.progress_reporter is not None:
            self.progress_reporter(stage, message)

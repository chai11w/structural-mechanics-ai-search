"""Single-question orchestration layer for the isolated question-bank Agent."""

from __future__ import annotations

import re
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
from tiku_agent.external_load_screen import ImageSearchCancelled
from tiku_agent.intent_contract import IntentResult
from tiku_agent.intent_runtime_v2 import (
    adapt_decision_v2,
    build_runtime_context_v2,
)
from tiku_agent.intent_v2 import call_qwen_decision_v2, decide_intent_v2
from tiku_agent.reply_shell_v2 import is_reply_shell_action, render_reply_shell_v2
from tiku_agent.safe_answer_context_v0 import (
    SafeAnswerValidationFacts,
    SafeConversationContext,
    build_safe_answer_context,
    build_safe_answer_validation_facts,
)
from tiku_agent.safe_answer_generator_v0 import SafeAnswerGeneratorV0
from tiku_agent.safe_answer_policy_v0 import evaluate_safe_answer_policy
from tiku_agent.safe_answer_reply_v0 import (
    render_grounded_safe_answer_v0,
    render_safe_answer_v0,
)
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
from tiku_shared.request_protocol import (
    RequestAction,
    RequestProtocol,
    RequestStatus,
)
from tiku_shared.chapter_catalog import (
    ChapterScopeResult,
    UNSUPPORTED_TOPIC_DEFINITIONS,
    parse_chapter_scope,
    resolve_image_scope,
)


_CLARIFICATION_PROTOCOL_CODES = {
    "missing_image": "UPLOAD_REQUIRED",
    "missing_chapter": "CHAPTER_REQUIRED",
    "missing_question_index": "QUESTION_INDEX_REQUIRED",
    "missing_candidate_rank": "CANDIDATE_RANK_REQUIRED",
    "candidate_list_unavailable": "CANDIDATE_LIST_UNAVAILABLE",
    "out_of_range": "SELECTION_OUT_OF_RANGE",
    "no_more_candidates": "NO_MORE_CANDIDATES",
}

_CHAPTER_SCOPE_LLM_SUPPLEMENT = """

8896 章节兜底补充规则：
- 仅当当前阶段为 WAIT_CHAPTER 时应用。
- 用户用自然语言描述具体题型或方法时，输出 set_chapter，并把 chapter_override 写成该题型或方法的中文名称；不要猜教材章号或 Excel 目录名。支持与否均由后续代码目录校验。
- 纯寒暄或致谢分别输出 greeting 或 small_talk；寒暄与明确题型同时出现时，题型动作优先。
- 无法判断具体题型时输出 clarification，clarification_reason=missing_chapter。
"""


@dataclass
class AgentResponse:
    text: str
    images: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    reply_source: str = ""
    fallback_reason: str = ""
    protocol: dict[str, Any] = field(default_factory=dict)


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
        enable_chapter_scope_fallback: bool = False,
        image_search_cancelled: Callable[[], bool] | None = None,
        commit_image_candidates: Callable[[], bool] | None = None,
    ) -> None:
        self.state = state or AgentState()
        self.tools = tools or AgentToolbox()
        self.config = config or AgentToolConfig()
        self.use_llm_intent = use_llm_intent
        self.llm_client = llm_client
        self.progress_reporter = progress_reporter
        self.enable_safe_answer_v0 = enable_safe_answer_v0
        self.safe_answer_generator_v0 = safe_answer_generator_v0
        self.enable_chapter_scope_fallback = bool(enable_chapter_scope_fallback)
        self.image_search_cancelled = image_search_cancelled
        self.commit_image_candidates = commit_image_candidates
        self._incoming_search_id = ""
        self._turn_protocol: dict[str, Any] = {}
        self._model_chapter_scope: ChapterScopeResult | None = None

    def handle_image(
        self,
        image_path: str | Path,
        *,
        search_id: str = "",
        prechecked_single: bool = False,
    ) -> AgentResponse:
        self._incoming_search_id = str(search_id or "").strip()
        self._turn_protocol = {}
        context = build_runtime_context_v2(self.state, trusted_image_event=True)
        decision = decide_intent_v2(
            None,
            context,
            event_type="image",
            llm_client=self._v2_llm_client(),
        )
        return self._dispatch_v2(
            decision,
            context,
            image_path=image_path,
            prechecked_single=prechecked_single,
        )

    def handle_preanalyzed_image(
        self,
        image_path: str | Path,
        *,
        loads: list[dict[str, Any]],
        chapter: str = "",
        context_text: str = "",
        classified: dict[str, Any] | None = None,
        search_id: str = "",
    ) -> AgentResponse:
        """Enter A2 with a verified crop and its independently recovered context."""

        clean_chapter = str(chapter or "").strip()
        if clean_chapter.lower() == "unknown":
            clean_chapter = ""
        self._incoming_search_id = str(search_id or "").strip()
        self._turn_protocol = {}
        self.state.start_search(str(image_path), search_id=self._incoming_search_id or None)
        analysis = dict(classified or {})
        analysis.setdefault("loads", list(loads or []))
        analysis.setdefault("chapter_hint", clean_chapter or "unknown")
        analysis.setdefault("chapter_confidence", 1.0 if clean_chapter else 0.0)
        analysis.setdefault("visible_problem_text", str(context_text or "").strip())

        scope: ChapterScopeResult | None = None
        if self.enable_chapter_scope_fallback:
            scope = self._resolve_analysis_scope(analysis)
            clean_chapter = scope.storage_key if scope.status == "supported" else ""
        self.state.set_analysis(
            loads=list(loads or []),
            chapter=clean_chapter,
            question_image_path=str(image_path),
            chapter_scope_status=scope.status if scope is not None else "",
            chapter_scope_topic_id=(scope.topic_id or "") if scope is not None else "",
        )
        if scope is not None and scope.status == "unsupported":
            return self._chapter_scope_unsupported_response(scope)
        if self.state.phase == "WAIT_CHAPTER":
            self.state.offer_global_search()
            return self._response(
                (
                    render.render_chapter_scope_prompt(self.state)
                    if self.enable_chapter_scope_fallback
                    else render.render_chapter_prompt(self.state)
                ),
                IntentResult("search_image"),
            )
        return self._run_search(
            intent=IntentResult("search_image"),
            classified=analysis,
        )

    def handle_text(self, text: str) -> AgentResponse:
        self._incoming_search_id = ""
        self._turn_protocol = {}
        self._model_chapter_scope = None
        if self.enable_chapter_scope_fallback and self.state.phase == "WAIT_CHAPTER":
            text_scope = parse_chapter_scope(text)
            if self.state.chapter_scope_topic_id == "non_chinese_question" and text_scope.status != "uncertain":
                return self._current_chapter_scope_unsupported_response()
            if text_scope.status == "unsupported":
                self.state.chapter_scope_status = text_scope.status
                self.state.chapter_scope_topic_id = text_scope.topic_id or ""
                self.state.global_search_offered = False
                return self._chapter_scope_unsupported_response(text_scope)
            if text_scope.status == "supported":
                context = build_runtime_context_v2(self.state)
                decision = ActionDecisionV2(
                    action="set_chapter",
                    chapter_override=text_scope.storage_key,
                    chapter_target="current_question",
                    source="rule",
                    confidence=1.0,
                    reason="代码识别到明确章节或解题方法",
                )
                return self._dispatch_v2(decision, context)
            if text_scope.reason == "numeric_chapter_requires_textbook":
                return self._chapter_scope_clarification_response(
                    include_supported_topics=(
                        self.state.last_intent.get("action") == "clarification"
                    ),
                )
            if self.state.global_search_offered and self._is_unknown_global_search_consent(text):
                context = build_runtime_context_v2(self.state)
                decision = ActionDecisionV2(
                    action="global_search",
                    source="rule",
                    confidence=1.0,
                    reason="用户明确表示不知道章节并同意题库内全局搜索",
                )
                return self._dispatch_v2(decision, context)
        if self.enable_safe_answer_v0:
            grounded_reply = render_grounded_safe_answer_v0(text)
            if grounded_reply is not None:
                return AgentResponse(
                    text=(
                        render.render_supported_chapter_scopes()
                        if self.enable_chapter_scope_fallback
                        else grounded_reply
                    ),
                    state=self.state.to_dict(),
                    intent="safe_answer",
                    reply_source="grounded_fact",
                )
            safe_decision = evaluate_safe_answer_policy(text)
            if safe_decision.eligible:
                if (
                    self.enable_chapter_scope_fallback
                    and self.state.phase == "WAIT_CHAPTER"
                    and safe_decision.category in {"greeting", "courtesy"}
                ):
                    return AgentResponse(
                        text=render.render_wait_chapter_conversation(safe_decision.category),
                        state=self.state.to_dict(),
                        intent="safe_answer",
                        reply_source="fixed_fallback",
                    )
                if safe_decision.category == "general":
                    context = build_runtime_context_v2(self.state)
                    decision = decide_intent_v2(
                        text,
                        context,
                        llm_client=self._v2_llm_client(),
                    )
                    if (
                        self.enable_chapter_scope_fallback
                        and self.state.phase == "WAIT_CHAPTER"
                    ) or decision.action in TASK_ACTIONS | SAFETY_ACTIONS:
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
        context, validation_facts = self._safe_answer_inputs()
        if self.safe_answer_generator_v0 is not None:
            try:
                generated = self.safe_answer_generator_v0.generate(
                    text,
                    context,
                    validation_facts,
                )
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
            text=render_safe_answer_v0(category, context, validation_facts),
            state=self.state.to_dict(),
            intent="safe_answer",
            reply_source="fixed_fallback",
            fallback_reason=(
                "generator_error" if self.safe_answer_generator_v0 is not None else ""
            ),
        )

    def _safe_answer_inputs(
        self,
    ) -> tuple[SafeConversationContext | None, SafeAnswerValidationFacts | None]:
        """Build separate model-visible and code-only state views.

        A malformed AgentState must never break the safe-answer seam: when the
        summary cannot be derived, generation and the fixed fallback both run
        state-free (context=None), which is the pre-wiring behavior.
        """
        try:
            return (
                build_safe_answer_context(self.state),
                build_safe_answer_validation_facts(self.state),
            )
        except Exception:  # noqa: BLE001 - degraded fallback must stay safe.
            return None, None

    def _dispatch_v2(
        self,
        decision: ActionDecisionV2,
        context: ConversationContextV2,
        *,
        image_path: str | Path | None = None,
        prechecked_single: bool = False,
    ) -> AgentResponse:
        previous_action = str(
            self.state.last_intent.get("action") or self.state.last_intent.get("intent") or ""
        )
        if (
            self.enable_chapter_scope_fallback
            and context.phase == "WAIT_CHAPTER"
            and decision.action == "set_chapter"
            and decision.source == "context_llm"
        ):
            validated_scope = parse_chapter_scope(decision.chapter_override)
            if self.state.chapter_scope_topic_id == "non_chinese_question":
                return self._current_chapter_scope_unsupported_response()
            if validated_scope.status == "unsupported":
                return self._chapter_scope_unsupported_response(validated_scope)
            if validated_scope.status == "supported":
                decision = ActionDecisionV2(
                    action="set_chapter",
                    chapter_override=validated_scope.storage_key,
                    chapter_target=decision.chapter_target or "current_question",
                    source="validator",
                    confidence=1.0,
                    reason="模型章节结果已通过共享目录校验并映射为存储键",
                )
            else:
                decision = ActionDecisionV2(
                    action="clarification",
                    clarification_reason="missing_chapter",
                    source="validator",
                    confidence=1.0,
                    reason="模型章节结果未通过共享目录校验",
                )
        self.state.remember_intent(decision.to_dict())
        if self.enable_chapter_scope_fallback and context.phase == "WAIT_CHAPTER":
            if decision.action == "out_of_scope":
                if (
                    self._model_chapter_scope is not None
                    and self._model_chapter_scope.status == "unsupported"
                ):
                    return self._chapter_scope_unsupported_response(
                        self._model_chapter_scope
                    )
                self.state.global_search_offered = False
                return AgentResponse(
                    text=render.render_chapter_scope_unsupported("", ""),
                    state=self.state.to_dict(),
                    intent=decision.action,
                    protocol=RequestProtocol.from_code(
                        "REQUEST_OUT_OF_SCOPE",
                        search_id=self.state.current_search_id,
                    ).to_dict(),
                )
            if decision.action in {"greeting", "small_talk"}:
                category = "greeting" if decision.action == "greeting" else "courtesy"
                return AgentResponse(
                    text=render.render_wait_chapter_conversation(category),
                    state=self.state.to_dict(),
                    intent=decision.action,
                    protocol=RequestProtocol.from_code(
                        "REQUEST_SUCCEEDED",
                        search_id=self.state.current_search_id,
                    ).to_dict(),
                )
            if decision.action == "clarification":
                return self._chapter_scope_clarification_response(
                    include_supported_topics=previous_action == "clarification",
                )
        if is_reply_shell_action(decision.action):
            return AgentResponse(
                text=render_reply_shell_v2(decision, context),
                state=self.state.to_dict(),
                intent=decision.action,
                protocol=self._reply_shell_protocol(decision),
            )
        if prechecked_single and decision.action == "search_image":
            return self._start_image_search(
                str(image_path or ""),
                prechecked_single=True,
            )
        return self._dispatch(
            adapt_decision_v2(decision, image_path=image_path),
            remember=False,
        )

    def _v2_llm_client(self) -> Callable[[str], dict[str, Any]] | None:
        if not self.use_llm_intent:
            return None
        client = self.llm_client or call_qwen_decision_v2
        if not self.enable_chapter_scope_fallback:
            return client

        def validate_model_chapter(prompt: str) -> dict[str, Any]:
            payload = dict(client(prompt + _CHAPTER_SCOPE_LLM_SUPPLEMENT))
            if payload.get("action") != "set_chapter":
                return payload
            scope = parse_chapter_scope(payload.get("chapter_override"))
            self._model_chapter_scope = scope
            if scope.status == "supported":
                payload["chapter_override"] = scope.storage_key
            elif scope.status == "unsupported":
                payload["action"] = "out_of_scope"
                payload["chapter_override"] = None
                payload["chapter_target"] = None
            return payload

        return validate_model_chapter

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
        prechecked_single: bool = False,
    ) -> AgentResponse:
        if not image_path:
            return self._fail("没有收到图片路径。")
        notices: list[str] = []
        self._raise_if_image_search_cancelled()
        pending_chapter = chapter_override or self.state.pending_chapter
        self.state.start_search(
            image_path,
            search_id=self._incoming_search_id or None,
        )
        if pending_chapter:
            self.state.set_pending_chapter(pending_chapter)
        if prechecked_single:
            multi = ToolResult.success(
                code="TRIAGE_SINGLE_QUESTION_CONFIRMED",
                data={"is_multi": False, "single_analysis": None},
                next_state="READY_FOR_SINGLE_ANALYSIS",
            )
        else:
            multi = self.tools.analyze_multi_image(image_path, config=self.config)
        self._raise_if_image_search_cancelled()
        stopped = self._stop_for_tool_result(multi, allow_partial=True)
        if stopped is not None:
            return stopped
        self._collect_partial_notice(notices, multi)
        if multi.ok and multi.data.get("is_multi"):
            prepared = self.tools.prepare_question_units(
                image_path,
                list(multi.data.get("questions") or []),
                config=self.config,
            )
            self._raise_if_image_search_cancelled()
            stopped = self._stop_for_tool_result(prepared, allow_partial=True)
            if stopped is not None:
                return stopped
            self._collect_partial_notice(notices, prepared)
            self.state.set_questions(list(prepared.data.get("questions") or []))
            return self._response(
                render.render_multi_question_list(
                    self.state, note=self._join_notices(notices)
                ),
                IntentResult("search_image"),
            )
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
                    "chapter_hint": scope_analysis.get("chapter_hint", "unknown"),
                    "chapter_confidence": scope_analysis.get("chapter_confidence", 0.0),
                    "visible_problem_text": scope_analysis.get("visible_problem_text", ""),
                    "classified": scope_analysis,
                },
            )
        else:
            analyzed = self.tools.analyze_image(image_path, chapter="auto", config=self.config)
        self._raise_if_image_search_cancelled()
        stopped = self._stop_for_tool_result(analyzed, allow_needs_input=True)
        if stopped is not None:
            return stopped
        scope: ChapterScopeResult | None = None
        resolved_chapter = pending_chapter or analyzed.data.get("chapter") or ""
        if self.enable_chapter_scope_fallback:
            scope = self._resolve_analysis_scope(analyzed.data)
            if pending_chapter and scope.status != "unsupported":
                resolved_chapter = pending_chapter
                scope_status = "supported"
                scope_topic_id = ""
            else:
                resolved_chapter = scope.storage_key if scope.status == "supported" else ""
                scope_status = scope.status
                scope_topic_id = scope.topic_id or ""
        else:
            scope_status = ""
            scope_topic_id = ""
        self.state.set_analysis(
            loads=analyzed.data.get("loads", []),
            chapter=resolved_chapter,
            question_image_path=analyzed.data.get("image_path") or image_path,
            chapter_scope_status=scope_status,
            chapter_scope_topic_id=scope_topic_id,
        )
        if scope is not None and scope.status == "unsupported" and not resolved_chapter:
            return self._chapter_scope_unsupported_response(scope)
        if pending_chapter:
            self.state.consume_pending_chapter()
        if self.state.phase == "WAIT_CHAPTER":
            self.state.offer_global_search()
            self._raise_if_image_search_cancelled()
            return self._response(
                (
                    render.render_chapter_scope_prompt(
                        self.state, note=self._join_notices(notices)
                    )
                    if self.enable_chapter_scope_fallback
                    else render.render_chapter_prompt(
                        self.state, note=self._join_notices(notices)
                    )
                ),
                IntentResult("search_image"),
            )
        return self._run_search(notices=notices)

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
        notices: list[str] | None = None,
    ) -> AgentResponse:
        notices = list(notices or [])
        chapter = self.state.current_chapter
        message = f"正在按「{chapter}」搜索题目…" if chapter else "正在搜索题目…"
        self._report_progress("searching", message)
        self._raise_if_image_search_cancelled()
        if continuing and self.state.current_route:
            route = self.state.current_route
            structure_type = self.state.current_structure_type
        else:
            routed = self.tools.route_bank(self.state.current_loads)
            self._raise_if_image_search_cancelled()
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
            self._raise_if_image_search_cancelled()
            stopped = self._stop_for_tool_result(structured, allow_partial=True)
            if stopped is not None:
                return stopped
            self._collect_partial_notice(notices, structured)
            structure_type = str(structured.data.get("structure_type") or "")
            self.state.set_route(route, structure_type=structure_type)

        coarse_kwargs: dict[str, Any] = {
            "chapter": self.state.current_chapter,
            "route": route,
            "structure_type": structure_type,
            "top_k": self.config.top_k,
        }
        if self.config.dimension_filter_enabled:
            coarse_kwargs["query_image_path"] = self._rerank_query_image_path()
            coarse_kwargs["config"] = self.config
        if continuing:
            coarse_kwargs["exclude_candidate_keys"] = list(self.state.attempted_candidate_keys)
        coarse = self.tools.coarse_search(self.state.current_loads, **coarse_kwargs)
        self._raise_if_image_search_cancelled()
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
        self._raise_if_image_search_cancelled()
        stopped = self._stop_for_tool_result(reranked, allow_partial=True)
        if stopped is not None:
            return stopped
        self._collect_partial_notice(notices, reranked)
        if reranked.outcome is ToolOutcome.NO_MATCH:
            self.state.set_candidates([])
            return self._response(
                reranked.error or "未找到可靠相似题。",
                intent or IntentResult("search_image"),
            )
        visible = list(reranked.data.get("visible_candidates") or candidates)
        self.state.set_candidates(visible)
        if visible and self.commit_image_candidates is not None:
            if not self.commit_image_candidates():
                raise ImageSearchCancelled("external-load screen won before candidates committed")
        text = render.render_candidates(
            self.state,
            reranked=bool(reranked.data.get("reranked")),
            note=self._join_notices(notices),
        )
        return self._response(text, intent or IntentResult("search_image"), images=[str(item.get("path")) for item in visible if item.get("path")])

    def _run_global_search(self, intent: IntentResult) -> AgentResponse:
        if not self.state.consume_global_search_offer():
            return self._response(render.render_unsupported(), intent)

        self._report_progress("global_searching", "正在全局搜索题目，可能需要一点时间…")
        notices: list[str] = []

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
        self._collect_partial_notice(notices, structured)
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
            return self._response(
                render.append_notice(
                    render.render_global_no_match(), self._join_notices(notices)
                ),
                intent,
            )
        return self._response(
            render.render_global_candidates(
                self.state, note=self._join_notices(notices)
            ),
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
                protocol=self._protocol_from_tool_result(answered),
            )
        paths = list(answered.data.get("copied_paths") or answered.data.get("answer_paths") or [])
        self.state.set_answer_paths([str(path) for path in paths])
        return self._response(render.render_answer(self.state), intent, images=self.state.last_answer_paths)

    @staticmethod
    def _resolve_analysis_scope(payload: dict[str, Any]) -> ChapterScopeResult:
        classified = payload.get("classified")
        source = classified if isinstance(classified, dict) else {}

        def value(name: str, default: Any) -> Any:
            return source[name] if name in source else payload.get(name, default)

        return resolve_image_scope(
            chapter_hint=value("chapter_hint", payload.get("chapter") or ""),
            chapter_confidence=value("chapter_confidence", 0.0),
            visible_problem_text=value("visible_problem_text", ""),
        )

    def _chapter_scope_unsupported_response(
        self,
        scope: ChapterScopeResult,
    ) -> AgentResponse:
        self.state.current_chapter = ""
        self.state.chapter_scope_status = "unsupported"
        self.state.chapter_scope_topic_id = scope.topic_id or ""
        self.state.global_search_offered = False
        self.state.phase = "WAIT_CHAPTER"
        self.state.remember_intent(
            {
                "action": "out_of_scope",
                "source": "chapter_scope",
                "topic_id": scope.topic_id or "",
            }
        )
        return AgentResponse(
            text=render.render_chapter_scope_unsupported(
                scope.topic_id or "",
                scope.display_name or "",
            ),
            state=self.state.to_dict(),
            intent="out_of_scope",
            protocol=RequestProtocol.from_code(
                "REQUEST_OUT_OF_SCOPE",
                search_id=self.state.current_search_id,
            ).to_dict(),
        )

    def _current_chapter_scope_unsupported_response(self) -> AgentResponse:
        topic_id = self.state.chapter_scope_topic_id
        definition = next(
            (item for item in UNSUPPORTED_TOPIC_DEFINITIONS if item.topic_id == topic_id),
            None,
        )
        if definition is None:
            return AgentResponse(
                text=render.render_chapter_scope_unsupported(topic_id),
                state=self.state.to_dict(),
                intent="out_of_scope",
                protocol=RequestProtocol.from_code(
                    "REQUEST_OUT_OF_SCOPE",
                    search_id=self.state.current_search_id,
                ).to_dict(),
            )
        return self._chapter_scope_unsupported_response(
            ChapterScopeResult(
                status="unsupported",
                topic_id=definition.topic_id,
                display_name=definition.display_name,
                reason="persisted_unsupported_scope",
            )
        )

    def _chapter_scope_clarification_response(
        self,
        *,
        include_supported_topics: bool = False,
    ) -> AgentResponse:
        self.state.phase = "WAIT_CHAPTER"
        if self.state.chapter_scope_topic_id != "non_chinese_question":
            self.state.chapter_scope_status = "uncertain"
            self.state.chapter_scope_topic_id = ""
        self.state.remember_intent(
            {
                "action": "clarification",
                "clarification_reason": "missing_chapter",
                "source": "chapter_scope",
            }
        )
        return AgentResponse(
            text=render.render_chapter_scope_prompt(
                self.state,
                include_supported_topics=include_supported_topics,
            ),
            state=self.state.to_dict(),
            intent="clarification",
            protocol=RequestProtocol.from_code(
                "CHAPTER_REQUIRED",
                search_id=self.state.current_search_id,
            ).to_dict(),
        )

    @staticmethod
    def _is_unknown_global_search_consent(text: str) -> bool:
        compact = re.sub(r"[\s，。！？!?、,.：:；;]+", "", str(text or ""))
        return any(
            re.fullmatch(pattern, compact) is not None
            for pattern in (
                r"(?:我)?(?:确实)?不知道(?:章节|题型|哪一章)?(?:那就|你|麻烦你)?(?:帮我)?(?:全局)?(?:搜|搜索|找|查)(?:一下|吧|一下吧)?",
                r"(?:可以|行|好|好的|同意)(?:那就)?(?:帮我)?(?:全局|全题库)?(?:搜|搜索|找|查)(?:一下|吧|一下吧)?",
            )
        )

    def _fail(self, error: str, result: ToolResult | None = None) -> AgentResponse:
        self.state.fail(error)
        protocol = None
        if result is not None:
            protocol = RequestProtocol(
                status=RequestStatus.ERROR,
                layer=result.layer,
                code=result.code or "TOOL_FAILED",
                retryable=result.retryable,
                action=(
                    result.action
                    if result.action is not RequestAction.NONE
                    else RequestAction.RETRY_SEARCH
                ),
                search_id=self.state.current_search_id,
            ).to_dict()
        return self._response(
            render.render_error(error),
            IntentResult("unsupported", ok=False, error=error),
            protocol=protocol,
        )

    def _stop_for_tool_result(
        self,
        result: ToolResult,
        *,
        allow_partial: bool = False,
        allow_needs_input: bool = False,
    ) -> AgentResponse | None:
        """Apply the five-state contract before consuming tool data."""

        if result.outcome is ToolOutcome.ERROR:
            return self._fail(result.error or "工具执行失败，请稍后重试。", result)
        if result.outcome is ToolOutcome.NEEDS_INPUT and not allow_needs_input:
            message = result.error or "需要补充信息后才能继续。"
            return self._response(
                message,
                IntentResult("clarification"),
                protocol=self._protocol_from_tool_result(result),
            )
        if result.outcome is ToolOutcome.PARTIAL and not allow_partial:
            self.state.fail(result.error or "工具只完成了部分处理，请稍后重试。")
            return self._response(
                render.render_error(self.state.last_error),
                IntentResult("unsupported", ok=False, error=self.state.last_error),
                protocol=self._protocol_from_tool_result(result),
            )
        return None

    def _collect_partial_notice(self, notices: list[str], result: ToolResult) -> None:
        if result.outcome is not ToolOutcome.PARTIAL:
            return
        if not self._turn_protocol:
            self._turn_protocol = self._protocol_from_tool_result(result)
        note = str(result.error or result.data.get("rerank_note") or "").strip()
        if note and note not in notices:
            notices.append(note)

    @staticmethod
    def _join_notices(notices: list[str]) -> str:
        return "；".join(dict.fromkeys(note.strip() for note in notices if note.strip()))

    def _response(
        self,
        text: str,
        intent: IntentResult,
        *,
        images: list[str] | None = None,
        protocol: dict[str, Any] | None = None,
    ) -> AgentResponse:
        derived_protocol = protocol
        if derived_protocol is None and self.state.phase in {
            PHASE_ERROR,
            PHASE_NO_MATCH,
            "WAIT_CHAPTER",
        }:
            derived_protocol = self._default_protocol()
        if derived_protocol is None:
            derived_protocol = self._turn_protocol or self._default_protocol()
        return AgentResponse(
            text=text,
            images=list(images or []),
            state=self.state.to_dict(),
            intent=intent.intent,
            protocol=dict(derived_protocol),
        )

    def _protocol_from_tool_result(self, result: ToolResult) -> dict[str, Any]:
        action = result.action
        if action is RequestAction.NONE and result.outcome is RequestStatus.ERROR:
            action = RequestAction.RETRY_SEARCH
        return RequestProtocol(
            status=result.outcome,
            layer=result.layer,
            code=result.code or "TOOL_FAILED",
            retryable=result.retryable,
            action=action,
            search_id=self.state.current_search_id,
        ).to_dict()

    def _reply_shell_protocol(self, decision: ActionDecisionV2) -> dict[str, Any]:
        if decision.action == "clarification":
            code = _CLARIFICATION_PROTOCOL_CODES.get(
                decision.clarification_reason or "",
                "CLARIFICATION_REQUIRED",
            )
        elif decision.action == "out_of_scope":
            code = "REQUEST_OUT_OF_SCOPE"
        elif decision.action == "reject":
            code = "ACTION_NOT_ALLOWED"
        else:
            code = "REQUEST_SUCCEEDED"
        return RequestProtocol.from_code(
            code,
            search_id=self.state.current_search_id,
        ).to_dict()

    def _default_protocol(self) -> dict[str, Any]:
        if self.state.phase == PHASE_ERROR:
            code = "AGENT_FAILED"
        elif self.state.phase == PHASE_NO_MATCH:
            code = "NO_MATCH"
        elif self.state.phase == "WAIT_CHAPTER":
            code = "CHAPTER_REQUIRED"
        else:
            code = "REQUEST_SUCCEEDED"
        return RequestProtocol.from_code(
            code, search_id=self.state.current_search_id
        ).to_dict()

    def _report_progress(self, stage: str, message: str) -> None:
        if self.progress_reporter is not None:
            self.progress_reporter(stage, message)

    def _raise_if_image_search_cancelled(self) -> None:
        if self.image_search_cancelled is not None and self.image_search_cancelled():
            raise ImageSearchCancelled("external-load screen stopped the image search")

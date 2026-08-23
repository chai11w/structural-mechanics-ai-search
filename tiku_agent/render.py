"""Text rendering helpers for the isolated question-bank Agent."""

from __future__ import annotations

import re

from tiku_agent.state import AgentState
from tiku_agent.tool_result import ToolOutcome, ToolResult
from tiku_shared.chapter_catalog import supported_topic_names


def render_greeting() -> str:
    return (
        "你好，我是力答，一个结构力学题库搜题助手。"
        "我可以识别你发来的题图、判断题目章节、从题库寻找相似题，并返回对应答案。"
        "发一张结构力学题图给我看看吧。"
    )


def render_chapter_prompt(state: AgentState, *, note: str = "") -> str:
    if state.global_search_offered:
        text = "我还不能确定这题属于哪一章。你知道的话直接告诉我；也可以让我全局搜索，不过会慢一点。"
    else:
        text = "我还不能确定这题属于哪一章。你知道的话告诉我就行。"
    return append_notice(text, note)


def render_chapter_scope_prompt(
    state: AgentState,
    *,
    include_supported_topics: bool = False,
    note: str = "",
) -> str:
    if include_supported_topics:
        text = (
            "我还是不能确定这题属于哪一章。请告诉我章节名称或解题方法。"
            f"当前支持：{_supported_scope_text()}。"
        )
    elif state.global_search_offered:
        text = (
            "我还不能确定这题属于哪一章。你知道的话直接告诉我章节名称或解题方法；"
            "也可以让我全局搜索，不过会慢一点。"
        )
    else:
        text = "我还不能确定这题属于哪一章。你知道的话直接告诉我章节名称或解题方法。"
    return append_notice(text, note)


def render_chapter_scope_unsupported(topic_id: str, display_name: str = "") -> str:
    if topic_id == "non_chinese_question":
        return "识别到的题干没有中文，当前只处理含中文题干的题目。请上传含中文题干的版本。"
    topic = str(display_name or "").strip()
    if topic:
        return f"这道题属于{topic}，当前题库暂不支持。当前支持：{_supported_scope_text()}。"
    return f"这道题不在当前题库支持范围内。当前支持：{_supported_scope_text()}。"


def render_supported_chapter_scopes() -> str:
    return (
        f"当前支持：{_supported_scope_text()}。"
        "矩阵位移法和影响线仅支持含具体外荷载的题目。"
    )


def render_wait_chapter_conversation(category: str) -> str:
    if category == "greeting":
        return "你好，我在。刚才那道题还在等待章节判断。"
    return "不客气。刚才那道题还在等待章节判断。"


def _supported_scope_text() -> str:
    return "、".join(supported_topic_names())


def render_multi_question_list(state: AgentState, *, note: str = "") -> str:
    if not state.questions:
        return append_notice("没有识别到可选择的题目。", note)
    return append_notice(
        f"我在这张图里看到了 {len(state.questions)} 道题。你想查哪一道？",
        note,
    )


def render_candidates(state: AgentState, *, reranked: bool = False, note: str = "") -> str:
    del reranked
    if not state.candidates:
        return append_notice("没有找到可用候选。", note)

    if len(state.candidates) == 1:
        text = "我从题库里找到了最相似的一道题。你看看是不是这道。"
    else:
        text = f"我从题库里找到了 {len(state.candidates)} 道比较像的题，你看看有没有想要的。"
    return append_notice(text, note)


def render_candidates_rejected(
    state: AgentState,
    *,
    author_contact_fallback: bool = False,
) -> str:
    if author_contact_fallback:
        if state.continuation_available:
            return "收到，这批候选都先排除。你可以回复“继续搜”看下一批，或联系作者手搓。"
        return "目前没有更多相似候选题，你可以联系作者手搓。"
    if state.continuation_available:
        return "收到，这批候选都先排除。你可以回复“继续搜”看下一批，或告诉我换哪个章节。"
    return "收到，这批候选都不匹配。当前范围已经没有更多候选，可以换章节或发一张更清楚的题图。"


def render_existing_candidates(state: AgentState) -> str:
    if not state.candidates:
        return "当前没有可以返回的候选。"
    return f"好，回到当前这 {len(state.candidates)} 个候选。直接回复候选编号即可。"


def render_answer_mismatch(state: AgentState) -> str:
    if state.continuation_available:
        return "收到，这个答案先标记为不匹配。你可以选其他候选，或回复“继续搜”看下一批。"
    return "收到，这个答案先标记为不匹配。你可以返回候选改选；当前范围没有更多候选。"


def render_no_more_candidates(
    state: AgentState,
    *,
    author_contact_fallback: bool = False,
) -> str:
    if author_contact_fallback:
        return "目前没有更多相似候选题，你可以联系作者手搓。"
    chapter = state.current_chapter or "当前范围"
    return f"{chapter}里已经没有更多未看过的候选了。可以换章节或发一张更清楚的题图。"


def render_global_candidates(state: AgentState, *, note: str = "") -> str:
    if not state.candidates:
        return append_notice(render_global_no_match(), note)
    chapters = []
    for item in state.candidates:
        for chapter in item.get("source_chapters") or [item.get("chapter")]:
            if chapter and chapter not in chapters:
                chapters.append(str(chapter))
    sources = "、".join(f"「{chapter}」" for chapter in chapters)
    if len(state.candidates) == 1:
        text = f"我从全题库找到了一道高相似题，来自{sources}。你看是不是这道。"
    else:
        text = f"我从全题库找到 {len(state.candidates)} 道高相似题，分别来自{sources}。你看有没有想要的。"
    return append_notice(text, note)


def render_global_no_match() -> str:
    return "我已经全局搜过了，但暂时没有足够可靠的结果。你如果知道章节，可以告诉我再搜一次。"


def render_answer(state: AgentState) -> str:
    if not state.last_answer_paths:
        return "没有找到可发送的答案文件。"
    return "找到了，答案发你了。"


def render_resend_answer(state: AgentState) -> str:
    if not state.last_answer_paths:
        return "我这里还没有上一题答案记录，请先选一个候选。"
    return "好，刚才的答案再发你一次。"


def render_cancelled() -> str:
    return "好，已经取消了。"


def render_unsupported(message: str = "") -> str:
    del message
    return "我没太明白。你换个说法试试？"


def render_error(error: str) -> str:
    detail = _safe_failure_detail(error)
    if "HTTP Error 5" in str(error) or "timed out" in str(error).lower() or "timeout" in str(error).lower():
        return f"题图识别服务暂时异常（{detail}）。题图已保留，你可以直接回复“重试”。"
    return "这次没查成功。题图已保留，你可以直接回复“重试”。"


# Tool ``error`` and ``data`` are internal compatibility fields.  Only this
# deterministic catalog may turn a non-success result into user-facing text.
_TOOL_FEEDBACK_BY_CODE = {
    "LOAD_ROUTE_MIXED_REVIEW_REQUIRED": (
        "识别到数字荷载和未赋值的字母荷载同时出现，当前无法可靠选择题库。"
        "请换一张更清楚的题图。"
    ),
    "LOAD_ROUTE_NEEDS_REVIEW": (
        "识别到的荷载信息暂时无法可靠选择题库，请换一张更清楚的题图。"
    ),
    "LOAD_ROUTE_INPUT_UNUSABLE": "暂时无法可靠识别题目的荷载信息，请上传更清楚的题图。",
    "CHAPTER_REQUIRED": "我还不能确定这题属于哪一章，请告诉我章节名称或解题方法。",
    "UNKNOWN_CHAPTER": "指定章节不存在，请选择当前支持的章节。",
    "GLOBAL_SEARCH_IMAGE_REQUIRED": "全局搜索需要当前题图，请重新上传题目。",
    "CANDIDATE_NUMBER_REQUIRED": "请回复候选编号，例如 1，或回复 0 取消。",
    "CANDIDATE_DELETE_RANK_OUT_OF_RANGE": "这个候选编号超出当前范围，请换一个。",
    "CANDIDATE_RANK_OUT_OF_RANGE": "这个候选编号超出当前范围，请换一个。",
    "CANDIDATE_RANK_INVALID": "这个候选编号无效，请从当前候选中选择。",
    "MULTI_DETECTION_FALLBACK": "多题判断未完成，已按单题流程继续。",
    "MULTI_CROPS_UNAVAILABLE": "部分题图裁剪未完成，仍可按题号继续。",
    "STRUCTURE_FILTER_SKIPPED_NO_IMAGE": "缺少题图，已跳过结构类型筛选。",
    "STRUCTURE_TYPE_UNCERTAIN": "结构类型无法可靠确定，已跳过该筛选。",
    "STRUCTURE_CLASSIFICATION_FALLBACK": "结构类型识别未完成，已跳过该筛选。",
    "RERANK_SKIPPED_NO_IMAGE": "缺少查询题图，已显示粗筛结果。",
    "RERANK_INCOMPLETE_COARSE_FALLBACK": "复筛未完成，已回退粗筛排序。",
    "RERANK_EMPTY_COARSE_FALLBACK": "视觉复筛未返回结果，已显示粗筛结果。",
    "GLOBAL_RERANK_INCOMPLETE": "全局复筛未完成，请稍后重试。",
    "NO_COARSE_CANDIDATES": "当前章节没有找到足够相似的题，可以换章节或发一张更清楚的题图。",
    "NO_GLOBAL_COARSE_CANDIDATES": "我已经全局搜过了，但暂时没有足够可靠的结果。",
    "NO_GLOBAL_RELIABLE_CANDIDATES": "我已经全局搜过了，但暂时没有足够可靠的结果。",
    "NO_CANDIDATES_TO_RERANK": "没有找到可供复筛的候选题。",
    "NO_RELIABLE_RERANK_CANDIDATES": (
        "没有找到足够可靠的相似候选题，可以换章节或发一张更清楚的题图。"
    ),
    "ANSWER_FILES_NOT_FOUND": "未找到该候选题对应的答案文件，请返回候选后选择其他题。",
    "IMAGE_ANALYSIS_FAILED": "题图识别暂时失败，题图已保留，你可以直接回复“重试”。",
    "MULTI_DETAIL_INVALID": "多题识别暂时失败，题图已保留，你可以直接回复“重试”。",
    "MULTI_DETAIL_FAILED": "多题识别暂时失败，题图已保留，你可以直接回复“重试”。",
    "MULTI_DETECTION_FAILED": "图片分析暂时失败，题图已保留，你可以直接回复“重试”。",
    "BANK_ROUTE_FAILED": "题库路由暂时失败，请稍后重试。",
    "COARSE_SEARCH_FAILED": "题库粗筛暂时失败，请稍后重试。",
    "GLOBAL_SEARCH_UNSUPPORTED_ROUTE": "当前题库路由不支持全局搜索，请重新上传题图或换章节。",
    "GLOBAL_SEARCH_FAILED": "全局搜索暂时失败，请稍后重试。",
    "RERANK_FAILED": "候选视觉复筛暂时失败，请稍后重试。",
    "ANSWER_LOOKUP_FAILED": "答案文件读取暂时失败，请稍后重试。",
    "CANDIDATE_ACTION_INVALID_STATE": "当前状态无法处理候选操作，请重新上传题图。",
}


def render_tool_feedback(result: ToolResult, *, context: str = "") -> str:
    """Render a non-success tool result without exposing internal fields.

    ``context`` is intentionally narrow: ``partial`` is used for a notice on
    an otherwise usable result; all other contexts are terminal feedback.
    ``safe_facts`` is reserved for reviewed facts and is currently only needed
    to preserve the route-code migration boundary.
    """

    code = str(result.code or "").strip().upper()
    if (
        code == "LOAD_ROUTE_NEEDS_REVIEW"
        and result.safe_facts.get("load_representation") == "unknown"
    ):
        return _TOOL_FEEDBACK_BY_CODE["LOAD_ROUTE_INPUT_UNUSABLE"]
    if code in _TOOL_FEEDBACK_BY_CODE:
        return _TOOL_FEEDBACK_BY_CODE[code]

    outcome = result.outcome
    if outcome is ToolOutcome.ERROR:
        return "这次处理没有完成，题图已保留，你可以直接回复“重试”。"
    if outcome is ToolOutcome.NEEDS_INPUT:
        return "还需要补充题图或章节信息后才能继续。"
    if outcome is ToolOutcome.NO_MATCH:
        return "暂时没有找到足够可靠的结果。"
    if outcome is ToolOutcome.PARTIAL:
        if context == "partial":
            return "部分处理未完成，已保留当前可用结果。"
        return "这次处理没有完成，题图已保留，你可以直接回复“重试”。"
    return ""


def render_failure_explanation(state: AgentState) -> str:
    if state.phase == "NO_MATCH":
        chapter = state.current_chapter or "这一章"
        return f"不是系统出错：我在{chapter}里没有找到足够相似的题。换个章节或发一张更清楚的图试试。"
    if not state.last_error:
        return "这次没有失败记录。你可以直接继续发题，或告诉我想换哪个章节。"
    detail = _safe_failure_detail(state.last_error)
    return f"刚才没查成功，是因为：{detail}。你重新发一下题图，我们再试一次。"


def render_no_match(
    state: AgentState,
    *,
    author_contact_fallback: bool = False,
) -> str:
    if author_contact_fallback:
        return "没有找到相似候选题，你可以联系作者手搓。"
    chapter = state.current_chapter or "这一章"
    return f"我在{chapter}里没找到很像的题。换个章节试试？"


def append_notice(text: str, note: str = "") -> str:
    """Append a concise, user-visible degradation notice once."""

    clean_text = str(text or "").strip()
    clean_note = str(note or "").strip()
    if not clean_note:
        return clean_text
    return f"{clean_text}\n\n提示：{clean_note}"


def _safe_failure_detail(error: str) -> str:
    raw = str(error or "").strip()
    lower = raw.lower()
    if "timeout" in lower or "timed out" in lower:
        return "题图识别服务响应超时"
    if "dashscope_api_key" in lower or "api key" in lower or "unauthorized" in lower:
        return "题图识别服务暂时不可用"
    if "file not found" in lower or "no such file" in lower:
        return "题图文件没有读取成功"
    if "invalid image" in lower or "cannot identify image" in lower:
        return "这张图片无法正常读取"
    cleaned = re.sub(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*)\S+", r"\1[已隐藏]", raw)
    cleaned = re.sub(r"[A-Za-z]:\\[^\s]+", "本地文件", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:120] or "处理过程中断了"

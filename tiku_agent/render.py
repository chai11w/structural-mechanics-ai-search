"""Text rendering helpers for the isolated question-bank Agent."""

from __future__ import annotations

from tiku_agent.state import AgentState


def render_chapter_prompt(state: AgentState) -> str:
    loads = _loads_summary(state.current_loads)
    suffix = f"\n已识别荷载：{loads}" if loads else ""
    return "这题章节还不确定，请告诉我是第几章或用什么方法，例如：3静定结构位移、4力法、5位移法。" + suffix


def render_multi_question_list(state: AgentState) -> str:
    if not state.questions:
        return "没有识别到可选择的题目。"
    lines = ["识别到多道题，请告诉我要搜哪一题；可同时指定章节，例如“帮我搜第二题，章节静定结构”。"]
    for index, question in enumerate(state.questions, 1):
        label = question.get("label") or index
        chapter = question.get("chapter") or "章节未知"
        loads = _loads_summary(question.get("loads") or [])
        lines.append(f"{index}. 第{label}题：{chapter}；荷载：{loads or '未识别'}")
    return "\n".join(lines)


def render_candidates(state: AgentState, *, reranked: bool = False, note: str = "") -> str:
    if not state.candidates:
        return "没有找到可用候选。"

    lines = []
    mode = "已复筛" if reranked else "未复筛"
    chapter = state.current_chapter or "未确定章节"
    lines.append(f"检索完成：{chapter}（{mode}）")
    if note:
        lines.append(note)
    for item in state.candidates:
        score = item.get("final_score")
        if score is None:
            score = item.get("score", 0)
        lines.append(f"{item['rank']}. {item.get('path', '')}    相似度: {round(float(score or 0) * 100)}%")
    lines.append("回复候选编号即可取答案；也可以说“换第三章”“按力法重新搜”。")
    return "\n".join(lines)


def render_answer(state: AgentState) -> str:
    if not state.last_answer_paths:
        return "没有找到可发送的答案文件。"
    lines = ["已找到答案："]
    lines.extend(state.last_answer_paths)
    lines.append("如果不是这题，可以继续说“给我第二个”或“换第三章重新搜”。")
    return "\n".join(lines)


def render_resend_answer(state: AgentState) -> str:
    if not state.last_answer_paths:
        return "我这里还没有上一题答案记录，请先选一个候选。"
    return "刚才的答案是：\n" + "\n".join(state.last_answer_paths)


def render_cancelled() -> str:
    return "已取消当前检索。"


def render_unsupported(message: str = "") -> str:
    if message:
        return f"当前不支持或无法理解这条指令：{message}"
    return "这条指令当前还不支持。你可以发题图、补章节、选候选编号，或说“刚才答案再发我”。"


def render_error(error: str) -> str:
    return f"处理失败：{error}"


def render_no_match(state: AgentState) -> str:
    chapter = state.current_chapter or "当前章节"
    return f"{chapter} 没有找到可用候选。你可以换章节重新搜，或重新发一张更清晰的题图。"


def _loads_summary(loads: list[dict]) -> str:
    parts = []
    for item in loads:
        typ = str(item.get("type") or "").strip()
        raw = str(item.get("raw") or "").strip()
        if typ or raw:
            parts.append(f"{typ}:{raw}".strip(":"))
    return "，".join(parts)

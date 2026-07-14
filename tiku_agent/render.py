"""Text rendering helpers for the isolated question-bank Agent."""

from __future__ import annotations

import re

from tiku_agent.state import AgentState


def render_greeting() -> str:
    return (
        "你好，我是力答，一个结构力学题库搜题助手。"
        "我可以识别你发来的题图、判断题目章节、从题库寻找相似题，并返回对应答案。"
        "发一张结构力学题图给我看看吧。"
    )


def render_chapter_prompt(state: AgentState) -> str:
    if state.global_search_offered:
        return "我还不能确定这题属于哪一章。你知道的话直接告诉我；也可以让我全局搜索，不过会慢一点。"
    return "我还不能确定这题属于哪一章。你知道的话告诉我就行。"


def render_multi_question_list(state: AgentState) -> str:
    if not state.questions:
        return "没有识别到可选择的题目。"
    return f"我在这张图里看到了 {len(state.questions)} 道题。你想查哪一道？"


def render_candidates(state: AgentState, *, reranked: bool = False, note: str = "") -> str:
    if not state.candidates:
        return "没有找到可用候选。"

    if len(state.candidates) == 1:
        return "我从题库里找到了最相似的一道题。你看看是不是这道。"
    return f"我从题库里找到了 {len(state.candidates)} 道比较像的题，你看看有没有想要的。"


def render_global_candidates(state: AgentState) -> str:
    if not state.candidates:
        return render_global_no_match()
    chapters = []
    for item in state.candidates:
        for chapter in item.get("source_chapters") or [item.get("chapter")]:
            if chapter and chapter not in chapters:
                chapters.append(str(chapter))
    sources = "、".join(f"「{chapter}」" for chapter in chapters)
    if len(state.candidates) == 1:
        return f"我从全题库找到了一道高相似题，来自{sources}。你看是不是这道。"
    return f"我从全题库找到 {len(state.candidates)} 道高相似题，分别来自{sources}。你看有没有想要的。"


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


def render_failure_explanation(state: AgentState) -> str:
    if state.phase == "NO_MATCH":
        chapter = state.current_chapter or "这一章"
        return f"不是系统出错：我在{chapter}里没有找到足够相似的题。换个章节或发一张更清楚的图试试。"
    if not state.last_error:
        return "这次没有失败记录。你可以直接继续发题，或告诉我想换哪个章节。"
    detail = _safe_failure_detail(state.last_error)
    return f"刚才没查成功，是因为：{detail}。你重新发一下题图，我们再试一次。"


def render_no_match(state: AgentState) -> str:
    chapter = state.current_chapter or "这一章"
    return f"我在{chapter}里没找到很像的题。换个章节试试？"


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

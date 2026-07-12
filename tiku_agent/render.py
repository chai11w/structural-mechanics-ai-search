"""Text rendering helpers for the isolated question-bank Agent."""

from __future__ import annotations

from tiku_agent.state import AgentState


def render_chapter_prompt(state: AgentState) -> str:
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
    del error
    return "这次没查成功。重新发一下题图，我们再试一次。"


def render_no_match(state: AgentState) -> str:
    chapter = state.current_chapter or "这一章"
    return f"我在{chapter}里没找到很像的题。换个章节试试？"

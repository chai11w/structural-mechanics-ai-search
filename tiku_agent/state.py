"""Dialogue state for the isolated question-bank Agent.

This layer stores the context a user can refer to in later turns. It does not
parse intent, call tools, search the bank, or touch the existing Feishu runtime.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid4

from tiku_agent.intent import (
    CHAPTERS,
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
)


PHASE_PROCESSING = "PROCESSING"
PHASE_READY_TO_ROUTE = "READY_TO_ROUTE"
PHASE_READY_FOR_SEARCH = "READY_FOR_SEARCH"
PHASE_ANSWERED = "ANSWERED"
PHASE_CANCELLED = "CANCELLED"
PHASE_ERROR = "ERROR"
PHASE_NO_MATCH = "NO_MATCH"

# Backward-compatible names used by existing tests/callers.
STATE_READY_TO_ROUTE = PHASE_READY_TO_ROUTE
STATE_READY_FOR_SEARCH = PHASE_READY_FOR_SEARCH
STATE_DONE = PHASE_ANSWERED
STATE_CANCELLED = PHASE_CANCELLED
STATE_ERROR = PHASE_ERROR
STATE_NO_MATCH = PHASE_NO_MATCH

ACTIVE_PHASES = {
    STATE_IDLE,
    PHASE_PROCESSING,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    STATE_WAIT_CANDIDATE_CHOICE,
    PHASE_READY_TO_ROUTE,
    PHASE_READY_FOR_SEARCH,
    PHASE_ANSWERED,
}
TERMINAL_PHASES = {PHASE_CANCELLED, PHASE_ERROR, PHASE_NO_MATCH}
KNOWN_PHASES = ACTIVE_PHASES | TERMINAL_PHASES


@dataclass
class AgentState:
    """Agent dialogue state for search, correction, multi-question, and answer recall."""

    session_id: str = field(default_factory=lambda: uuid4().hex)
    phase: str = STATE_IDLE

    current_image_path: str = ""
    current_question_image_path: str = ""
    current_loads: list[dict] = field(default_factory=list)
    current_chapter: str = ""
    current_route: str = ""
    current_structure_type: str = ""

    questions: list[dict] = field(default_factory=list)
    selected_question: int | None = None
    previous_question: int | None = None
    completed_questions: list[int] = field(default_factory=list)

    candidates: list[dict] = field(default_factory=list)
    selected_rank: int | None = None
    last_answer_paths: list[str] = field(default_factory=list)

    last_intent: dict = field(default_factory=dict)
    last_error: str = ""
    revision_count: int = 0
    pending_chapter: str = ""

    def __init__(
        self,
        session_id: str | None = None,
        phase: str | None = None,
        current_image_path: str = "",
        current_question_image_path: str = "",
        current_loads: list[dict] | None = None,
        current_chapter: str = "",
        current_route: str = "",
        current_structure_type: str = "",
        questions: list[dict] | None = None,
        selected_question: int | None = None,
        previous_question: int | None = None,
        completed_questions: list[int] | None = None,
        candidates: list[dict] | None = None,
        selected_rank: int | None = None,
        last_answer_paths: list[str] | None = None,
        last_intent: dict | None = None,
        last_error: str = "",
        revision_count: int = 0,
        pending_chapter: str = "",
        *,
        state: str | None = None,
        image_path: str = "",
        chapter: str = "",
        loads: list[dict] | None = None,
        route: str = "",
        structure_type: str = "",
    ) -> None:
        self.session_id = session_id or uuid4().hex
        self.phase = phase or state or STATE_IDLE
        self.current_image_path = current_image_path or image_path
        self.current_question_image_path = current_question_image_path
        self.current_loads = list(current_loads if current_loads is not None else (loads or []))
        self.current_chapter = current_chapter or chapter
        self.current_route = current_route or route
        self.current_structure_type = current_structure_type or structure_type
        self.questions = list(questions or [])
        self.selected_question = selected_question
        self.previous_question = previous_question
        self.completed_questions = sorted(set(int(index) for index in (completed_questions or [])))
        self.candidates = list(candidates or [])
        self.selected_rank = selected_rank
        self.last_answer_paths = [str(path) for path in (last_answer_paths or [])]
        self.last_intent = dict(last_intent or {})
        self.last_error = last_error
        self.revision_count = revision_count
        self.pending_chapter = pending_chapter
        self.validate()

    @property
    def state(self) -> str:
        return self.phase

    @state.setter
    def state(self, value: str) -> None:
        self.phase = value

    @property
    def image_path(self) -> str:
        return self.current_image_path

    @image_path.setter
    def image_path(self, value: str) -> None:
        self.current_image_path = value

    @property
    def chapter(self) -> str:
        return self.current_chapter

    @chapter.setter
    def chapter(self, value: str) -> None:
        self.current_chapter = value

    @property
    def loads(self) -> list[dict]:
        return self.current_loads

    @loads.setter
    def loads(self, value: list[dict]) -> None:
        self.current_loads = list(value)

    @property
    def route(self) -> str:
        return self.current_route

    @route.setter
    def route(self, value: str) -> None:
        self.current_route = value

    @property
    def structure_type(self) -> str:
        return self.current_structure_type

    @structure_type.setter
    def structure_type(self, value: str) -> None:
        self.current_structure_type = value

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        return cls(**data)

    def validate(self) -> None:
        if self.phase not in KNOWN_PHASES:
            raise ValueError(f"Unknown Agent phase: {self.phase}")
        if self.selected_rank is not None and self.selected_rank < 1:
            raise ValueError("selected_rank must be positive")
        if self.selected_question is not None and self.selected_question < 1:
            raise ValueError("selected_question must be positive")
        if self.previous_question is not None and self.previous_question < 1:
            raise ValueError("previous_question must be positive")
        if any(index < 1 for index in self.completed_questions):
            raise ValueError("completed_questions must contain positive indexes")
        if self.revision_count < 0:
            raise ValueError("revision_count must not be negative")
        if self.pending_chapter and self.pending_chapter not in CHAPTERS:
            raise ValueError("pending_chapter must be a supported chapter")

    def remember_intent(self, intent: dict) -> None:
        self.last_intent = dict(intent)

    def start_search(self, image_path: str) -> None:
        self.current_image_path = str(image_path)
        self.current_question_image_path = ""
        self.current_loads = []
        self.current_chapter = ""
        self.current_route = ""
        self.current_structure_type = ""
        self.questions = []
        self.selected_question = None
        self.previous_question = None
        self.completed_questions = []
        self.candidates = []
        self.selected_rank = None
        self.last_answer_paths = []
        self.last_error = ""
        self.pending_chapter = ""
        self.phase = PHASE_PROCESSING

    def set_analysis(self, *, loads: list[dict], chapter: str = "", question_image_path: str = "") -> None:
        self.current_loads = list(loads)
        self.current_chapter = chapter
        if question_image_path:
            self.current_question_image_path = str(question_image_path)
        self.phase = PHASE_READY_TO_ROUTE if chapter else STATE_WAIT_CHAPTER

    def set_chapter(self, chapter: str, *, corrected: bool = False) -> None:
        if corrected and self.current_chapter and self.current_chapter != chapter:
            self.revision_count += 1
            self.candidates = []
            self.selected_rank = None
            self.last_answer_paths = []
        self.current_chapter = chapter
        self.phase = PHASE_READY_TO_ROUTE

    def correct_chapter(self, chapter: str) -> None:
        self.set_chapter(chapter, corrected=True)

    def set_route(self, route: str, *, structure_type: str = "") -> None:
        self.current_route = route
        self.current_structure_type = structure_type
        self.phase = PHASE_READY_FOR_SEARCH

    def set_questions(self, questions: list[dict]) -> None:
        self.questions = list(questions)
        self.selected_question = None
        self.previous_question = None
        self.completed_questions = []
        self.phase = STATE_WAIT_QUESTION_CHOICE if questions else PHASE_NO_MATCH

    def select_question(self, index: int, *, chapter_override: str | None = None) -> dict:
        if not 1 <= index <= len(self.questions):
            raise ValueError(f"Question index out of range: {index}")
        if self.selected_question is not None and self.selected_question != index:
            self.previous_question = self.selected_question
        self.selected_question = index
        question = dict(self.questions[index - 1])
        question_image = str(question.get("question_image_path") or question.get("image_path") or "")
        if question_image:
            self.current_question_image_path = question_image
        elif not self.questions and self.current_image_path:
            self.current_question_image_path = self.current_image_path
        else:
            # A selected multi-question item without a reliable crop must not
            # reuse the full page for visual reranking.
            self.current_question_image_path = ""
        self.current_loads = list(question.get("loads") or self.current_loads)
        self.current_chapter = str(chapter_override or question.get("chapter") or self.current_chapter)
        self.candidates = []
        self.selected_rank = None
        self.last_answer_paths = []
        self.phase = PHASE_READY_TO_ROUTE if self.current_chapter else STATE_WAIT_CHAPTER
        return question

    def set_candidates(self, candidates: list[dict]) -> None:
        self.candidates = _renumber(candidates)
        self.selected_rank = None
        self.phase = STATE_WAIT_CANDIDATE_CHOICE if self.candidates else PHASE_NO_MATCH

    def select_candidate(self, rank: int) -> dict:
        if not 1 <= rank <= len(self.candidates):
            raise ValueError(f"Candidate rank out of range: {rank}")
        self.selected_rank = rank
        return dict(self.candidates[rank - 1])

    def set_answer_paths(self, paths: list[str]) -> None:
        self.last_answer_paths = [str(path) for path in paths]
        if self.selected_question is not None:
            self.completed_questions = sorted(
                set((*self.completed_questions, self.selected_question))
            )
        self.phase = PHASE_ANSWERED

    def set_pending_chapter(self, chapter: str) -> None:
        if chapter not in CHAPTERS:
            raise ValueError("pending chapter must be supported")
        self.pending_chapter = chapter

    def consume_pending_chapter(self) -> str:
        chapter = self.pending_chapter
        self.pending_chapter = ""
        return chapter

    def mark_done(self) -> None:
        self.phase = PHASE_ANSWERED

    def cancel(self) -> None:
        self.pending_chapter = ""
        self.phase = PHASE_CANCELLED

    def fail(self, error: str = "") -> None:
        self.last_error = error
        self.phase = PHASE_ERROR

    @property
    def active_image_path(self) -> str:
        return self.current_question_image_path or self.current_image_path

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


def _renumber(candidates: list[dict]) -> list[dict]:
    renumbered = []
    for rank, item in enumerate(candidates, 1):
        copied = dict(item)
        copied["rank"] = rank
        renumbered.append(copied)
    return renumbered

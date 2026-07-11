"""Minimal session state for the isolated question-bank Agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid4

from tiku_agent.intent import (
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
)


STATE_READY_TO_ROUTE = "READY_TO_ROUTE"
STATE_READY_FOR_SEARCH = "READY_FOR_SEARCH"
STATE_DONE = "DONE"
STATE_CANCELLED = "CANCELLED"
STATE_ERROR = "ERROR"
STATE_NO_MATCH = "NO_MATCH"

ACTIVE_STATES = {
    STATE_IDLE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_READY_TO_ROUTE,
    STATE_READY_FOR_SEARCH,
}
TERMINAL_STATES = {STATE_DONE, STATE_CANCELLED, STATE_ERROR, STATE_NO_MATCH}
KNOWN_STATES = ACTIVE_STATES | TERMINAL_STATES


@dataclass
class AgentState:
    """The 11 fields the first Agent needs to continue a retrieval dialogue."""

    session_id: str = field(default_factory=lambda: uuid4().hex)
    state: str = STATE_IDLE
    image_path: str = ""
    chapter: str = ""
    loads: list[dict] = field(default_factory=list)
    route: str = ""
    structure_type: str = ""
    candidates: list[dict] = field(default_factory=list)
    selected_rank: int | None = None
    questions: list[dict] = field(default_factory=list)
    selected_question: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        state = cls(**data)
        state.validate()
        return state

    def validate(self) -> None:
        if self.state not in KNOWN_STATES:
            raise ValueError(f"Unknown Agent state: {self.state}")
        if self.selected_rank is not None and self.selected_rank < 1:
            raise ValueError("selected_rank must be positive")
        if self.selected_question is not None and self.selected_question < 1:
            raise ValueError("selected_question must be positive")

    def start_search(self, image_path: str) -> None:
        self.image_path = str(image_path)
        self.chapter = ""
        self.loads = []
        self.route = ""
        self.structure_type = ""
        self.candidates = []
        self.selected_rank = None
        self.questions = []
        self.selected_question = None
        self.state = STATE_IDLE

    def set_analysis(self, *, loads: list[dict], chapter: str = "") -> None:
        self.loads = list(loads)
        self.chapter = chapter
        self.state = STATE_READY_TO_ROUTE if chapter else STATE_WAIT_CHAPTER

    def set_chapter(self, chapter: str) -> None:
        self.chapter = chapter
        self.state = STATE_READY_TO_ROUTE

    def set_route(self, route: str, *, structure_type: str = "") -> None:
        self.route = route
        self.structure_type = structure_type
        self.state = STATE_READY_FOR_SEARCH

    def set_questions(self, questions: list[dict]) -> None:
        self.questions = list(questions)
        self.selected_question = None
        self.state = STATE_WAIT_QUESTION_CHOICE if questions else STATE_NO_MATCH

    def select_question(self, index: int) -> dict:
        if not 1 <= index <= len(self.questions):
            raise ValueError(f"Question index out of range: {index}")
        self.selected_question = index
        question = dict(self.questions[index - 1])
        self.image_path = str(question.get("image_path") or self.image_path)
        self.loads = list(question.get("loads") or self.loads)
        self.chapter = str(question.get("chapter") or self.chapter)
        self.state = STATE_READY_TO_ROUTE if self.chapter else STATE_WAIT_CHAPTER
        return question

    def set_candidates(self, candidates: list[dict]) -> None:
        self.candidates = _renumber(candidates)
        self.selected_rank = None
        self.state = STATE_WAIT_CANDIDATE_CHOICE if self.candidates else STATE_NO_MATCH

    def select_candidate(self, rank: int) -> dict:
        if not 1 <= rank <= len(self.candidates):
            raise ValueError(f"Candidate rank out of range: {rank}")
        self.selected_rank = rank
        return dict(self.candidates[rank - 1])

    def mark_done(self) -> None:
        self.state = STATE_DONE

    def cancel(self) -> None:
        self.state = STATE_CANCELLED

    def fail(self) -> None:
        self.state = STATE_ERROR

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def _renumber(candidates: list[dict]) -> list[dict]:
    renumbered = []
    for rank, item in enumerate(candidates, 1):
        copied = dict(item)
        copied["rank"] = rank
        renumbered.append(copied)
    return renumbered

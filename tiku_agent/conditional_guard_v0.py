"""Pure-code guard for conditional business authorization.

Conditional wording is not automatically current authorization.  This module
only recognizes a small set of conditions that current Agent state can prove;
everything else remains unresolved and must not execute a business action.
It does not call a model, execute tools, or mutate state.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


CONDITION_NOT_PRESENT = "not_present"
CONDITION_SATISFIED = "satisfied"
CONDITION_UNSATISFIED = "unsatisfied"
CONDITION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConditionalAssessment:
    is_conditional: bool
    outcome: str
    code: str
    matched_text: str = ""
    consequent_text: str = ""
    clarification_reason: str = "ambiguous_action"

    @property
    def blocks_execution(self) -> bool:
        return self.outcome in {CONDITION_UNSATISFIED, CONDITION_UNKNOWN}


def assess_conditional_request(
    user_text: str,
    *,
    phase: str,
    retryable_error: bool = False,
    continuation_available: bool = False,
) -> ConditionalAssessment:
    """Classify whether a stated condition is code-verifiable right now."""

    text = _normalize(user_text)
    if not _looks_conditional(text):
        return ConditionalAssessment(False, CONDITION_NOT_PRESENT, "not_conditional")

    consequent = _extract_consequent(text)
    courtesy = _courtesy_condition(text)
    if courtesy:
        return ConditionalAssessment(
            True,
            CONDITION_SATISFIED,
            "courtesy_hedge",
            courtesy,
            consequent,
        )

    retry = re.search(
        r"(?:如果|若是?|要是|假如|倘若)(?:还)?(?:能|可以)(?:再)?重试",
        text,
    )
    if retry:
        return _known_condition(
            bool(retryable_error),
            "retryable_error",
            retry.group(0),
            consequent,
        )

    continuation = re.search(
        r"(?:如果|若是?|要是|假如|倘若)(?:还)?有(?:更多|别的|其他|下一批)?候选",
        text,
    )
    if continuation:
        return _known_condition(
            bool(continuation_available),
            "continuation_available",
            continuation.group(0),
            consequent,
            clarification_reason="no_more_candidates",
        )

    no_match = re.search(
        r"(?:如果|若是?|要是|假如|倘若)(?:确实|真的)?(?:没有|没)(?:找到|查到|搜到|匹配)",
        text,
    )
    if no_match:
        return _known_condition(
            phase == "NO_MATCH",
            "no_match_state",
            no_match.group(0),
            consequent,
        )

    marker = re.search(
        r"(?:如果|若是?|要是|假如|万一|倘若|否则|不然|前提是|只要|的话)",
        text,
    )
    return ConditionalAssessment(
        True,
        CONDITION_UNKNOWN,
        "condition_unresolved",
        marker.group(0) if marker else "",
        consequent,
    )


def _known_condition(
    satisfied: bool,
    code: str,
    matched_text: str,
    consequent_text: str,
    *,
    clarification_reason: str = "ambiguous_action",
) -> ConditionalAssessment:
    return ConditionalAssessment(
        True,
        CONDITION_SATISFIED if satisfied else CONDITION_UNSATISFIED,
        f"{code}_{'satisfied' if satisfied else 'not_met'}",
        matched_text,
        consequent_text,
        clarification_reason,
    )


def _looks_conditional(text: str) -> bool:
    if re.search(r"^(?:如果|若是?|要是|假如|万一|倘若|前提是|只要)", text):
        return True
    if re.search(r"(?:否则|不然)", text):
        return True
    return bool(re.search(r"^.{1,24}的话(?:[，,]|就|再|继续|把|选|搜|重|回)", text))


def _courtesy_condition(text: str) -> str:
    patterns = (
        r"^(?:如果|若是?|要是)?(?:你)?(?:方便|愿意|有空)(?:的话|[，,])",
        r"^(?:如果|若是?|要是)?可以(?:的话|[，,])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return ""


def _extract_consequent(text: str) -> str:
    branch = re.split(r"(?:否则|不然)", text, maxsplit=1)[0]
    if "，" in branch or "," in branch:
        parts = re.split(r"[，,]", branch, maxsplit=1)
        consequent = parts[1] if len(parts) == 2 else ""
    elif "的话" in branch:
        consequent = branch.split("的话", 1)[1]
    else:
        marker = re.match(r"^(?:如果|若是?|要是|假如|万一|倘若|前提是|只要)", branch)
        start = marker.end() if marker else 0
        position = branch.find("就", start)
        consequent = branch[position + 1:] if position >= 0 else ""
    consequent = consequent.strip(" ，,。；;：:！!")
    return consequent[1:] if consequent.startswith("就") else consequent


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())

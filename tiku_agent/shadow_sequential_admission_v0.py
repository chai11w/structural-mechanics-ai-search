"""Strict, offline admission check for the first two sequential request classes.

The classifier only reads text and phase.  It does not call a model, execute a
tool, mutate Agent state, or participate in ``Agent.handle_text``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tiku_agent.conditional_guard_v0 import assess_conditional_request


SHOW_THEN_SELECT = "show_then_select"
REPORT_THEN_SHOW = "report_then_show"


@dataclass(frozen=True)
class SequentialEvidence:
    action: str
    matched_text: str
    start: int
    end: int


@dataclass(frozen=True)
class SequentialAdmissionDecision:
    admitted: bool
    scenario: str | None
    code: str
    evidence: tuple[SequentialEvidence, ...] = ()


_SHOW_PATTERNS = (
    re.compile(r"(?:回到|返回|切回|再看|重看|重新看|再发|重发)[^，,。；;！？!?]{0,6}候选(?:列表|名单|清单|页|结果页)?"),
    re.compile(r"候选(?:列表|名单|清单|页|结果页)?[^，,。；;！？!?]{0,8}(?:再发|重发|发回来|调回来|调出来|打开|展示)"),
)
_SELECT_PATTERNS = (
    re.compile(r"(?:选择|选中|选|打开|查看|看|改选|换看|换成)[^，,。；;！？!?]{0,4}(?:候选(?:第?[一二三四五六七八九十]|[1-9])|第?[一二三四五六七八九十](?:个)?候选|[1-9](?:个)?候选)"),
    re.compile(r"候选(?:第?[一二三四五六七八九十]|[1-9])(?:个)?[^，,。；;！？!?]{0,4}(?:选择|选中|打开|查看)"),
)
_MISMATCH_PATTERNS = (
    re.compile(r"(?:答案|结果|解答).{0,10}(?:不对|不匹配|对不上|错了|有误)"),
    re.compile(r"(?:不对|不匹配|对不上|错了|有误).{0,6}(?:答案|结果|解答)"),
)
_SEQUENCE_CONNECTOR = re.compile(
    r"(?:然后|接着|之后|随后|再|接下来|完成后|处理完|处理后|记录后|记下后|标记后|再来|后再)"
)
_NEGATION = re.compile(r"(?:不要|别|不用|不必|无需|取消|暂时不|先不|不能|并没有|没有)")
_UNCERTAINTY = re.compile(r"(?:是不是|是否|可能|好像|似乎|不确定|[吗么呢？?])")


def classify_sequential_shadow_admission(
    user_text: str,
    *,
    phase: str,
) -> SequentialAdmissionDecision:
    """Return a conservative admission decision for two proven scenarios."""

    text = _normalize(user_text)
    if not text:
        return _reject("empty_text")

    condition = assess_conditional_request(text, phase=phase)
    if condition.is_conditional:
        return _reject("conditional_request")
    if _NEGATION.search(text):
        return _reject("negated_request")
    if _UNCERTAINTY.search(text):
        return _reject("uncertain_request")

    if phase == "WAIT_CANDIDATE_CHOICE":
        return _ordered_decision(
            text,
            scenario=SHOW_THEN_SELECT,
            first_action="show_candidates",
            first_patterns=_SHOW_PATTERNS,
            second_action="select_candidate",
            second_patterns=_SELECT_PATTERNS,
        )

    if phase == "ANSWERED":
        return _ordered_decision(
            text,
            scenario=REPORT_THEN_SHOW,
            first_action="report_answer_mismatch",
            first_patterns=_MISMATCH_PATTERNS,
            second_action="show_candidates",
            second_patterns=_SHOW_PATTERNS,
        )

    return _reject("phase_not_eligible")


def _ordered_decision(
    text: str,
    *,
    scenario: str,
    first_action: str,
    first_patterns: tuple[re.Pattern[str], ...],
    second_action: str,
    second_patterns: tuple[re.Pattern[str], ...],
) -> SequentialAdmissionDecision:
    first_matches = _matches(text, first_action, first_patterns)
    second_matches = _matches(text, second_action, second_patterns)
    if not first_matches or not second_matches:
        return _reject("required_evidence_missing")

    for first in first_matches:
        for second in second_matches:
            if first.end > second.start:
                continue
            between = text[first.end:second.start]
            if not _SEQUENCE_CONNECTOR.search(between):
                continue
            return SequentialAdmissionDecision(
                True,
                scenario,
                "explicit_ordered_actions",
                (first, second),
            )
    return _reject("order_or_connector_not_proven")


def _matches(
    text: str,
    action: str,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[SequentialEvidence, ...]:
    found: list[SequentialEvidence] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            evidence = SequentialEvidence(action, match.group(0), match.start(), match.end())
            if evidence not in found:
                found.append(evidence)
    return tuple(sorted(found, key=lambda item: (item.start, item.end)))


def _reject(code: str) -> SequentialAdmissionDecision:
    return SequentialAdmissionDecision(False, None, code)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())

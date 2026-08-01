"""Code-only semantic authorization for Stage 5 shadow plans.

The planner may infer a useful intent from an elliptical request, but inferred
meaning is not user authorization.  This module checks every proposed action
against the raw user utterance and records whether the required evidence is
explicit.  It never calls a model, executes a tool, or mutates Agent state.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tiku_agent.intent_contract import CHAPTERS, chinese_number_to_int
from tiku_agent.shadow_plan_v0 import (
    REVIEW_ALLOW,
    PermissionReview,
    PermissionReviewFacts,
    ShadowPlan,
    ShadowPlanStep,
    ShadowPlannerResult,
)


REVIEW_NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True)
class ActionAuthorizationEvidence:
    """Raw-text evidence for one proposed plan step."""

    action: str
    authorized: bool
    code: str
    matched_text: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "authorized": self.authorized,
            "code": self.code,
            "matched_text": list(self.matched_text),
        }


@dataclass(frozen=True)
class SemanticAuthorizationResult:
    """Deterministic authorization result plus safe rewrite diagnostics."""

    review: PermissionReview
    evidence: tuple[ActionAuthorizationEvidence, ...]
    explicit_keywords: tuple[str, ...]
    inferred_keywords: tuple[str, ...]
    confidence: float
    requires_confirmation: bool


def review_shadow_plan_semantics(
    user_text: str,
    result: ShadowPlannerResult,
    facts: PermissionReviewFacts,
) -> SemanticAuthorizationResult:
    """Require explicit raw-text evidence for every proposed action.

    ``result.keywords`` are useful for understanding, but a keyword introduced
    only by the rewrite is classified as inferred and never grants permission.
    The state facts may narrow an already-explicit reference (for example a
    one-candidate confirmation), but state alone never invents consent.
    """
    raw_text = str(user_text or "").strip()
    plan = result.plan
    explicit_keywords, inferred_keywords = _split_keywords(raw_text, result.keywords)

    if not plan.steps:
        return SemanticAuthorizationResult(
            review=PermissionReview(
                outcome=REVIEW_ALLOW,
                code="unplannable",
                reason="规划器没有提出需要授权的动作。",
                plan=plan,
            ),
            evidence=(),
            explicit_keywords=explicit_keywords,
            inferred_keywords=inferred_keywords,
            confidence=0.0,
            requires_confirmation=False,
        )

    evidence = tuple(_authorize_step(raw_text, step, facts) for step in plan.steps)
    missing = tuple(item for item in evidence if not item.authorized)
    if missing:
        actions = "、".join(dict.fromkeys(item.action for item in missing))
        return SemanticAuthorizationResult(
            review=PermissionReview(
                outcome=REVIEW_NEEDS_CONFIRMATION,
                code="needs_confirmation",
                reason=f"用户原话没有明确授权：{actions}。",
                violations=tuple(item.code for item in missing),
                plan=plan,
            ),
            evidence=evidence,
            explicit_keywords=explicit_keywords,
            inferred_keywords=inferred_keywords,
            confidence=0.0,
            requires_confirmation=True,
        )

    return SemanticAuthorizationResult(
        review=PermissionReview(
            outcome=REVIEW_ALLOW,
            code="semantic_allow",
            reason="计划中的每个动作都能在用户原话中找到明确授权证据。",
            plan=plan,
        ),
        evidence=evidence,
        explicit_keywords=explicit_keywords,
        inferred_keywords=inferred_keywords,
        confidence=1.0,
        requires_confirmation=False,
    )


def _authorize_step(
    user_text: str,
    step: ShadowPlanStep,
    facts: PermissionReviewFacts,
) -> ActionAuthorizationEvidence:
    action = step.action
    compact = _compact(user_text)

    if action == "select_question":
        return _authorize_index_selection(user_text, step, namespace="question")
    if action == "select_candidate":
        evidence = _authorize_index_selection(user_text, step, namespace="candidate")
        if evidence.authorized:
            return evidence
        if facts.candidate_count == 1 and compact in {
            "是", "是的", "对", "对的", "没错", "确认", "确定", "可以", "行", "好", "好的",
            "就这个", "就它", "这个", "选这个", "就这道", "选这道",
        }:
            return _allowed(action, "unique_candidate_confirmation", compact)
        return evidence
    if action == "set_chapter":
        chapter = str(step.params.get("chapter_override") or "")
        matched = _chapter_evidence(compact, chapter)
        return _evidence(action, "explicit_chapter", "chapter_not_explicit", matched)
    if action == "global_search":
        matched = _matches(
            compact,
            r"全局(?:搜索|搜|找|查|检索)?",
            r"全题库(?:搜索|搜|找|查|检索)?",
            r"(?:所有|全部|每个)章节(?:都)?(?:搜索|搜|找|查|检索)?",
            r"跨章节(?:搜索|搜|找|查|检索)?",
        )
        if not matched and facts.global_search_offered and compact in {
            "是", "是的", "对", "对的", "确认", "确定", "可以", "行", "好", "好的", "同意",
        }:
            matched = (compact,)
        return _evidence(action, "explicit_global_search", "global_search_not_explicit", matched)
    if action == "continue_search":
        matched = _matches(
            compact,
            r"(?:继续|重新|再|接着)(?:搜|找|检索)",
            r"(?:换|再来)(?:一|下)?批",
            r"下一批",
            r"更多候选",
        )
        return _evidence(action, "explicit_continue_search", "continue_search_not_explicit", matched)
    if action == "reject_candidates":
        matched = _matches(
            compact,
            r"(?:这些|这几个|这一批|候选|都|一个也?).{0,6}(?:不对|不是|不匹配|不合适|没想要)",
            r"(?:都不是|都不对|没一个对|一个都不是|没有想要的)",
        )
        return _evidence(action, "explicit_candidate_rejection", "candidate_rejection_not_explicit", matched)
    if action == "show_candidates":
        matched = _matches(
            compact,
            r"(?:回到|返回|再看|重看|重新看|再发).{0,5}候选",
            r"候选.{0,5}(?:列表|再发|重发)",
        )
        return _evidence(action, "explicit_show_candidates", "show_candidates_not_explicit", matched)
    if action == "resend_answer":
        matched = _matches(
            compact,
            r"(?:答案|结果|解答).{0,6}(?:再发|重发|再给|重看|再看)",
            r"(?:再发|重发|再给|重看|再看).{0,6}(?:答案|结果|解答)",
        )
        return _evidence(action, "explicit_resend_answer", "resend_answer_not_explicit", matched)
    if action == "report_answer_mismatch":
        uncertain = bool(
            re.search(
                r"(?:是不是|是否|会不会|可能|好像|感觉|吗|么|[?？]|不是不对|并非不对|不一定不对)",
                compact,
            )
        )
        matched = () if uncertain else _matches(
            compact,
            r"(?:答案|结果|解答).{0,6}(?:不对|不匹配|对不上|错了|有误)",
            r"(?:这题|候选).{0,5}(?:不是|不对).{0,5}(?:答案|结果)?",
        )
        return _evidence(action, "explicit_answer_mismatch", "answer_mismatch_not_explicit", matched)
    if action == "retry_search":
        matched = _matches(
            compact,
            r"重试",
            r"再试(?:一次|试看|一下)?",
            r"重新(?:试|检索|搜索)",
            r"再搜一次",
        )
        return _evidence(action, "explicit_retry", "retry_not_explicit", matched)
    if action == "explain_failure":
        matched = _matches(
            compact,
            r"(?:为什么|为啥).{0,6}(?:失败|没找到|没查到|没搜到|没有匹配)",
            r"(?:失败|没找到|没查到|没搜到|没有匹配).{0,6}(?:原因|怎么回事)",
        )
        return _evidence(action, "explicit_failure_question", "failure_question_not_explicit", matched)

    return ActionAuthorizationEvidence(
        action=action,
        authorized=False,
        code="unsupported_semantic_action",
    )


def _authorize_index_selection(
    user_text: str,
    step: ShadowPlanStep,
    *,
    namespace: str,
) -> ActionAuthorizationEvidence:
    action = step.action
    param = "question_index" if namespace == "question" else "candidate_rank"
    try:
        expected = int(step.params[param])
    except (KeyError, TypeError, ValueError):
        return ActionAuthorizationEvidence(action, False, f"{namespace}_index_missing")

    patterns = (
        (
            r"第?\s*([0-9一二两三四五六七八九十]+)\s*(?:小\s*)?[题問问]",
            r"第\s*([0-9一二两三四五六七八九十]+)\s*道\s*[题問问]?",
        )
        if namespace == "question"
        else (
            r"第?\s*([0-9一二两三四五六七八九十]+)\s*个?\s*候选",
            r"候选\s*第?\s*([0-9一二两三四五六七八九十]+)",
            r"第?\s*([0-9一二两三四五六七八九十]+)\s*个\s*答案",
        )
    )
    mentions: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, user_text):
            if chinese_number_to_int(match.group(1)) == expected:
                mentions.append(match.group(0))
    if not mentions:
        return ActionAuthorizationEvidence(action, False, f"{namespace}_index_not_explicit")

    compact = _compact(user_text)
    if any(_match_is_negated(compact, _compact(item)) for item in mentions):
        return ActionAuthorizationEvidence(
            action,
            False,
            f"{namespace}_selection_negated",
            tuple(dict.fromkeys(mentions)),
        )
    explicit_command = any(
        re.search(
            rf"(?:选|选择|打开|查看|就|要|看).{{0,4}}{re.escape(_compact(item))}",
            compact,
        )
        for item in mentions
    )
    target_only = any(_compact(item) == compact for item in mentions)
    if not explicit_command and not target_only:
        return ActionAuthorizationEvidence(
            action,
            False,
            f"{namespace}_selection_not_explicit",
            tuple(dict.fromkeys(mentions)),
        )
    return _allowed(action, f"explicit_{namespace}_selection", *mentions)


def _chapter_evidence(compact: str, chapter: str) -> tuple[str, ...]:
    if chapter not in CHAPTERS:
        return ()
    aliases_by_chapter = {
        "2静定结构": ("2静定结构", "第二章", "静定结构"),
        "3静定结构位移": ("3静定结构位移", "第三章", "静定结构位移"),
        "4力法": ("4力法", "第四章", "力法"),
        "5位移法": ("5位移法", "第五章", "位移法"),
        "6力矩分配": ("6力矩分配", "第六章", "力矩分配"),
        "7矩阵位移": ("7矩阵位移", "第七章", "矩阵位移"),
        "8影响线": ("8影响线", "第八章", "影响线"),
    }
    aliases = aliases_by_chapter[chapter]
    present = tuple(alias for alias in aliases if alias in compact)
    if not present:
        return ()
    if all(_match_is_negated(compact, alias) for alias in present):
        return ()

    # Do not authorize the wrong chapter from a substring.  In particular,
    # “静定结构位移” contains “静定结构” but is chapter 3, not chapter 2.
    conflicting = tuple(
        alias
        for other, other_aliases in aliases_by_chapter.items()
        if other != chapter
        for alias in other_aliases
        if alias in compact and not any(alias in expected for expected in present)
    )
    if any(expected in conflict for expected in present for conflict in conflicting):
        return ()
    targeted = any(
        re.search(rf"(?:按|用|换到|改成|切换到|选|选择).{{0,3}}{re.escape(alias)}", compact)
        for alias in present
    )
    if conflicting and not targeted:
        return ()
    if compact in aliases or targeted or re.search(r"(?:搜|找|查|检索).{0,3}$", compact):
        return present
    return ()


def _split_keywords(raw_text: str, keywords: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    compact = _compact(raw_text)
    explicit: list[str] = []
    inferred: list[str] = []
    for keyword in keywords:
        cleaned = str(keyword).strip()
        if not cleaned:
            continue
        target = explicit if _compact(cleaned) in compact else inferred
        if cleaned not in target:
            target.append(cleaned)
    return tuple(explicit), tuple(inferred)


def _evidence(
    action: str,
    allow_code: str,
    deny_code: str,
    matched: tuple[str, ...],
) -> ActionAuthorizationEvidence:
    if matched:
        return ActionAuthorizationEvidence(action, True, allow_code, matched)
    return ActionAuthorizationEvidence(action, False, deny_code)


def _allowed(action: str, code: str, *matched: str) -> ActionAuthorizationEvidence:
    return ActionAuthorizationEvidence(
        action=action,
        authorized=True,
        code=code,
        matched_text=tuple(dict.fromkeys(item for item in matched if item)),
    )


def _matches(compact: str, *patterns: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in patterns:
        found.extend(
            match.group(0)
            for match in re.finditer(pattern, compact)
            if not _match_is_negated(compact, match.group(0), start=match.start())
        )
    return tuple(dict.fromkeys(found))


def _match_is_negated(compact: str, matched: str, *, start: int | None = None) -> bool:
    if start is None:
        start = compact.find(matched)
    if start < 0:
        return False
    prefix = compact[max(0, start - 10):start]
    return bool(
        re.search(
            r"(?:不要|别|不用|不必|无需|不想|不能|先不|暂时不|取消|算了|不是|并非).{0,6}$",
            prefix,
        )
    )


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;：:~～]+", "", str(text or "").lower())

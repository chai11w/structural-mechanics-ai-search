"""Run a real-Qwen matrix evaluation of the Stage 5 shadow planner.

Each (phase, long-tail utterance) pair builds a legal ``AgentState``, derives the
sanitized conversation payload, asks the real Qwen planner for one shadow plan,
runs the code permission review, and records the result.  This answers three
questions the injected-stub tests cannot:
  1. structural validity — can real Qwen output parse into a ``ShadowPlan``?
  2. action reasonableness — is the reviewed plan allowed and non-empty?
  3. false rejection — how often is a legitimate plan rejected by the review?

Output goes to an ignored directory (``.tmp_shadow_plan_eval_8794``); no model
output or raw plan is committed.  Requires ``DASHSCOPE_API_KEY`` from the
environment, never from local config.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from tiku_agent.conversation_context_v2 import ConversationContextV2
from tiku_agent.intent_runtime_v2 import build_runtime_context_v2
from tiku_agent.shadow_plan_v0 import (
    PermissionReview,
    ShadowPlan,
    ShadowPlannerResult,
    build_permission_review_facts,
    review_shadow_plan,
)
from tiku_agent.shadow_planner_v0 import SHADOW_PLAN_PROMPT, ShadowPlannerV0
from tiku_agent.state import (
    PHASE_ANSWERED,
    PHASE_ERROR,
    PHASE_NO_MATCH,
    STATE_IDLE,
    STATE_WAIT_CANDIDATE_CHOICE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    AgentState,
)


DEFAULT_OUTPUT_ROOT = BASE / ".tmp_shadow_plan_eval_8794"

# Phases where the planner may propose a multi-step plan.  ``CANCELLED`` is
# terminal and never reaches the planner; the rest mirror the safe-answer matrix.
MATRIX_PHASES = (
    STATE_IDLE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    STATE_WAIT_CANDIDATE_CHOICE,
    PHASE_ANSWERED,
    PHASE_NO_MATCH,
    PHASE_ERROR,
)

# Real long-tail utterances a user might actually say — ambiguous reference,
# ellipsis, informal wording, goal switching — exactly what the fixed rules do
# not cover.  These are spoken-language samples, not fixture sentences.
MATRIX_UTTERANCES = (
    "这个题你帮我看看",
    "那换一道难一点的吧",
    "别的那个也行",
    "算了不看了",
    "你再帮我找找别的",
    "刚才那个是不是不对啊",
    "换个思路试试",
    "这个看着眼熟你查查",
    "有没有更接近的",
    "后面那几道也看看",
    "它怎么说的来着",
    "帮我看看这题哪一章的",
    "那要是换个条件呢",
    "就这样吧",
    "你说哪个靠谱",
)

_PHASE_LABELS = {
    STATE_IDLE: "IDLE（无任务）",
    STATE_WAIT_CHAPTER: "WAIT_CHAPTER（等章节）",
    STATE_WAIT_QUESTION_CHOICE: "WAIT_QUESTION_CHOICE（选题目）",
    STATE_WAIT_CANDIDATE_CHOICE: "WAIT_CANDIDATE_CHOICE（选候选）",
    PHASE_ANSWERED: "ANSWERED（已答）",
    PHASE_NO_MATCH: "NO_MATCH（无匹配）",
    PHASE_ERROR: "ERROR（出错）",
}


def build_phase_state(phase: str) -> AgentState:
    """Build a legal AgentState making the phase's summary observable."""
    common = dict(
        session_id=f"shadow-matrix-{phase.lower()}",
        current_image_path="question.jpg",
        current_question_image_path="question.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
    )
    if phase == STATE_IDLE:
        return AgentState(session_id="shadow-matrix-idle")
    if phase == STATE_WAIT_CHAPTER:
        common.update(global_search_offered=True)
    elif phase == STATE_WAIT_QUESTION_CHOICE:
        common.update(questions=[{"index": 1}, {"index": 2}], selected_question=1)
    elif phase == STATE_WAIT_CANDIDATE_CHOICE:
        common.update(
            current_chapter="4力法",
            candidates=[{"rank": 1}, {"rank": 2}, {"rank": 3}],
            continuation_available=True,
        )
    elif phase == PHASE_ANSWERED:
        common.update(
            current_chapter="4力法",
            candidates=[{"rank": 1}, {"rank": 2}],
            last_answer_paths=["answer.png"],
        )
    elif phase == PHASE_NO_MATCH:
        common.update(current_chapter="4力法", last_error="no match")
    elif phase == PHASE_ERROR:
        common.update(last_error="tool error")
    return AgentState(phase=phase, **common)


def evaluate_matrix() -> list[dict]:
    """Run every (phase, utterance) pair through the real shadow-plan path."""
    records = []
    for phase in MATRIX_PHASES:
        state = build_phase_state(phase)
        facts = build_permission_review_facts(state)
        context: ConversationContextV2 = build_runtime_context_v2(state)
        for text in MATRIX_UTTERANCES:
            captured_raw = ""

            def recording_client(prompt: str) -> dict:
                nonlocal captured_raw
                captured_raw = _dashscope_raw(prompt)
                return _json_or_raw(captured_raw)

            planner = ShadowPlannerV0(model_client=recording_client)
            result = planner.plan(text, context.to_prompt_payload())
            plan = result.plan if result is not None else None
            review = review_shadow_plan(plan, facts) if plan is not None else None
            records.append(
                {
                    "phase": phase,
                    "utterance": text,
                    "rewritten": _result_to_record(result),
                    "plan_structurally_valid": plan is not None,
                    "plan": _plan_to_record(plan),
                    "review": _review_to_record(review),
                    "outcome": review.outcome if review else "no_plan",
                    "reject_code": review.code if review else ("planner_unavailable" if plan is None else ""),
                    "accepted": review.allowed if review else False,
                    # Raw model text so a parse failure is diagnosable instead
                    # of a silent planner_unavailable black box.
                    "raw_output": captured_raw,
                }
            )
    return records


def summarize(records: list[dict]) -> dict:
    total = len(records)
    valid = sum(bool(r["plan_structurally_valid"]) for r in records)
    accepted = sum(bool(r["accepted"]) for r in records)
    by_phase: dict[str, dict[str, int]] = {}
    for phase in MATRIX_PHASES:
        phase_records = [r for r in records if r["phase"] == phase]
        phase_valid = sum(bool(r["plan_structurally_valid"]) for r in phase_records)
        phase_accepted = sum(bool(r["accepted"]) for r in phase_records)
        by_phase[phase] = {
            "total": len(phase_records),
            "valid": phase_valid,
            "accepted": phase_accepted,
            "valid_rate": round(phase_valid / len(phase_records), 4) if phase_records else 0.0,
            "accepted_rate": round(phase_accepted / len(phase_records), 4) if phase_records else 0.0,
        }
    return {
        "total": total,
        "structurally_valid": valid,
        "structurally_valid_rate": round(valid / total, 4) if total else 0.0,
        "review_accepted": accepted,
        "review_accepted_rate": round(accepted / total, 4) if total else 0.0,
        # Of plans the model produced, how many survived the review — this is the
        # false-rejection signal: a low accepted/valid ratio means the review
        # rejects plans the planner intended as legitimate.
        "accepted_of_valid": (
            round(accepted / valid, 4) if valid else 0.0
        ),
        "reject_codes": dict(
            Counter(
                str(r["reject_code"]) for r in records if r["reject_code"]
            )
        ),
        "outcomes": dict(Counter(str(r["outcome"]) for r in records)),
        "by_phase": by_phase,
    }


def render_matrix_report(records: list[dict]) -> str:
    lines = []
    for phase in MATRIX_PHASES:
        lines.append(f"\n## {_PHASE_LABELS[phase]}  阶段={phase}")
        for record in [r for r in records if r["phase"] == phase]:
            if record["plan_structurally_valid"]:
                plan_desc = _plan_summary(record["plan"])
                marker = f"review={record['outcome']}({record['reject_code']})"
            else:
                plan_desc = ""
                marker = "planner_unavailable"
            lines.append(
                f"  [{marker}] {record['utterance']}\n"
                f"       plan: {plan_desc or '（模型未产出合法计划）'}"
            )
    return "\n".join(lines)


def _result_to_record(result: ShadowPlannerResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "rewritten_text": result.rewritten_text,
        "keywords": list(result.keywords),
        "reason": result.reason,
    }


def _plan_to_record(plan: ShadowPlan | None) -> dict | None:
    if plan is None:
        return None
    return {
        "goal": plan.goal,
        "steps": [
            {
                "action": step.action,
                "params": dict(step.params),
                "reason": step.reason,
            }
            for step in plan.steps
        ],
        "stop_condition": plan.stop_condition,
        "source": plan.source,
    }


def _plan_summary(plan: dict | None) -> str:
    if not plan:
        return ""
    steps = "; ".join(
        f"{step['action']}({','.join(f'{k}={v}' for k, v in step['params'].items())})"
        for step in plan["steps"]
    )
    return f"{plan['goal']} → {steps}"


def _review_to_record(review: PermissionReview | None) -> dict | None:
    if review is None:
        return None
    return {
        "outcome": review.outcome,
        "code": review.code,
        "reason": review.reason,
        "violations": list(review.violations),
    }


def _dashscope_raw(prompt: str) -> str:
    """Call DashScope directly and return the raw model text.

    Unlike ``call_qwen_planner_v0`` (which already parsed to a dict), this keeps
    the raw string so a parse failure inside the planner is diagnosable.
    """
    import json as _json
    import os as _os
    import urllib.request as _urlrequest

    api_key = _os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set")
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SHADOW_PLAN_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 768,
        "enable_thinking": False,
    }
    request = _urlrequest.Request(
        DEFAULT_ENDPOINT,
        data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with _urlrequest.urlopen(request, timeout=60) as response:
        data = _json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return content if isinstance(content, str) else str(content)


def _json_or_raw(text: str) -> dict:
    """Try to parse the model text; return a sentinel dict on failure.

    The sentinel guarantees ``plan()`` degrades to ``None`` (recording
    ``planner_unavailable``) while ``raw_output`` still holds the real text.
    """
    from scripts.classify_question_bank import parse_model_json

    try:
        parsed = parse_model_json(text)
        return parsed if isinstance(parsed, dict) else {"raw": text}
    except Exception:  # noqa: BLE001 - keep the raw text for diagnosis.
        return {"raw": text}


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate shadow planning with real Qwen")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")

    records = evaluate_matrix()
    summary = summarize(records)
    report = render_matrix_report(records)

    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "records.jsonl").write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "matrix_report.txt").write_text(report + "\n", encoding="utf-8")
    print(report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

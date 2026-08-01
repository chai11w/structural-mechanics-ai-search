"""Run isolated pilot or full-suite Qwen safe-answer evaluation."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Callable


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.classify_question_bank import DEFAULT_ENDPOINT, DEFAULT_MODEL
from tiku_agent.safe_answer_context_v0 import (
    SAFE_ACTION_LABELS,
    SafeConversationContext,
    build_safe_answer_context,
)
from tiku_agent.safe_answer_generator_v0 import (
    SafeAnswerGeneratorV0,
    SafeAnswerModelRequestV0,
)
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0
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


FIXTURE = BASE / "tests" / "fixtures" / "safe_answer_v0_cases.json"
DEFAULT_OUTPUT_ROOT = BASE / ".tmp_safe_answer_eval_8794"
PILOT_CASE_IDS = (
    "greeting_hello_idle",
    "greeting_are_you_there_error",
    "courtesy_thanks_idle",
    "courtesy_hard_work_answered",
    "identity_real_review_who_are_you",
    "identity_difference_from_chatbot_idle",
    "capability_what_can_you_do_idle",
    "capability_supported_search_features_idle",
    "workflow_how_do_you_work_wait_chapter",
    "workflow_why_chapter_needed_idle",
)


def load_pilot_cases(fixture: Path = FIXTURE) -> list[dict]:
    suite = json.loads(fixture.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in suite["cases"]}
    return [by_id[case_id] for case_id in PILOT_CASE_IDS]


def load_full_cases(fixture: Path = FIXTURE) -> list[dict]:
    suite = json.loads(fixture.read_text(encoding="utf-8"))
    return [case for case in suite["cases"] if case["expected"]["eligible"]]


def evaluate_cases(
    cases: list[dict],
    *,
    runs: int,
    model_client: Callable[[SafeAnswerModelRequestV0], str],
) -> list[dict]:
    if runs <= 0:
        raise ValueError("runs must be positive")
    records = []
    for case in cases:
        for run_index in range(1, runs + 1):
            captured_output = ""

            def recording_client(request: SafeAnswerModelRequestV0) -> str:
                nonlocal captured_output
                captured_output = model_client(request)
                return captured_output

            result = SafeAnswerGeneratorV0(recording_client).generate(case["text"])
            records.append(
                {
                    "case_id": case["id"],
                    "run": run_index,
                    "category": case["expected"]["category"],
                    "source": result.source,
                    "accepted": result.source == "model",
                    "fallback_reason": result.fallback_reason,
                    "latency_ms": result.latency_ms,
                    "character_count": len(captured_output.strip()),
                    "model_output": captured_output,
                    "final_answer": result.text,
                }
            )
    return records


def summarize(records: list[dict]) -> dict:
    total = len(records)
    accepted = sum(bool(record["accepted"]) for record in records)
    latencies = [int(record["latency_ms"]) for record in records]
    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "accepted": 0}
    )
    for record in records:
        category = str(record["category"])
        by_category[category]["total"] += 1
        by_category[category]["accepted"] += int(bool(record["accepted"]))
    return {
        "total": total,
        "accepted": accepted,
        "acceptance_rate": round(accepted / total, 4) if total else 0.0,
        "fallback_reasons": dict(
            Counter(
                str(record["fallback_reason"])
                for record in records
                if record["fallback_reason"]
            )
        ),
        "latency_ms": {
            "average": round(sum(latencies) / len(latencies)) if latencies else 0,
            "minimum": min(latencies) if latencies else 0,
            "maximum": max(latencies) if latencies else 0,
        },
        "by_category": dict(by_category),
    }


def write_results(output_dir: Path, records: list[dict], summary: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    jsonl = "\n".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    )
    (output_dir / "records.jsonl").write_text(jsonl + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# State-aware matrix evaluation: a spread of chitchat utterances, each run
# against a real per-phase AgentState, so we can see what the model actually
# says when it can perceive the current business phase.
# ---------------------------------------------------------------------------

# Representative phases where state awareness should visibly change the reply.
MATRIX_PHASES = (
    STATE_IDLE,
    STATE_WAIT_CHAPTER,
    STATE_WAIT_QUESTION_CHOICE,
    STATE_WAIT_CANDIDATE_CHOICE,
    PHASE_ANSWERED,
    PHASE_NO_MATCH,
    PHASE_ERROR,
)

# (utterance, expected_category) — all are eligible pure chitchat per the
# fixture, so every cell should reach the model (or fall back deterministically).
MATRIX_UTTERANCES = (
    ("你好", "greeting"),
    ("您好", "greeting"),
    ("在吗", "greeting"),
    ("谢谢", "courtesy"),
    ("辛苦了", "courtesy"),
    ("你是谁？", "identity"),
    ("你是什么助手？", "identity"),
    ("你能做什么？", "capability"),
    ("你是怎么工作的？", "workflow"),
    ("为什么需要我提供章节？", "workflow"),
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
    """Build a legal AgentState that makes the phase's summary observable."""
    common = dict(
        session_id=f"matrix-{phase.lower()}",
        current_image_path="question.jpg",
        current_question_image_path="question.jpg",
        current_loads=[{"type": "集中", "raw": "P"}],
    )
    if phase == STATE_IDLE:
        return AgentState(session_id="matrix-idle")
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


def evaluate_matrix(
    *,
    model_client: Callable[[SafeAnswerModelRequestV0], str],
) -> list[dict]:
    """Run every (phase, utterance) pair through the real state-aware path."""
    records = []
    for phase in MATRIX_PHASES:
        state = build_phase_state(phase)
        context = build_safe_answer_context(state)
        for text, category in MATRIX_UTTERANCES:
            captured_output = ""

            def recording_client(request: SafeAnswerModelRequestV0) -> str:
                nonlocal captured_output
                captured_output = model_client(request)
                return captured_output

            result = SafeAnswerGeneratorV0(recording_client).generate(text, context)
            records.append(
                {
                    "phase": phase,
                    "utterance": text,
                    "category": category,
                    "context": context.to_prompt_payload(),
                    "source": result.source,
                    "accepted": result.source == "model",
                    "fallback_reason": result.fallback_reason,
                    "latency_ms": result.latency_ms,
                    "character_count": len(captured_output.strip()),
                    "model_output": captured_output,
                    "final_answer": result.text,
                }
            )
    return records


def _raw_action_request(
    request: SafeAnswerModelRequestV0,
    context: SafeConversationContext,
) -> SafeAnswerModelRequestV0:
    """Replace only the user-facing action labels in one evaluation request."""
    if not context.allowed_actions:
        return request
    action_by_label = {label: action for action, label in SAFE_ACTION_LABELS.items()}
    label_line = f"- 允许的下一步：{', '.join(context.allowed_actions)}"
    raw_actions = tuple(action_by_label[label] for label in context.allowed_actions)
    raw_line = f"- 允许的下一步：{', '.join(raw_actions)}"
    system_prompt = request.prompt.system_prompt.replace(label_line, raw_line, 1)
    if system_prompt == request.prompt.system_prompt:
        raise ValueError("allowed-action line was not found in the evaluation prompt")
    return replace(
        request,
        prompt=replace(request.prompt, system_prompt=system_prompt),
    )


def evaluate_action_label_comparison(
    *,
    model_client: Callable[[SafeAnswerModelRequestV0], str],
) -> list[dict]:
    """Compare raw executor actions with reviewed Chinese labels, pair by pair."""
    records = []
    pair_index = 0
    for phase in MATRIX_PHASES:
        state = build_phase_state(phase)
        context = build_safe_answer_context(state)
        for utterance, category in MATRIX_UTTERANCES:
            pair_index += 1
            # Alternate call order to reduce a simple time/order bias in one live run.
            variants = ("translated", "raw") if pair_index % 2 else ("raw", "translated")
            for variant in variants:
                captured_output = ""

                def comparison_client(request: SafeAnswerModelRequestV0) -> str:
                    nonlocal captured_output
                    actual_request = (
                        request
                        if variant == "translated"
                        else _raw_action_request(request, context)
                    )
                    captured_output = model_client(actual_request)
                    return captured_output

                result = SafeAnswerGeneratorV0(comparison_client).generate(
                    utterance,
                    context,
                )
                records.append(
                    {
                        "pair": pair_index,
                        "variant": variant,
                        "phase": phase,
                        "utterance": utterance,
                        "category": category,
                        "context": context.to_prompt_payload(),
                        "shown_actions": (
                            list(context.allowed_actions)
                            if variant == "translated"
                            else [
                                action
                                for action, label in SAFE_ACTION_LABELS.items()
                                if label in context.allowed_actions
                            ]
                        ),
                        "source": result.source,
                        "accepted": result.source == "model",
                        "fallback_reason": result.fallback_reason,
                        "latency_ms": result.latency_ms,
                        "character_count": len(captured_output.strip()),
                        "model_output": captured_output,
                        "final_answer": result.text,
                    }
                )
    return records


def summarize_action_label_comparison(records: list[dict]) -> dict:
    """Summarize each arm and the paired acceptance changes."""
    by_variant = {}
    for variant in ("raw", "translated"):
        variant_records = [record for record in records if record["variant"] == variant]
        variant_summary = summarize(variant_records)
        action_records = [record for record in variant_records if record["shown_actions"]]
        state_records = [record for record in variant_records if record["phase"] != STATE_IDLE]
        reflected = [record for record in state_records if _mentions_state(record)]
        variant_summary["action_context"] = {
            "total": len(action_records),
            "accepted": sum(bool(record["accepted"]) for record in action_records),
            "acceptance_rate": round(
                sum(bool(record["accepted"]) for record in action_records)
                / len(action_records),
                4,
            ),
        }
        variant_summary["state_reflection"] = {
            "total": len(state_records),
            "reflected": len(reflected),
            "reflection_rate": round(len(reflected) / len(state_records), 4),
        }
        variant_summary["raw_action_echoes"] = sum(
            any(action in str(record["model_output"]) for action in SAFE_ACTION_LABELS)
            for record in variant_records
        )
        by_variant[variant] = variant_summary
    by_pair: dict[int, dict[str, dict]] = defaultdict(dict)
    for record in records:
        by_pair[int(record["pair"])][str(record["variant"])] = record
    paired = Counter()
    for pair in by_pair.values():
        raw_accepted = bool(pair["raw"]["accepted"])
        translated_accepted = bool(pair["translated"]["accepted"])
        if raw_accepted == translated_accepted:
            paired["both_accepted" if raw_accepted else "both_fallback"] += 1
        elif translated_accepted:
            paired["translation_improved"] += 1
        else:
            paired["translation_regressed"] += 1
    return {
        "by_variant": by_variant,
        "paired_outcomes": dict(paired),
        "translated_acceptance_rate_delta": round(
            by_variant["translated"]["acceptance_rate"]
            - by_variant["raw"]["acceptance_rate"],
            4,
        ),
    }


def render_action_label_comparison(records: list[dict]) -> str:
    """Render only pairs whose accepted/fallback result or final answer differs."""
    by_pair: dict[int, dict[str, dict]] = defaultdict(dict)
    for record in records:
        by_pair[int(record["pair"])][str(record["variant"])] = record
    lines = []
    for pair_index, pair in by_pair.items():
        raw = pair["raw"]
        translated = pair["translated"]
        if (
            raw["accepted"] == translated["accepted"]
            and raw["final_answer"] == translated["final_answer"]
        ):
            continue
        lines.extend(
            (
                f"\n## pair={pair_index} {_PHASE_LABELS[raw['phase']]} / {raw['utterance']}",
                f"  raw [{raw['source']} {raw['fallback_reason']}] → {raw['final_answer']}",
                "  translated "
                f"[{translated['source']} {translated['fallback_reason']}] → "
                f"{translated['final_answer']}",
            )
        )
    return "\n".join(lines) if lines else "所有配对的最终回答与放行结果均相同。"


def render_matrix_report(records: list[dict]) -> str:
    """Render a readable per-phase matrix of what the model actually said."""
    lines = []
    for phase in MATRIX_PHASES:
        lines.append(f"\n## {_PHASE_LABELS[phase]}  阶段={phase}")
        for record in [r for r in records if r["phase"] == phase]:
            if record["accepted"]:
                marker = "model "
            else:
                marker = f"fallback[{record['fallback_reason']}]"
            context_hint = "  ← 感知状态" if _mentions_state(record) else ""
            lines.append(
                f"  [{marker}] {record['utterance']}\n"
                f"       → {record['final_answer']}{context_hint}"
            )
    return "\n".join(lines)


def _mentions_state(record: dict) -> bool:
    """Loose heuristic: did the model echo a phase-specific whitelisted fact?"""
    phase = record["phase"]
    if phase == STATE_IDLE:
        return False
    answer = record["final_answer"]
    context = record.get("context", {})
    facts = []
    if context.get("current_chapter"):
        facts.append(context["current_chapter"])
    if context.get("candidate_count"):
        facts.append(str(context["candidate_count"]))
    if phase == STATE_WAIT_CHAPTER:
        facts.extend(["章节", "全局搜索"])
    if phase == STATE_WAIT_QUESTION_CHOICE:
        return "题" in answer and any(term in answer for term in ("选", "选择"))
    if phase == STATE_WAIT_CANDIDATE_CHOICE:
        facts.append("候选")
    if phase == PHASE_ANSWERED:
        facts.append("答案")
    if phase == PHASE_NO_MATCH:
        facts.extend(["无匹配", "没有匹配", "换章节", "更换章节", "新题图"])
    if phase == PHASE_ERROR:
        facts.extend(["失败", "出错", "重试", "新题图"])
    return any(fact in answer for fact in facts)


def run_matrix_evaluation(
    *,
    model: str,
    endpoint: str,
    output_dir: Path,
) -> int:
    records = evaluate_matrix(
        model_client=QwenSafeAnswerClientV0(model=model, endpoint=endpoint),
    )
    summary = summarize(records)
    report = render_matrix_report(records)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "records.jsonl").write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records
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


def run_action_label_comparison(
    *,
    model: str,
    endpoint: str,
    output_dir: Path,
) -> int:
    records = evaluate_action_label_comparison(
        model_client=QwenSafeAnswerClientV0(model=model, endpoint=endpoint),
    )
    summary = summarize_action_label_comparison(records)
    report = render_action_label_comparison(records)
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
    (output_dir / "comparison_report.txt").write_text(report + "\n", encoding="utf-8")
    print(report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir.resolve()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate bounded safe answers with Qwen")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--suite", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--compare-action-labels", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    if args.compare_action_labels:
        return run_action_label_comparison(
            model=args.model,
            endpoint=args.endpoint,
            output_dir=output_dir,
        )
    if args.matrix:
        return run_matrix_evaluation(
            model=args.model,
            endpoint=args.endpoint,
            output_dir=output_dir,
        )
    cases = load_pilot_cases() if args.suite == "pilot" else load_full_cases()
    records = evaluate_cases(
        cases,
        runs=args.runs,
        model_client=QwenSafeAnswerClientV0(model=args.model, endpoint=args.endpoint),
    )
    summary = summarize(records)
    write_results(output_dir, records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"output_dir={output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

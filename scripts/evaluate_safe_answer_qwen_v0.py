"""Run isolated pilot or full-suite Qwen safe-answer evaluation."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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
from tiku_agent.safe_answer_context_v0 import build_safe_answer_context
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
    facts = []
    if record["context"].get("current_chapter"):
        facts.append(record["context"]["current_chapter"])
    if record["context"].get("candidate_count"):
        facts.append(str(record["context"]["candidate_count"]))
    if phase == STATE_WAIT_CHAPTER:
        facts.extend(["章节", "全局搜索"])
    if phase == STATE_WAIT_CANDIDATE_CHOICE:
        facts.append("候选")
    if phase == PHASE_ANSWERED:
        facts.append("答案")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate bounded safe answers with Qwen")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--suite", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
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

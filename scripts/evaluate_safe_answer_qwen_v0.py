"""Run the isolated 10-question x 3 Qwen safe-answer pilot."""

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
from tiku_agent.safe_answer_generator_v0 import (
    SafeAnswerGeneratorV0,
    SafeAnswerModelRequestV0,
)
from tiku_agent.safe_answer_qwen_v0 import QwenSafeAnswerClientV0


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate bounded safe answers with Qwen")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DASHSCOPE_API_KEY", ""):
        raise SystemExit("DASHSCOPE_API_KEY is not set")
    output_dir = args.output_dir or (
        DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    records = evaluate_cases(
        load_pilot_cases(),
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

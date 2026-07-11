"""Compare legacy rerank prompt with the shape-only rerank prompt on 10 pairs.

This script calls the configured Zhipu vision model. It writes results under
`.tmp_tiku_agent` and does not touch Feishu runtime state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from zhipuai import ZhipuAI

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import search


@dataclass
class EvalPair:
    name: str
    query: str
    candidate: str
    same_shape: bool


@dataclass
class EvalScore:
    score: float
    reason: str
    seconds: float
    ok: bool


PAIRS = [
    EvalPair("L-L", "2静定结构/3钢架/1内力图/题目2/1L形/具体/14.jpg", "2静定结构/3钢架/1内力图/题目2/1L形/具体/17.jpg", True),
    EvalPair("门-门", "2静定结构/3钢架/1内力图/题目2/4门/10.jpg", "2静定结构/3钢架/1内力图/题目2/4门/16.jpg", True),
    EvalPair("T-T", "2静定结构/3钢架/1内力图/题目2/5T/20.jpg", "2静定结构/3钢架/1内力图/题目2/5T/24.jpg", True),
    EvalPair("双门-双门", "2静定结构/3钢架/1内力图/题目2/6双门/31.jpg", "2静定结构/3钢架/1内力图/题目2/6双门/59.jpg", True),
    EvalPair("2跨-2跨", "2静定结构/2多跨梁/内力图/题目2/2跨/1力/13.jpg", "2静定结构/2多跨梁/内力图/题目2/2跨/1力/19.jpg", True),
    EvalPair("T-L", "2静定结构/3钢架/1内力图/题目2/5T/20.jpg", "2静定结构/3钢架/1内力图/题目2/1L形/具体/14.jpg", False),
    EvalPair("T-门", "2静定结构/3钢架/1内力图/题目2/5T/20.jpg", "2静定结构/3钢架/1内力图/题目2/4门/10.jpg", False),
    EvalPair("L-门", "2静定结构/3钢架/1内力图/题目2/1L形/具体/14.jpg", "2静定结构/3钢架/1内力图/题目2/4门/10.jpg", False),
    EvalPair("双门-门", "2静定结构/3钢架/1内力图/题目2/6双门/31.jpg", "2静定结构/3钢架/1内力图/题目2/4门/10.jpg", False),
    EvalPair("2跨-3跨", "2静定结构/2多跨梁/内力图/题目2/2跨/1力/13.jpg", "2静定结构/2多跨梁/内力图/题目2/3跨/1力/11.jpg", False),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare old rerank prompt and shape-only rerank prompt.")
    parser.add_argument("--output", default=str(BASE_DIR / ".tmp_tiku_agent" / "shape_rerank_eval.json"))
    parser.add_argument("--same-threshold", type=float, default=0.8)
    parser.add_argument("--different-threshold", type=float, default=0.5)
    args = parser.parse_args()

    if not search.ZHIPUAI_API_KEY:
        raise RuntimeError("ZHIPUAI_API_KEY is not configured")

    client = ZhipuAI(api_key=search.ZHIPUAI_API_KEY)
    rows = []
    for pair in PAIRS:
        query = search.ROOT / pair.query
        candidate = search.ROOT / pair.candidate
        if not query.is_file() or not candidate.is_file():
            raise FileNotFoundError(f"Missing eval image: {query} or {candidate}")

        old_score = score_with_prompt(client, query, candidate, search.LEGACY_RERANK_PROMPT, pair.same_shape, args)
        shape_score = score_with_prompt(client, query, candidate, search.SHAPE_RERANK_PROMPT, pair.same_shape, args)
        row = {
            "name": pair.name,
            "same_shape": pair.same_shape,
            "query": str(query),
            "candidate": str(candidate),
            "legacy": asdict(old_score),
            "shape": asdict(shape_score),
        }
        rows.append(row)
        print(
            f"{pair.name}: expected={'same' if pair.same_shape else 'diff'} "
            f"old={old_score.score:.2f}/{old_score.seconds:.2f}s/{old_score.reason} "
            f"shape={shape_score.score:.2f}/{shape_score.seconds:.2f}s/{shape_score.reason}"
        )

    summary = {
        "pair_count": len(rows),
        "same_threshold": args.same_threshold,
        "different_threshold": args.different_threshold,
        "legacy_success": sum(1 for row in rows if row["legacy"]["ok"]),
        "shape_success": sum(1 for row in rows if row["shape"]["ok"]),
        "legacy_avg_seconds": round(sum(row["legacy"]["seconds"] for row in rows) / len(rows), 3),
        "shape_avg_seconds": round(sum(row["shape"]["seconds"] for row in rows) / len(rows), 3),
        "legacy_avg_score_same": avg_score(rows, "legacy", True),
        "legacy_avg_score_diff": avg_score(rows, "legacy", False),
        "shape_avg_score_same": avg_score(rows, "shape", True),
        "shape_avg_score_diff": avg_score(rows, "shape", False),
    }
    payload = {"summary": summary, "rows": rows}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved={output}")
    return 0


def score_with_prompt(client, query: Path, candidate: Path, prompt: str, same_shape: bool, args) -> EvalScore:
    start = time.perf_counter()
    score, reason = search.score_candidate_pair(client, str(query), str(candidate), prompt=prompt)
    seconds = time.perf_counter() - start
    if same_shape:
        ok = score >= args.same_threshold
    else:
        ok = score <= args.different_threshold
    return EvalScore(score=score, reason=reason, seconds=round(seconds, 3), ok=ok)


def avg_score(rows: list[dict], key: str, same_shape: bool) -> float:
    values = [row[key]["score"] for row in rows if row["same_shape"] is same_shape]
    return round(sum(values) / len(values), 3)


if __name__ == "__main__":
    raise SystemExit(main())

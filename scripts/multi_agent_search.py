"""
Run the multi-agent retrieval pipeline from the command line.

Examples:
  python scripts/multi_agent_search.py --image "D:/path/to/question.jpg" --chapter "2静定结构"
  python scripts/multi_agent_search.py --image "D:/path/to/question.jpg" --chapter auto
  python scripts/multi_agent_search.py --loads "{\"loads\":[{\"type\":\"均布\",\"raw\":\"q\"}]}" --chapter "2静定结构" --no-rerank
  python scripts/multi_agent_search.py --types 均布 --raws q --chapter "2静定结构" --no-rerank
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from multi_agent_pipeline import MultiAgentCoordinator, format_pipeline_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多 Agent 结构力学题库检索")
    parser.add_argument("--image", help="题目图片路径，使用 Qwen Agent 识别和分类")
    parser.add_argument("--loads", help="荷载 JSON，用于不调用 Qwen 的本地路由验证")
    parser.add_argument("--types", nargs="+", help="手动荷载类型列表: 集中 均布 弯矩")
    parser.add_argument("--raws", nargs="+", help="手动荷载标注列表，与 --types 一一对应")
    parser.add_argument("--chapter", default="auto", help="章节名称，如 2静定结构；图片检索可用 auto 自动识别")
    parser.add_argument("--top", type=int, default=5, help="粗筛返回数量")
    parser.add_argument("--rerank-top", type=int, default=3, help="Zhipu 复筛返回数量")
    parser.add_argument("--no-rerank", action="store_true", help="跳过 Zhipu 视觉复筛")
    parser.add_argument("--no-cache", action="store_true", help="禁用 Qwen 识别缓存")
    args = parser.parse_args()

    source_count = sum(bool(value) for value in (args.image, args.loads, args.types))
    if source_count != 1:
        parser.error("必须且只能提供 --image、--loads、--types 三者之一")
    if bool(args.types) != bool(args.raws):
        parser.error("--types 和 --raws 必须同时提供")
    if args.types and len(args.types) != len(args.raws):
        parser.error(f"--types 和 --raws 数量必须一致 (types={len(args.types)}, raws={len(args.raws)})")
    return args


def main() -> int:
    args = parse_args()
    coordinator = MultiAgentCoordinator(top_k=args.top)
    coordinator.qwen.use_cache = not args.no_cache

    if args.image:
        result = coordinator.search_image(
            args.image,
            args.chapter,
            rerank=not args.no_rerank,
            rerank_top=args.rerank_top,
            source="cli_image_search",
        )
    elif args.loads:
        loads = json.loads(args.loads).get("loads", [])
        result = coordinator.search_loads(
            loads,
            args.chapter,
            rerank=False,
            source="cli_load_search",
        )
    else:
        loads = [{"type": typ, "raw": raw} for typ, raw in zip(args.types, args.raws)]
        result = coordinator.search_loads(
            loads,
            args.chapter,
            rerank=False,
            source="cli_load_search",
        )

    print(format_pipeline_result(result))
    if result.route.route == "needs_chapter":
        return 4
    return 3 if result.route.route == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())

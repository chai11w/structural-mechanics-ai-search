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

import search
from multi_agent_pipeline import MultiAgentCoordinator, format_pipeline_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多 Agent 结构力学题库检索")
    parser.add_argument("--image", help="题目图片路径，使用 Qwen Agent 识别和分类")
    parser.add_argument("--loads", help="荷载 JSON，用于不调用 Qwen 的本地路由验证")
    parser.add_argument("--types", nargs="+", help="手动荷载类型列表: 集中 均布 弯矩")
    parser.add_argument("--raws", nargs="+", help="手动荷载标注列表，与 --types 一一对应")
    parser.add_argument("--chapter", default="auto", help="章节名称，如 2静定结构；图片检索可用 auto 自动识别")
    parser.add_argument("--top", type=int, default=3, help="粗筛返回数量")
    parser.add_argument("--rerank-top", type=int, default=search.DISPLAY_MAX_RESULTS, help="视觉复筛展示上限")
    parser.add_argument("--rerank-provider", choices=("zhipu", "qwen"), default=None, help="视觉复筛提供方，默认读取配置")
    parser.add_argument("--rerank-model", default=None, help="视觉复筛模型，默认按提供方读取配置")
    parser.add_argument("--no-rerank", action="store_true", help="跳过视觉复筛")
    parser.add_argument("--no-cache", action="store_true", help="禁用 Qwen 识别缓存")
    dimension_filter_group = parser.add_mutually_exclusive_group()
    dimension_filter_group.add_argument(
        "--enable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_true",
        help="字母库荷载粗筛候选超过20条时启用V5.2尺寸复筛（默认）",
    )
    dimension_filter_group.add_argument(
        "--disable-dimension-filter",
        dest="enable_dimension_filter",
        action="store_false",
        help="临时关闭V5.2尺寸复筛",
    )
    parser.set_defaults(enable_dimension_filter=True)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

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
    coordinator = MultiAgentCoordinator(
        top_k=args.top,
        dimension_filter_enabled=args.enable_dimension_filter,
    )
    coordinator.qwen.use_cache = not args.no_cache

    if args.image:
        result = coordinator.search_image(
            args.image,
            args.chapter,
            rerank=not args.no_rerank,
            rerank_top=args.rerank_top,
            rerank_provider=args.rerank_provider,
            rerank_model=args.rerank_model,
        )
    elif args.loads:
        loads = json.loads(args.loads).get("loads", [])
        result = coordinator.search_loads(
            loads,
            args.chapter,
            rerank=False,
        )
    else:
        loads = [{"type": typ, "raw": raw} for typ, raw in zip(args.types, args.raws)]
        result = coordinator.search_loads(
            loads,
            args.chapter,
            rerank=False,
        )

    print(format_pipeline_result(result))
    if result.route.route == "needs_chapter":
        return 4
    return 3 if result.route.route == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())

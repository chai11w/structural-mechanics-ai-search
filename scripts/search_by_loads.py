"""
search_by_loads.py - 命令行搜题工具，供 SKILL 调用
用法:
  python search_by_loads.py loads-search --types "均布" --raws "20kN/m" --chapter "2静定结构"
  python search_by_loads.py loads-search --types "集中" "弯矩" --raws "10kN" "10kN·m" --chapter "2静定结构"
  python search_by_loads.py image-search --image "D:/path/to/img.jpg" --chapter "2静定结构" --rerank
  python search_by_loads.py answer 1
"""

import sys
import os
import json
import argparse
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from search import search, answer, ROOT, ANSWER_OUTPUT, load_chapter_excel
from zhipuai import ZhipuAI
from search import extract_loads, load_local_config

cfg = load_local_config()
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY") or cfg.get("zhipuai_api_key", "")

def main():
    parser = argparse.ArgumentParser(description="结构力学题库检索")
    sub = parser.add_subparsers(dest="cmd")

    # image search
    p_img = sub.add_parser("image-search", help="图片搜题")
    p_img.add_argument("--image", required=True, help="题目图片路径")
    p_img.add_argument("--chapter", required=True, help="章节名称")
    p_img.add_argument("--rerank", action="store_true", help="对荷载粗筛结果进行 LLM 复筛，并按复筛相似度阈值输出")

    # loads search (new)
    p_loads = sub.add_parser("loads-search", help="荷载描述搜题")
    p_loads.add_argument("--types", nargs="+", required=True, help="荷载类型: 集中 均布 弯矩")
    p_loads.add_argument("--raws", nargs="+", required=True, help="对应标注: 10kN 20kN/m 10kN·m")
    p_loads.add_argument("--chapter", required=True, help="章节名称")

    # answer
    p_ans = sub.add_parser("answer", help="获取答案")
    p_ans.add_argument("rank", type=int, help="排名编号 (1-N, 0=跳过)")

    args = parser.parse_args()

    if args.cmd == "loads-search":
        types = args.types
        raws = args.raws
        if len(types) != len(raws):
            print(f"ERROR: --types 和 --raws 数量必须一致 (types={len(types)}, raws={len(raws)})")
            sys.exit(1)

        query_loads = [{"type": t, "raw": r} for t, r in zip(types, raws)]
        print(f"查询荷载: {query_loads}")
        print()
        search(query_loads, args.chapter)

    elif args.cmd == "image-search":
        client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
        print(f"识别: {args.image}")
        result = extract_loads(client, args.image)
        query_loads = result.get("loads", [])
        print(f"识别荷载: {json.dumps(result, ensure_ascii=False)}")
        print()
        if not query_loads:
            print("未识别到荷载，返回章节前几题")
        else:
            rerank_image_path = args.image if args.rerank else None
            search(query_loads, args.chapter, rerank_image_path=rerank_image_path)

    elif args.cmd == "answer":
        answer(args.rank)

if __name__ == "__main__":
    main()

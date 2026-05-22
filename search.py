"""
search.py - 结构力学题目荷载检索与储存

用法:
  # 检索 (给图)
  python search.py search --image "D:/path/to/img.jpg" --chapter "2静定结构"

  # 检索 (给荷载描述)
  python search.py search --loads '{"loads":[{"type":"集中","raw":"10kN"}]}' --chapter "2静定结构"

  # 储存 (给图)
  python search.py store --image "D:/path/to/img.jpg" --chapter "2静定结构"

  # 储存 (给路径+荷载)
  python search.py store --path "2静定结构/新题/1.jpg" --loads '{"loads":[...]}' --chapter "2静定结构"
"""

import os
import json
import re
import sys
import argparse
from pathlib import Path

os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'

from collections import Counter

import pandas as pd
from zhipuai import ZhipuAI

# ============================================================
# 配置
# ============================================================
ROOT = Path(r"D:\桌面\答疑、帮做\结构力学\帮做")
ANSWER_OUTPUT = Path(r"D:\桌面\答疑、帮做\答案输出")
LAST_SEARCH_FILE = ROOT / "_last_search.json"
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY", "")
TOP_K = 5

SYSTEM_PROMPT = """从图片中提取所有外部荷载信息。严格按以下JSON格式输出，不要输出任何其他内容。

{"loads": [{"type": "<荷载类型>", "raw": "<图中标注>"}]}

荷载类型只有三种:
- "集中": 集中力, 如 10kN, P, F=20kN, Fp=100kN
- "均布": 均布荷载/分布荷载, 如 q=4kN/m, 20kN/m, q
- "弯矩": 弯矩/力偶, 如 10kN·m, M=20kN·m

不是荷载的(忽略):
- 刚度: EI, 2EI, EA
- 尺寸: l, L, h, 4m, 6m, l/2
- 编号: A, B, C, 1, 2, 3 (节点/杆件编号)
- 支座反力符号: FA, FB, RA, RB, HA, VA, MA (这些是未知反力)
- 截面/材料: I, E, G, k
- 虚功单位力
- 公式: ql²/8, ql/2

raw字段保留图中原标注。无荷载输出{"loads":[]}。按集中→均布→弯矩排序。"""


# ============================================================
# 相似度计算
# ============================================================

def normalize_raw(raw):
    s = raw.strip()
    s = re.sub(r'\s+', '', s)
    s = s.lower()
    s = s.replace('kn', 'kN')
    # 去掉等号前缀: F=10kN→10kN, q=4kN/m→4kN/m, M=20kN·m→20kN·m
    s = re.sub(r'^[a-z_]+=\s*', '', s)
    # 符号归一化: ql/qL → q (均布荷载q标注在跨度l旁，模型误合并)
    s = re.sub(r'^q[lL]$', 'q', s)
    # F_P/Fp → F
    s = re.sub(r'^[fF]_?[pP]$', 'F', s)
    return s


def fix_load_types(loads):
    """修正分类错误"""
    for item in loads:
        raw = item.get("raw", "")
        # ql/qL 是均布荷载q标注在跨度l旁，模型误合并，非集中力
        if re.match(r'^q[lL]$', raw.lower().strip().replace(' ', '')):
            item["type"] = "均布"
    return loads


def compute_similarity(query_loads, db_loads):
    """类型级相似度：每类取交集/各自总数的 min，0/0 跳过，返回 0~1"""
    def group_by_type(loads):
        groups = {"集中": [], "均布": [], "弯矩": []}
        for item in loads:
            typ = item.get("type", "")
            raw = item.get("raw", "")
            if typ in groups:
                groups[typ].append(normalize_raw(raw))
        return groups

    q = group_by_type(query_loads)
    d = group_by_type(db_loads)

    scores = []
    for typ in ["集中", "均布", "弯矩"]:
        q_list = q[typ]
        d_list = d[typ]
        if not q_list and not d_list:
            continue

        q_counts = Counter(q_list)
        d_counts = Counter(d_list)
        inter = sum(min(q_counts[r], d_counts.get(r, 0)) for r in q_counts)

        q_total = len(q_list)
        d_total = len(d_list)

        if q_total == 0 or d_total == 0:
            scores.append(0.0)
        else:
            scores.append(min(inter / q_total, inter / d_total))

    if not scores:
        return 0.0
    return sum(scores) / len(scores)


# ============================================================
# API 调用
# ============================================================

def encode_image_base64(image_path):
    ext = Path(image_path).suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    mime = mime_map.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        import base64
        data = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{data}"


def extract_loads(client, image_path):
    data_url = encode_image_base64(image_path)

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model="GLM-5V-Turbo",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": "输出JSON。"},
                    ]},
                ],
                temperature=0.1,
                max_tokens=1024,
                extra_body={"thinking": {"type": "disabled"}},
            )
            raw_text = resp.choices[0].message.content

            if not raw_text or not raw_text.strip():
                rc = getattr(resp.choices[0].message, 'reasoning_content', '')
                if rc:
                    m = re.search(r'\{[^{}]*"loads"\s*:\s*\[.*?\]\s*\}', rc, re.DOTALL)
                    raw_text = m.group(0) if m else rc.strip()

            if not raw_text or not raw_text.strip():
                raise ValueError("Empty response")

            raw_text = raw_text.strip()
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
            raw_text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw_text)

            result = json.loads(raw_text)
            if "loads" not in result:
                raise ValueError("Missing 'loads' key")

            type_order = {"集中": 0, "均布": 1, "弯矩": 2}
            result["loads"].sort(key=lambda x: type_order.get(x.get("type", ""), 99))
            return result

        except (json.JSONDecodeError, ValueError, KeyError):
            if attempt < 2:
                import time
                time.sleep(1)
            else:
                return {"loads": []}
        except Exception as e:
            err = str(e)
            if '429' in err or '1113' in err:
                import time
                time.sleep((attempt + 1) * 3)
                continue
            return {"loads": []}

    return {"loads": []}


# ============================================================
# 加载 Excel
# ============================================================

def load_chapter_excel(chapter_name):
    """加载章节 Excel，返回 DataFrame 或 None"""
    xlsx_path = ROOT / f"{chapter_name}.xlsx"
    if not xlsx_path.exists():
        # 尝试模糊匹配
        matches = list(ROOT.glob(f"*{chapter_name}*.xlsx"))
        if matches:
            xlsx_path = matches[0]
        else:
            return None
    df = pd.read_excel(xlsx_path)
    return df


# ============================================================
# 检索
# ============================================================

def _update_excel_path(chapter_name, old_rel, new_rel):
    """更新 Excel 中失效的题目路径"""
    if old_rel == new_rel:
        return
    xlsx_path = ROOT / f"{chapter_name}.xlsx"
    if not xlsx_path.exists():
        matches = list(ROOT.glob(f"*{chapter_name}*.xlsx"))
        if not matches:
            return
        xlsx_path = matches[0]
    df = pd.read_excel(xlsx_path)
    mask = df["题目名称"] == old_rel
    if not mask.any():
        return
    df.loc[mask, "题目名称"] = new_rel
    df.to_excel(xlsx_path, index=False)
    print(f"[路径更新] {old_rel} -> {new_rel}")


def search(query_loads, chapter_name, top_k=TOP_K):
    df = load_chapter_excel(chapter_name)
    if df is None:
        print(f"ERROR: Chapter '{chapter_name}' not found")
        return

    # 修正查询荷载分类
    query_loads = fix_load_types(query_loads)

    results = []
    for _, row in df.iterrows():
        try:
            db_loads = json.loads(row["荷载"]).get("loads", [])
        except (json.JSONDecodeError, KeyError):
            db_loads = []

        # 修正数据库荷载分类
        db_loads = fix_load_types(db_loads)

        score = compute_similarity(query_loads, db_loads)
        results.append((score, row["题目名称"]))

    results.sort(key=lambda x: x[0], reverse=True)

    # 100% 相似的不管几个都输出，不足 top_k 再补次高分
    perfect = [r for r in results if r[0] >= 1.0]
    if len(perfect) >= top_k:
        top = perfect
    else:
        rest = [r for r in results if r[0] < 1.0][:top_k - len(perfect)]
        top = perfect + rest

    if not top or top[0][0] == 0:
        print("(未找到高相似度匹配，以下是章节内最近题目)")

    output_path = ROOT / "_search_result.txt"
    lines = []
    paths = []
    for rank, (score, name) in enumerate(top, 1):
        if score <= 0:
            continue
        pct = round(score * 100)
        full_path = str(ROOT / name)
        lines.append(f"{rank}. {full_path}    相似度: {pct}%")
        paths.append({"rank": rank, "path": full_path, "score": score})

    result_text = "\n".join(lines) if lines else "无匹配结果"
    output_path.write_text(result_text, encoding="utf-8")
    LAST_SEARCH_FILE.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    print(result_text)
    print(f"\n结果已保存: {output_path}")

    # 自动打开 Top 结果图片（倒序，最后打开#1在最上层；跳过0%）
    import time
    opened = 0
    for score, name in reversed(top):
        if score <= 0:
            continue
        img_path = str(ROOT / name)
        os.startfile(img_path)
        opened += 1
        time.sleep(0.3)
    if opened:
        print(f"已打开 {opened} 个匹配图片")


# ============================================================
# 储存
# ============================================================

def store_chapter_excel(chapter_name, records):
    """追加记录到章节 Excel"""
    xlsx_path = ROOT / f"{chapter_name}.xlsx"

    # 加载现有数据
    if xlsx_path.exists():
        existing = pd.read_excel(xlsx_path)
    else:
        existing = pd.DataFrame(columns=["题目名称", "荷载"])

    # 追加新记录
    new_df = pd.DataFrame(records)
    combined = pd.concat([existing, new_df], ignore_index=True)

    # 去重 (按题目名称)
    combined = combined.drop_duplicates(subset=["题目名称"], keep="last")

    combined.to_excel(xlsx_path, index=False)
    print(f"[OK] {chapter_name}.xlsx: {len(combined)} rows (+{len(records)})")


def store(chapter_name, *, image_path=None, rel_path=None, loads_json=None):
    client = ZhipuAI(api_key=ZHIPUAI_API_KEY)

    if image_path:
        print(f"识别: {image_path}")
        loads = extract_loads(client, image_path)
        print(f"荷载: {json.dumps(loads, ensure_ascii=False)}")

        # 用相对于 ROOT 的路径作为题目名称
        try:
            rel_path = str(Path(image_path).relative_to(ROOT)).replace("\\", "/")
        except ValueError:
            rel_path = Path(image_path).name

    elif rel_path and loads_json:
        loads = json.loads(loads_json)
    else:
        print("ERROR: 需要 --image 或 (--path + --loads)")
        return

    loads_str = json.dumps(loads, ensure_ascii=False)
    store_chapter_excel(chapter_name, [{"题目名称": rel_path, "荷载": loads_str}])


# ============================================================
# 答案查询
# ============================================================

def find_answer_files(question_path):
    """根据题目路径定位答案文件，返回路径列表

    规则: 题目路径里以"题目"开头的目录，替换为同级"答案"文件夹，
    然后匹配 {编号}, {编号}+, {编号}++, ... 等所有图片
    """
    p = Path(question_path)
    parts = p.parts
    stem = p.stem  # 文件名不含扩展，如 "3"

    # 从右向左找以"题目"开头的目录
    qi = None
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].startswith("题目"):
            qi = i
            break
    if qi is None:
        return []

    # 把"题目X"替换为"答案"，丢弃题目下面的所有子路径（答案目录平铺存放）
    answer_dir = Path(*parts[:qi]) / "答案"
    if not answer_dir.is_dir():
        return []

    # 匹配 stem, stem+, stem++, stem+++ 的所有图片
    found = []
    for ext in [".jpg", ".jpeg", ".png"]:
        for suffix in ["", "+", "++", "+++"]:
            f = answer_dir / f"{stem}{suffix}{ext}"
            if f.is_file():
                found.append(f)
    return found


def answer(rank):
    """根据上次检索的排名，把对应答案复制到输出文件夹"""
    import shutil

    if rank == 0:
        print("无匹配答案，跳过")
        return

    if not LAST_SEARCH_FILE.exists():
        print("ERROR: 找不到上次检索结果，请先运行 search")
        return

    last = json.loads(LAST_SEARCH_FILE.read_text(encoding="utf-8"))
    target = next((x for x in last if x["rank"] == rank), None)
    if target is None:
        print(f"ERROR: 排名 {rank} 不在上次结果中（共 {len(last)} 个）")
        return

    question_path = target["path"]
    answers = find_answer_files(question_path)

    # 清空输出文件夹
    if ANSWER_OUTPUT.exists():
        shutil.rmtree(ANSWER_OUTPUT)
    ANSWER_OUTPUT.mkdir(parents=True, exist_ok=True)

    if not answers:
        print(f"WARNING: 未找到答案文件 (题目: {question_path})")
        return

    for src in answers:
        dst = ANSWER_OUTPUT / src.name
        shutil.copy2(src, dst)

    print(f"已输出 {len(answers)} 张答案到 {ANSWER_OUTPUT}")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="结构力学荷载检索与储存")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # search
    p_search = sub.add_parser("search", help="检索相似题目")
    p_search.add_argument("--image", help="题目图片路径")
    p_search.add_argument("--loads", help="荷载 JSON 字符串")
    p_search.add_argument("--chapter", required=True, help="章节名称，如 '2静定结构'")
    p_search.add_argument("--top", type=int, default=TOP_K, help=f"返回条数 (默认 {TOP_K})")

    # store
    p_store = sub.add_parser("store", help="储存新题目")
    p_store.add_argument("--image", help="题目图片路径")
    p_store.add_argument("--path", help="相对路径，如 '2静定结构/新题/1.jpg'")
    p_store.add_argument("--loads", help="荷载 JSON 字符串")
    p_store.add_argument("--chapter", required=True, help="章节名称，如 '2静定结构'")

    # answer
    p_answer = sub.add_parser("answer", help="打开检索结果对应的答案")
    p_answer.add_argument("--rank", type=int, required=True, help="选择排名 (1-N 打开答案, 0 无匹配)")

    args = parser.parse_args()

    if args.cmd == "search":
        if args.image:
            client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
            print(f"识别查询图: {args.image}")
            result = extract_loads(client, args.image)
            query_loads = result.get("loads", [])
            print(f"识别荷载: {json.dumps(result, ensure_ascii=False)}")
            print()
        elif args.loads:
            query_loads = json.loads(args.loads).get("loads", [])
        else:
            print("ERROR: search 需要 --image 或 --loads")
            return

        if not query_loads:
            print("WARNING: 未识别到荷载，返回章节前几题")
            df = load_chapter_excel(args.chapter)
            if df is not None:
                for i, (_, row) in enumerate(df.head(args.top).iterrows()):
                    full_path = str(ROOT / row["题目名称"])
                    print(f"{i+1}. {full_path}    相似度: N/A")
            return

        search(query_loads, args.chapter, args.top or TOP_K)

    elif args.cmd == "store":
        store(
            args.chapter,
            image_path=args.image,
            rel_path=getattr(args, 'path', None),
            loads_json=args.loads,
        )

    elif args.cmd == "answer":
        answer(args.rank)


if __name__ == "__main__":
    main()

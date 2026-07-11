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
import shutil
from datetime import datetime
from pathlib import Path

os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'

from collections import Counter

import pandas as pd
from zhipuai import ZhipuAI

# ============================================================
# 配置
# ============================================================

def load_local_config():
    base = Path(__file__).parent
    cfg = {}
    for name in ("config.json", "config.local.json"):
        p = base / name
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg.update(json.load(f))
    return cfg

cfg = load_local_config()
ROOT = Path(cfg.get("root", r"D:\桌面\答疑、帮做\结构力学\帮做"))
ANSWER_OUTPUT = Path(cfg.get("answer_output", r"D:\桌面\答疑、帮做\答案输出"))
LAST_SEARCH_FILE = ROOT / "_last_search.json"
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY") or cfg.get("zhipuai_api_key", "")
TOP_K = cfg.get("top_k", 3)
RERANK_MIN_LOAD_SCORE = 0.5
DISPLAY_MIN_SCORE = 0.8
DISPLAY_ALL_SCORE = 0.9
DISPLAY_MAX_RESULTS = 3
RERANK_LOAD_WEIGHT = 0.5
RERANK_VISION_WEIGHT = 0.5
LENGTH_TIE_FINAL_FLOOR = 0.9
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_PATH_REPAIR_BACKUPS = set()
_PATH_REPAIR_BACKUP_DIR = None

SYSTEM_PROMPT = """从图片中提取所有外部荷载信息。严格按以下JSON格式输出，不要输出任何其他内容。

{"loads": [{"type": "<荷载类型>", "raw": "<去掉单位后的荷载标注>"}]}

荷载类型只有三种:
- "集中": 集中力, 如 10kN, P, F=20kN, Fp=100kN
- "均布": 均布荷载/分布荷载, 如 q=4kN/m, 20kN/m, q, F/L
- "弯矩": 弯矩/力偶, 如 10kN·m, M=20kN·m, FL, ql²

荷载类型必须根据图形形态判断，不要根据单位判断:
- 直箭头/集中箭头是集中力。
- 一排分布箭头是均布荷载。
- 弧形箭头/力偶符号才是弯矩。
- 如果图形是均布荷载但文字单位误写成 kN·m 或 kN.m，仍输出均布，不要额外输出弯矩。

raw 不要输出单位:
- 5kN、5kN/m、5kN·m、5kN.m 都只输出 5。
- q=20kN/m 输出 q=20。
- M=20kN·m 输出 M=20。
- q、P、2P/a、ql² 等纯符号表达保持原样。

不是荷载的(忽略):
- 刚度: EI, 2EI, EA
- 尺寸: l, L, h, 4m, 6m, l/2
- 编号: A, B, C, 1, 2, 3 (节点/杆件编号)
- 支座反力符号: FA, FB, RA, RB, HA, VA, MA (这些是未知反力)
- 截面/材料: I, E, G, k
- 虚功单位力
- 公式: ql²/8, ql/2

符号荷载规则:
- 先检查题干文字或图片说明里是否给出符号赋值，例如 P=40kN、q=20kN/m、F1=40kN、F2=80kN、M=20kN·m。若符号荷载在图中出现且题干给出赋值，raw 必须保留赋值形式，例如输出 P=40kN、q=20kN/m，而不是只输出 P 或 q。
- 如果同一符号荷载出现多次且题干只给出一次赋值，每个对应荷载都使用这个赋值 raw；带下标的符号如 F1/F2/P1/P2 要分别匹配自己的赋值。
- 复合符号要作为整体提取，不要拆分。例如图上是 ql 只输出 ql，不要额外输出 q；图上是 qa² 只输出 qa²，不要额外输出 q。
- F/L, F, FL 是同一 F 符号体系在不同量纲下的表达；q, qL, ql² 是同一 q 符号体系在不同量纲下的表达；M/L², M/L, M 是同一 M 符号体系在不同量纲下的表达。
- Pa, ql, qL, qa, qa², ql², FL 这类表达如果作为外荷载标注出现，应保留完整 raw。

无荷载输出{"loads":[]}。按集中→均布→弯矩排序。"""


# ============================================================
# 相似度计算
# ============================================================

SYMBOL_FAMILY_BASES = {
    "distributed": 0.010, # 均布力为主题: A, AL, AL²
    "force": 0.020,       # 集中力为主题: A/L, A, AL
    "moment": 0.030,      # 集中弯矩为主题: A/L², A/L, A
}


def _format_symbol_code(base, factor):
    value = base + (factor - 1.0) * 0.001
    if abs(value - round(value, 3)) < 1e-9:
        return f"{value:.3f}"
    return f"{value:.4f}"


def _split_symbol_factor(expr):
    match = re.match(r"^(\d+(?:\.\d+)?)(.+)$", expr)
    if match:
        return float(match.group(1)), match.group(2)

    match = re.match(r"^([a-z]+)/(\d+(?:\.\d+)?)$", expr)
    if match:
        return 1.0 / float(match.group(2)), match.group(1)

    return 1.0, expr


def _canonical_symbol_expr(raw):
    s = str(raw or "").strip()
    s = re.sub(r"\s+", "", s)
    if "=" in s:
        s = s.split("=", 1)[1]
    s = s.lower()
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("^2", "²").replace("平方", "²").replace("方", "²")
    s = s.replace("²", "2")
    s = s.replace("·", "").replace("*", "").replace("×", "")
    s = s.replace("_", "")
    return s


def _symbol_length_power(body):
    if body in {"fp"}:
        return 0
    if "/" in body:
        denominator = body.rsplit("/", 1)[1]
        if re.fullmatch(r"[a-z]+2", denominator):
            return -2
        if re.fullmatch(r"[a-z]+", denominator):
            return -1
    if re.fullmatch(r"[a-z]+2", body) and len(body) > 2:
        return 2
    if re.fullmatch(r"[a-z]+", body) and len(body) > 1:
        return 1
    return 0


def _symbol_family_by_dimension(body, load_type):
    power = _symbol_length_power(body)
    if load_type == "均布":
        if power == -1:
            return "force"
        if power == -2:
            return "moment"
        return "distributed"
    if load_type == "集中":
        if power == 1:
            return "distributed"
        if power == -1:
            return "moment"
        return "force"
    if load_type == "弯矩":
        if power == 2:
            return "distributed"
        if power == 1:
            return "force"
        return "moment"
    return None


def _symbol_family_fallback(body):
    distributed_family = {"q", "ql", "qa", "ql2", "qa2"}
    force_family = {"f/l", "p/l", "fp/l", "f", "p", "fp", "fl", "pl", "pa", "fa", "fpl"}
    moment_family = {"m/l2", "m/l", "m"}
    if body in distributed_family:
        return "distributed"
    if body in force_family:
        return "force"
    if body in moment_family:
        return "moment"
    return None


def _symbol_family_and_factor(raw, load_type=None):
    s = _canonical_symbol_expr(raw)
    factor, body = _split_symbol_factor(s)
    body = body.strip("()")

    if not re.search(r"[a-z]", body):
        return None, factor

    family = _symbol_family_by_dimension(body, load_type) if load_type else None
    if family is None:
        family = _symbol_family_fallback(body)
    return family, factor


def normalize_symbol_raw(raw, load_type=None, family_override=None):
    """Map letter loads into reserved comparison codes.

    The load type still separates 均布/集中/弯矩. These codes only encode the
    symbol family and coefficient so the old similarity algorithm can be reused.
    """
    family, factor = _symbol_family_and_factor(raw, load_type)
    if family_override and family:
        family = family_override
    if family:
        return _format_symbol_code(SYMBOL_FAMILY_BASES[family], factor)
    return None


def _assignment_rhs(raw):
    s = str(raw or "").strip()
    if "=" not in s:
        return s
    left, right = s.split("=", 1)
    if re.fullmatch(r"\s*[A-Za-z][A-Za-z0-9_]*\s*", left) and right.strip():
        return right.strip()
    return s


def _looks_like_numeric_unit(raw):
    s = str(raw or "").strip()
    s = re.sub(r"\s+", "", s).lower()
    return re.match(r"^\d+(?:\.\d+)?(?:k|kn|n)(?:[/·\.\-*a-z0-9]*)?$", s) is not None


LOAD_UNIT_PATTERNS = [
    r"kN[·.\-*×]?m",
    r"N[·.\-*×]?m",
    r"kN/m",
    r"N/m",
    r"kN",
    r"(?<=\d)k\b",
    r"\bN\b",
]


def strip_load_unit(raw):
    """Remove physical units from a load label while keeping symbols/assignments."""
    text = str(raw or "").strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("KN", "kN").replace("kn", "kN")
    for pattern in LOAD_UNIT_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.I)
    text = text.strip(".·*-×/")
    return text


def normalize_raw(raw, load_type=None, family_override=None):
    comparable_raw = _assignment_rhs(strip_load_unit(raw))
    if not _looks_like_numeric_unit(comparable_raw):
        symbol_code = normalize_symbol_raw(comparable_raw, load_type, family_override)
        if symbol_code is not None:
            return symbol_code

    s = str(comparable_raw or "").strip()
    s = re.sub(r'\s+', '', s)
    s = s.lower()
    # 去掉等号前缀: F=10kN→10kN, F1=40kN→40kN, q=4kN/m→4kN/m
    s = re.sub(r'^[a-z][a-z0-9_]*=\s*', '', s)
    # 去掉数值荷载单位后缀，只保留数值/符号（20k/20kN→20, 4kN/m→4, 10kN·m→10）
    # 类型已经由 type 字段区分，单位信息冗余
    s = re.sub(r'(?<=\d)k(?:n)?.*$', '', s)
    s = s.strip('.')
    return s


def _safe_parse_loads(raw_str):
    """容错解析荷载 JSON：自动修复中文逗号/引号等常见编辑错误"""
    if not isinstance(raw_str, str):
        return []
    # 中文标点 → 英文标点
    fixed = raw_str.replace("，", ",").replace("：", ":") \
                   .replace("“", '"').replace("”", '"')
    try:
        return json.loads(fixed).get("loads", [])
    except (json.JSONDecodeError, KeyError):
        return []


def normalize_load_type(load_type, raw=None):
    typ = str(load_type or "").strip()
    aliases = {
        "集中": "集中",
        "集中力": "集中",
        "单个力": "集中",
        "点荷载": "集中",
        "均布": "均布",
        "均布荷载": "均布",
        "分布力": "均布",
        "分布荷载": "均布",
        "弯矩": "弯矩",
        "力偶": "弯矩",
        "集中力偶": "弯矩",
        "集中弯矩": "弯矩",
    }
    if typ in aliases:
        return aliases[typ]

    family, _ = _symbol_family_and_factor(raw, None)
    if family == "moment":
        return "弯矩"
    return typ


DEFAULT_NUMERIC_UNITS = {
    "集中": "kN",
    "均布": "kN/m",
    "弯矩": "kN·m",
}


def add_default_numeric_unit(raw, load_type):
    """Normalize manual input under the unitless storage/search rule."""
    text = str(raw or "").strip()
    return strip_load_unit(text)


def normalize_query_loads(loads):
    """Normalize user/query loads before routing and similarity comparison."""
    normalized = fix_load_types([dict(item) for item in loads])
    for item in normalized:
        item["raw"] = strip_load_unit(item.get("raw", ""))
    return normalized


def fix_load_types(loads):
    """修正模型输出的荷载类型别名。"""
    for item in loads:
        item["type"] = normalize_load_type(item.get("type", ""), item.get("raw", ""))
    return loads


def _canonical_symbol(raw):
    """Return a compact symbol expression for post-processing only."""
    s = str(raw or "").strip()
    s = re.sub(r"\s+", "", s)
    if "=" in s:
        s = s.split("=", 1)[1]
    s = s.replace("^2", "²").replace("^3", "³")
    s = s.replace("L", "l")
    s = s.replace("·", "").replace("*", "")
    s = re.sub(r"^[0-9.]+", "", s)
    return s.lower()


def postprocess_extracted_loads(result):
    """Normalize model output without deleting independent symbol loads."""
    loads = result.get("loads", [])
    if not isinstance(loads, list):
        result["loads"] = []
        return result

    result["loads"] = fix_load_types(loads)
    for item in result["loads"]:
        item["raw"] = strip_load_unit(item.get("raw", ""))
    return result


def _dominant_symbol_family(loads):
    counts = Counter()
    for item in loads:
        typ = normalize_load_type(item.get("type", ""), item.get("raw", ""))
        family, _ = _symbol_family_and_factor(item.get("raw", ""), typ)
        if family:
            counts[family] += 1
    if not counts:
        return None
    top_family, top_count = counts.most_common(1)[0]
    if top_count < 2:
        return None
    if len(counts) == 1:
        return top_family
    second_count = counts.most_common(2)[1][1]
    if top_count > second_count:
        return top_family
    return None


def normalize_load_for_similarity(item, dominant_family=None):
    typ = normalize_load_type(item.get("type", ""), item.get("raw", ""))
    raw = item.get("raw", "")
    family, _ = _symbol_family_and_factor(raw, typ)
    override = dominant_family if family and dominant_family and family != dominant_family else None
    return normalize_raw(raw, typ, family_override=override)


RERANK_PROMPT = """你是结构力学搜题结果复筛器。候选题已经通过荷载相似度粗筛。

你会看到：
1. 查询题图片
2. 一个候选题图片

请根据结构形状是否相近、荷载位置和方向是否相同给候选题打相似度分数。

不要解题。
不要重新计算荷载数量。
不要重新判断荷载类型数量。
不要因为题号、节点字母、尺寸标注不同而降分。

严格输出JSON，不要输出其它文字：
{"score":0.95,"reason":"短评"}
reason 必须少于8个汉字。"""


LENGTH_TIE_PROMPT = """你是结构力学搜题结果打平复核器。候选题已经被判定为高度相似。

你会看到：
1. 查询题图片
2. 一个候选题图片

请只比较杆件长度、跨长、高度、分段长度和整体比例是否一致。
不要解题。
不要判断荷载位置。
不要重新计算荷载数量。
不要重新判断荷载类型数量。
不要因为题号、节点字母不同而降分。

严格输出JSON，不要输出其它文字：
{"score":0.95,"reason":"理由不超过20字"}"""


def score_candidate_pair(client, query_image_path, candidate_path, prompt=RERANK_PROMPT):
    content = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "查询题图片："},
        {
            "type": "image_url",
            "image_url": {"url": encode_image_base64(query_image_path)},
        },
        {"type": "text", "text": "候选题图片："},
        {
            "type": "image_url",
            "image_url": {"url": encode_image_base64(candidate_path)},
        },
    ]

    resp = client.chat.completions.create(
        model="GLM-5V-Turbo",
        messages=[
            {"role": "system", "content": "你只输出JSON。"},
            {"role": "user", "content": content},
        ],
        temperature=0.0,
        max_tokens=512,
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw_text = resp.choices[0].message.content.strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)
    parsed = json.loads(raw_text)
    score = float(parsed.get("score", 0))
    score = max(0.0, min(1.0, score))
    return score, str(parsed.get("reason", "")).strip()


def compute_final_rerank_score(load_score, rerank_score):
    load_score = max(0.0, min(1.0, float(load_score or 0)))
    rerank_score = max(0.0, min(1.0, float(rerank_score or 0)))
    return load_score * RERANK_LOAD_WEIGHT + rerank_score * RERANK_VISION_WEIGHT


def apply_length_tie_break(client, query_image_path, scored):
    perfect = [item for item in scored if float(item.get("final_score") or 0) >= 0.999]
    if len(perfect) <= 1:
        return scored

    for item in perfect:
        path = Path(item["path"])
        try:
            length_score, length_reason = score_candidate_pair(
                client, query_image_path, str(path), prompt=LENGTH_TIE_PROMPT
            )
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: 候选 {item['rank']} 杆长复核失败: {exc}")
            length_score, length_reason = 0.0, "杆长复核失败"
        item["length_score"] = length_score
        item["length_reason"] = length_reason
        item["final_score"] = LENGTH_TIE_FINAL_FLOOR + (1.0 - LENGTH_TIE_FINAL_FLOOR) * length_score

    return scored


def rerank_candidates(query_image_path, candidates, top_n=3):
    """Use the vision model to rerank already-selected search candidates."""
    if not query_image_path or not candidates:
        return []

    usable = []
    for candidate in candidates:
        path, resolved_name, repaired = resolve_question_path(candidate["path"], update_excel=False)
        if not path.is_file():
            continue
        item = dict(candidate)
        item["path"] = str(path)
        if repaired:
            item["name"] = resolved_name
        usable.append(item)

    if not usable:
        return []

    client = ZhipuAI(api_key=ZHIPUAI_API_KEY)
    scored = []
    for candidate in usable:
        path = Path(candidate["path"])
        try:
            score, reason = score_candidate_pair(client, query_image_path, str(path))
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: 候选 {candidate['rank']} 复筛失败: {exc}")
            score, reason = 0.0, "复筛失败"
        item = dict(candidate)
        item["rerank_score"] = score
        item["final_score"] = compute_final_rerank_score(item.get("score", 0), score)
        item["rerank_reason"] = reason
        scored.append(item)

    scored = apply_length_tie_break(client, query_image_path, scored)
    scored.sort(
        key=lambda x: (
            x.get("final_score", 0),
            x.get("length_score", 1),
            x.get("score", 0),
            x.get("rerank_score", 0),
        ),
        reverse=True,
    )
    return select_display_results(scored, max_results=top_n)


def display_similarity_score(item):
    """Return the similarity score shown to users for thresholded result output."""
    if isinstance(item, dict):
        final_score = item.get("final_score")
        if final_score is not None:
            return float(final_score or 0)
        return float(item.get("score") or 0)
    return float(item[0] or 0)


def select_display_results(
    results,
    min_score=DISPLAY_MIN_SCORE,
    max_results=DISPLAY_MAX_RESULTS,
    all_score=DISPLAY_ALL_SCORE,
):
    """For reranked results: show all >90%, else top 3 >80%, else the best one."""
    if not results:
        return []

    very_high = [item for item in results if display_similarity_score(item) > all_score]
    if very_high:
        selected = very_high
    else:
        high_quality = [item for item in results if display_similarity_score(item) > min_score]
        selected = high_quality[:max_results] if high_quality else [max(results, key=display_similarity_score)]

    renumbered = []
    for rank, item in enumerate(selected, 1):
        if isinstance(item, dict):
            copied = dict(item)
            copied["rank"] = rank
            renumbered.append(copied)
        else:
            renumbered.append(item)
    return renumbered


def compute_similarity(query_loads, db_loads):
    """类型级相似度：每类取交集/各自总数的 min，0/0 跳过，返回 0~1"""
    def group_by_type(loads):
        groups = {"集中": [], "均布": [], "弯矩": []}
        dominant_family = _dominant_symbol_family(loads)
        for item in loads:
            typ = normalize_load_type(item.get("type", ""), item.get("raw", ""))
            if typ in groups:
                groups[typ].append(normalize_load_for_similarity(item, dominant_family))
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

            result = postprocess_extracted_loads(result)
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
# 题目路径解析 / 检索
# ============================================================

def _rel_path_from_question_path(question_path):
    p = Path(str(question_path))
    if p.is_absolute():
        try:
            return p.relative_to(ROOT).as_posix()
        except ValueError:
            return p.as_posix()
    return str(question_path).replace("\\", "/")


def _exact_case_path(base, relative_path):
    """Resolve a relative path while preserving actual filesystem casing."""
    current = Path(base)
    exact_case = True

    for part in str(relative_path).replace("\\", "/").split("/"):
        if not part:
            continue
        if not current.is_dir():
            return False, exact_case, None
        try:
            children = list(current.iterdir())
        except OSError:
            return False, exact_case, None

        exact = next((child for child in children if child.name == part), None)
        if exact is not None:
            current = exact
            continue

        case_match = next((child for child in children if child.name.lower() == part.lower()), None)
        if case_match is not None:
            current = case_match
            exact_case = False
            continue

        return False, exact_case, None

    return current.is_file(), exact_case, current if current.is_file() else None


def _common_prefix_len(left, right):
    count = 0
    for a, b in zip(left, right):
        if a.lower() != b.lower():
            break
        count += 1
    return count


def _find_relocated_question_image(relative_path, chapter_name=None):
    """Find a moved question image by searching the nearest question folder."""
    rel = str(relative_path).replace("\\", "/")
    parts = [part for part in rel.split("/") if part]
    if not parts:
        return None

    filename = parts[-1].lower()
    if Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
        return None

    candidate_anchors = []
    for end in range(len(parts) - 1, 0, -1):
        anchor = ROOT.joinpath(*parts[:end])
        if anchor.is_dir():
            candidate_anchors.append(anchor)
            break

    if chapter_name:
        chapter_anchor = ROOT / chapter_name
        if chapter_anchor.is_dir() and chapter_anchor not in candidate_anchors:
            candidate_anchors.append(chapter_anchor)
    elif len(parts) > 1:
        chapter_anchor = ROOT / parts[0]
        if chapter_anchor.is_dir() and chapter_anchor not in candidate_anchors:
            candidate_anchors.append(chapter_anchor)

    for anchor in candidate_anchors:
        candidates = [
            p for p in anchor.rglob("*")
            if p.is_file()
            and p.suffix.lower() in IMAGE_SUFFIXES
            and p.name.lower() == filename
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            return candidates[0]

        scored = []
        for candidate in candidates:
            try:
                cand_rel = candidate.relative_to(ROOT).as_posix().split("/")
            except ValueError:
                cand_rel = candidate.as_posix().split("/")
            scored.append((
                _common_prefix_len(parts[:-1], cand_rel[:-1]),
                -abs(len(cand_rel) - len(parts)),
                candidate,
            ))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        if len(scored) == 1 or scored[0][:2] != scored[1][:2]:
            return scored[0][2]

    return None


def _backup_excel_for_path_repair(xlsx_path):
    global _PATH_REPAIR_BACKUP_DIR

    xlsx_path = Path(xlsx_path)
    if xlsx_path in _PATH_REPAIR_BACKUPS or not xlsx_path.exists():
        return

    if _PATH_REPAIR_BACKUP_DIR is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _PATH_REPAIR_BACKUP_DIR = Path(__file__).parent / "backups" / f"auto_path_repair_{stamp}"
        _PATH_REPAIR_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = _PATH_REPAIR_BACKUP_DIR / xlsx_path.name
    if not backup_path.exists():
        shutil.copy2(xlsx_path, backup_path)
    _PATH_REPAIR_BACKUPS.add(xlsx_path)


def _update_excel_path(chapter_name, old_rel, new_rel):
    """更新 Excel 中失效的题目路径"""
    if old_rel == new_rel:
        return False
    xlsx_path = ROOT / f"{chapter_name}.xlsx"
    if not xlsx_path.exists():
        matches = list(ROOT.glob(f"*{chapter_name}*.xlsx"))
        if not matches:
            return False
        xlsx_path = matches[0]
    df = pd.read_excel(xlsx_path)
    mask = df["题目名称"] == old_rel
    if not mask.any():
        return False
    _backup_excel_for_path_repair(xlsx_path)
    df.loc[mask, "题目名称"] = new_rel
    before = len(df)
    df = df.drop_duplicates(subset=["题目名称", "荷载"], keep="first")
    df.to_excel(xlsx_path, index=False)
    removed = before - len(df)
    suffix = f"，删除重复 {removed} 条" if removed else ""
    print(f"[路径更新] {old_rel} -> {new_rel}{suffix}")
    return True


def resolve_question_path(question_path, chapter_name=None, update_excel=False):
    """Resolve stale Excel question paths and optionally repair the workbook."""
    old_rel = _rel_path_from_question_path(question_path)
    exists, exact_case, actual_path = _exact_case_path(ROOT, old_rel)

    if exists and actual_path is not None:
        new_rel = actual_path.relative_to(ROOT).as_posix()
        repaired = new_rel != old_rel or not exact_case
        if repaired and update_excel and chapter_name:
            _update_excel_path(chapter_name, old_rel, new_rel)
        return actual_path, new_rel, repaired

    relocated = _find_relocated_question_image(old_rel, chapter_name)
    if relocated is not None and relocated.is_file():
        try:
            new_rel = relocated.relative_to(ROOT).as_posix()
        except ValueError:
            new_rel = relocated.as_posix()
        if update_excel and chapter_name:
            _update_excel_path(chapter_name, old_rel, new_rel)
        return relocated, new_rel, True

    return ROOT / old_rel, old_rel, False


def search(query_loads, chapter_name, top_k=TOP_K, rerank_image_path=None, rerank_top=3):
    df = load_chapter_excel(chapter_name)
    if df is None:
        print(f"ERROR: Chapter '{chapter_name}' not found")
        return

    # 修正查询荷载分类，并给手动纯数值输入补默认单位
    query_loads = normalize_query_loads(query_loads)

    results = []
    for _, row in df.iterrows():
        db_loads = _safe_parse_loads(row["荷载"])

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
        resolved_path, resolved_name, repaired = resolve_question_path(
            name, chapter_name=chapter_name, update_excel=True
        )
        if repaired:
            name = resolved_name
        full_path = str(resolved_path)
        paths.append({"rank": rank, "path": full_path, "score": score, "name": name})

    all_paths = list(paths)
    lines = []
    for item in paths:
        pct = round(display_similarity_score(item) * 100)
        lines.append(f"{item['rank']}. {item['path']}    相似度: {pct}%")

    result_text = "\n".join(lines) if lines else "无匹配结果"
    output_path.write_text(result_text, encoding="utf-8")
    LAST_SEARCH_FILE.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    print(result_text)
    print(f"\n结果已保存: {output_path}")

    filtered_rerank_paths = [item for item in all_paths if item["score"] >= RERANK_MIN_LOAD_SCORE]
    if rerank_image_path and filtered_rerank_paths:
        reranked = rerank_candidates(rerank_image_path, filtered_rerank_paths, rerank_top)
        if reranked:
            reranked = select_display_results(reranked)
            rerank_lines = []
            rerank_paths = []
            for rank, item in enumerate(reranked, 1):
                final_pct = round(float(item.get("final_score") or 0) * 100)
                reason = item.get("rerank_reason") or "LLM复筛"
                rerank_lines.append(
                    f"{rank}. {item['path']}    相似度: {final_pct}%"
                )
                rerank_paths.append({
                    "rank": rank,
                    "path": item["path"],
                    "score": item["score"],
                    "coarse_rank": item["rank"],
                    "rerank_score": item.get("rerank_score"),
                    "final_score": item.get("final_score"),
                    "length_score": item.get("length_score"),
                    "length_reason": item.get("length_reason"),
                    "rerank_reason": reason,
                })
            rerank_text = "\n".join(rerank_lines)
            output_path.write_text(rerank_text, encoding="utf-8")
            LAST_SEARCH_FILE.write_text(json.dumps(rerank_paths, ensure_ascii=False), encoding="utf-8")
            print("\nLLM复筛结果:")
            print(rerank_text)
            print(f"\n复筛结果已保存: {output_path}")

    # （已禁用自动弹图片）


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

        if not loads.get("loads"):
            print("未识别到荷载，取消储存")
            return

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
    p, _, _ = resolve_question_path(question_path, update_excel=False)
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
    p_search.add_argument("--rerank", action="store_true", help="对图片搜索的粗筛结果进行 LLM 复筛，并按复筛相似度阈值输出")

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
        query_image_path = None
        if args.image:
            query_image_path = args.image
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
                    full_path = str(resolve_question_path(
                        row["题目名称"], chapter_name=args.chapter, update_excel=True
                    )[0])
                    print(f"{i+1}. {full_path}    相似度: N/A")
            return

        rerank_image_path = query_image_path if args.rerank else None
        if args.rerank and not rerank_image_path:
            print("WARNING: --rerank 只支持 --image 搜索，当前跳过复筛")
        search(query_loads, args.chapter, args.top or TOP_K, rerank_image_path=rerank_image_path)

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

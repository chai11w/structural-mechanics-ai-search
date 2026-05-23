"""
build_index.py - 批量识别结构力学题目图片中的荷载信息

用法:
  1. 安装依赖: pip install zhipuai pandas openpyxl
  2. 运行: python build_index.py

输出: 根目录下每个章节一个 Excel 文件 (如 "2静定结构.xlsx")
"""

import os
import json
import time
import base64
import re
import sys
from pathlib import Path

# 绕过 Windows 系统代理 (httpx 会用系统代理, 但代理不可达)
os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'

import pandas as pd
from zhipuai import ZhipuAI

# ============================================================
# 配置区
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
ROOT = Path(cfg.get("root", r"D:/桌面/答疑、帮做/结构力学/帮做"))
ZHIPUAI_API_KEY = os.environ.get("ZHIPUAI_API_KEY", "")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
REQUEST_INTERVAL = 1.0
MAX_RETRIES = 2

# ============================================================
# Prompt (optimized for GLM-5V-Turbo with thinking disabled)
# ============================================================

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
- 虚功单位力: 蓝色/彩色箭头标注的虚拟力
- 公式: ql²/8, ql/2

raw字段保留图中原标注。无荷载输出{"loads":[]}。按集中→均布→弯矩排序。"""

# For non-thinking models (glm-4v), use the detailed prompt
SYSTEM_PROMPT_DETAILED = """你是结构力学题目荷载提取器。从图片中识别所有作用在结构上的真实外部荷载，输出严格JSON。

## 什么是荷载 (必须提取)
荷载是作用在结构上的外力，只有三种类型:
- "集中": 集中力，如 10kN, 20kN, P, F=15kN, Fp=100kN, ql
- "均布": 均布荷载/分布荷载，如 q, q=4kN/m, 20kN/m, 16kN/m
- "弯矩": 弯矩/力偶，如 9kN·m, 10kN·m, M=20kN·m, m=4kN·m

## 什么不是荷载 (绝对不要提取!!!)
以下内容经常出现在结构力学图中但都不是荷载，必须全部忽略:

1. 刚度参数: EI, 2EI, 3EI, EA, k (弹簧刚度)
2. 几何尺寸: l, L, h, a, b, 4m, 6m, l/2, 2l (长度/跨度标注)
3. 节点/支座编号: A, B, C, D, 1, 2, 3
4. 截面特性: I, A, W
5. 材料常数: E, G, ν
6. 纯数字比例: 1/3, 0.5, 2, 3,4
7. 支座反力标记: R_A, R_B (这些是未知反力不是外加荷载)
8. 位移/转角标记: Δ, θ, φ
9. 虚功法单位力标记: 手绘箭头、蓝色/彩色箭头、自由端处标注的虚拟单位力
10. 方程片段: ql²/8, ql/2 (这些是计算结果不是荷载)
11. EI=常数, EA=常数 等标注

## 判断准则
一个标注只有在满足以下条件之一时才是荷载:
- 包含力的单位 (kN, N, KN) 或力矩单位 (kN·m, kNm)
- 是公认的荷载符号 (q, P, F, M) 且标注在荷载位置
- 表示分布荷载集度 (有 /m 单位)
- 如果图中只有符号没有数值 (如仅标 q 或 P)，这也算荷载

## 输出规则
1. 按 集中 → 均布 → 弯矩 排序
2. 同类荷载按图中标注顺序
3. raw字段保留图中完整原始标注，不修改不翻译
4. 无荷载输出 {"loads": []}
5. 仅输出JSON，不要任何解释文字或markdown标记

## 格式示例
{"loads": [{"type": "集中", "raw": "20kN"}, {"type": "集中", "raw": "10kN"}, {"type": "均布", "raw": "q=4kN/m"}, {"type": "弯矩", "raw": "10kN·m"}]}
{"loads": []}"""


# ============================================================
# 后处理: 过滤模型幻觉
# ============================================================

# 绝对非荷载模式 (regex)
NON_LOAD_PATTERNS = [
    # 刚度
    r'^[0-9.]*\s*EI$', r'^[0-9.]*\s*EA$', r'^[0-9.]*\s*ei$',
    # 纯长度 (不带力单位)
    r'^[0-9.]*\s*[lLhHabd]$', r'^[lLhH]/[0-9.]+$', r'^[0-9.]+\s*[lL]$',
    r'^[0-9.]+\s*m$',  # 如 "4m", "6m" 但非 "kN/m"
    r'^[0-9.]+\s*mm$', r'^[0-9.]+\s*cm$',
    # 纯数字/比例
    r'^[0-9.]+\s*$', r'^[0-9.,]+\s*$', r'^[0-9.]+/[0-9.]+$',
    # 单字母标签 (但保留 q, P, F, M, p, f, m)
    r'^[A-EH-JL-OQ-Za-egh-jln-z]$', r'^\([a-zA-Z]\)$',
    # 支座反力符号 (FA, FB, RA, RB, HA, VA, MA 等)
    r'^[FR]\s*[A-D]$', r'^[HV]\s*[A-D]$', r'^M\s*[A-D]$',
    r'^[FRHVM]_[A-D]$',  # F_A, R_A, H_A, V_A, M_A
    # 刚度相关
    r'^k[0-9.]*$', r'^[0-9.]*\s*k$',
    r'^EI\s*=\s*常数', r'^EA\s*=\s*常数',
    # 截面/材料
    r'^[IWE]$', r'^[0-9.]*\s*[IWE]$',
    # 计算式子
    r'^ql\^?[2-9]', r'^ql/[0-9]', r'^[0-9.]*ql\^?[2-9]',
]

# 荷载必须有单位的模式 (对数值型荷载)
HAS_LOAD_UNIT = re.compile(r'[kK]?[Nn]', re.IGNORECASE)  # 含 N/n 的力单位
HAS_MOMENT_UNIT = re.compile(r'[kK]?[Nn][·.*\-]?[mM]|[kK][Nn][Mm]', re.IGNORECASE)  # kN·m, kNm
HAS_DIST_UNIT = re.compile(r'[kK]?[Nn]/[mM]', re.IGNORECASE)  # kN/m

def is_non_load(item):
    """判断一个荷载项是否为幻觉，返回 True 表示应该删除"""
    raw = item.get("raw", "")
    typ = item.get("type", "")

    # 1. 检查是否匹配已知的非荷载模式
    for pat in NON_LOAD_PATTERNS:
        if re.fullmatch(pat, raw.strip()):
            return True

    # 2. 数值型荷载必须有对应单位
    has_digit = bool(re.search(r'\d', raw))
    if has_digit:
        # 集中力应有力的单位 (kN, N 等), 但允许纯符号如 "ql"
        if typ == "集中":
            if HAS_DIST_UNIT.search(raw) and not HAS_MOMENT_UNIT.search(raw):
                pass  # 集中力不太可能有 /m, 但宽松处理
            if not HAS_LOAD_UNIT.search(raw):
                # 允许如 "ql", "3qL", "Fp" 等符号表达式
                if not re.search(r'[qQpPfFlL]', raw):
                    return True
        # 均布荷载应有 /m 或 符号 q
        elif typ == "均布":
            if not HAS_DIST_UNIT.search(raw) and not re.search(r'^[qQ]', raw):
                if has_digit and not HAS_LOAD_UNIT.search(raw):
                    return True
        # 弯矩应有 ·m 或 kNm 等
        elif typ == "弯矩":
            if not HAS_MOMENT_UNIT.search(raw):
                # 允许如 "M=...", "m=..." 的符号表达
                if has_digit and not HAS_LOAD_UNIT.search(raw):
                    return True
                if has_digit and not re.search(r'[Mm]', raw):
                    return True

    # 3. 类型-单位 明显不匹配
    if typ == "弯矩" and HAS_DIST_UNIT.search(raw):
        return True  # 弯矩有 /m 单位，不对
    if typ == "均布" and HAS_MOMENT_UNIT.search(raw):
        return True  # 均布有 ·m 单位，不对

    return False

def fix_and_clean_loads(result):
    """修正类型错误 + 清理幻觉，返回 (result, fixed_count, removed_count)"""
    if "loads" not in result:
        return result, 0, 0

    original = result["loads"]
    fixed = 0
    cleaned = []

    for item in original:
        raw = item.get("raw", "")
        old_type = item.get("type", "")

        # 去掉等号前缀: F=10kN→10kN, q=4kN/m→4kN/m
        item["raw"] = re.sub(r'^[A-Za-z_]+=\s*', '', raw)
        raw = item["raw"]

        # 根据 raw 的单位特征自动修正 type
        new_type = old_type
        has_moment = bool(re.search(r'[kK]?[Nn][·.*\-]?[mM]|[kK][Nn][Mm]', raw))  # kN·m, kNm
        has_dist = bool(re.search(r'[kK]?[Nn]/[mM]', raw))  # kN/m
        has_force = bool(re.search(r'[kK]?[Nn]', raw))

        if has_moment and not has_dist:
            new_type = "弯矩"
        elif has_dist and not has_moment:
            new_type = "均布"
        elif has_force and not has_dist and not has_moment:
            # 纯力单位 → 集中
            new_type = "集中"
        # 如果没有数字单位 (纯符号), 保持原样

        if new_type != old_type:
            item["type"] = new_type
            fixed += 1

        # 过滤幻觉
        if not is_non_load(item):
            cleaned.append(item)

    removed = len(original) - len(cleaned)
    result["loads"] = cleaned
    return result, fixed, removed


# ============================================================
# 安全打印 (避免 GBK 编码崩溃)
# ============================================================

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # 用 ascii 回退
        text = ' '.join(str(a) for a in args)
        print(text.encode('ascii', errors='replace').decode('ascii'), **kwargs)


# ============================================================
# 核心逻辑
# ============================================================

def encode_image_base64(image_path: Path) -> str:
    ext = image_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    mime = mime_map.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{data}"


def extract_loads(client: ZhipuAI, image_path: Path) -> dict:
    data_url = encode_image_base64(image_path)

    for attempt in range(1 + MAX_RETRIES):
        try:
            response = client.chat.completions.create(
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

            raw_text = response.choices[0].message.content

            # Fallback: if content is empty, try reasoning_content
            if not raw_text or not raw_text.strip():
                rc = getattr(response.choices[0].message, 'reasoning_content', '')
                if rc:
                    json_match = re.search(r'\{[^{}]*"loads"\s*:\s*\[.*?\]\s*\}', rc, re.DOTALL)
                    if json_match:
                        raw_text = json_match.group(0)
                    else:
                        raw_text = rc.strip()

            if not raw_text or not raw_text.strip():
                raise ValueError("Empty response")

            raw_text = raw_text.strip()
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
            raw_text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw_text)

            result = json.loads(raw_text)

            if "loads" not in result:
                raise ValueError("Missing 'loads' key")
            for item in result["loads"]:
                if "type" not in item or "raw" not in item:
                    raise ValueError("Missing 'type' or 'raw'")
                if item["type"] not in ("集中", "均布", "弯矩"):
                    raise ValueError(f"Invalid type: {item['type']}")

            # 排序
            type_order = {"集中": 0, "均布": 1, "弯矩": 2}
            result["loads"].sort(key=lambda x: type_order[x["type"]])

            # 后处理: 修正类型 + 清理幻觉
            result, fixed, removed = fix_and_clean_loads(result)
            # 修正类型后重新排序
            if fixed:
                result["loads"].sort(key=lambda x: type_order[x["type"]])
            if fixed or removed:
                safe_print(f"[fix={fixed} filt={removed}]", end=" ")

            return result

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            if attempt < MAX_RETRIES:
                safe_print(f"[retry{attempt+1}]", end=" ")
                time.sleep(1)
            else:
                safe_print(f"[BAD_JSON]", end=" ")
                return {"loads": []}
        except Exception as e:
            err_msg = str(e)
            # 429 限流 → 退避重试
            if '429' in err_msg or '1113' in err_msg:
                if attempt < MAX_RETRIES + 1:
                    wait = (attempt + 1) * 3
                    safe_print(f"[limit-{wait}s]", end=" ")
                    time.sleep(wait)
                    continue
                else:
                    safe_print(f"[LIMIT]", end=" ")
            else:
                safe_print(f"[ERR]", end=" ")
            return {"loads": []}

    return {"loads": []}


def find_images(chapter_dir: Path) -> list[Path]:
    images = []
    for root, dirs, files in os.walk(chapter_dir):
        dirs[:] = [d for d in dirs if "答案" not in d]
        for f in files:
            if Path(f).suffix.lower() in IMAGE_EXTENSIONS:
                images.append(Path(root) / f)
    return sorted(images)


def process_chapter(client: ZhipuAI, chapter_dir: Path, resume: bool = False) -> list[dict]:
    chapter_name = chapter_dir.name
    safe_print(f"\n{'='*60}")
    safe_print(f"Chapter: {chapter_name}")
    safe_print(f"{'='*60}")

    images = find_images(chapter_dir)
    if not images:
        safe_print(f"  No images, skip")
        return []

    # 中间结果文件 (防丢失)
    tmp_file = ROOT / f"_{chapter_name}_tmp.json"

    # 恢复已处理的记录
    records = []
    processed = set()
    if resume and tmp_file.exists():
        try:
            records = json.load(open(tmp_file, 'r', encoding='utf-8'))
            processed = {r["题目名称"] for r in records}
            safe_print(f"  Resumed {len(records)} records from tmp")
        except Exception:
            pass

    for i, img_path in enumerate(images, 1):
        rel_path = str(img_path.relative_to(ROOT)).replace("\\", "/")

        # 跳过已处理的
        if rel_path in processed:
            safe_print(f"  [{i}/{len(images)}] {rel_path} [skip]")
            continue

        safe_print(f"  [{i}/{len(images)}] {rel_path}", end=" ", flush=True)

        loads = extract_loads(client, img_path)
        loads_str = json.dumps(loads, ensure_ascii=False)
        safe_print(loads_str)

        records.append({"题目名称": rel_path, "荷载": loads_str})

        time.sleep(REQUEST_INTERVAL)

        # 每 20 张存一次中间结果
        if len(records) % 20 == 0:
            try:
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(records, f, ensure_ascii=False)
            except Exception:
                pass

    safe_print(f"  Chapter {chapter_name} done: {len(records)} images")

    # 清理临时文件
    try:
        os.remove(tmp_file)
    except Exception:
        pass

    return records


def save_chapter_excel(chapter_name: str, records: list[dict]):
    if not records:
        safe_print(f"  No data, skip Excel: {chapter_name}")
        return

    # 过滤空荷载
    removed_empty = [r for r in records if r["荷载"] == '{"loads": []}']
    records = [r for r in records if r["荷载"] != '{"loads": []}']

    if removed_empty:
        safe_print(f"  Removed {len(removed_empty)} empty-load rows")

    if not records:
        safe_print(f"  All empty, skip Excel: {chapter_name}")
        return

    df = pd.DataFrame(records)
    output_path = ROOT / f"{chapter_name}.xlsx"
    df.to_excel(output_path, index=False)
    safe_print(f"  [OK] {output_path.name} ({len(records)} rows)")


def main():
    safe_print("=" * 60)
    safe_print("StructMech Load Extraction")
    safe_print(f"Root: {ROOT}")
    safe_print("=" * 60)

    if not ZHIPUAI_API_KEY:
        safe_print("\nERROR: No API Key configured")
        return

    client = ZhipuAI(api_key=ZHIPUAI_API_KEY)

    # 只处理第三章
    chapter_dir = ROOT / "3静定结构位移"
    if not chapter_dir.is_dir():
        safe_print(f"Chapter not found: {chapter_dir}")
        return

    records = process_chapter(client, chapter_dir, resume=True)
    if records:
        save_chapter_excel(chapter_dir.name, records)
        safe_print(f"\nDone! {len(records)} images -> {chapter_dir.name}.xlsx")
    else:
        safe_print("No records produced")


if __name__ == "__main__":
    main()

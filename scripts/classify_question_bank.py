"""
Classify the live structure-mechanics image bank into numeric vs symbolic rows.

This is intentionally read-only for the existing Excel indexes. It scans images
under the configured ROOT, calls DashScope Qwen for fresh load extraction, and
writes review artifacts under an output directory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from io import BytesIO
from datetime import datetime
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


BASE = Path(__file__).resolve().parent.parent
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DEFAULT_ROOT = Path(r"D:\桌面\答疑、帮做\结构力学\帮做")
DEFAULT_MODEL = "qwen3.7-plus"
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
CHAPTERS = ["2静定结构", "3静定结构位移", "4力法", "5位移法", "6力矩分配"]

SYSTEM_PROMPT = """你是结构力学题目荷载提取与分类助手。请从题目图片中识别真实外部荷载，只输出JSON。

输出格式:
{"loads":[{"type":"集中|均布|弯矩","raw":"图中原始标注"}]}

荷载类型只有三种:
- 集中: 集中力，如 10kN, P, F=20kN, F1=40kN, ql, 2P
- 均布: 分布荷载，如 20kN/m, q=20kN/m, q, 2P/a
- 弯矩: 力偶/集中弯矩，如 10kN·m, M=20kN·m, Pa, ql²

必须注意赋值:
- 如果题干或图片说明给出符号赋值，例如 P=40kN、q=20kN/m、F1=40kN、M=20kN·m，raw 必须保留赋值形式。
- 如果等号右侧仍是符号表达式，例如 F1=2ql、F2=2ql，raw 也保留完整形式，但它仍属于未赋值符号表达。
- F、P、Fp、F_P、F1、F2 等标在箭头旁的水平或竖向外力都必须提取，即使没有数值单位。

不要提取:
- 刚度 EI、EA、2EI
- 尺寸 l、L、a、b、h、4m、l/2
- 节点或杆件编号 A、B、C、1、2、3
- 支座反力 RA、RB、FA、FB、HA、VA、MA
- 公式结果 ql²/8、ql/2

无荷载输出 {"loads":[]}。不要输出解释、Markdown或代码块。"""


def load_config() -> dict:
    cfg: dict = {}
    for name in ("config.json", "config.local.json"):
        path = BASE / name
        if path.exists():
            try:
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return cfg


def configured_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("root") or DEFAULT_ROOT)


def scan_images(root: Path, chapters: list[str] | None = None) -> list[Path]:
    selected = chapters or CHAPTERS
    images: list[Path] = []
    for chapter in selected:
        chapter_dir = root / chapter
        if not chapter_dir.is_dir():
            continue
        for current, dirs, files in os.walk(chapter_dir):
            dirs[:] = [
                d for d in dirs
                if "答案" not in d and not d.startswith(".") and not d.startswith("_")
            ]
            for file_name in files:
                path = Path(current) / file_name
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    images.append(path)
    return sorted(images, key=lambda p: str(p).lower())


def image_to_data_url(path: Path, upscale_min_side: int = 900) -> str:
    mime = "image/jpeg"
    try:
        img = Image.open(path).convert("RGB")
        shortest = min(img.size)
        if shortest and shortest < upscale_min_side:
            scale = upscale_min_side / shortest
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=94)
        raw = buf.getvalue()
    except Exception:  # noqa: BLE001
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        raw = path.read_bytes()
    data = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{data}"


def qwen_extract_loads(image_path: Path, *, model: str, endpoint: str, api_key: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                    {"type": "text", "text": "只输出JSON。"},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 1024,
        "enable_thinking": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    parsed = parse_model_json(content)
    usage = data.get("usage", {})
    return {"loads": parsed.get("loads", []), "raw_content": content, "usage": usage}


def parse_model_json(content: str) -> dict:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def normalize_load_item(item: dict) -> dict:
    typ = str(item.get("type", "")).strip()
    raw = clean_raw(item.get("raw", ""))
    aliases = {
        "集中力": "集中",
        "点荷载": "集中",
        "均布荷载": "均布",
        "分布荷载": "均布",
        "分布力": "均布",
        "力偶": "弯矩",
        "集中弯矩": "弯矩",
        "集中力偶": "弯矩",
    }
    typ = aliases.get(typ, typ)
    return {"type": typ, "raw": raw}


def clean_raw(raw: object) -> str:
    text = str(raw or "").strip()
    text = text.replace("$", "")
    text = re.sub(r"\\left|\\right", "", text)
    text = text.replace("\\,", "")
    text = re.sub(r"\\mathrm\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"([A-Za-z])_\{?([0-9A-Za-z]+)\}?", r"\1\2", text)
    text = text.replace("^2", "²").replace("^{2}", "²")
    text = text.replace("^3", "³").replace("^{3}", "³")
    text = text.replace("\\cdot", "·").replace("\\times", "×")
    text = re.sub(r"\s+", "", text)
    return text


UNIT_PATTERNS = [
    r"kN[·.\-*×]?m",
    r"N[·.\-*×]?m",
    r"kN/m",
    r"N/m",
    r"kN",
    r"(?<=\d)k\b",
    r"\bN\b",
]


def strip_units(text: str) -> str:
    value = text
    for pattern in UNIT_PATTERNS:
        value = re.sub(pattern, "", value, flags=re.I)
    return value


def symbolic_residue(raw: str) -> str:
    text = clean_raw(raw)
    if "=" in text:
        left, right = text.split("=", 1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", left):
            text = right
    text = strip_units(text)
    text = re.sub(r"\d+(?:\.\d+)?", "", text)
    text = re.sub(r"[=+\-*/×·().,，:：\[\]{}]", "", text)
    text = text.replace("²", "").replace("³", "")
    return text.strip()


def has_load_unit(raw: str) -> bool:
    return any(re.search(pattern, raw, flags=re.I) for pattern in UNIT_PATTERNS)


def is_numeric_assignment(raw: str) -> bool:
    text = clean_raw(raw)
    if "=" not in text:
        return False
    left, right = text.split("=", 1)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", left):
        return False
    if not re.search(r"\d", right) or not has_load_unit(right):
        return False
    return not re.search(r"[A-Za-z]", symbolic_residue(right))


def assignment_symbol(raw: str) -> str | None:
    text = clean_raw(raw)
    if not is_numeric_assignment(text):
        return None
    left = text.split("=", 1)[0]
    return left.lower()


def symbolic_residue_letters(raw: str) -> str:
    residue = symbolic_residue(raw).lower()
    return re.sub(r"[^a-z]", "", residue)


def classify_raw(raw: str) -> str:
    text = clean_raw(raw)
    if is_numeric_assignment(text):
        return "assigned_numeric"
    residue = symbolic_residue(text)
    if residue and re.search(r"[A-Za-z]", residue):
        return "symbolic_unassigned"
    if re.search(r"\d", text) and has_load_unit(text):
        return "numeric"
    return "unknown"


def classify_loads(loads: list[dict]) -> tuple[str, list[dict]]:
    normalized = [normalize_load_item(item) for item in loads if isinstance(item, dict)]
    assigned_symbols = {
        symbol for symbol in (assignment_symbol(item["raw"]) for item in normalized) if symbol
    }
    details = []
    for item in normalized:
        load_class = classify_raw(item["raw"])
        if load_class == "symbolic_unassigned" and assigned_symbols:
            letters = symbolic_residue_letters(item["raw"])
            if letters in assigned_symbols:
                load_class = "assigned_numeric"
        details.append({**item, "load_class": load_class})

    if not details:
        return "needs_review", details

    classes = {item["load_class"] for item in details}
    if "unknown" in classes:
        return "needs_review", details
    has_symbolic = "symbolic_unassigned" in classes
    has_main = bool(classes.intersection({"numeric", "assigned_numeric"}))
    if has_symbolic and has_main:
        return "mixed_symbolic_numeric", details
    if has_symbolic:
        return "symbolic_unassigned", details
    if "assigned_numeric" in classes:
        return "main_assigned_symbolic", details
    return "main_numeric", details


def load_existing_excel_index(root: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for xlsx in sorted(root.glob("*.xlsx")):
        if xlsx.stem.startswith("_"):
            continue
        try:
            df = pd.read_excel(xlsx)
        except Exception:
            continue
        if not {"题目名称", "荷载"}.issubset(df.columns):
            continue
        for row_index, row in df.iterrows():
            rel = str(row["题目名称"]).replace("\\", "/")
            index[rel] = {
                "xlsx": xlsx.name,
                "row": row_index + 2,
                "old_loads": row["荷载"],
            }
    return index


def save_artifacts(records: list[dict], output_dir: Path, root: Path, sample_size: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "classification_results.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for r in records:
        rows.append({
            "chapter": r["chapter"],
            "category": r["category"],
            "in_main_excel": r["in_main_excel"],
            "excel_row": r.get("excel_row", ""),
            "question": r["rel_path"],
            "loads": json.dumps({"loads": r["loads"]}, ensure_ascii=False),
            "load_classes": "; ".join(f"{x['type']}:{x['raw']}->{x['load_class']}" for x in r["load_details"]),
            "error": r.get("error", ""),
        })
    df = pd.DataFrame(rows)
    xlsx_path = output_dir / "classification_results.xlsx"
    df.to_excel(xlsx_path, index=False)

    for category, part in df.groupby("category", sort=True):
        safe_name = category.replace("/", "_")
        part.to_csv(output_dir / f"{safe_name}.csv", index=False, encoding="utf-8-sig")

    save_markdown(records, output_dir, root)
    save_contact_sheet(records, output_dir, root, sample_size)


def save_markdown(records: list[dict], output_dir: Path, root: Path) -> None:
    counts: dict[str, int] = {}
    for r in records:
        counts[r["category"]] = counts.get(r["category"], 0) + 1

    lines = ["# 题库图片重新识别分类报告", ""]
    lines.append(f"- root: `{root}`")
    lines.append(f"- total_images_processed: {len(records)}")
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines.append("")
    lines.append("## Samples By Category")
    lines.append("")

    for category in sorted(counts):
        lines.append(f"### {category}")
        subset = [r for r in records if r["category"] == category][:20]
        for r in subset:
            loads = json.dumps({"loads": r["loads"]}, ensure_ascii=False)
            lines.append(f"- `{r['rel_path']}` | in_main={r['in_main_excel']} | {loads}")
        lines.append("")

    (output_dir / "classification_report.md").write_text("\n".join(lines), encoding="utf-8")


def save_contact_sheet(records: list[dict], output_dir: Path, root: Path, sample_size: int) -> None:
    if not records or sample_size <= 0:
        return
    rng = random.Random(20260623)
    selected: list[dict] = []
    categories = sorted({r["category"] for r in records})
    per_category = max(1, sample_size // max(1, len(categories)))
    for category in categories:
        subset = [r for r in records if r["category"] == category]
        selected.extend(rng.sample(subset, min(per_category, len(subset))))
    if len(selected) < sample_size:
        rest = [r for r in records if r not in selected]
        selected.extend(rng.sample(rest, min(sample_size - len(selected), len(rest))))

    thumb_w, thumb_h = 240, 170
    label_h = 82
    cols = 3
    rows = (len(selected) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for idx, record in enumerate(selected):
        x = (idx % cols) * thumb_w
        y = (idx // cols) * (thumb_h + label_h)
        img_path = root / record["rel_path"]
        try:
            img = Image.open(img_path).convert("RGB")
            img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
            sheet.paste(img, (x + (thumb_w - img.width) // 2, y))
        except Exception as exc:  # noqa: BLE001
            draw.text((x + 4, y + 4), f"image error: {exc}", fill=(180, 0, 0), font=font)

        loads = "; ".join(f"{i['type']}:{i['raw']}" for i in record["load_details"])
        label = f"{idx+1}. {record['category']}\n{record['rel_path']}\n{loads[:90]}"
        draw.multiline_text((x + 4, y + thumb_h + 4), label, fill=(0, 0, 0), font=font, spacing=2)

    sheet.save(output_dir / "sample_contact_sheet.jpg", quality=92)


def classify_images(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else configured_root()
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY is not set", file=sys.stderr)
        return 2

    if args.image:
        images = []
        for raw_image in args.image:
            p = Path(raw_image)
            if not p.is_absolute():
                p = root / raw_image
            images.append(p)
    else:
        chapters = args.chapter or CHAPTERS
        images = scan_images(root, chapters)
        if args.only_missing or args.only_existing:
            existing = load_existing_excel_index(root)
            if args.only_missing:
                images = [p for p in images if str(p.relative_to(root)).replace("\\", "/") not in existing]
            if args.only_existing:
                images = [p for p in images if str(p.relative_to(root)).replace("\\", "/") in existing]
    if args.shuffle:
        random.Random(args.seed).shuffle(images)
    if args.limit:
        images = images[:args.limit]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else BASE / ".tmp_symbol_sheets" / f"classify_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    working_path = output_dir / "classification_working.json"

    existing_index = load_existing_excel_index(root)
    records: list[dict] = []
    if args.resume and working_path.exists():
        records = json.loads(working_path.read_text(encoding="utf-8"))
        for record in records:
            category, load_details = classify_loads(record.get("loads", []))
            if record.get("error"):
                category = "needs_review"
            record["category"] = category
            record["load_details"] = load_details
        working_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    done = {r["rel_path"] for r in records}
    processed_new = 0

    print(f"root={root}")
    print(f"images={len(images)}")
    print(f"output_dir={output_dir}")
    print(f"model={args.model}")

    for index, image_path in enumerate(images, 1):
        rel_path = str(image_path.relative_to(root)).replace("\\", "/")
        if rel_path in done:
            continue
        if args.max_new and processed_new >= args.max_new:
            break
        started = time.time()
        error = ""
        loads: list[dict] = []
        raw_content = ""
        try:
            extracted = qwen_extract_loads(
                image_path,
                model=args.model,
                endpoint=args.endpoint,
                api_key=api_key,
                timeout=args.timeout,
            )
            loads = extracted["loads"]
            raw_content = extracted.get("raw_content", "")
        except Exception as exc:  # noqa: BLE001 - keep batch jobs running on per-image failures.
            error = str(exc)
        category, load_details = classify_loads(loads)
        if error:
            category = "needs_review"

        chapter = rel_path.split("/", 1)[0]
        old = existing_index.get(rel_path, {})
        record = {
            "chapter": chapter,
            "rel_path": rel_path,
            "category": category,
            "loads": [normalize_load_item(item) for item in loads if isinstance(item, dict)],
            "load_details": load_details,
            "in_main_excel": rel_path in existing_index,
            "excel_file": old.get("xlsx", ""),
            "excel_row": old.get("row", ""),
            "old_loads": old.get("old_loads", ""),
            "seconds": round(time.time() - started, 2),
            "error": error,
            "raw_content": raw_content,
        }
        records.append(record)
        done.add(rel_path)
        processed_new += 1

        print(
            f"[{index}/{len(images)}] {category} | {rel_path} | "
            f"{json.dumps({'loads': record['loads']}, ensure_ascii=False)} | {record['seconds']}s"
        )
        working_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.sleep:
            time.sleep(args.sleep)

    save_artifacts(records, output_dir, root, args.sample_size)
    print(f"done={len(records)}")
    print(f"report={output_dir / 'classification_report.md'}")
    print(f"xlsx={output_dir / 'classification_results.xlsx'}")
    print(f"contact_sheet={output_dir / 'sample_contact_sheet.jpg'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen classify full question-bank images without editing live Excel.")
    parser.add_argument("--root", help="question bank root; defaults to config root")
    parser.add_argument("--chapter", action="append", help="chapter to scan; may be repeated")
    parser.add_argument("--image", action="append", help="single image path to classify; may be repeated")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output-dir", help="directory for reports")
    parser.add_argument("--limit", type=int, default=0, help="only process first N images after filtering")
    parser.add_argument("--max-new", type=int, default=0, help="process at most N images not already in the working file")
    parser.add_argument("--shuffle", action="store_true", help="shuffle image order before applying --limit")
    parser.add_argument("--seed", type=int, default=20260623)
    parser.add_argument("--only-missing", action="store_true", help="only classify images not present in live Excel")
    parser.add_argument("--only-existing", action="store_true", help="only classify images already present in live Excel")
    parser.add_argument("--resume", action="store_true", help="resume from classification_working.json in output dir")
    parser.add_argument("--sleep", type=float, default=0.0, help="sleep seconds between API calls")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sample-size", type=int, default=12, help="number of images in contact sheet")
    args = parser.parse_args()
    return classify_images(args)


if __name__ == "__main__":
    raise SystemExit(main())

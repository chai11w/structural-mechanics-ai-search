"""Prepare and render a reproducible v4/v5/manual dimension comparison.

Generated manifests, contact sheets, model outputs, and HTML reports belong in
an ignored experiment directory. This script never writes the live question bank.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageFont, ImageOps


DEFAULT_QUOTAS = {"梁": 15, "钢架": 20, "桁架": 15}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _chapter(path: str) -> str:
    return str(path).replace("\\", "/").split("/", 1)[0]


def _stable_order(path: str) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _human_paths(payload: Mapping[str, Any]) -> set[str]:
    return {
        str(item.get("path") or "").replace("\\", "/").strip()
        for item in payload.get("verdicts", [])
        if isinstance(item, Mapping) and item.get("path")
    }


def select_samples(
    v4_rows: Iterable[Mapping[str, Any]],
    human_paths: set[str],
    quotas: Mapping[str, int] | None = None,
) -> list[dict[str, str]]:
    """Select hard cases first, then fill each structure type across chapters."""

    wanted = dict(quotas or DEFAULT_QUOTAS)
    rows_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in v4_rows:
        row = dict(raw)
        structure_type = str(row.get("expected_structure_type") or "").strip()
        path = str(row.get("path") or "").replace("\\", "/").strip()
        if structure_type in wanted and path:
            row["path"] = path
            rows_by_type[structure_type].append(row)

    selected: list[dict[str, str]] = []
    for structure_type, quota in wanted.items():
        available = rows_by_type.get(structure_type, [])
        if len(available) < quota:
            raise ValueError(f"not enough {structure_type} samples: {len(available)} < {quota}")

        chosen: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(row: dict[str, Any]) -> None:
            path = str(row["path"])
            if path not in seen and len(chosen) < quota:
                seen.add(path)
                chosen.append(row)

        priority = sorted(
            available,
            key=lambda row: (
                0 if str(row["path"]) in human_paths else 1,
                0 if not bool((row.get("normalized") or {}).get("dimensions_verified")) else 1,
                _stable_order(str(row["path"])),
            ),
        )
        for row in priority:
            if str(row["path"]) in human_paths or not bool(
                (row.get("normalized") or {}).get("dimensions_verified")
            ):
                add(row)

        remaining_by_chapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in available:
            if str(row["path"]) not in seen:
                remaining_by_chapter[_chapter(str(row["path"]))].append(row)
        for rows in remaining_by_chapter.values():
            rows.sort(key=lambda row: _stable_order(str(row["path"])))

        chapters = sorted(remaining_by_chapter)
        while len(chosen) < quota:
            progressed = False
            for chapter in chapters:
                rows = remaining_by_chapter[chapter]
                if rows and len(chosen) < quota:
                    add(rows.pop(0))
                    progressed = True
            if not progressed:
                raise RuntimeError(f"could not fill {structure_type} quota")

        for row in chosen:
            selected.append(
                {
                    "id": hashlib.sha1(str(row["path"]).encode("utf-8")).hexdigest()[:12],
                    "expected_structure_type": structure_type,
                    "path": str(row["path"]),
                    "selection_note": (
                        "human_verdict"
                        if str(row["path"]) in human_paths
                        else "v4_unverified"
                        if not bool((row.get("normalized") or {}).get("dimensions_verified"))
                        else "chapter_diversity_fill"
                    ),
                }
            )
    return selected


def write_manifest(path: Path, samples: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "description": "Fixed 50-image v4/v5/manual structural-dimension comparison.",
                "samples": samples,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ):
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def write_contact_sheets(
    manifest: Mapping[str, Any],
    *,
    root: Path,
    output_dir: Path,
    per_sheet: int = 4,
) -> list[Path]:
    samples = list(manifest.get("samples") or [])
    output_dir.mkdir(parents=True, exist_ok=True)
    label_font = _font(34)
    meta_font = _font(24)
    sheet_paths: list[Path] = []
    cell_width, cell_height = 1600, 1150
    columns = 2
    rows_per_sheet = math.ceil(per_sheet / columns)
    for sheet_index, start in enumerate(range(0, len(samples), per_sheet), 1):
        sheet = Image.new("RGB", (cell_width * columns, cell_height * rows_per_sheet), "white")
        draw = ImageDraw.Draw(sheet)
        for offset, sample in enumerate(samples[start : start + per_sheet]):
            source = root / str(sample["path"])
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((cell_width - 40, cell_height - 130), Image.Resampling.LANCZOS)
            column, row = offset % columns, offset // columns
            x, y = column * cell_width, row * cell_height
            image_x = x + (cell_width - image.width) // 2
            image_y = y + 90 + (cell_height - 110 - image.height) // 2
            sheet.paste(image, (image_x, image_y))
            number = start + offset + 1
            draw.text((x + 18, y + 14), f"#{number:02d}  {sample['expected_structure_type']}", fill="black", font=label_font)
            draw.text((x + 18, y + 55), str(sample["id"]), fill="#475569", font=meta_font)
            draw.rectangle((x, y, x + cell_width - 1, y + cell_height - 1), outline="#94a3b8", width=3)
        output_path = output_dir / f"sheet_{sheet_index:02d}.jpg"
        sheet.save(output_path, format="JPEG", quality=92)
        sheet_paths.append(output_path)
    return sheet_paths


def _result_map(payload: Mapping[str, Any], path_key: str) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get(path_key) or row.get("relative_path") or "").replace("\\", "/"): row
        for row in payload.get("results", [])
        if isinstance(row, Mapping)
    }


def _manual_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("path") or "").replace("\\", "/"): row
        for row in payload.get("verdicts", [])
        if isinstance(row, Mapping)
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, math.floor((len(ordered) - 1) * fraction))]


def write_disagreement_report(
    *,
    manifest: Mapping[str, Any],
    v4_payload: Mapping[str, Any],
    v5_payload: Mapping[str, Any],
    manual_payload: Mapping[str, Any],
    root: Path,
    output_path: Path,
) -> dict[str, Any]:
    samples = list(manifest.get("samples") or [])
    v4_by_path = _result_map(v4_payload, "path")
    v5_by_path = _result_map(v5_payload, "path")
    manual_by_path = _manual_map(manual_payload)
    image_dir = output_path.parent / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    times: list[float] = []
    v4_manual_matches = 0
    v5_manual_matches = 0
    for index, sample in enumerate(samples, 1):
        path = str(sample["path"]).replace("\\", "/")
        v4 = v4_by_path.get(path, {})
        v5 = v5_by_path.get(path, {})
        manual = manual_by_path.get(path, {})
        if not manual.get("long_width"):
            raise ValueError(f"missing manual verdict for sample #{index}: {path}")
        v4_norm = v4.get("normalized") or {}
        v5_result = v5.get("qwen") if isinstance(v5.get("qwen"), Mapping) else v5
        v5_norm = (v5_result or {}).get("normalized") or {}
        seconds = float((v5_result or {}).get("seconds") or v5.get("seconds") or 0.0)
        times.append(seconds)
        values = (
            str(v4_norm.get("long_width") or "unknown"),
            str(v5_norm.get("long_width") or "unknown"),
            str(manual.get("long_width") or "unknown"),
        )
        v4_manual_matches += values[0] == values[2]
        v5_manual_matches += values[1] == values[2]
        if len(set(values)) == 1:
            continue
        source = root / path
        target_name = f"{index:02d}_{sample['id']}{source.suffix.lower() or '.jpg'}"
        shutil.copy2(source, image_dir / target_name)
        rows.append(
            {
                "index": index,
                "sample": sample,
                "image": f"images/{target_name}",
                "v4": v4_norm,
                "v5": v5_norm,
                "manual": manual,
                "seconds": seconds,
                "values": values,
            }
        )

    type_counts = Counter(str(sample.get("expected_structure_type") or "") for sample in samples)
    summary = {
        "total": len(samples),
        "disagreements": len(rows),
        "agreements": len(samples) - len(rows),
        "type_counts": dict(type_counts),
        "v4_manual_matches": v4_manual_matches,
        "v5_manual_matches": v5_manual_matches,
        "v5_seconds_mean": round(sum(times) / len(times), 3) if times else 0.0,
        "v5_seconds_p50": round(_percentile(times, 0.50), 3),
        "v5_seconds_p90": round(_percentile(times, 0.90), 3),
        "v5_seconds_p95": round(_percentile(times, 0.95), 3),
        "v5_seconds_max": round(max(times), 3) if times else 0.0,
    }

    cards: list[str] = []
    for row in rows:
        sample = row["sample"]
        manual = row["manual"]
        cards.append(
            f"""
<article class="card">
  <header><h2>#{row['index']:02d} · {html.escape(str(sample['expected_structure_type']))}</h2><span>{row['seconds']:.3f}s</span></header>
  <p class="path">{html.escape(str(sample['path']))}</p>
  <div class="content">
    <figure><img src="{html.escape(row['image'])}" alt="题图 #{row['index']:02d}"></figure>
    <div class="results">
      <section><h3>v4</h3><strong>{html.escape(row['values'][0])}</strong><p>水平：{html.escape(str(row['v4'].get('horizontal_segments') or []))}</p><p>竖直：{html.escape(str(row['v4'].get('vertical_segments') or []))}</p></section>
      <section><h3>v5</h3><strong>{html.escape(row['values'][1])}</strong><p>水平：{html.escape(str(row['v5'].get('horizontal_segments') or []))}</p><p>竖直：{html.escape(str(row['v5'].get('vertical_segments') or []))}</p></section>
      <section class="manual"><h3>人工判断</h3><strong>{html.escape(row['values'][2])}</strong><p>{html.escape(str(manual.get('reason') or ''))}</p></section>
    </div>
  </div>
</article>"""
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>尺寸 Prompt v4 / v5 / 人工不一致对比</title>
<style>body{{margin:0;background:#f1f5f9;color:#172033;font:16px/1.5 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1500px;margin:auto;padding:28px}}h1{{margin:0 0 8px}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:22px 0}}.metric,.card{{background:#fff;border:1px solid #dbe3ee;border-radius:14px;box-shadow:0 2px 8px #0f172a0b}}.metric{{padding:16px}}.metric strong{{display:block;font-size:28px}}.card{{padding:20px;margin:20px 0}}header{{display:flex;justify-content:space-between;align-items:center}}h2{{margin:0}}.path{{color:#64748b;word-break:break-all}}.content{{display:grid;grid-template-columns:minmax(420px,1.2fr) minmax(520px,1fr);gap:20px}}figure{{margin:0;border:1px solid #e2e8f0;background:#fff}}img{{display:block;width:100%;max-height:820px;object-fit:contain}}.results{{display:grid;gap:12px}}section{{padding:14px;border:1px solid #dbe3ee;border-radius:10px;background:#f8fafc}}section h3{{margin:0 0 6px}}section strong{{font-size:24px}}section p{{margin:6px 0;word-break:break-word}}.manual{{border-color:#f59e0b;background:#fffbeb}}@media(max-width:900px){{.content{{grid-template-columns:1fr}}}}</style></head><body><main>
<h1>尺寸 Prompt 三方不一致对比</h1><p>固定50题全部参与统计；下方只展开 v4、v5、人工判断不完全一致的题目。</p>
<div class="summary"><div class="metric">样本<strong>{summary['total']}</strong></div><div class="metric">完全一致<strong>{summary['agreements']}</strong></div><div class="metric">不一致<strong>{summary['disagreements']}</strong></div><div class="metric">V4 对人工<strong>{summary['v4_manual_matches']} / {summary['total']}</strong></div><div class="metric">V5 对人工<strong>{summary['v5_manual_matches']} / {summary['total']}</strong></div><div class="metric">类型<strong>梁 {type_counts['梁']} / 钢架 {type_counts['钢架']} / 桁架 {type_counts['桁架']}</strong></div><div class="metric">v5平均<strong>{summary['v5_seconds_mean']}s</strong></div><div class="metric">v5 P50 / P95<strong>{summary['v5_seconds_p50']}s / {summary['v5_seconds_p95']}s</strong></div><div class="metric">v5 P90 / 最大<strong>{summary['v5_seconds_p90']}s / {summary['v5_seconds_max']}s</strong></div></div>
{''.join(cards) if cards else '<p>三方结果全部一致。</p>'}
</main></body></html>""",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser("select")
    select.add_argument("--v4-results", type=Path, required=True)
    select.add_argument("--human-verdicts", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)

    sheets = subparsers.add_parser("contact-sheets")
    sheets.add_argument("--manifest", type=Path, required=True)
    sheets.add_argument("--root", type=Path, required=True)
    sheets.add_argument("--output-dir", type=Path, required=True)
    sheets.add_argument("--per-sheet", type=int, default=4)

    report = subparsers.add_parser("report")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--v4-results", type=Path, required=True)
    report.add_argument("--v5-results", type=Path, required=True)
    report.add_argument("--manual-verdicts", type=Path, required=True)
    report.add_argument("--root", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "select":
        v4 = load_json(args.v4_results)
        samples = select_samples(
            v4.get("results") or [],
            _human_paths(load_json(args.human_verdicts)),
        )
        write_manifest(args.output, samples)
        print(json.dumps(Counter(sample["expected_structure_type"] for sample in samples), ensure_ascii=False))
        print(args.output)
        return 0
    if args.command == "contact-sheets":
        paths = write_contact_sheets(
            load_json(args.manifest),
            root=args.root,
            output_dir=args.output_dir,
            per_sheet=args.per_sheet,
        )
        for path in paths:
            print(path)
        return 0
    summary = write_disagreement_report(
        manifest=load_json(args.manifest),
        v4_payload=load_json(args.v4_results),
        v5_payload=load_json(args.v5_results),
        manual_payload=load_json(args.manual_verdicts),
        root=args.root,
        output_path=args.output,
    )
    print(json.dumps(summary, ensure_ascii=False))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

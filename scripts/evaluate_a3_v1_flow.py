"""Run the production A3-V1 grounding and validation flow over local images."""

from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.run_tiku_agent_8897 import build_runtime


DEFAULT_IMAGES_DIR = Path(r"D:\桌面\A3")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the production A3-V1 page crop and Qwen validation flow",
    )
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--grounding-timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Run page understanding and GLM crop only, without Qwen crop gates",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    images_dir = args.images_dir.resolve()
    images = sorted(
        (
            path
            for path in images_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=lambda path: path.name.lower(),
    )
    if not images:
        raise SystemExit(f"no images found: {images_dir}")
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else (BASE / "output" / f"a3_v1_flow_{datetime.now():%Y%m%d_%H%M%S}").resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(
        output_dir / "runtime",
        model_timeout_seconds=args.model_timeout_seconds,
        grounding_timeout_seconds=args.grounding_timeout_seconds,
        enable_triage=False,
    )
    records: list[dict[str, Any]] = []
    for index, image_path in enumerate(images, 1):
        session_id = f"a3-v1-eval-{index:03d}-{image_path.stem}"
        case_dir = output_dir / "cases" / f"{index:02d}_{_safe_name(image_path.stem)}"
        case_dir.mkdir(parents=True, exist_ok=True)
        staged_source = case_dir / f"source{image_path.suffix.lower()}"
        shutil.copy2(image_path, staged_source)
        started = time.perf_counter()
        progress_events: list[dict[str, str]] = []

        def progress(stage: str, message: str) -> None:
            progress_events.append({"stage": str(stage), "message": str(message)})
            print(f"[{index}/{len(images)}] {image_path.name} {stage}: {message}", flush=True)

        try:
            response = runtime.handle_image(session_id, image_path, progress=progress)
            before = runtime.store.load(session_id)
            if before is None:
                raise RuntimeError("A3 session was not persisted")
            requested = [str(unit["unit_id"]) for unit in before.remaining_units]
            prepare_intent = "skipped"
            if requested and not args.skip_validation:
                prepared = runtime.prepare_units(
                    session_id,
                    requested,
                    task_revision=before.task_revision,
                    progress=progress,
                )
                prepare_intent = prepared.intent
            state = runtime.store.load(session_id)
            if state is None:
                raise RuntimeError("A3 session expired during evaluation")
            artifacts = _stage_artifacts(state.auto_crop_overlay_path, state.auto_crops, case_dir)
            record = {
                "image": image_path.name,
                "status": "completed",
                "page_intent": response.intent,
                "prepare_intent": prepare_intent,
                "seconds": round(time.perf_counter() - started, 3),
                "unit_count": len(state.searchable_units),
                "page_status": str(state.auto_crop_page.get("page_status") or ""),
                "units": [
                    _unit_record(unit, state.auto_crops.get(str(unit["unit_id"])) or {})
                    for unit in state.searchable_units
                ],
                "progress": progress_events,
                "artifacts": artifacts,
            }
        except Exception as exc:  # noqa: BLE001 - preserve the paid batch.
            record = {
                "image": image_path.name,
                "status": "error",
                "error_type": type(exc).__name__,
                "seconds": round(time.perf_counter() - started, 3),
                "unit_count": 0,
                "page_status": "",
                "units": [],
                "progress": progress_events,
                "artifacts": {"source": str(staged_source)},
            }
            print(f"[{index}/{len(images)}] {image_path.name} ERROR {type(exc).__name__}", flush=True)
        _write_json(case_dir / "result.json", record)
        records.append(record)

    summary = _build_summary(images_dir, records)
    _write_json(output_dir / "summary.json", summary)
    _write_review_html(output_dir, records, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"review={output_dir / 'review.html'}")
    return 0 if summary["errors"] == 0 else 1


def _unit_record(unit: Mapping[str, Any], crop: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "unit_id": str(unit.get("unit_id") or ""),
        "display_label": str(unit.get("display_label") or ""),
        "target_id": str(crop.get("target_id") or ""),
        "bbox": crop.get("bbox"),
        "grounding_status": str(crop.get("grounding_status") or ""),
        "validation_status": str(crop.get("validation_status") or ""),
        "external_load_status": str(crop.get("external_load_status") or ""),
        "verification_checks": dict(crop.get("verification_checks") or {}),
        "reason_codes": list(crop.get("reason_codes") or []),
        "binding_evidence": str(crop.get("binding_evidence") or ""),
        "error_type": str(crop.get("error_type") or ""),
    }


def _stage_artifacts(
    overlay_path: str,
    crops: Mapping[str, Mapping[str, Any]],
    case_dir: Path,
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    if overlay_path and Path(overlay_path).is_file():
        overlay = case_dir / "overlay.jpg"
        shutil.copy2(overlay_path, overlay)
        artifacts["overlay"] = str(overlay)
    staged_crops: dict[str, str] = {}
    crops_dir = case_dir / "crops"
    for index, (unit_id, record) in enumerate(crops.items(), 1):
        source = Path(str(record.get("path") or ""))
        if not source.is_file():
            continue
        crops_dir.mkdir(parents=True, exist_ok=True)
        target = crops_dir / f"{index:02d}_{_safe_name(unit_id)}.jpg"
        shutil.copy2(source, target)
        staged_crops[str(unit_id)] = str(target)
    artifacts["crops"] = staged_crops
    return artifacts


def _build_summary(images_dir: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    units = [unit for record in records for unit in record.get("units", [])]
    return {
        "schema_version": "a3-v1-production-eval-v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "images_dir": str(images_dir),
        "images": len(records),
        "completed": sum(record.get("status") == "completed" for record in records),
        "errors": sum(record.get("status") == "error" for record in records),
        "units": len(units),
        "grounded_auto_ready": sum(unit.get("grounding_status") == "auto_ready" for unit in units),
        "grounded_manual": sum(unit.get("grounding_status") != "auto_ready" for unit in units),
        "validated_auto_ready": sum(unit.get("validation_status") == "auto_ready" for unit in units),
        "validated_manual": sum(unit.get("validation_status") == "manual_required" for unit in units),
        "seconds": round(sum(float(record.get("seconds") or 0) for record in records), 3),
    }


def _write_review_html(
    output_dir: Path,
    records: list[dict[str, Any]],
    summary: Mapping[str, Any],
) -> None:
    rows = []
    for index, record in enumerate(records, 1):
        case_dir = output_dir / "cases" / f"{index:02d}_{_safe_name(Path(record['image']).stem)}"
        source_candidates = list(case_dir.glob("source.*"))
        source_url = _relative_url(source_candidates[0], output_dir) if source_candidates else ""
        overlay = Path(str(record.get("artifacts", {}).get("overlay") or ""))
        overlay_url = _relative_url(overlay, output_dir) if overlay.is_file() else ""
        crop_paths = record.get("artifacts", {}).get("crops", {})
        unit_html = []
        for unit in record.get("units", []):
            crop_path = Path(str(crop_paths.get(unit.get("unit_id"), "")))
            crop_image = (
                f'<a href="{escape(_relative_url(crop_path, output_dir))}"><img src="{escape(_relative_url(crop_path, output_dir))}" loading="lazy"></a>'
                if crop_path.is_file()
                else '<div class="missing">无可靠裁图</div>'
            )
            checks = unit.get("verification_checks") or {}
            checks_text = ", ".join(f"{key}={value}" for key, value in checks.items())
            unit_html.append(
                '<article class="unit">'
                f'{crop_image}<strong>{escape(str(unit.get("display_label") or unit.get("unit_id") or "未标号题"))}</strong>'
                f'<span>GLM {escape(str(unit.get("grounding_status") or ""))} · Qwen {escape(str(unit.get("validation_status") or ""))} · load {escape(str(unit.get("external_load_status") or ""))}</span>'
                f'<small>{escape(checks_text or str(unit.get("error_type") or ""))}</small>'
                '</article>'
            )
        source_html = f'<a href="{escape(source_url)}"><img src="{escape(source_url)}" loading="lazy"></a>' if source_url else ""
        overlay_html = f'<a href="{escape(overlay_url)}"><img src="{escape(overlay_url)}" loading="lazy"></a>' if overlay_url else '<div class="missing">无叠加图</div>'
        rows.append(
            '<section class="case">'
            f'<header><h2>{escape(record["image"])}</h2><span>{escape(str(record.get("page_status") or record.get("error_type") or ""))} · {float(record.get("seconds") or 0):.1f}s</span></header>'
            f'<div class="overview"><div>{source_html}<b>原图</b></div><div>{overlay_html}<b>GLM 标签框</b></div></div>'
            f'<div class="units">{"".join(unit_html)}</div>'
            '</section>'
        )
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A3-V1 production review</title><style>
body{{margin:0;background:#f3f3f0;color:#222;font:13px/1.45 Arial,sans-serif}}header.top{{position:sticky;top:0;z-index:3;padding:14px 20px;border-bottom:1px solid #ddd;background:#fff}}header.top h1{{margin:0;font-size:18px;letter-spacing:0}}header.top p{{margin:5px 0 0;color:#666}}main{{max-width:1280px;margin:auto;padding:18px}}.case{{margin-bottom:18px;padding:14px;border:1px solid #ddd;border-radius:8px;background:#fff}}.case>header{{display:flex;justify-content:space-between;gap:12px;align-items:baseline}}h2{{margin:0 0 12px;font-size:15px;letter-spacing:0}}.overview{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.overview>div{{min-width:0}}img{{display:block;width:100%;height:270px;object-fit:contain;background:#fafafa;border:1px solid #e2e2de}}b{{display:block;margin-top:4px;font-size:11px;color:#666}}.units{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:9px;margin-top:12px}}.unit{{min-width:0;padding:8px;border:1px solid #e0e0dc;border-radius:7px}}.unit img{{height:130px}}.unit strong,.unit span,.unit small{{display:block;margin-top:5px;overflow-wrap:anywhere}}.unit span{{color:#555;font-size:11px}}.unit small{{color:#777;font-size:10px}}.missing{{height:130px;display:grid;place-items:center;background:#f2f2ef;color:#777}}@media(max-width:700px){{main{{padding:8px}}.overview{{grid-template-columns:1fr}}img{{height:220px}}}}
</style></head><body><header class="top"><h1>A3-V1 生产流程复核</h1><p>8 图 · units {int(summary['units'])} · GLM auto {int(summary['grounded_auto_ready'])} · 最终 auto {int(summary['validated_auto_ready'])} · errors {int(summary['errors'])}</p></header><main>{''.join(rows)}</main></body></html>"""
    (output_dir / "review.html").write_text(html, encoding="utf-8")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _relative_url(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def _safe_name(value: str) -> str:
    clean = "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
    return clean[:80] or "item"


if __name__ == "__main__":
    raise SystemExit(main())

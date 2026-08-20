"""Run the isolated A3-V1 crop comparison over a directory of images."""

from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from PIL import Image, ImageOps

from scripts.classify_question_bank import parse_model_json
from tiku_agent.a3_crop_strategy_eval import (
    build_direct_grounding_prompt,
    build_paddle_binding_prompt,
    call_qwen_json,
    export_normalized_boxes,
    parse_direct_grounding,
    parse_paddle_binding,
    render_paddle_candidate_overlay,
    write_json,
)
from tiku_agent.a3_region_cropper import crop_a3_regions, write_crop_manifest
from tiku_agent.image_region_mapper import (
    QwenA3RegionObserver,
    assess_a3_region_map,
    parse_a3_region_map,
    render_a3_region_overlay,
)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
STRATEGIES = {
    "1": "01_qwen37_opencv",
    "2": "02_ppstructure_qwen37",
    "3a": "03a_qwen37_grounding",
    "3b": "03b_qwen38_grounding",
}
PRICE_CNY_PER_MILLION_TOKENS = {
    "qwen3.7-plus": {"input": 1.6, "output": 6.4},
    "qwen3.8-max": {"input": 12.0, "output": 36.0},
}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare four isolated A3 automatic-crop strategies.")
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=BASE / "experiments" / "complex_image_eval" / "images",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=tuple(STRATEGIES),
        default=list(STRATEGIES),
    )
    parser.add_argument("--qwen37-model", default="qwen3.7-plus")
    parser.add_argument("--qwen38-model", default="qwen3.8-max")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--paddle-python",
        type=Path,
        default=BASE / ".tmp_a3_ppstructure_env" / "Scripts" / "python.exe",
    )
    parser.add_argument("--skip-paddle-extraction", action="store_true")
    parser.add_argument("--skip-completed", action="store_true")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    images_dir = args.images_dir.resolve(strict=True)
    images = sorted(
        path for path in images_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not images:
        raise SystemExit("no evaluation images found")
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else (BASE / "output" / f"a3_crop_eval_{datetime.now():%Y%m%d_%H%M%S}").resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        output_dir / "run_config.json",
        {
            "schema_version": "a3-crop-eval-run-v1",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "images_dir": str(images_dir),
            "image_count": len(images),
            "strategies": args.strategies,
            "models": {"qwen37": args.qwen37_model, "qwen38": args.qwen38_model},
            "pricing_cny_per_million_tokens": PRICE_CNY_PER_MILLION_TOKENS,
            "pricing_note": "Run-time estimate only; supplier billing is authoritative.",
            "timeout_seconds": args.timeout_seconds,
            "automatic_retries": 0,
        },
    )

    if "2" in args.strategies and not args.skip_paddle_extraction:
        _run_paddle_extraction(args.paddle_python, images_dir, output_dir / "paddle_layout")

    records: list[dict[str, Any]] = []
    for strategy in args.strategies:
        for image_path in images:
            strategy_dir = output_dir / STRATEGIES[strategy] / image_path.stem
            result_path = strategy_dir / "result.json"
            if args.skip_completed and result_path.exists():
                previous = json.loads(result_path.read_text(encoding="utf-8"))
                if previous.get("status") == "completed":
                    records.append(previous)
                    continue
            started = time.perf_counter()
            try:
                if strategy == "1":
                    record = _run_strategy_1(
                        image_path,
                        strategy_dir,
                        model=args.qwen37_model,
                        timeout_seconds=args.timeout_seconds,
                    )
                elif strategy == "2":
                    record = _run_strategy_2(
                        image_path,
                        strategy_dir,
                        paddle_json=output_dir / "paddle_layout" / f"{image_path.stem}.json",
                        model=args.qwen37_model,
                        timeout_seconds=args.timeout_seconds,
                    )
                elif strategy == "3a":
                    record = _run_direct_grounding(
                        image_path,
                        strategy_dir,
                        strategy="3a",
                        model=args.qwen37_model,
                        timeout_seconds=args.timeout_seconds,
                    )
                else:
                    record = _run_direct_grounding(
                        image_path,
                        strategy_dir,
                        strategy="3b",
                        model=args.qwen38_model,
                        timeout_seconds=args.timeout_seconds,
                    )
            except Exception as exc:  # noqa: BLE001 - preserve the rest of the paid evaluation batch.
                record = _base_record(strategy, image_path)
                record.update(
                    {
                        "status": "failed",
                        "page_status": "failed",
                        "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                        "usage": _empty_usage(),
                        "artifacts": {},
                    }
                )
            record["seconds"] = round(time.perf_counter() - started, 3)
            strategy_dir.mkdir(parents=True, exist_ok=True)
            write_json(result_path, record)
            records.append(record)
            write_json(output_dir / "summary.json", _build_summary(records, output_dir, images))
            _write_review_html(output_dir, records, images)
            print(
                json.dumps(
                    {
                        "strategy": strategy,
                        "sample": image_path.stem,
                        "status": record["status"],
                        "page_status": record.get("page_status", ""),
                        "seconds": record["seconds"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    summary = _build_summary(records, output_dir, images)
    write_json(output_dir / "summary.json", summary)
    _write_review_html(output_dir, records, images)
    print(json.dumps({"output_dir": str(output_dir), **summary["totals"]}, ensure_ascii=False, indent=2))
    return 0 if summary["totals"]["failed"] == 0 else 2


def _run_strategy_1(
    image_path: Path,
    output_dir: Path,
    *,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = QwenA3RegionObserver(model=model, timeout_seconds=timeout_seconds).observe(image_path)
    raw_path = output_dir / "raw_model_response.txt"
    raw_path.write_text(response.raw_text, encoding="utf-8")
    with Image.open(image_path) as opened:
        image_size = ImageOps.exif_transpose(opened).size
    observation = parse_a3_region_map(parse_model_json(response.raw_text), image_size=image_size)
    normalized_path = write_json(output_dir / "normalized.json", observation.to_dict())
    region_overlay = render_a3_region_overlay(image_path, observation, output_dir / "region_overlay.jpg")
    crops = crop_a3_regions(image_path, observation, output_dir / "crops")
    crop_manifest = write_crop_manifest(crops, output_dir / "crops" / "crop_manifest.json")
    crop_overlay = output_dir / "crops" / "a3_region_crop_overlay.jpg"
    reasons = assess_a3_region_map(observation)
    return {
        **_base_record("1", image_path),
        "status": "completed",
        "model": model,
        "page_status": "ready" if not reasons else "review_required",
        "reason_codes": list(reasons),
        "crop_count": len(crops),
        "usage": {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
        },
        "artifacts": {
            "raw_model_response": str(raw_path),
            "normalized": str(normalized_path),
            "region_overlay": str(region_overlay),
            "overlay": str(crop_overlay),
            "crop_manifest": str(crop_manifest),
            "crops": [str(item.crop_path) for item in crops],
        },
    }


def _run_strategy_2(
    image_path: Path,
    output_dir: Path,
    *,
    paddle_json: Path,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not paddle_json.exists():
        raise FileNotFoundError(f"missing PP-StructureV3 result: {paddle_json}")
    output_dir.mkdir(parents=True, exist_ok=True)
    paddle_payload = json.loads(paddle_json.read_text(encoding="utf-8"))
    candidates = paddle_payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("PP-StructureV3 candidate list is invalid")
    candidate_json = write_json(output_dir / "paddle_candidates.json", paddle_payload)
    candidate_overlay = render_paddle_candidate_overlay(
        image_path, candidates, output_dir / "paddle_candidate_overlay.jpg"
    )
    if not candidates:
        normalized = {
            "schema_version": "a3-paddle-binding-v1",
            "page_status": "no_searchable_target",
            "bindings": [],
            "unknowns": ["ppstructurev3_no_visual_candidates"],
        }
        normalized_path = write_json(output_dir / "normalized.json", normalized)
        artifacts = export_normalized_boxes(
            image_path, [], output_dir / "crops", id_field="binding_id", normalized_1000=False
        )
        return {
            **_base_record("2", image_path),
            "status": "completed",
            "model": model,
            "page_status": normalized["page_status"],
            "reason_codes": normalized["unknowns"],
            "crop_count": 0,
            "usage": _empty_usage(),
            "artifacts": {
                "paddle_candidates": str(candidate_json),
                "candidate_overlay": str(candidate_overlay),
                "normalized": str(normalized_path),
                **artifacts,
            },
        }
    response = call_qwen_json(
        [image_path, candidate_overlay],
        prompt=build_paddle_binding_prompt(candidates),
        model=model,
        timeout_seconds=timeout_seconds,
        call_type="qwen_a3_paddle_binding_eval",
    )
    raw_path = output_dir / "raw_model_response.txt"
    raw_path.write_text(response.raw_text, encoding="utf-8")
    normalized = parse_paddle_binding(response.payload, candidates)
    normalized_path = write_json(output_dir / "normalized.json", normalized)
    artifacts = export_normalized_boxes(
        image_path,
        normalized["bindings"],
        output_dir / "crops",
        id_field="binding_id",
        normalized_1000=False,
    )
    return {
        **_base_record("2", image_path),
        "status": "completed",
        "model": model,
        "page_status": normalized["page_status"],
        "reason_codes": normalized["unknowns"],
        "crop_count": len(normalized["bindings"]),
        "usage": response.usage_dict(),
        "artifacts": {
            "paddle_candidates": str(candidate_json),
            "candidate_overlay": str(candidate_overlay),
            "raw_model_response": str(raw_path),
            "normalized": str(normalized_path),
            **artifacts,
        },
    }


def _run_direct_grounding(
    image_path: Path,
    output_dir: Path,
    *,
    strategy: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    response = call_qwen_json(
        [image_path],
        prompt=build_direct_grounding_prompt(),
        model=model,
        timeout_seconds=timeout_seconds,
        call_type="qwen_a3_direct_grounding_eval",
    )
    raw_path = output_dir / "raw_model_response.txt"
    raw_path.write_text(response.raw_text, encoding="utf-8")
    normalized = parse_direct_grounding(response.payload)
    normalized_path = write_json(output_dir / "normalized.json", normalized)
    artifacts = export_normalized_boxes(
        image_path,
        normalized["targets"],
        output_dir / "crops",
        id_field="target_id",
        normalized_1000=True,
    )
    return {
        **_base_record(strategy, image_path),
        "status": "completed",
        "model": model,
        "page_status": normalized["page_status"],
        "reason_codes": normalized["unknowns"],
        "crop_count": len(normalized["targets"]),
        "usage": response.usage_dict(),
        "artifacts": {
            "raw_model_response": str(raw_path),
            "normalized": str(normalized_path),
            **artifacts,
        },
    }


def _run_paddle_extraction(python_path: Path, images_dir: Path, output_dir: Path) -> None:
    if not python_path.exists():
        raise FileNotFoundError(f"Paddle Python not found: {python_path}")
    command = [
        str(python_path),
        str(BASE / "scripts" / "run_ppstructurev3_layout.py"),
        "--images-dir",
        str(images_dir),
        "--output-dir",
        str(output_dir),
    ]
    environment = os.environ.copy()
    environment.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
    environment.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    environment.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")
    environment.setdefault("FLAGS_use_mkldnn", "0")
    completed = subprocess.run(
        command,
        cwd=BASE,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "runner_stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "runner_stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode not in {0, 2}:
        raise RuntimeError(f"PP-StructureV3 runner failed with exit code {completed.returncode}")


def _base_record(strategy: str, image_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "a3-crop-eval-result-v1",
        "strategy": strategy,
        "strategy_name": STRATEGIES[strategy],
        "sample_id": image_path.stem,
        "source_image": str(image_path.resolve()),
    }


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _build_summary(records: Sequence[Mapping[str, Any]], output_dir: Path, images: Sequence[Path]) -> dict[str, Any]:
    usage = {
        "prompt_tokens": sum(int(item.get("usage", {}).get("prompt_tokens", 0)) for item in records),
        "completion_tokens": sum(int(item.get("usage", {}).get("completion_tokens", 0)) for item in records),
        "total_tokens": sum(int(item.get("usage", {}).get("total_tokens", 0)) for item in records),
    }
    by_strategy: dict[str, Any] = {}
    for strategy in STRATEGIES:
        strategy_records = [item for item in records if item.get("strategy") == strategy]
        if not strategy_records:
            continue
        strategy_usage = {
            "prompt_tokens": sum(int(item.get("usage", {}).get("prompt_tokens", 0)) for item in strategy_records),
            "completion_tokens": sum(int(item.get("usage", {}).get("completion_tokens", 0)) for item in strategy_records),
            "total_tokens": sum(int(item.get("usage", {}).get("total_tokens", 0)) for item in strategy_records),
        }
        models = sorted({str(item.get("model")) for item in strategy_records if item.get("model")})
        by_strategy[strategy] = {
            "completed": sum(item.get("status") == "completed" for item in strategy_records),
            "failed": sum(item.get("status") != "completed" for item in strategy_records),
            "crops": sum(int(item.get("crop_count", 0)) for item in strategy_records),
            "models": models,
            "usage": strategy_usage,
            "estimated_cost_cny": round(
                sum(_estimated_record_cost(item) for item in strategy_records), 6
            ),
        }
    return {
        "schema_version": "a3-crop-eval-summary-v1",
        "output_dir": str(output_dir),
        "sample_count": len(images),
        "totals": {
            "records": len(records),
            "completed": sum(item.get("status") == "completed" for item in records),
            "failed": sum(item.get("status") != "completed" for item in records),
            "crops": sum(int(item.get("crop_count", 0)) for item in records),
            "usage": usage,
            "estimated_cost_cny": round(sum(_estimated_record_cost(item) for item in records), 6),
        },
        "pricing_cny_per_million_tokens": PRICE_CNY_PER_MILLION_TOKENS,
        "pricing_note": "Run-time estimate only; supplier billing is authoritative.",
        "by_strategy": by_strategy,
        "records": list(records),
    }


def _write_review_html(output_dir: Path, records: Sequence[Mapping[str, Any]], images: Sequence[Path]) -> Path:
    record_map = {(str(item.get("sample_id")), str(item.get("strategy"))): item for item in records}
    columns = [strategy for strategy in STRATEGIES if any(item.get("strategy") == strategy for item in records)]
    rows: list[str] = []
    staged_sources = _stage_source_images(images, output_dir / "sources")
    for image_path in images:
        original_rel = _relative_url(staged_sources[image_path.resolve()], output_dir)
        cells = [
            f'<td class="original"><div class="meta">{escape(image_path.stem)}</div><a href="{escape(original_rel)}"><img src="{escape(original_rel)}" loading="lazy"></a></td>'
        ]
        for strategy in columns:
            record = record_map.get((image_path.stem, strategy))
            if not record:
                cells.append('<td class="missing">not run</td>')
                continue
            artifacts = record.get("artifacts") if isinstance(record.get("artifacts"), Mapping) else {}
            overlay = artifacts.get("overlay") or artifacts.get("candidate_overlay") or artifacts.get("region_overlay")
            image_html = ""
            if overlay and Path(str(overlay)).exists():
                overlay_rel = _relative_url(Path(str(overlay)), output_dir)
                image_html = f'<a href="{escape(overlay_rel)}"><img src="{escape(overlay_rel)}" loading="lazy"></a>'
            crop_paths = _artifact_crop_paths(artifacts)
            crop_html = "".join(
                f'<a class="crop" href="{escape(_relative_url(path, output_dir))}"><img src="{escape(_relative_url(path, output_dir))}" loading="lazy"></a>'
                for path in crop_paths
                if path.exists()
            )
            normalized = artifacts.get("normalized")
            normalized_html = ""
            if normalized and Path(str(normalized)).exists():
                normalized_rel = _relative_url(Path(str(normalized)), output_dir)
                normalized_html = f'<a class="json" href="{escape(normalized_rel)}">normalized.json</a>'
            error = record.get("error") if isinstance(record.get("error"), Mapping) else {}
            detail = escape(str(error.get("message") or ""))
            cells.append(
                '<td>'
                f'<div class="meta"><strong>{escape(str(record.get("page_status", "")))}</strong> · crops {int(record.get("crop_count", 0))} · {float(record.get("seconds", 0)):.1f}s</div>'
                f'{image_html}<div class="crops">{crop_html}</div>{normalized_html}<div class="error">{detail}</div>'
                '</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    headings = "".join(f"<th>{escape(STRATEGIES[value])}</th>" for value in columns)
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A3 crop strategy review</title>
<style>
body{{margin:0;font:13px/1.4 Arial,sans-serif;color:#202020;background:#f4f4f1}}header{{position:sticky;top:0;z-index:2;padding:14px 18px;border-bottom:1px solid #ccc;background:#fff}}h1{{margin:0;font-size:18px;letter-spacing:0}}main{{padding:16px;overflow:auto}}table{{border-collapse:collapse;min-width:1200px;width:100%;background:#fff}}th,td{{width:260px;padding:10px;vertical-align:top;border:1px solid #d7d7d2}}th{{position:sticky;top:52px;z-index:1;background:#ecece7;text-align:left}}img{{display:block;width:100%;height:220px;object-fit:contain;background:#fafafa}}.original{{width:220px}}.meta{{min-height:20px;margin-bottom:8px}}.crops{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:8px}}.crop img{{height:100px;border:1px solid #ddd}}.json{{display:inline-block;margin-top:8px;color:#075985}}.error{{margin-top:8px;color:#a21d1d;word-break:break-word}}.missing{{color:#777}}
</style></head><body><header><h1>A3-V1 四方案裁剪结果</h1></header><main><table><thead><tr><th>原图</th>{headings}</tr></thead><tbody>{''.join(rows)}</tbody></table></main></body></html>"""
    target = output_dir / "review.html"
    target.write_text(html, encoding="utf-8")
    return target


def _relative_url(path: Path, base: Path) -> str:
    resolved_path = path.resolve()
    try:
        return Path(os.path.relpath(resolved_path, base.resolve())).as_posix()
    except ValueError:
        # Windows cannot produce a relative path across drive letters.
        return resolved_path.as_uri()


def _estimated_record_cost(record: Mapping[str, Any]) -> float:
    model = str(record.get("model") or "")
    price = PRICE_CNY_PER_MILLION_TOKENS.get(model)
    usage = record.get("usage")
    if not price or not isinstance(usage, Mapping):
        return 0.0
    return (
        int(usage.get("prompt_tokens", 0)) * price["input"]
        + int(usage.get("completion_tokens", 0)) * price["output"]
    ) / 1_000_000


def _artifact_crop_paths(artifacts: Mapping[str, Any]) -> list[Path]:
    values = artifacts.get("crops")
    if not isinstance(values, list):
        return []
    paths: list[Path] = []
    for value in values:
        if isinstance(value, str):
            paths.append(Path(value))
        elif isinstance(value, Mapping) and value.get("crop_path"):
            paths.append(Path(str(value["crop_path"])))
    return paths


def _stage_source_images(images: Sequence[Path], target_dir: Path) -> dict[Path, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[Path, Path] = {}
    for image_path in images:
        source = image_path.resolve(strict=True)
        target = target_dir / source.name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
        staged[source] = target
    return staged


if __name__ == "__main__":
    raise SystemExit(main())

"""A/B test an additive Qwen orientation signal without changing production routing."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Any

from PIL import Image, ImageOps


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.evaluate_complex_image_routing import (  # noqa: E402
    QWEN_MODEL,
    call_model,
    load_local_config,
    load_prompt,
)
from tiku_shared.model_costs import estimate_cost  # noqa: E402
from tiku_agent.image_triage_8897 import observation_from_model_text_8897_v1  # noqa: E402


DEFAULT_MANIFEST = BASE / "test_sets" / "routing" / "a1_a2_a3" / "manifest.json"
DEFAULT_BASELINE_PROMPT = (
    BASE / "experiments" / "complex_image_eval" / "observation_prompt_8897_boundary_v1.md"
)
DEFAULT_CANDIDATE_PROMPT = (
    BASE
    / "experiments"
    / "complex_image_eval"
    / "observation_prompt_8897_boundary_v1_orientation_signal.md"
)
DEFAULT_OUTPUT = BASE / ".tmp_qwen_orientation_routing_eval" / "ab_results.json"
DEFAULT_ROTATIONS = (0, 90, 180, 270)
PIXEL_SHA256_SCHEMA = "sha256(RGB\\0width\\0height\\0raw_pixels)"
VALID_ROUTES = {"A1", "A2", "A3"}
ROUTING_FIELDS = (
    "suggested_route",
    "final_route",
    "question_count",
    "original_structure_count",
    "auxiliary_diagram_count",
    "has_actual_load_evidence",
    "image_recoverable",
    "has_structure_content",
    "image_boundary_clear",
    "has_ambiguity",
)


@dataclass(frozen=True)
class RoutingSource:
    source_id: str
    expected_route: str
    image_path: Path
    source_sha256: str
    orientation_observable: bool


@dataclass(frozen=True)
class RotatedCase:
    case_id: str
    source_id: str
    expected_route: str
    applied_clockwise_degrees: int
    expected_correction_clockwise: int | None
    orientation_observable: bool
    image_path: Path
    input_sha256: str
    pixel_sha256: str


def image_pixel_sha256(image: Image.Image) -> str:
    """Hash normalized RGB dimensions and pixels, independent of image encoding."""
    normalized = image if image.mode == "RGB" else image.convert("RGB")
    digest = sha256()
    digest.update(b"RGB\0")
    digest.update(str(normalized.width).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(normalized.height).encode("ascii"))
    digest.update(b"\0")
    digest.update(normalized.tobytes())
    return digest.hexdigest()


def _orientation_is_observable(image: Image.Image) -> bool:
    return any(lower != upper for lower, upper in image.getextrema())


def load_routing_sources(manifest_path: Path) -> list[RoutingSource]:
    manifest_path = manifest_path.resolve()
    manifest_root = manifest_path.parent.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("suite") != "routing/a1_a2_a3":
        raise ValueError("routing manifest has an unexpected suite id")

    sources: list[RoutingSource] = []
    seen: set[str] = set()
    for item in payload.get("sources", []):
        source_id = str(item.get("path") or "").strip()
        expected_route = str(item.get("expected_route") or "").strip().upper()
        expected_sha = str(item.get("sha256") or "").strip().lower()
        relative = PurePosixPath(source_id)
        if not source_id or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid routing source path: {source_id!r}")
        if source_id in seen:
            raise ValueError(f"duplicate routing source path: {source_id}")
        if expected_route not in VALID_ROUTES:
            raise ValueError(f"invalid expected route for {source_id}: {expected_route}")
        image_path = manifest_root.joinpath(*relative.parts).resolve()
        try:
            image_path.relative_to(manifest_root)
        except ValueError as exc:
            raise ValueError(f"routing source escapes manifest directory: {source_id}") from exc
        if not image_path.is_file():
            raise FileNotFoundError(f"routing source is missing: {source_id}")
        actual_sha = sha256(image_path.read_bytes()).hexdigest()
        if not expected_sha or actual_sha != expected_sha:
            raise ValueError(f"routing source hash mismatch: {source_id}")
        with Image.open(image_path) as opened:
            normalized = ImageOps.exif_transpose(opened).convert("RGB")
            orientation_observable = _orientation_is_observable(normalized)
        seen.add(source_id)
        sources.append(
            RoutingSource(
                source_id=source_id,
                expected_route=expected_route,
                image_path=image_path,
                source_sha256=actual_sha,
                orientation_observable=orientation_observable,
            )
        )
    if not sources:
        raise ValueError("routing manifest has no sources")
    return sources


def _rotate_clockwise(image: Image.Image, degrees: int) -> Image.Image:
    operations = {
        0: None,
        90: Image.Transpose.ROTATE_270,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,
    }
    if degrees not in operations:
        raise ValueError(f"unsupported clockwise rotation: {degrees}")
    operation = operations[degrees]
    return image.copy() if operation is None else image.transpose(operation)


def materialize_rotated_cases(
    sources: list[RoutingSource],
    rotations: tuple[int, ...],
    output_dir: Path,
) -> list[RotatedCase]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases: list[RotatedCase] = []
    for source_index, source in enumerate(sources):
        with Image.open(source.image_path) as opened:
            upright = ImageOps.exif_transpose(opened).convert("RGB")
        for applied in rotations:
            rotated = _rotate_clockwise(upright, applied)
            image_path = output_dir / f"source-{source_index:02d}-cw{applied}.png"
            rotated.save(image_path, format="PNG")
            cases.append(
                RotatedCase(
                    case_id=f"{source.source_id}@cw{applied}",
                    source_id=source.source_id,
                    expected_route=source.expected_route,
                    applied_clockwise_degrees=applied,
                    expected_correction_clockwise=(
                        (-applied) % 360 if source.orientation_observable else None
                    ),
                    orientation_observable=source.orientation_observable,
                    image_path=image_path,
                    input_sha256=sha256(image_path.read_bytes()).hexdigest(),
                    pixel_sha256=image_pixel_sha256(rotated),
                )
            )
    return cases


def _summary_values(text: str, label: str) -> list[str]:
    matches = re.findall(
        rf"^[\s>*`*_]*{re.escape(label)}\s*[:：]\s*([^\r\n]+)",
        str(text or ""),
        flags=re.MULTILINE,
    )
    return [str(value).strip().strip("`*_ ") for value in matches]


def parse_orientation_signal(text: str) -> dict[str, Any]:
    correction_values = _summary_values(text, "回正所需顺时针旋转")
    confidence_values = _summary_values(text, "方向判断")
    errors: list[str] = []

    correction_text = correction_values[0] if len(correction_values) == 1 else None
    confidence_text = confidence_values[0] if len(confidence_values) == 1 else None
    if len(correction_values) > 1:
        errors.append("duplicate_correction")
    if len(confidence_values) > 1:
        errors.append("duplicate_confidence")

    correction: int | None = None
    correction_unknown = False
    if correction_text is None:
        errors.append("missing_correction")
    elif correction_text == "不确定":
        correction_unknown = True
    else:
        match = re.fullmatch(r"(0|90|180|270)\s*(?:度|°)?", correction_text)
        if match:
            correction = int(match.group(1))
        else:
            errors.append("invalid_correction")

    confident: bool | None = None
    if confidence_text is None:
        if not confidence_values:
            errors.append("missing_confidence")
    elif confidence_text == "不确定":
        confident = False
    elif confidence_text == "明确":
        confident = True
    else:
        errors.append("invalid_confidence")

    if confident is True and correction is None:
        errors.append("confident_without_correction")
    if confident is False and not correction_unknown:
        errors.append("uncertain_with_numeric_correction")

    schema_valid = not errors
    actionable = correction if schema_valid and confident is True else None
    return {
        "correction_clockwise": correction,
        "confident": confident,
        "schema_valid": schema_valid,
        "errors": errors,
        "actionable_correction_clockwise": actionable,
        "raw_correction": correction_text,
        "raw_confidence": confidence_text,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _latency_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(item["elapsed_seconds"]) for item in results if item.get("elapsed_seconds") is not None]
    return {
        "count": len(values),
        "average_seconds": _rounded(sum(values) / len(values) if values else None),
        "p50_seconds": _rounded(_percentile(values, 0.50)),
        "p95_seconds": _rounded(_percentile(values, 0.95)),
        "maximum_seconds": _rounded(max(values) if values else None),
    }


def _route_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [item for item in results if not item.get("error")]
    by_rotation: dict[str, dict[str, int]] = {}
    for item in successful:
        key = str(item["applied_clockwise_degrees"])
        bucket = by_rotation.setdefault(key, {"correct": 0, "total": 0})
        bucket["total"] += 1
        bucket["correct"] += int(bool(item.get("final_route_matches_label")))
    return {
        "successful_calls": len(successful),
        "errors": len(results) - len(successful),
        "final_route_correct": sum(bool(item.get("final_route_matches_label")) for item in successful),
        "final_route_total": len(successful),
        "by_applied_rotation": by_rotation,
    }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    variants = sorted({str(item.get("variant") or "") for item in results if item.get("variant")})
    variant_summary: dict[str, Any] = {}
    for variant in variants:
        selected = [item for item in results if item.get("variant") == variant]
        variant_summary[variant] = {
            "route": _route_summary(selected),
            "latency": _latency_summary(selected),
            "prompt_tokens": sum(int((item.get("usage") or {}).get("prompt_tokens") or 0) for item in selected),
            "completion_tokens": sum(
                int((item.get("usage") or {}).get("completion_tokens") or 0) for item in selected
            ),
            "estimated_cost_cny": round(
                sum(float(item.get("estimated_cost_cny") or 0.0) for item in selected),
                6,
            ),
        }

    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for item in results:
        pairs.setdefault(str(item.get("case_id") or ""), {})[str(item.get("variant") or "")] = item
    comparable = []
    for case_id, pair in pairs.items():
        baseline = pair.get("baseline")
        candidate = pair.get("candidate")
        if not baseline or not candidate or baseline.get("error") or candidate.get("error"):
            continue
        comparable.append((case_id, baseline, candidate))

    route_regressions = []
    route_improvements = []
    route_changes = []
    observation_changes = []
    latency_deltas = []
    for case_id, baseline, candidate in comparable:
        baseline_correct = bool(baseline.get("final_route_matches_label"))
        candidate_correct = bool(candidate.get("final_route_matches_label"))
        if baseline.get("final_route") != candidate.get("final_route"):
            route_changes.append(case_id)
        if baseline_correct and not candidate_correct:
            route_regressions.append(case_id)
        if candidate_correct and not baseline_correct:
            route_improvements.append(case_id)
        if any(baseline.get(field) != candidate.get(field) for field in ROUTING_FIELDS):
            observation_changes.append(case_id)
        if baseline.get("elapsed_seconds") is not None and candidate.get("elapsed_seconds") is not None:
            latency_deltas.append(float(candidate["elapsed_seconds"]) - float(baseline["elapsed_seconds"]))

    candidate_results = [
        item for item in results if item.get("variant") == "candidate" and not item.get("error")
    ]
    signals = [(item, item.get("orientation_signal") or {}) for item in candidate_results]
    observable_signals = [
        (item, signal)
        for item, signal in signals
        if item.get("orientation_observable") is True
    ]
    unobservable_signals = [
        (item, signal)
        for item, signal in signals
        if item.get("orientation_observable") is False
    ]
    actionable = [
        (item, signal)
        for item, signal in signals
        if signal.get("actionable_correction_clockwise") is not None
    ]
    observable_actionable = [
        (item, signal)
        for item, signal in observable_signals
        if signal.get("actionable_correction_clockwise") is not None
    ]
    dangerous = [
        item["case_id"]
        for item, signal in observable_actionable
        if int(signal["actionable_correction_clockwise"])
        != int(item["expected_correction_clockwise"])
    ]
    actionable_correct = [
        item["case_id"]
        for item, signal in observable_actionable
        if int(signal["actionable_correction_clockwise"])
        == int(item["expected_correction_clockwise"])
    ]
    unobservable_expected_uncertain = [
        item["case_id"]
        for item, signal in unobservable_signals
        if bool(signal.get("schema_valid"))
        and signal.get("confident") is False
        and signal.get("correction_clockwise") is None
        and signal.get("actionable_correction_clockwise") is None
    ]
    false_confident_on_unobservable = [
        item["case_id"]
        for item, signal in unobservable_signals
        if signal.get("actionable_correction_clockwise") is not None
    ]
    upright_signals = [
        (item, signal)
        for item, signal in observable_signals
        if int(item["applied_clockwise_degrees"]) == 0
    ]
    rotated_signals = [
        (item, signal)
        for item, signal in observable_signals
        if int(item["applied_clockwise_degrees"]) != 0
    ]
    # This gate measures only whether a nonzero rotation would be attempted.
    upright_false_positives = [
        item["case_id"]
        for item, signal in upright_signals
        if signal.get("actionable_correction_clockwise") is not None
        and int(signal["actionable_correction_clockwise"]) != 0
    ]
    rotated_detected = [
        item["case_id"]
        for item, signal in rotated_signals
        if signal.get("actionable_correction_clockwise") is not None
        and int(signal["actionable_correction_clockwise"]) != 0
    ]
    rotated_missed = [
        item["case_id"]
        for item, signal in rotated_signals
        if signal.get("actionable_correction_clockwise") is None
        or int(signal["actionable_correction_clockwise"]) == 0
    ]

    return {
        "variants": variant_summary,
        "paired_comparison": {
            "comparable_cases": len(comparable),
            "final_route_changes": route_changes,
            "route_regressions": route_regressions,
            "route_improvements": route_improvements,
            "legacy_observation_changes": observation_changes,
            "candidate_minus_baseline_latency_seconds": {
                "average": _rounded(sum(latency_deltas) / len(latency_deltas) if latency_deltas else None),
                "p50": _rounded(_percentile(latency_deltas, 0.50)),
                "p95": _rounded(_percentile(latency_deltas, 0.95)),
            },
        },
        "candidate_orientation": {
            "total": len(signals),
            "schema_valid": sum(bool(signal.get("schema_valid")) for _, signal in signals),
            "confident": len(actionable),
            "observable_total": len(observable_signals),
            "unobservable_total": len(unobservable_signals),
            "exact_correction": sum(
                bool(signal.get("schema_valid"))
                and signal.get("correction_clockwise") == item.get("expected_correction_clockwise")
                for item, signal in observable_signals
            ),
            "exact_correction_total": len(observable_signals),
            "actionable_correct": len(actionable_correct),
            "actionable_wrong_cases": dangerous,
            "unobservable_expected_uncertain": len(unobservable_expected_uncertain),
            "unobservable_expected_uncertain_cases": unobservable_expected_uncertain,
            "false_confident_on_unobservable": len(false_confident_on_unobservable),
            "false_confident_on_unobservable_cases": false_confident_on_unobservable,
            "upright_total": len(upright_signals),
            "upright_false_positive": len(upright_false_positives),
            "upright_false_positive_cases": upright_false_positives,
            "rotated_total": len(rotated_signals),
            "rotated_detected": len(rotated_detected),
            "rotated_detected_cases": rotated_detected,
            "rotated_missed_cases": rotated_missed,
            "binary_correct": len(upright_signals) - len(upright_false_positives) + len(rotated_detected),
            "binary_total": len(upright_signals) + len(rotated_signals),
            "upright_false_rotation_cases": upright_false_positives,
        },
    }


def _pricing_for_result(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage") or {}
    normalized_tokens = {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or usage.get("cached_input_tokens") or 0),
    }
    pricing = estimate_cost(
        "dashscope",
        str(result.get("model") or QWEN_MODEL),
        normalized_tokens,
    )
    return {
        "pricing_status": pricing["pricing_status"],
        "price_version": pricing["price_version"],
        "estimated_cost_cny": round(int(pricing["estimated_cost_micros"]) / 1_000_000, 6),
    }


def _write_document(
    path: Path,
    *,
    metadata: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": metadata,
        "summary": summarize_results(results),
        "results": sorted(
            results,
            key=lambda item: (str(item.get("case_id") or ""), str(item.get("variant") or "")),
        ),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _parse_rotations(values: list[int]) -> tuple[int, ...]:
    rotations = tuple(dict.fromkeys(int(value) for value in values))
    invalid = [value for value in rotations if value not in DEFAULT_ROTATIONS]
    if invalid:
        raise argparse.ArgumentTypeError(f"rotations must be 0/90/180/270, got {invalid}")
    return rotations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--baseline-prompt", type=Path, default=DEFAULT_BASELINE_PROMPT)
    parser.add_argument("--candidate-prompt", type=Path, default=DEFAULT_CANDIDATE_PROMPT)
    parser.add_argument("--rotation", action="append", type=int, dest="rotations")
    parser.add_argument("--sample-id", action="append", dest="sample_ids")
    parser.add_argument("--model", default=QWEN_MODEL)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=120)
    parser.add_argument(
        "--sources-are-upright",
        action="store_true",
        help="Explicitly assert that every observable manifest source is upright before synthetic rotation",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        rotations = _parse_rotations(args.rotations or list(DEFAULT_ROTATIONS))
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    sources = load_routing_sources(args.manifest)
    if not args.sources_are_upright:
        parser.error(
            "direction scoring requires --sources-are-upright after all observable source images are verified"
        )
    if args.sample_ids:
        requested = set(args.sample_ids)
        available = {source.source_id for source in sources}
        missing = sorted(requested - available)
        if missing:
            parser.error(f"unknown sample ids: {', '.join(missing)}")
        sources = [source for source in sources if source.source_id in requested]

    baseline_prompt = load_prompt(args.baseline_prompt)
    candidate_prompt = load_prompt(args.candidate_prompt)
    prompt_variants = {
        "baseline": baseline_prompt,
        "candidate": candidate_prompt,
    }
    planned_calls = len(sources) * len(rotations) * len(prompt_variants)
    if args.max_calls <= 0 or planned_calls > args.max_calls:
        parser.error(f"planned calls {planned_calls} exceed --max-calls {args.max_calls}")
    if args.workers <= 0:
        parser.error("--workers must be greater than zero")

    metadata = {
        "status": "not_run" if args.dry_run else "running",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": "dashscope",
        "model": args.model,
        "route_policy": "8897-v1",
        "temperature": 0,
        "max_output_tokens": 1600,
        "timeout_seconds": args.timeout,
        "workers": args.workers,
        "max_retries": 0,
        "evaluation_scope": "single_pass_prompt_ab_and_qwen_orientation_signal",
        "variant_order": "counterbalanced_by_source_and_rotation",
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": sha256(args.manifest.resolve().read_bytes()).hexdigest(),
        "source_count": len(sources),
        "source_orientation_ground_truth": "operator_asserted_upright_when_observable",
        "orientation_observability_rule": "single_color_after_exif_transpose_rgb_is_unobservable",
        "pixel_sha256_schema": PIXEL_SHA256_SCHEMA,
        "orientation_observable_source_count": sum(source.orientation_observable for source in sources),
        "orientation_unobservable_source_count": sum(
            not source.orientation_observable for source in sources
        ),
        "orientation_unobservable_source_ids": [
            source.source_id for source in sources if not source.orientation_observable
        ],
        "rotations": list(rotations),
        "case_count": len(sources) * len(rotations),
        "planned_calls": planned_calls,
        "prompt_sha256": {
            name: sha256(prompt.encode("utf-8")).hexdigest()
            for name, prompt in prompt_variants.items()
        },
    }
    if args.dry_run:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0

    config = load_local_config()
    config["dashscope_model"] = args.model
    results: list[dict[str, Any]] = []
    output_path = args.output.resolve()

    with tempfile.TemporaryDirectory(prefix="qwen-orientation-routing-") as temp_dir:
        cases = materialize_rotated_cases(sources, rotations, Path(temp_dir))
        jobs: list[tuple[str, str, RotatedCase]] = []
        rotation_count = len(rotations)
        for index, case in enumerate(cases):
            source_index = index // rotation_count
            rotation_index = index % rotation_count
            baseline_first = (source_index + rotation_index) % 2 == 0
            names = ("baseline", "candidate") if baseline_first else ("candidate", "baseline")
            for variant in names:
                jobs.append((variant, prompt_variants[variant], case))

        def run_job(variant: str, prompt: str, case: RotatedCase) -> dict[str, Any]:
            sample = {
                "id": case.case_id,
                "expected_route": case.expected_route,
                "label_status": "manifest_locked",
                "source_kind": "routing_rotation_ab",
                "image_path": case.image_path,
            }
            try:
                result = call_model(
                    "qwen",
                    sample,
                    prompt=prompt,
                    config=config,
                    timeout=args.timeout,
                    route_policy="8897-v1",
                )
                parsed = observation_from_model_text_8897_v1(
                    str(result.get("raw_content") or "")
                )
                result["has_structure_content"] = parsed.has_structure_content
                result.update(_pricing_for_result(result))
            except Exception as exc:  # noqa: BLE001 - preserve each failed case in the partial report.
                result = {
                    "sample_id": case.case_id,
                    "provider": "qwen",
                    "model": args.model,
                    "expected_route": case.expected_route,
                    "suggested_route": None,
                    "final_route": None,
                    "route_matches_label": False,
                    "final_route_matches_label": False,
                    "elapsed_seconds": None,
                    "usage": {},
                    "raw_content": "",
                    "pricing_status": "not_called_or_failed",
                    "price_version": "",
                    "estimated_cost_cny": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            result.update(
                {
                    "case_id": case.case_id,
                    "source_id": case.source_id,
                    "variant": variant,
                    "applied_clockwise_degrees": case.applied_clockwise_degrees,
                    "expected_correction_clockwise": case.expected_correction_clockwise,
                    "orientation_observable": case.orientation_observable,
                    "input_sha256": case.input_sha256,
                    "pixel_sha256": case.pixel_sha256,
                }
            )
            result["orientation_signal"] = (
                parse_orientation_signal(str(result.get("raw_content") or ""))
                if variant == "candidate" and not result.get("error")
                else None
            )
            return result

        executor = ThreadPoolExecutor(max_workers=args.workers)
        job_iterator = iter(jobs)
        pending: dict[Any, tuple[str, str]] = {}

        def submit_next() -> bool:
            try:
                variant, prompt, case = next(job_iterator)
            except StopIteration:
                return False
            future = executor.submit(run_job, variant, prompt, case)
            pending[future] = (variant, case.case_id)
            return True

        for _ in range(min(args.workers, len(jobs))):
            submit_next()
        try:
            completed = 0
            while pending:
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    pending.pop(future, None)
                    result = future.result()
                    results.append(result)
                    completed += 1
                    _write_document(output_path, metadata=metadata, results=results)
                    status = result.get("final_route") or "ERROR"
                    print(
                        f"[{completed:03d}/{planned_calls}] {result['variant']:9} "
                        f"{result['case_id']}: {status}",
                        flush=True,
                    )
                    submit_next()
        except KeyboardInterrupt:
            metadata["status"] = "interrupted"
            metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            _write_document(output_path, metadata=metadata, results=results)
            print(f"interrupted after {len(results)}/{planned_calls} calls; output={output_path}")
            return 130
        except BaseException:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    metadata["status"] = "completed"
    metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_document(output_path, metadata=metadata, results=results)
    errors = sum(bool(item.get("error")) for item in results)
    print(f"completed={len(results) - errors}/{len(results)} output={output_path}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

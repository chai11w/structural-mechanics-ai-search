"""Evaluate local RapidOrientation predictions with the saved Qwen signal."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version as package_version
import json
from pathlib import Path
import sys
from time import perf_counter
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.evaluate_qwen_orientation_routing import (  # noqa: E402
    DEFAULT_MANIFEST,
    DEFAULT_ROTATIONS,
    PIXEL_SHA256_SCHEMA,
    RotatedCase,
    load_routing_sources,
    materialize_rotated_cases,
)


DEFAULT_QWEN_RESULTS = BASE / ".tmp_qwen_orientation_routing_eval" / "ab_results.json"
DEFAULT_OUTPUT = (
    BASE / ".tmp_qwen_orientation_routing_eval" / "rapid_joint_results.json"
)
EXPECTED_RAPID_ORIENTATION_VERSION = "0.0.11"
EXPECTED_MODEL_SHA256 = (
    "2f62c9bfb830a0b417241269fde7ef2d0ad5446c0ed2b8af33b1f6543545e8e2"
)
EXPECTED_LABELS = ("0", "90", "180", "270")
EXPECTED_PROVIDER = "CPUExecutionProvider"
THRESHOLDS = tuple(index / 100 for index in range(101))
POLICIES = ("rapid_only", "qwen_binary_gate", "qwen_angle_agreement")


def file_sha256(path: str | Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def resolve_default_model_path() -> Path:
    from rapid_orientation.main import DEFAULT_PATH

    return Path(DEFAULT_PATH).resolve()


def load_qwen_candidates(path: str | Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    result_path = Path(path).resolve()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Qwen results must be a JSON object")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("status") != "completed":
        raise ValueError("Qwen evaluation is not complete")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Qwen results must contain a results list")

    candidates: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict) or item.get("variant") != "candidate":
            continue
        if item.get("error"):
            raise ValueError(f"Qwen candidate failed: {item.get('case_id') or 'unknown'}")
        case_id = str(item.get("case_id") or "").strip()
        if not case_id:
            raise ValueError("Qwen candidate is missing case_id")
        if case_id in candidates:
            raise ValueError(f"duplicate Qwen candidate case_id: {case_id}")
        candidates[case_id] = item
    if not candidates:
        raise ValueError("Qwen results contain no candidate rows")
    return candidates, metadata


def validate_case_alignment(
    cases: Sequence[RotatedCase],
    qwen_candidates: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    generated_ids = [case.case_id for case in cases]
    if len(generated_ids) != len(set(generated_ids)):
        raise ValueError("generated RapidOrientation cases contain duplicate case_id")
    generated_set = set(generated_ids)
    qwen_set = set(qwen_candidates)
    if generated_set != qwen_set:
        missing = sorted(generated_set - qwen_set)
        unexpected = sorted(qwen_set - generated_set)
        raise ValueError(
            f"Qwen/Rapid case_id mismatch: missing={missing}, unexpected={unexpected}"
        )

    for case in cases:
        qwen = qwen_candidates[case.case_id]
        expected_values = {
            "source_id": case.source_id,
            "applied_clockwise_degrees": case.applied_clockwise_degrees,
            "expected_correction_clockwise": case.expected_correction_clockwise,
            "orientation_observable": case.orientation_observable,
            "pixel_sha256": case.pixel_sha256,
        }
        for field, expected in expected_values.items():
            actual = qwen.get(field)
            if actual != expected:
                raise ValueError(
                    f"Qwen/Rapid {field} mismatch for {case.case_id}: "
                    f"expected={expected!r}, actual={actual!r}"
                )
    return [
        case.case_id
        for case in cases
        if qwen_candidates[case.case_id].get("input_sha256") != case.input_sha256
    ]


def _validated_probabilities(output: object, labels: Sequence[str]) -> np.ndarray:
    array = np.asarray(output, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] < 1 or array.shape[1] != len(labels):
        raise ValueError(f"unexpected RapidOrientation output shape: {array.shape}")
    first = array[0]
    if not np.all(np.isfinite(first)):
        raise ValueError("RapidOrientation output contains non-finite values")
    if np.any(first < 0.0) or not np.isclose(first.sum(), 1.0, atol=1e-5):
        raise ValueError("RapidOrientation out[0] is not a probability vector")
    return array


def infer_rapid_case(
    engine: object,
    case: RotatedCase,
    qwen_candidate: Mapping[str, Any],
) -> dict[str, Any]:
    total_started = perf_counter()
    image = engine.load_img(case.image_path)
    model_started = perf_counter()
    batch = engine.preprocess(image)
    output = engine.session(batch)[0]
    model_latency_ms = round((perf_counter() - model_started) * 1000, 3)
    total_latency_ms = round((perf_counter() - total_started) * 1000, 3)

    labels = tuple(str(label) for label in engine.labels)
    if labels != EXPECTED_LABELS:
        raise ValueError(f"unexpected RapidOrientation labels: {labels}")
    probabilities = _validated_probabilities(output, labels)
    first = probabilities[0]
    predicted_index = int(np.argmax(first))
    predicted_label = int(labels[predicted_index])
    predicted_correction = (-predicted_label) % 360
    confidence = float(first[predicted_index])
    qwen_signal = qwen_candidate.get("orientation_signal")
    qwen_signal = qwen_signal if isinstance(qwen_signal, dict) else {}
    qwen_schema_valid = qwen_signal.get("schema_valid") is True
    qwen_confident = qwen_signal.get("confident") is True
    qwen_reported_actionable = qwen_signal.get("actionable_correction_clockwise")
    if qwen_reported_actionable not in (None, 0, 90, 180, 270):
        raise ValueError(f"invalid Qwen actionable correction for {case.case_id}")
    qwen_actionable = (
        qwen_reported_actionable
        if qwen_schema_valid and qwen_confident
        else None
    )

    expected = case.expected_correction_clockwise
    return {
        "case_id": case.case_id,
        "source_id": case.source_id,
        "expected_route": case.expected_route,
        "applied_clockwise_degrees": case.applied_clockwise_degrees,
        "expected_correction_clockwise": expected,
        "orientation_observable": case.orientation_observable,
        "input_sha256": case.input_sha256,
        "pixel_sha256": case.pixel_sha256,
        "qwen_input_sha256": qwen_candidate.get("input_sha256"),
        "encoded_input_sha256_matches_qwen": (
            qwen_candidate.get("input_sha256") == case.input_sha256
        ),
        "rapid_orientation_label": predicted_label,
        "rapid_correction_clockwise": predicted_correction,
        "rapid_confidence": round(confidence, 9),
        "rapid_probabilities": {
            label: round(float(first[index]), 9)
            for index, label in enumerate(labels)
        },
        "rapid_batch_rows": int(probabilities.shape[0]),
        "rapid_batch_argmax_labels": [
            int(labels[int(index)]) for index in np.argmax(probabilities, axis=1)
        ],
        "rapid_model_latency_ms": model_latency_ms,
        "rapid_total_latency_ms": total_latency_ms,
        "rapid_raw_argmax_correct": (
            None if expected is None else predicted_correction == expected
        ),
        "qwen_schema_valid": qwen_schema_valid,
        "qwen_confident": qwen_confident,
        "qwen_reported_actionable_correction_clockwise": qwen_reported_actionable,
        "qwen_actionable_correction_clockwise": qwen_actionable,
    }


def policy_action(record: Mapping[str, Any], threshold: float, policy: str) -> int:
    if policy not in POLICIES:
        raise ValueError(f"unsupported policy: {policy}")
    if not bool(record.get("orientation_observable")):
        return 0

    rapid_correction = int(record["rapid_correction_clockwise"])
    confidence = float(record["rapid_confidence"])
    if rapid_correction == 0 or confidence < threshold:
        return 0
    if policy == "rapid_only":
        return rapid_correction

    if record.get("qwen_schema_valid") is not True or record.get("qwen_confident") is not True:
        return 0
    qwen_actionable = record.get("qwen_actionable_correction_clockwise")
    if qwen_actionable not in (90, 180, 270):
        return 0
    if policy == "qwen_angle_agreement" and int(qwen_actionable) != rapid_correction:
        return 0
    return rapid_correction


def evaluate_policy(
    records: Sequence[Mapping[str, Any]],
    threshold: float,
    policy: str,
) -> dict[str, Any]:
    observable = [record for record in records if record.get("orientation_observable") is True]
    unobservable = [record for record in records if record.get("orientation_observable") is False]
    counts = {
        "exact": 0,
        "upright_total": 0,
        "upright_false_rotate": 0,
        "rotated_total": 0,
        "rotated_corrected": 0,
        "rotated_missed": 0,
        "rotated_wrong": 0,
        "unsafe_action": 0,
        "action_count": 0,
    }
    unsafe_cases: list[str] = []
    for record in observable:
        expected = int(record["expected_correction_clockwise"])
        action = policy_action(record, threshold, policy)
        counts["action_count"] += int(action != 0)
        counts["exact"] += int(action == expected)
        if expected == 0:
            counts["upright_total"] += 1
            if action != 0:
                counts["upright_false_rotate"] += 1
                counts["unsafe_action"] += 1
                unsafe_cases.append(str(record["case_id"]))
            continue

        counts["rotated_total"] += 1
        if action == expected:
            counts["rotated_corrected"] += 1
        elif action == 0:
            counts["rotated_missed"] += 1
        else:
            counts["rotated_wrong"] += 1
            counts["unsafe_action"] += 1
            unsafe_cases.append(str(record["case_id"]))

    unobservable_actions = [policy_action(record, threshold, policy) for record in unobservable]
    if any(action != 0 for action in unobservable_actions):
        raise AssertionError("unobservable policy action must stay at zero")
    return {
        "threshold": round(float(threshold), 2),
        "policy": policy,
        "observable_total": len(observable),
        **counts,
        "rotated_recall": round(
            counts["rotated_corrected"] / counts["rotated_total"], 6
        )
        if counts["rotated_total"]
        else None,
        "unsafe_cases": sorted(unsafe_cases),
        "unobservable_total": len(unobservable),
        "unobservable_preserved": sum(action == 0 for action in unobservable_actions),
    }


def scan_thresholds(records: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        policy: [evaluate_policy(records, threshold, policy) for threshold in THRESHOLDS]
        for policy in POLICIES
    }


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * (position - lower))


def latency_summary(records: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = [float(record[field]) for record in records]
    return {
        "count": len(values),
        "average_ms": round(sum(values) / len(values), 3) if values else None,
        "p50_ms": round(_percentile(values, 0.50), 3) if values else None,
        "p95_ms": round(_percentile(values, 0.95), 3) if values else None,
        "maximum_ms": round(max(values), 3) if values else None,
    }


def raw_confusion(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observable = [record for record in records if record.get("orientation_observable") is True]
    labels = (0, 90, 180, 270)
    by_applied = {
        str(applied): {str(predicted): 0 for predicted in labels}
        for applied in labels
    }
    by_expected_correction = {
        str(expected): {str(predicted): 0 for predicted in labels}
        for expected in labels
    }
    correct = 0
    for record in observable:
        applied = int(record["applied_clockwise_degrees"])
        expected = int(record["expected_correction_clockwise"])
        predicted_label = int(record["rapid_orientation_label"])
        predicted_correction = int(record["rapid_correction_clockwise"])
        by_applied[str(applied)][str(predicted_label)] += 1
        by_expected_correction[str(expected)][str(predicted_correction)] += 1
        correct += int(predicted_correction == expected)
    return {
        "correct": correct,
        "total": len(observable),
        "accuracy": round(correct / len(observable), 6) if observable else None,
        "by_applied_clockwise_vs_predicted_label": by_applied,
        "by_expected_correction_vs_predicted_correction": by_expected_correction,
    }


def zero_unsafe_candidates(
    scans: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for policy, rows in scans.items():
        safe = [row for row in rows if int(row["unsafe_action"]) == 0]
        if not safe:
            result[policy] = {"available": False, "candidate_thresholds": []}
            continue
        maximum_corrected = max(int(row["rotated_corrected"]) for row in safe)
        best = [row for row in safe if int(row["rotated_corrected"]) == maximum_corrected]
        result[policy] = {
            "available": True,
            "maximum_rotated_corrected": maximum_corrected,
            "rotated_total": int(best[0]["rotated_total"]),
            "maximum_recall": best[0]["rotated_recall"],
            "candidate_thresholds": [row["threshold"] for row in best],
            "selection_rule": "zero unsafe_action, then maximum rotated_corrected",
        }
    return result


def build_summary(
    records: Sequence[Mapping[str, Any]],
    scans: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    return {
        "rapid_raw_argmax": raw_confusion(records),
        "rapid_model_latency": latency_summary(records, "rapid_model_latency_ms"),
        "rapid_total_latency": latency_summary(records, "rapid_total_latency_ms"),
        "unobservable": {
            "total": sum(record.get("orientation_observable") is False for record in records),
            "case_ids": sorted(
                str(record["case_id"])
                for record in records
                if record.get("orientation_observable") is False
            ),
            "policy": "always preserve input; excluded from orientation accuracy",
        },
        "zero_unsafe_max_recall_threshold_candidates": zero_unsafe_candidates(scans),
        "conclusion": (
            "Offline evidence only. Alignment is verified for pre-transport RGB pixels, "
            "not for any resized or re-encoded Qwen outbound payload. This 15-source "
            "fixed set is too small to justify a production threshold or deployment."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--qwen-results", type=Path, default=DEFAULT_QWEN_RESULTS)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest_path = args.manifest.resolve()
    qwen_results_path = args.qwen_results.resolve()
    output_path = args.output.resolve()
    model_path = (
        args.model_path.resolve() if args.model_path is not None else resolve_default_model_path()
    )
    if not model_path.is_file():
        raise FileNotFoundError(f"RapidOrientation model not found: {model_path}")

    rapid_version = package_version("rapid_orientation")
    if rapid_version != EXPECTED_RAPID_ORIENTATION_VERSION:
        raise RuntimeError(
            f"RapidOrientation version mismatch: expected "
            f"{EXPECTED_RAPID_ORIENTATION_VERSION}, got {rapid_version}"
        )
    model_hash = file_sha256(model_path)
    if model_hash != EXPECTED_MODEL_SHA256:
        raise RuntimeError(
            f"RapidOrientation model hash mismatch: expected {EXPECTED_MODEL_SHA256}, "
            f"got {model_hash}"
        )

    from rapid_orientation import RapidOrientation

    engine_started = perf_counter()
    engine = RapidOrientation(model_path=model_path)
    engine_initialization_ms = round((perf_counter() - engine_started) * 1000, 3)
    providers = list(engine.session.session.get_providers())
    if providers != [EXPECTED_PROVIDER]:
        raise RuntimeError(f"unexpected RapidOrientation providers: {providers}")

    qwen_candidates, qwen_metadata = load_qwen_candidates(qwen_results_path)
    if qwen_metadata.get("pixel_sha256_schema") != PIXEL_SHA256_SCHEMA:
        raise ValueError("Qwen results use an unexpected or missing pixel_sha256 schema")
    sources = load_routing_sources(manifest_path)
    with tempfile.TemporaryDirectory(prefix="rapid-qwen-orientation-") as temp_dir:
        cases = materialize_rotated_cases(
            sources,
            DEFAULT_ROTATIONS,
            Path(temp_dir),
        )
        if len(cases) != 60:
            raise ValueError(f"expected 60 generated cases, got {len(cases)}")
        encoded_hash_mismatch_cases = validate_case_alignment(cases, qwen_candidates)
        records = [
            infer_rapid_case(engine, case, qwen_candidates[case.case_id])
            for case in cases
        ]

    scans = scan_thresholds(records)
    metadata = {
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_scope": "offline_rapid_orientation_and_saved_qwen_threshold_scan",
        "network_calls": 0,
        "source_count": len(sources),
        "case_count": len(records),
        "rotations": list(DEFAULT_ROTATIONS),
        "manifest": str(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "qwen_results": str(qwen_results_path),
        "qwen_results_sha256": file_sha256(qwen_results_path),
        "qwen_model": qwen_metadata.get("model"),
        "qwen_prompt_sha256": qwen_metadata.get("prompt_sha256"),
        "case_alignment_gate": "pre_transport_rgb_pixel_sha256",
        "pixel_sha256_schema": PIXEL_SHA256_SCHEMA,
        "qwen_outbound_payload_alignment": (
            "not verified; image_to_model_data_url may resize or re-encode large inputs"
        ),
        "encoded_input_sha256_match_count": len(records) - len(encoded_hash_mismatch_cases),
        "encoded_input_sha256_mismatch_count": len(encoded_hash_mismatch_cases),
        "encoded_input_sha256_mismatch_cases": encoded_hash_mismatch_cases,
        "rapid_orientation_package_version": rapid_version,
        "rapid_orientation_model_path": str(model_path),
        "rapid_orientation_model_sha256": model_hash,
        "rapid_orientation_model_size_bytes": model_path.stat().st_size,
        "rapid_orientation_providers": providers,
        "rapid_orientation_labels": list(engine.labels),
        "rapid_orientation_output_probability_row": "session_output[0][0]",
        "rapid_orientation_label_semantics": "input clockwise rotation",
        "rapid_orientation_correction_formula": "(-label) % 360",
        "rapid_orientation_engine_initialization_ms": engine_initialization_ms,
        "thresholds": [round(value, 2) for value in THRESHOLDS],
        "threshold_comparison": ">=",
        "policies": {
            "rapid_only": "apply nonzero Rapid correction when confidence reaches threshold",
            "qwen_binary_gate": (
                "apply Rapid correction when Qwen actionable is nonzero and Rapid correction "
                "is nonzero and reaches threshold; Qwen angle is ignored"
            ),
            "qwen_angle_agreement": (
                "comparison only: qwen_binary_gate plus exact Qwen/Rapid angle agreement"
            ),
            "unobservable": "always preserve input",
        },
    }
    document = {
        "metadata": metadata,
        "summary": build_summary(records, scans),
        "threshold_scan": scans,
        "results": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded projections of actual A3 page and crop results."""

from collections.abc import Mapping, Sequence
import json

from tiku_agent.checkpoint_contract import CROP_VALIDATION_CHECKS, MAX_RESULT_BYTES


def excerpt(value: object, limit: int = 1000) -> str:
    return value[:limit] if isinstance(value, str) else ""


def page_result(page: Mapping, units: Sequence[Mapping]) -> dict:
    diagrams = page.get("diagrams", [])
    stored = []
    for unit in units[:50]:
        roles = sorted({diagram["role"] for diagram in diagrams
                        if unit["unit_id"] in diagram.get("unit_ids", [])})[:50]
        stored.append({
            "unit_id": unit["unit_id"], "group_id": unit["group_id"],
            **{key: excerpt(unit.get(key), 200) for key in
               ("parent_question_label", "question_label", "display_label")},
            "searchability": unit["searchability"], "status": unit["status"],
            "reason_codes": list(unit.get("reason_codes", []))[:50],
            "diagram_roles": roles,
            "recognized_text_excerpt": excerpt("\n".join(excerpt(unit.get(key)) for key in
                ("shared_stem_text", "title_text", "visible_text"))),
        })
    result = {
        "page_summary": {
            "source_schema_version": page["schema_version"],
            "page_disposition": page["page_disposition"],
            "group_count": len(page.get("groups", [])), "unit_count": len(units),
            "stored_unit_count": len(stored), "units_truncated": len(stored) < len(units),
            "searchable_unit_count": sum(unit.get("searchability") == "searchable_candidate" for unit in units),
            "diagram_count": len(diagrams), "unknown_count": len(page.get("unknowns", [])),
        },
        "unit_results": stored,
    }
    # Preserve unit identities and counts while fitting the aggregate byte budget.
    text_fields = ("recognized_text_excerpt", "display_label", "parent_question_label", "question_label")
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RESULT_BYTES:
        changed = False
        for unit in stored:
            for key in text_fields:
                value = unit[key]
                if value:
                    unit[key] = value[:len(value) // 2]
                    changed = True
            for key in ("reason_codes", "diagram_roles"):
                if len(unit[key]) > 1:
                    unit[key] = unit[key][:max(1, len(unit[key]) // 2)]
                    changed = True
        if not changed:
            raise ValueError("unit identities exceed checkpoint byte limit")
    return result


def crop_result(unit_id: str, record: Mapping, page: Mapping, *,
                source_size: tuple[int, int], crop_size: tuple[int, int], method: str) -> dict:
    width, height = source_size
    bounds = record["bounds"]
    # Match the existing Pillow crop's rounding and clamping exactly.
    left = max(0, min(width - 1, round(float(bounds["x"]) * width)))
    top = max(0, min(height - 1, round(float(bounds["y"]) * height)))
    right = max(left + 1, min(width, round((float(bounds["x"]) + float(bounds["width"])) * width)))
    bottom = max(top + 1, min(height, round((float(bounds["y"]) + float(bounds["height"])) * height)))
    return {
        "crop_geometry": {
            "unit_id": unit_id, "method": method,
            "model_bbox": record.get("model_bbox"), "expanded_bbox": record.get("bbox"),
            "pixel_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
            "source_width_px": width, "source_height_px": height,
            "crop_width_px": crop_size[0], "crop_height_px": crop_size[1],
        },
        "crop_grounding": {
            "schema_version": page.get("schema_version", "manual-crop-v1"),
            "page_status": page.get("page_status", "manual"),
            "grounding_status": record.get("grounding_status", "manual"),
            "reason_codes": list(record.get("reason_codes", []))[:50],
            "binding_evidence_excerpt": excerpt(record.get("binding_evidence")),
        },
    }


def validation_result(record: Mapping) -> dict:
    checks = {key: record.get("verification_checks", {}).get(key) for key in sorted(CROP_VALIDATION_CHECKS)}
    verified = all(value is True for value in checks.values())
    return {"crop_validation": {
        "schema_version": "a3-crop-compare-v1",
        "verdict": "verified" if verified else "review_required",
        "checks": checks,
        "external_load_status": record["external_load_status"],
        "reason_codes": list(record.get("reason_codes", []))[:50],
    }}

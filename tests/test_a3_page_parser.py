import json
from copy import deepcopy
import unittest

from tiku_agent.a3_page_parser import (
    A3PageParseError,
    build_a2_context_text,
    build_display_label,
    parse_a3_page_understanding,
)


def valid_payload() -> dict:
    return {
        "schema_version": "a3-page-understanding-v2",
        "page_disposition": "has_searchable_candidates",
        "a3_reason_evidence": [{"code": "multi_question_page", "evidence": "two units"}],
        "groups": [
            {
                "group_id": "g1",
                "parent_question_label": "四",
                "parent_title_text": "",
                "shared_stem_text": "试作图示刚架的 M 图。",
                "units": [
                    {
                        "unit_id": "g1-u1",
                        "parent_question_label": "四",
                        "question_label": "1",
                        "title_text": "",
                        "shared_stem_text": "试作图示刚架的 M 图。",
                        "visible_text": "10 kN, 4 m",
                        "searchability": "searchable_candidate",
                        "reason_codes": [],
                        "diagram_ids": ["d1"],
                        "status": "clear",
                        "evidence": ["complete"],
                        "notes": "",
                    },
                    {
                        "unit_id": "g1-u2",
                        "parent_question_label": "四",
                        "question_label": "2",
                        "title_text": "",
                        "shared_stem_text": "试作图示刚架的 M 图。",
                        "visible_text": "q, 6 m",
                        "searchability": "uncertain",
                        "reason_codes": ["incomplete_diagram"],
                        "diagram_ids": [],
                        "status": "partial",
                        "evidence": ["truncated"],
                        "notes": "补图",
                    },
                ],
            }
        ],
        "diagrams": [
            {
                "diagram_id": "d1",
                "role": "original_structure",
                "group_id": "g1",
                "unit_ids": ["g1-u1"],
                "status": "clear",
                "evidence": "load visible",
            }
        ],
        "unassigned_content": [],
        "unknowns": [],
    }


class A3PageParserTests(unittest.TestCase):
    def test_parse_valid_payload_and_build_derived_fields(self):
        result = parse_a3_page_understanding(valid_payload())
        self.assertEqual(len(result.searchable_units), 1)
        unit = result.groups[0].units[0]
        self.assertEqual(build_display_label(unit, 1), "四-1")
        self.assertEqual(
            build_a2_context_text(unit),
            "试作图示刚架的 M 图。",
        )
        output = result.to_dict(include_derived=True)
        self.assertEqual(output["groups"][0]["units"][0]["display_label"], "四-1")

    def test_parse_single_json_code_fence_with_warning(self):
        raw = "```json\n" + json.dumps(valid_payload(), ensure_ascii=False) + "\n```"
        result = parse_a3_page_understanding(raw)
        self.assertEqual(result.warnings, ("markdown_code_fence_stripped",))

    def test_display_label_does_not_prefix_page_range(self):
        payload = valid_payload()
        payload["groups"][0]["parent_question_label"] = "3-15~3-24"
        payload["groups"][0]["units"][0]["parent_question_label"] = "3-15~3-24"
        payload["groups"][0]["units"][0]["question_label"] = "3-15"

        result = parse_a3_page_understanding(payload)

        self.assertEqual(build_display_label(result.groups[0].units[0], 1), "3-15")

    def test_compact_unit_contract_derives_parent_and_shared_stem(self):
        payload = valid_payload()
        unit = payload["groups"][0]["units"][0]
        for field in ("parent_question_label", "shared_stem_text", "visible_text", "evidence", "notes"):
            unit.pop(field)
        payload["diagrams"][0].pop("evidence")

        result = parse_a3_page_understanding(payload)
        parsed = result.groups[0].units[0]

        self.assertEqual(parsed.parent_question_label, "四")
        self.assertEqual(build_a2_context_text(parsed), "试作图示刚架的 M 图。")

    def test_group_parent_title_is_derived_into_each_units_a2_context(self):
        payload = valid_payload()
        payload["groups"][0]["parent_title_text"] = "用力法计算图示结构。"
        payload["groups"][0]["shared_stem_text"] = ""
        payload["groups"][0]["units"][0]["shared_stem_text"] = ""
        payload["groups"][0]["units"][0]["title_text"] = ""

        result = parse_a3_page_understanding(payload)
        output = result.to_dict(include_derived=True)

        self.assertEqual(
            output["groups"][0]["units"][0]["a2_context_text"],
            "用力法计算图示结构。",
        )

    def test_unlabelled_units_use_page_wide_ordinals_across_groups(self):
        payload = valid_payload()
        first_unit = payload["groups"][0]["units"][0]
        first_unit["parent_question_label"] = ""
        first_unit["question_label"] = ""
        payload["groups"][0]["parent_question_label"] = ""
        payload["groups"][0]["units"] = [first_unit]

        second_unit = deepcopy(first_unit)
        second_unit["unit_id"] = "g2-u1"
        second_unit["diagram_ids"] = ["d2"]
        payload["groups"].append({
            "group_id": "g2",
            "parent_question_label": "",
            "parent_title_text": "",
            "shared_stem_text": "",
            "units": [second_unit],
        })
        second_diagram = deepcopy(payload["diagrams"][0])
        second_diagram["diagram_id"] = "d2"
        second_diagram["group_id"] = "g2"
        second_diagram["unit_ids"] = ["g2-u1"]
        payload["diagrams"].append(second_diagram)

        result = parse_a3_page_understanding(payload)
        output = result.to_dict(include_derived=True)

        self.assertEqual(
            [group["units"][0]["display_label"] for group in output["groups"]],
            ["未标号题1", "未标号题2"],
        )

    def test_reject_duplicate_unit_id(self):
        payload = valid_payload()
        payload["groups"][0]["units"][1]["unit_id"] = "g1-u1"
        with self.assertRaisesRegex(A3PageParseError, "unique"):
            parse_a3_page_understanding(payload)

    def test_reject_inconsistent_diagram_reference(self):
        payload = valid_payload()
        payload["groups"][0]["units"][0]["diagram_ids"] = ["missing"]
        with self.assertRaisesRegex(A3PageParseError, "does not reference diagram"):
            parse_a3_page_understanding(payload)

    def test_reject_candidate_without_original_structure(self):
        payload = valid_payload()
        payload["groups"][0]["units"][0]["diagram_ids"] = []
        payload["diagrams"] = []
        with self.assertRaisesRegex(A3PageParseError, "no original_structure"):
            parse_a3_page_understanding(payload)

    def test_remove_unreferenced_empty_group_and_preserve_text(self):
        payload = valid_payload()
        payload["groups"].append(
            {
                "group_id": "g2",
                "parent_question_label": "五",
                "parent_title_text": "计算图示结构。",
                "shared_stem_text": "荷载如图所示。",
                "units": [],
            }
        )
        result = parse_a3_page_understanding(payload)

        self.assertEqual([group.group_id for group in result.groups], ["g1"])
        self.assertEqual(
            result.unassigned_content,
            (
                ("五", "unreferenced_empty_group:g2:parent_question_label"),
                ("计算图示结构。", "unreferenced_empty_group:g2:parent_title_text"),
                ("荷载如图所示。", "unreferenced_empty_group:g2:shared_stem_text"),
            ),
        )
        self.assertIn("unreferenced_empty_group_removed:g2", result.warnings)

    def test_reject_referenced_empty_group(self):
        payload = valid_payload()
        payload["groups"].append(
            {
                "group_id": "g2",
                "parent_question_label": "五",
                "parent_title_text": "",
                "shared_stem_text": "",
                "units": [],
            }
        )
        payload["diagrams"].append(
            {
                "diagram_id": "d2",
                "role": "dimension_or_annotation",
                "group_id": "g2",
                "unit_ids": [],
                "status": "clear",
                "evidence": "group reference",
            }
        )
        with self.assertRaisesRegex(A3PageParseError, "empty groups"):
            parse_a3_page_understanding(payload)

    def test_reject_page_with_only_unreferenced_empty_groups(self):
        payload = valid_payload()
        payload["groups"] = [
            {
                "group_id": "g2",
                "parent_question_label": "五",
                "parent_title_text": "",
                "shared_stem_text": "",
                "units": [],
            }
        ]
        payload["diagrams"] = []
        with self.assertRaisesRegex(A3PageParseError, "after removing"):
            parse_a3_page_understanding(payload)

    def test_reject_invalid_reason_code(self):
        payload = valid_payload()
        payload["groups"][0]["units"][0]["reason_codes"] = ["no_question_text"]
        with self.assertRaisesRegex(A3PageParseError, "reason code"):
            parse_a3_page_understanding(payload)

    def test_reject_inconsistent_page_disposition(self):
        payload = valid_payload()
        payload["page_disposition"] = "a1_only"
        with self.assertRaisesRegex(A3PageParseError, "a1_only"):
            parse_a3_page_understanding(payload)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openpyxl

from scripts.feishu_store_flow import (
    FeishuStoreService,
    append_excel_record,
    format_store_confirmation,
)


class _FakeQwen:
    def classify_image(self, _image_path):
        return {
            "loads": [{"type": "均布", "raw": "q"}],
            "chapter_hint": "2静定结构",
            "chapter_confidence": 1.0,
            "chapter_evidence": "‘求图示桁架杆件轴力’",
        }

    def classify_structure_type(self, _image_path):
        return {"structure_type": "桁架"}

    def recognize_dimensions(self, _image_path, known_structure_type):
        self.known_structure_type = known_structure_type
        return {
            "normalized": {
                "long_width": "4L×2L",
                "single_side": "",
                "dimension_state": "full",
            }
        }


class _FakeCoordinator:
    def __init__(self):
        self.qwen = _FakeQwen()


class FeishuStoreFlowTests(unittest.TestCase):
    def test_symbolic_store_classification_carries_structure_and_dimensions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "main"
            symbolic = Path(temp_dir) / "symbolic"
            draft = FeishuStoreService(root=root, symbolic=symbolic, dry_run=True).classify_question(
                Path(temp_dir) / "question.jpg", _FakeCoordinator()
            )

        self.assertEqual(draft.route, "symbolic")
        self.assertEqual(draft.structure_type, "桁架")
        self.assertEqual(draft.long_width, "4L×2L")
        self.assertEqual(draft.dimension_state, "full")

    def test_append_persists_symbolic_metadata_and_adds_missing_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "2静定结构.xlsx"
            append_excel_record(
                workbook,
                "2静定结构/题目/1.jpg",
                [{"type": "均布", "raw": "q"}],
                structure_type="桁架",
                long_width="4L×2L",
            )
            wb = openpyxl.load_workbook(workbook, read_only=True)
            headers = [cell.value for cell in next(wb.active.iter_rows(max_row=1))]
            values = [cell.value for cell in next(wb.active.iter_rows(min_row=2, max_row=2))]
            wb.close()

        row = dict(zip(headers, values))
        self.assertEqual(row["结构类型"], "桁架")
        self.assertEqual(row["长×宽"], "4L×2L")

    def test_confirmation_shows_symbolic_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FeishuStoreService(root=Path(temp_dir) / "main", symbolic=Path(temp_dir) / "symbolic", dry_run=True)
            draft = service.classify_question(Path(temp_dir) / "question.jpg", _FakeCoordinator())
            draft.answer_image_paths = [str(Path(temp_dir) / "answer.jpg")]
            plan = service.prepare_plan(draft)

        confirmation = format_store_confirmation(plan)
        self.assertIn("结构类型：桁架", confirmation)
        self.assertIn("外围尺寸（长×宽）：4L×2L", confirmation)


if __name__ == "__main__":
    unittest.main()

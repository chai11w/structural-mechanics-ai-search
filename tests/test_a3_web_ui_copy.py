from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class A3WebUiCopyTests(unittest.TestCase):
    def test_candidate_and_original_question_actions_use_distinct_labels(self):
        script = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")

        self.assertIn("switchButton.textContent = '换题重新搜'", script)
        self.assertIn("choose.textContent = isCurrent ? '选择这个候选'", script)
        self.assertNotIn("switchButton.textContent = '换一道题'", script)
        self.assertNotIn("choose.textContent = isCurrent ? '选择这道题'", script)

    def test_question_sheet_names_its_image_scope(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("选择图片中的题目", page)
        self.assertIn("选择其他题目后会重新裁剪并搜索", page)


if __name__ == "__main__":
    unittest.main()

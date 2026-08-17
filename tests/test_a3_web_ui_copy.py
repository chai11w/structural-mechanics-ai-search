from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class A3WebUiCopyTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")
        self.style = (ROOT / "tiku_agent" / "demo_web" / "demo.css").read_text(encoding="utf-8")

    def test_candidate_and_original_question_actions_use_distinct_labels(self):
        self.assertIn("switchButton.textContent = '换题重新搜'", self.script)
        self.assertIn("choose.textContent = isCurrent ? '选择'", self.script)
        self.assertNotIn("switchButton.textContent = '换一道题'", self.script)
        self.assertNotIn("choose.textContent = isCurrent ? '选择这道题'", self.script)

    def test_question_sheet_names_its_image_scope(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("选择图片中的题目", page)
        self.assertIn("选择其他题目后会重新裁剪并搜索", page)

    def test_mobile_crop_box_can_move_and_resize(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")

        for handle in ("nw", "ne", "se", "sw"):
            self.assertIn(f'data-a3-handle="{handle}"', page)
        self.assertIn("if (origin && handle) mode = 'resize'", self.script)
        self.assertIn("mode = 'move'", self.script)
        self.assertIn("setPointerCapture(event.pointerId)", self.script)
        self.assertIn("releasePointerCapture(event.pointerId)", self.script)
        self.assertIn("width: 44px; height: 44px", self.style)
        self.assertIn("pointer-events: auto; touch-action: none", self.style)
        self.assertIn("44 / frameRect.width", self.script)

    def test_mobile_crop_question_label_is_centered_under_title(self):
        self.assertIn(".a3-crop-header { min-width: 0; position: relative;", self.style)
        self.assertIn("position: absolute; top: 50%; left: 50%", self.style)
        self.assertIn("transform: translate(-50%, -50%)", self.style)
        self.assertIn("width: 100%; max-width: 42vw; margin: 2px auto 0", self.style)

    def test_crop_image_is_fitted_without_zoom_controls(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("a3-zoom-controls", page)
        self.assertNotIn("a3Zoom", self.script)
        self.assertNotIn("changeA3Zoom", self.script)
        self.assertIn("function fitA3Image()", self.script)

    def test_current_a3_buttons_refresh_their_derived_labels(self):
        self.assertIn("button.textContent = unit.display_label || '未标号题目'", self.script)
        self.assertIn("label.textContent = `${unit.display_label || '未标号题目'} · 已完成`", self.script)

    def test_crop_workspace_actions_do_not_repeat_in_chat(self):
        select_body = self.script[
            self.script.index("async function selectA3Unit"):
            self.script.index("function openA3Crop")
        ]
        crop_body = self.script[
            self.script.index("async function submitA3Crop"):
            self.script.index("function renderA3SheetUnits")
        ]

        self.assertNotIn("addMessage({ message: `选择", select_body)
        self.assertIn("!A3_INLINE_ONLY_INTENTS.has(data.intent)", select_body)
        self.assertIn("!A3_INLINE_ONLY_INTENTS.has(data.intent)", crop_body)
        self.assertIn("a3CropStatus.textContent = a3Current()?.crop_review_feedback", crop_body)
        self.assertIn("className = 'a3-unit-choice a3-continue-crop'", self.script)
        self.assertIn("isPersistentImage(data.submitted_crop)", crop_body)
        self.assertLess(
            crop_body.index("message: '我提交了裁剪后的题图。'"),
            crop_body.index("if (!A3_INLINE_ONLY_INTENTS.has(data.intent)) addMessage(response)"),
        )

    def test_old_crop_workspace_echoes_are_removed_from_history(self):
        restore_body = self.script[
            self.script.index("function isLegacyInlineOnlyMessage"):
            self.script.index("function flushStartupNotices")
        ]

        self.assertIn("A3_INLINE_ONLY_INTENTS.has(String(item?.intent || ''))", restore_body)
        self.assertIn("item.message === `选择${label}`", restore_body)
        self.assertIn("!isLegacyInlineOnlyMessage(item, index, storedMessages)", restore_body)

    def test_expired_media_stays_inline_and_legacy_notice_is_removed(self):
        media_body = self.script[
            self.script.index("function createMediaCard"):
            self.script.index("function addMessage")
        ]
        restore_body = self.script[
            self.script.index("function restoreHistory"):
            self.script.index("function flushStartupNotices")
        ]

        self.assertIn("note.textContent = '图片已失效，请重新上传'", media_body)
        self.assertIn("candidateButton.textContent = '候选已失效'", media_body)
        self.assertNotIn("showFailureNotice(", media_body)
        self.assertIn("item?.code === 'MEDIA_NOT_FOUND'", restore_body)
        self.assertIn("item?.message === LEGACY_EXPIRED_MEDIA_MESSAGE", restore_body)

    def test_failure_notice_key_survives_refresh(self):
        self.assertIn("noticeKey: String(item.noticeKey || '')", self.script)
        self.assertIn("message, variant: 'error', recoveryActions, noticeKey", self.script)
        self.assertIn("if (noticeKey) activeFailureNotices.add(noticeKey)", self.script)


if __name__ == "__main__":
    unittest.main()

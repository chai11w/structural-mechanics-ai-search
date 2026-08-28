from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.run_tiku_agent_8898 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_app,
    build_argument_parser,
)


class TikuAgent8898ShadowTest(unittest.TestCase):
    def test_shadow_enables_text_orientation_with_bounded_rollback(self):
        parser = build_argument_parser()
        defaults = parser.parse_args([])
        disabled = parser.parse_args(["--disable-a3-text-orientation"])

        self.assertEqual(DEFAULT_PORT, 8898)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_shadow_8898")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8898_shadow_session")
        self.assertTrue(defaults.enable_a3_text_orientation)
        self.assertFalse(disabled.enable_a3_text_orientation)

    def test_shadow_forwards_rotation_only_to_its_isolated_runtime(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8898.build_8896_runtime"
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8898.RapidOcrTextPageOrienter"
        ) as orienter_type, patch(
            "scripts.run_tiku_agent_8898.create_app",
            return_value=object(),
        ):
            build_runtime.return_value = object()
            orienter = orienter_type.return_value

            build_app(Path(temp), enable_a3_text_orientation=True)

            orienter_type.assert_called_once_with()
            self.assertIs(build_runtime.call_args.kwargs["a3_page_orienter"], orienter)

    def test_shadow_can_disable_text_orientation_without_loading_ocr(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8898.build_8896_runtime"
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8898.RapidOcrTextPageOrienter"
        ) as orienter_type, patch(
            "scripts.run_tiku_agent_8898.create_app",
            return_value=object(),
        ):
            build_runtime.return_value = object()

            build_app(Path(temp), enable_a3_text_orientation=False)

            orienter_type.assert_not_called()
            self.assertIsNone(build_runtime.call_args.kwargs["a3_page_orienter"])


if __name__ == "__main__":
    unittest.main()

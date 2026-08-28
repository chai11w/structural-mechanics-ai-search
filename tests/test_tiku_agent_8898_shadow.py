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
    def test_shadow_enables_a3_landscape_rotation_with_bounded_rollback(self):
        parser = build_argument_parser()
        defaults = parser.parse_args([])
        disabled = parser.parse_args(["--disable-a3-landscape-rotation"])

        self.assertEqual(DEFAULT_PORT, 8898)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_shadow_8898")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8898_shadow_session")
        self.assertTrue(defaults.rotate_a3_landscape_pages_clockwise)
        self.assertFalse(disabled.rotate_a3_landscape_pages_clockwise)

    def test_shadow_forwards_rotation_only_to_its_isolated_runtime(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "scripts.run_tiku_agent_8898.build_8896_runtime"
        ) as build_runtime, patch(
            "scripts.run_tiku_agent_8898.create_app",
            return_value=object(),
        ):
            build_runtime.return_value = object()

            build_app(Path(temp), rotate_a3_landscape_pages_clockwise=True)

            self.assertTrue(
                build_runtime.call_args.kwargs["rotate_a3_landscape_pages_clockwise"]
            )


if __name__ == "__main__":
    unittest.main()

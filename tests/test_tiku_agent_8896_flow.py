from pathlib import Path
import tempfile
import unittest

from scripts.run_tiku_agent_8896 import (
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_argument_parser,
    build_runtime,
)
from scripts.run_tiku_agent_demo import build_runtime as build_a2_runtime
from tiku_agent.state import AgentState


class TikuAgent8896FlowTest(unittest.TestCase):
    def test_launcher_enables_full_triage_by_default(self):
        defaults = build_argument_parser().parse_args([])

        self.assertEqual(DEFAULT_PORT, 8896)
        self.assertEqual(DEFAULT_RUNTIME_DIR.name, ".tmp_tiku_agent_a3_mvp_8896")
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8896_session")
        self.assertTrue(defaults.enable_triage)

    def test_launcher_can_disable_or_inject_the_triage_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            disabled = build_runtime(root / "disabled", enable_triage=False)
            authority = object()
            injected = build_runtime(
                root / "injected",
                image_triage_authority=authority,
            )

            self.assertIsNone(disabled.image_triage_authority)
            self.assertIs(injected.image_triage_authority, authority)

    def test_only_8896_enables_chapter_scope_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_8896 = build_runtime(root / "8896", enable_triage=False)
            runtime_8790 = build_a2_runtime(
                root / "8790",
                enable_external_load_screen=False,
            )

            agent_8896 = runtime_8896.a2_runtime.agent_factory(AgentState())
            agent_8790 = runtime_8790.agent_factory(AgentState())

            self.assertTrue(agent_8896.enable_chapter_scope_fallback)
            self.assertFalse(agent_8790.enable_chapter_scope_fallback)


if __name__ == "__main__":
    unittest.main()

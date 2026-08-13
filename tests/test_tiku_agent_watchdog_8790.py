import unittest
from pathlib import Path


class TikuAgentWatchdog8790Test(unittest.TestCase):
    def test_external_load_screen_defaults_on_with_explicit_rollback(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "tiku_agent_watchdog_8790.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[switch]$DisableExternalLoadScreen", script)
        self.assertIn('"--disable-external-load-screen"', script)
        self.assertNotIn('"--enable-external-load-screen"', script)


if __name__ == "__main__":
    unittest.main()

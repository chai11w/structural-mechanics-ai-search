import unittest
from pathlib import Path


class TikuAgentWatchdog8790Test(unittest.TestCase):
    def test_watchdog_runs_a3_v1_with_control_db_and_manual_crop_rollback(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "tiku_agent_watchdog_8790.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn('"scripts\\run_tiku_agent_8790.py"', script)
        self.assertIn('".tmp_tiku_admin_8795\\control.sqlite3"', script)
        self.assertIn("[switch]$DisableAutoCrop", script)
        self.assertIn('"--disable-auto-crop"', script)
        self.assertIn("[switch]$DisableOutputWatchdog", script)
        self.assertIn('"--disable-output-watchdog"', script)
        self.assertNotIn('"scripts\\run_tiku_agent_demo.py"', script)


if __name__ == "__main__":
    unittest.main()
